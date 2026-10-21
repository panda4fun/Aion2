"""Положение персонажа по миникарте AION 2, только по снимку экрана.

Игру не трогает: не читает память и ничего не внедряет. Делает снимок
области миникарты, как OBS или Discord, пока игра на переднем плане.

Как считается:
1. Привязка. Телепорты на миникарте рисуются голубыми ромбами одного цвета,
   их координаты известны. По двум ромбам находим и позицию, и масштаб.
   По одному ромбу уточняем позицию, если она уже примерно известна.
2. Слежение. Между привязками сравниваем соседние кадры миникарты
   (фазовая корреляция): на сколько сдвинулась картинка, на столько
   переместился персонаж. Миникарта смотрит севером вверх и не вращается.

Нужен numpy: py -m pip install numpy
"""

import ctypes
import math
import time
from ctypes import wintypes

import numpy as np

# Цвет телепорта на миникарте: RGB (141, 255, 238).
TELE_RGB = ((105, 180), (225, 256), (200, 256))

# Миникарта при высоте экрана 1440: прямоугольник внутри рамки и центр
# (стрелка персонажа), в пикселях от правого верхнего угла экрана.
BASE_H = 1440
BASE_RECT = (355, 135, 35, 350)  # слева от правого края, сверху, справа от правого края, снизу
BASE_CENTER = (200, 245)
BASE_SCALE = 1.25  # метров на пиксель миникарты
TELE_AREA = (35, 120)  # площадь ромба в пикселях

ANGLE_TOL = math.radians(3)
MATCH_TOL = 6  # пикселей: насколько предсказанный телепорт может не совпасть с найденным
SINGLE_MAX_JUMP = 150  # метров: привязка по одному ромбу не дальше этого от прежней позиции
HINT_RADIUS = 300  # метров: без прежней позиции ищем телепорт не дальше этого от подсказки
LOST_AFTER = 20  # секунд без надёжного слежения: позиция считается потерянной


class ScreenGrab:
    """Снимок прямоугольника экрана через GDI в массив (h, w, 3) RGB."""

    def __init__(self):
        self.u = ctypes.windll.user32
        self.g = ctypes.windll.gdi32
        self.u.GetDC.restype = wintypes.HDC
        self.g.CreateCompatibleDC.restype = wintypes.HDC
        self.g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        self.g.SelectObject.restype = wintypes.HGDIOBJ

    def screen_size(self):
        return self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1)

    def grab(self, x, y, w, h):
        sdc = self.u.GetDC(None)
        mdc = self.g.CreateCompatibleDC(sdc)
        bmp = self.g.CreateCompatibleBitmap(sdc, w, h)
        old = self.g.SelectObject(mdc, bmp)
        self.g.BitBlt(mdc, 0, 0, w, h, sdc, x, y, 0x00CC0020)  # SRCCOPY

        class BMI(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        self.g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        self.g.SelectObject(mdc, old)
        self.g.DeleteObject(bmp)
        self.g.DeleteDC(mdc)
        self.u.ReleaseDC(None, sdc)
        bgra = np.frombuffer(buf, np.uint8).reshape(h, w, 4)
        return bgra[:, :, 2::-1].copy()


def find_blobs(mask, amin, amax):
    """Центры связных областей маски с площадью в [amin, amax]."""
    ys, xs = np.nonzero(mask)
    left = set(zip(xs.tolist(), ys.tolist()))
    out = []
    while left:
        st = [left.pop()]
        pts = []
        while st:
            x, y = st.pop()
            pts.append((x, y))
            for q in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if q in left:
                    left.remove(q)
                    st.append(q)
        if amin <= len(pts) <= amax:
            out.append((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))
    return out


def phase_shift(a, b):
    """Сдвиг содержимого из кадра a в кадр b (dx, dy) и уверенность (отношение пиков)."""
    a = a - a.mean()
    b = b - b.mean()
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    r = np.fft.fft2(b * win) * np.conj(np.fft.fft2(a * win))
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    y, x = np.unravel_index(c.argmax(), c.shape)
    if y > a.shape[0] // 2:
        y -= a.shape[0]
    if x > a.shape[1] // 2:
        x -= a.shape[1]
    top = np.partition(c.ravel(), -2)[-2:]
    return int(x), int(y), float(top[1] / max(top[0], 1e-9))


class Locator:
    def __init__(self, teleports, screen=(3440, 1440)):
        self.tele = [(t["x"], t["z"], t["n"]) for t in teleports]
        w, h = screen
        k = h / BASE_H
        r = BASE_RECT
        self.rect = (round(w - r[0] * k), round(r[1] * k), round(w - r[2] * k), round(r[3] * k))
        self.center = (w - BASE_CENTER[0] * k - self.rect[0], BASE_CENTER[1] * k - self.rect[1])
        self.size = (self.rect[2] - self.rect[0], self.rect[3] - self.rect[1])
        self.area = (TELE_AREA[0] * k * k, TELE_AREA[1] * k * k)
        self.scale = BASE_SCALE / k
        self.pos = None  # (x, z) в метрах
        self.hint = None  # (x, z): где игрок примерно, пока позиции нет (задаёт оверлей)
        self.prev = None  # серый кадр для слежения
        self.last_ok = 0.0
        self.mode = "нет"
        self.blobs = 0

    # --- привязка ----------------------------------------------------------

    def detect(self, rgb):
        (r0, r1), (g0, g1), (b0, b1) = TELE_RGB
        r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
        mask = (r >= r0) & (r < r1) & (g >= g0) & (g < g1) & (b >= b0) & (b < b1)
        cx, cy = self.center
        return [(x - cx, y - cy) for x, y in find_blobs(mask, *self.area)]

    def visible(self, px, pz, s):
        """Сколько известных телепортов попадает в рамку при позиции (px, pz) и масштабе s."""
        cx, cy = self.center
        w, h = self.size
        return sum(1 for x, z, _ in self.tele
                   if 0 <= cx + (x - px) / s < w and 0 <= cy + (z - pz) / s < h)

    def residual(self, blobs, px, pz, s):
        """Средняя ошибка в пикселях: каждый найденный ромб к ближайшему телепорту."""
        err = 0
        for dx, dy in blobs:
            err += min(math.hypot(px + dx * s - x, pz + dy * s - z) for x, z, _ in self.tele) / s
        return err / len(blobs)

    def fix_pair(self, blobs):
        """Позиция и масштаб по двум и более ромбам."""
        (ax, ay), (bx, by) = blobs[0], blobs[1]
        dl = math.hypot(bx - ax, by - ay)
        if dl < 8:
            return None
        da = math.atan2(by - ay, bx - ax)
        best = None
        for tx, tz, _ in self.tele:
            for ux, uz, _ in self.tele:
                vx, vz = ux - tx, uz - tz
                if vx == 0 and vz == 0:
                    continue
                ang = abs((math.atan2(vz, vx) - da + math.pi) % (2 * math.pi) - math.pi)
                if ang > ANGLE_TOL:
                    continue
                s = math.hypot(vx, vz) / dl
                if not 0.2 < s < 8:
                    continue
                px, pz = tx - ax * s, tz - ay * s
                res = self.residual(blobs, px, pz, s)
                if res > MATCH_TOL or self.visible(px, pz, s) > len(blobs) + 1:
                    continue
                # Прежний масштаб и позиция в приоритете: это почти всегда тот же кадр.
                score = res + 20 * abs(math.log(s / self.scale))
                if self.pos:
                    score += math.hypot(px - self.pos[0], pz - self.pos[1]) / 200
                if best is None or score < best[0]:
                    best = (score, px, pz, s)
        return best[1:] if best else None

    def fix_single(self, blob):
        """Позиция по одному ромбу: ближайший к прежней позиции (или к подсказке) вариант.

        Без прежней позиции берём подсказку от оверлея (последняя отмеченная точка) и отбрасываем
        варианты, при которых в рамку попали бы ещё телепорты. Центр района как подсказка
        не годится: под него подходит сразу несколько телепортов.
        """
        ref, limit = (self.pos, SINGLE_MAX_JUMP) if self.pos else (self.hint, HINT_RADIUS)
        if not ref:
            return None
        dx, dy = blob
        s = self.scale
        cands = sorted((math.hypot(x - dx * s - ref[0], z - dy * s - ref[1]), x - dx * s, z - dy * s)
                       for x, z, _ in self.tele)
        for d, px, pz in cands:
            if d > limit:
                break
            if self.pos or self.visible(px, pz, s) <= 1:
                return px, pz
        return None

    def pin(self, x, z):
        """Игрок сам сказал, где стоит (клик по телепорту или точке)."""
        self.pos, self.prev = (x, z), None
        self.last_ok = time.monotonic()
        self.mode = "по клику"

    # --- шаг ---------------------------------------------------------------

    def process(self, rgb, now=None):
        """Обрабатывает кадр миникарты. Возвращает (x, z) или None, если позиция неизвестна."""
        now = time.monotonic() if now is None else now
        gray = rgb.astype(np.float32).mean(axis=2)
        tracked = False
        if self.pos and self.prev is not None:
            dx, dy, conf = phase_shift(self.prev, gray)
            if conf > 2.5 and abs(dx) < self.size[0] / 3 and abs(dy) < self.size[1] / 3:
                self.pos = (self.pos[0] - dx * self.scale, self.pos[1] - dy * self.scale)
                tracked = True
        blobs = self.detect(rgb)
        self.blobs = len(blobs)
        fixed = None
        if len(blobs) >= 2:
            fixed = self.fix_pair(blobs)
            if fixed:
                self.pos, self.scale = (fixed[0], fixed[1]), fixed[2]
                self.mode = f"по телепортам ({len(blobs)})"
        if not fixed and len(blobs) == 1:
            fixed = self.fix_single(blobs[0])
            if fixed:
                self.pos = fixed
                self.mode = "по телепорту"
        if fixed or tracked:
            self.last_ok = now
            self.prev = gray
            if not fixed:
                self.mode = "по сдвигу миникарты"
        elif self.pos and now - self.last_ok > LOST_AFTER:
            self.pos, self.prev = None, None
            self.mode = "потеряна"
        elif self.prev is None:
            self.prev = gray
        return self.pos


class LiveLocator:
    """Снимает миникарту, пока игра на переднем плане."""

    def __init__(self, teleports):
        self.grabber = ScreenGrab()
        self.loc = Locator(teleports, self.grabber.screen_size())

    def step(self):
        x0, y0, x1, y1 = self.loc.rect
        return self.loc.process(self.grabber.grab(x0, y0, x1 - x0, y1 - y0))
