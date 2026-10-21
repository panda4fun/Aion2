"""Оверлей поверх AION 2: маршрут по точкам Hidden Cube и ручные отметки.

Игру с процессом не трогает: не читает память и ничего не внедряет.
Это обычное окно поверх остальных; игра должна быть в оконном режиме
или в режиме «окно без рамки». Положение персонажа берётся со снимка
миникарты (locator.py).

Запуск: py cube_overlay/overlay.py
"""

import ctypes
import json
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import ttk

from route import build_route, dist, route_length

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "overlay.log"

MAPS = {"World_D_A": "Altgard", "World_L_A": "Verteron"}
GAME_EXE = "aion2.exe"
LOCATE_EVERY = 0.35  # секунд между снимками миникарты
NEAR_TARGET = 12  # метров: «ты у точки»

# Глобальные горячие клавиши: работают, когда в фокусе игра.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
HOTKEYS = {
    "next": (MOD_CONTROL | MOD_ALT, 0x27, "Ctrl+Alt+→", "пусто, дальше"),
    "found": (MOD_CONTROL | MOD_ALT, 0x26, "Ctrl+Alt+↑", "нашёл куб"),
    "skip": (MOD_CONTROL | MOD_ALT, 0x28, "Ctrl+Alt+↓", "пропустить"),
    "undo": (MOD_CONTROL | MOD_ALT, 0x25, "Ctrl+Alt+←", "отменить"),
    "zoom_in": (MOD_CONTROL | MOD_ALT, 0xBB, "Ctrl+Alt+=", "приблизить"),
    "zoom_out": (MOD_CONTROL | MOD_ALT, 0xBD, "Ctrl+Alt+-", "отдалить"),
    "toggle": (MOD_CONTROL | MOD_ALT, 0x48, "Ctrl+Alt+H", "скрыть/показать"),
}

# Пикселей на метр. Подложка 4 м на пиксель, поэтому 4*zoom или 1/(4*zoom) целое.
ZOOMS = [1 / 16, 1 / 12, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 1.5, 2, 3]
COMPASS = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]

BG = "#101418"
FG = "#d8dee6"
DIM = "#7b8794"
C_TARGET = "#3ddc84"
C_NEXT = "#ffd34d"
C_ROUTE = "#ff9f43"
C_DONE = "#4a525c"
C_HERE = "#ffffff"
C_WATER = "#4dabff"
C_ZONE = "#56606a"
C_TELE = "#5ce1ff"
MARK_STYLE = {"teleport": (C_TELE, "телепорт"), "dungeon": ("#c792ea", "вход в данж"),
              "monolith": ("#b388ff", "монолит")}


class Hotkeys(threading.Thread):
    """RegisterHotKey в отдельном потоке со своим циклом сообщений."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.tid = None
        self.failed = []

    def run(self):
        user32 = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        names = list(HOTKEYS)
        for i, name in enumerate(names, 1):
            mods, vk, label, _ = HOTKEYS[name]
            if not user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                self.failed.append(label)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and 1 <= msg.wParam <= len(names):  # WM_HOTKEY
                self.out.put(names[msg.wParam - 1])
        for i in range(1, len(names) + 1):
            user32.UnregisterHotKey(None, i)

    def stop(self):
        if self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def process_name(pid):
    """Имя exe по pid из списка процессов, не открывая сам процесс."""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return ""
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ProcessID == pid:
                return e.szExeFile
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return ""


_names = {}


def foreground():
    """"game", если на переднем плане окно игры, "self", если наше окно, иначе None."""
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    if pid.value == os.getpid():
        return "self"
    if pid.value not in _names:
        _names[pid.value] = process_name(pid.value)
    return "game" if _names[pid.value].lower() == GAME_EXE else None


class Watcher(threading.Thread):
    """Следит, какое окно на переднем плане, и определяет положение по миникарте."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.live = None
        self.error = None
        self.running = True

    def set_teleports(self, teleports):
        try:
            from locator import LiveLocator
        except ImportError:
            self.live, self.error = None, "нужен numpy: py -m pip install numpy"
            return
        self.live = LiveLocator(teleports)

    def log(self, text):
        """Журнал для разбора проблем: пишем только изменения."""
        if text != self.last_log:
            self.last_log = text
            try:
                with LOG_FILE.open("a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            except OSError:
                pass

    def run(self):
        last = object()
        self.last_log = None
        while self.running:
            fg = foreground()
            if fg != last:
                self.out.put(("fg", fg))
                last = fg
            live = self.live
            if fg == "game" and live:
                try:
                    pos = live.step()
                    err = ""
                except Exception as e:  # noqa: BLE001 — сбой одного кадра не должен останавливать слежение
                    pos, err = None, f" ошибка: {e!r}"
                self.out.put(("live", pos, live.loc.mode))
                self.log(f"игра; ромбов {live.loc.blobs}; режим {live.loc.mode}"
                         f"{'' if pos else '; позиции нет'}{err}")
            else:
                self.log(f"на переднем плане: {fg}" + ("" if live else f"; {self.error}"))
            time.sleep(LOCATE_EVERY)


def short_group(gid):
    m = re.search(r"Hidden(?:Box|Cube)_?(.*)$", gid)
    return (m.group(1) if m else gid) or "—"


def load_map(key):
    path = DATA_DIR / f"{key}.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "relief" not in data or not (DATA_DIR / data["relief"]["file"]).exists():
        import fetch_cubes
        fetch_cubes.main()
        data = json.loads(path.read_text(encoding="utf-8"))
    points = {}
    for g in data["groups"]:
        for i, p in enumerate(g["points"]):
            points[f"{g['id']}#{i}"] = {**p, "gid": g["id"], "area": g["area"],
                                        "area_name": g["area_name"], "water": g["water"]}
    return data, points


class Overlay:
    def __init__(self):
        self.st = self.load_state()
        self.events = queue.Queue()
        self.pan = [0.0, 0.0]
        self.drag = None
        self.live = None  # положение по миникарте: {"x", "z", "y": None}
        self.live_mode = ""
        self.hidden = False
        self.seen_game = False
        self.watcher = Watcher(self.events)

        self.root = tk.Tk()
        self.root.title("Hidden Cube")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.st["alpha"])
        self.root.configure(bg=BG)
        self.root.geometry(self.safe_geometry(self.st["geometry"]))

        self.build_ui()
        self.set_map(self.st["map"], keep=True)

        self.hotkeys = Hotkeys(self.events)
        self.hotkeys.start()
        self.watcher.start()
        self.root.after(500, self.report_hotkeys)
        self.root.after(50, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # --- состояние ---------------------------------------------------------

    def load_state(self):
        st = {"map": "World_D_A", "maps": {}, "alpha": 0.88, "zoom": 6,
              "geometry": "360x500+40+120", "auto_hide": True}
        try:
            st.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        return st

    def ms(self):
        """Состояние текущей карты."""
        return self.st["maps"].setdefault(self.st["map"], {
            "areas": [], "checked": [], "found": [], "route": [], "pos": 0, "here": None})

    def safe_geometry(self, geo):
        """Не меньше 260x300 и на экране, даже если в state.json что-то не то."""
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", geo or "")
        w, h, x, y = map(int, m.groups()) if m else (360, 500, 40, 120)
        if w < 200 or h < 200:
            w, h = 360, 500
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = max(260, min(w, sw)), max(300, min(h, sh))
        x, y = max(0, min(x, sw - w)), max(0, min(y, sh - h))
        return f"{w}x{h}+{x}+{y}"

    def save(self):
        # У спрятанного окна размер 1x1: такой сохранять нельзя, иначе окно пропадёт при запуске.
        if self.root.state() != "withdrawn" and self.root.winfo_width() >= 200:
            self.st["geometry"] = self.root.geometry()
        STATE_FILE.write_text(json.dumps(self.st, ensure_ascii=False), encoding="utf-8")

    # --- интерфейс ---------------------------------------------------------

    def build_ui(self):
        head = tk.Frame(self.root, bg="#1b222a")
        head.pack(fill="x")
        self.title = tk.Label(head, bg="#1b222a", fg=FG, font=("Segoe UI", 9, "bold"), anchor="w")
        self.title.pack(side="left", padx=6, pady=3)
        for text, cmd, tip in [("✕", self.quit, "Закрыть"), ("+", lambda: self.zoom(1), "Приблизить"),
                               ("−", lambda: self.zoom(-1), "Отдалить"),
                               ("⟳", self.rebuild, "Перестроить маршрут от текущей точки"),
                               ("⚙", self.open_settings, "Районы и настройки")]:
            b = tk.Label(head, text=text, bg="#1b222a", fg=FG, font=("Segoe UI", 10), padx=6, cursor="hand2")
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.tip = tip
        for w in (head, self.title):
            w.bind("<ButtonPress-1>", self.win_drag_start)
            w.bind("<B1-Motion>", self.win_drag)
            w.bind("<ButtonRelease-1>", lambda e: self.save())

        self.info = tk.Label(self.root, bg=BG, fg=FG, font=("Segoe UI", 10), justify="left", anchor="w")
        self.info.pack(fill="x", padx=8, pady=(6, 2))
        self.sub = tk.Label(self.root, bg=BG, fg=DIM, font=("Segoe UI", 8), justify="left", anchor="w")
        self.sub.pack(fill="x", padx=8)
        for lbl in (self.info, self.sub):
            lbl.bind("<Configure>", lambda e: e.widget.config(wraplength=max(100, e.width - 4)))

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=4, pady=4)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<ButtonPress-1>", self.canvas_press)
        self.canvas.bind("<B1-Motion>", self.canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self.canvas_release)
        self.canvas.bind("<Button-3>", self.canvas_right)
        self.canvas.bind("<MouseWheel>", lambda e: self.zoom(1 if e.delta > 0 else -1))

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x")
        hint = "  ".join(f"{HOTKEYS[k][2]} {HOTKEYS[k][3]}" for k in ("next", "found", "skip", "undo"))
        tk.Label(foot, text=hint, bg=BG, fg=DIM, font=("Segoe UI", 7), wraplength=320,
                 justify="left").pack(side="left", padx=6, pady=(0, 4))
        grip = tk.Label(foot, text="◢", bg=BG, fg=DIM, cursor="size_nw_se")
        grip.pack(side="right", anchor="se")
        grip.bind("<ButtonPress-1>", self.resize_start)
        grip.bind("<B1-Motion>", self.resize)
        grip.bind("<ButtonRelease-1>", lambda e: self.save())

        keys = {"<Right>": "next", "<Up>": "found", "<Down>": "skip", "<Left>": "undo",
                "<plus>": "zoom_in", "<equal>": "zoom_in", "<minus>": "zoom_out"}
        for seq, name in keys.items():
            self.root.bind(seq, lambda e, n=name: self.events.put(n))

    def report_hotkeys(self):
        if self.hotkeys.failed:
            self.sub.config(text="Не удалось занять клавиши: " + ", ".join(self.hotkeys.failed)
                            + " (заняты другой программой)", fg="#ff6b6b")

    def win_drag_start(self, e):
        self.wdrag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def win_drag(self, e):
        self.root.geometry(f"+{e.x_root - self.wdrag[0]}+{e.y_root - self.wdrag[1]}")

    def resize_start(self, e):
        self.rdrag = (e.x_root, e.y_root, self.root.winfo_width(), self.root.winfo_height())

    def resize(self, e):
        x, y, w, h = self.rdrag
        self.root.geometry(f"{max(260, w + e.x_root - x)}x{max(300, h + e.y_root - y)}")

    # --- карта и маршрут ---------------------------------------------------

    def set_map(self, key, keep=False):
        self.st["map"] = key
        self.data, self.points = load_map(key)
        self.areas = {}
        for g in self.data["groups"]:
            a = self.areas.setdefault(g["area"], {"name": g["area_name"], "groups": [], "n": 0})
            a["groups"].append(g["id"])
            a["n"] += len(g["points"])
        self.marks = {f"lm#{i}": m for i, m in enumerate(self.data["landmarks"])}
        self.relief = tk.PhotoImage(file=str(DATA_DIR / self.data["relief"]["file"]))
        self.live = None
        self.watcher.set_teleports([m for m in self.data["landmarks"] if m["k"] == "teleport"])
        ms = self.ms()
        self.st["zoom"] = max(0, min(len(ZOOMS) - 1, self.st["zoom"]))
        # Точки могли исчезнуть после обновления данных.
        ms["route"] = [p for p in ms["route"] if p in self.points]
        ms["checked"] = [p for p in ms["checked"] if p in self.points]
        if ms["here"] and not self.loc(ms["here"]):
            ms["here"] = None
        if not keep:
            self.pan = [0.0, 0.0]
        self.history = []
        self.update_hint()
        self.refresh()

    def loc(self, key):
        """Точка куба или ориентир по ключу; ms["here"] бывает и тем, и другим."""
        return self.points.get(key) or self.marks.get(key) if key else None

    def start_point(self):
        """Откуда идём: живое положение по миникарте, иначе последняя отмеченная точка."""
        return self.live or self.loc(self.ms()["here"])

    def candidates(self):
        ms = self.ms()
        areas, checked, found = set(ms["areas"]), set(ms["checked"]), set(ms["found"])
        return [pid for pid, p in self.points.items()
                if p["area"] in areas and pid not in checked and p["gid"] not in found]

    def rebuild(self, first=None):
        """Маршрут по непроверенным точкам. first: с какой точки начать."""
        ms = self.ms()
        rest = [pid for pid in self.candidates() if pid != first]
        start = self.points[first] if first else self.start_point()
        order = build_route([self.points[p] for p in rest], start=start)
        ms["route"] = ([first] if first else []) + [rest[i] for i in order]
        ms["pos"] = 0
        self.pan = [0.0, 0.0]
        self.history = []
        self.update_hint()
        self.refresh()

    def target(self):
        ms = self.ms()
        return ms["route"][ms["pos"]] if ms["pos"] < len(ms["route"]) else None

    def act(self, name):
        ms = self.ms()
        pid = self.target()
        if name in ("zoom_in", "zoom_out"):
            return self.zoom(1 if name == "zoom_in" else -1)
        if name == "toggle":
            self.hidden = not self.hidden
            self.show(not self.hidden)
            return
        if name == "undo":
            return self.undo()
        if pid is None:
            return
        if name == "next":
            self.history.append(("next", pid, ms["here"]))
            ms["checked"].append(pid)
            ms["here"] = pid
            ms["pos"] += 1
        elif name == "found":
            gid = self.points[pid]["gid"]
            tail = ms["route"][ms["pos"] + 1:]
            dropped = [p for p in tail if self.points[p]["gid"] == gid]
            self.history.append(("found", pid, ms["here"], list(ms["route"])))
            ms["checked"].append(pid)
            ms["found"].append(gid)
            ms["here"] = pid
            ms["route"] = ms["route"][:ms["pos"] + 1] + [p for p in tail if p not in dropped]
            ms["pos"] += 1
        elif name == "skip":
            self.history.append(("skip", pid, ms["here"], list(ms["route"])))
            ms["route"].pop(ms["pos"])
            ms["route"].append(pid)
        self.pan = [0.0, 0.0]
        self.refresh()

    def undo(self):
        if not self.history:
            return
        ms = self.ms()
        h = self.history.pop()
        kind, pid, here = h[0], h[1], h[2]
        if kind in ("next", "found"):
            ms["checked"].remove(pid)
            ms["pos"] -= 1
        if kind == "found":
            ms["found"].remove(self.points[pid]["gid"])
        if kind in ("found", "skip"):
            ms["route"] = h[3]
        ms["here"] = here
        self.refresh()

    def zoom(self, step):
        self.st["zoom"] = max(0, min(len(ZOOMS) - 1, self.st["zoom"] + step))
        self.draw()
        self.save()

    def refresh(self):
        self.update_info()
        self.draw()
        self.save()

    # --- текст -------------------------------------------------------------

    def update_info(self):
        ms = self.ms()
        self.title.config(text=f"Hidden Cube · {MAPS[self.st['map']]}")
        route, pos = ms["route"], ms["pos"]
        if not ms["areas"]:
            self.info.config(text="Выбери районы: ⚙")
            self.sub.config(text="")
            return
        if not route:
            self.info.config(text="Маршрут не построен: ⟳")
            self.sub.config(text="")
            return
        pid = self.target()
        if pid is None:
            self.info.config(text="Маршрут пройден")
            self.sub.config(text=f"Найдено кубов: {len(ms['found'])}. Новый маршрут: ⟳")
            return
        p = self.points[pid]
        head = f"{pos + 1}/{len(route)} · {p['area_name']} · {short_group(p['gid'])}"
        here = self.start_point()
        if here and here is self.live and dist(here, p) < NEAR_TARGET:
            line = "Ты у точки: осмотрись и отметь Ctrl+Alt+→ или ↑"
        elif here:
            frm = ("от тебя" if here is self.live else
                   f"от телепорта «{here['n']}»" if "n" in here else "от прошлой точки")
            line = f"{self.leg(here, p)} {frm}"
        else:
            line = "Кликни по телепорту или точке, где стоишь"
        if p.get("dep"):
            line += f" · под водой {p['dep']:.0f} м"
        elif p["water"]:
            line += " · под водой"
        tp = min((m for m in self.marks.values() if m["k"] == "teleport"),
                 key=lambda m: dist(m, p), default=None)
        hint = (f"\nОриентир: телепорт «{tp['n']}», цель {self.leg(tp, p)} от него"
                if tp and tp is not here else "")
        self.info.config(text=f"{head}\n{line}{hint}")
        left = route_length([self.points[q] for q in route[pos:]], list(range(len(route) - pos)), here)
        self.sub.config(fg=DIM, text=f"{self.live_status()} · осталось ≈{left / 1000:.1f} км · "
                                     f"проверено {len(ms['checked'])} · найдено кубов {len(ms['found'])}")

    def live_status(self):
        if self.watcher.error:
            return f"Положение: {self.watcher.error}"
        if self.live:
            return f"Положение: {self.live_mode}"
        return "Положение не найдено: встань так, чтобы на миникарте было 2 телепорта, или кликни, где стоишь"

    @staticmethod
    def leg(a, b):
        dx, dz = b["x"] - a["x"], b["z"] - a["z"]
        d = math.hypot(dx, dz)
        ang = (math.degrees(math.atan2(dx, -dz)) + 360) % 360
        s = f"{d:.0f} м на {COMPASS[round(ang / 45) % 8]}"
        if a["y"] is not None and b["y"] is not None:
            dy = b["y"] - a["y"]
            if abs(dy) >= 3:
                s += f", {'выше' if dy > 0 else 'ниже'} на {abs(dy):.0f} м"
        return s

    # --- миникарта ---------------------------------------------------------

    def view(self):
        """Центр (x, z) и масштаб миникарты."""
        ms = self.ms()
        # Есть живое положение: карта идёт за персонажем, как миникарта игры.
        c = self.live or self.loc(self.target() or ms["here"])
        if c:
            cx, cz = c["x"], c["z"]
        else:
            pts = [p for p in self.points.values() if p["area"] in ms["areas"]] or list(self.points.values())
            cx = sum(p["x"] for p in pts) / len(pts)
            cz = sum(p["z"] for p in pts) / len(pts)
        return cx + self.pan[0], cz + self.pan[1], ZOOMS[self.st["zoom"]]

    def to_screen(self, x, z):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        cx, cz, s = self.vw
        return w / 2 + (x - cx) * s, h / 2 + (z - cz) * s

    def draw(self):
        cv = self.canvas
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 10:
            return
        self.vw = self.view()
        ms = self.ms()
        S = self.to_screen
        scale = self.vw[2]

        self.draw_relief(w, h)
        for z in self.data["subzones"]:
            xy = [c for q in z["poly"] for c in S(q[0], q[1])]
            cv.create_line(*xy, fill=C_ZONE, dash=(2, 4))
        for z in self.data["subzones"]:
            x, y = S(*z["c"])
            if 0 < x < w and 0 < y < h:
                cv.create_text(x, y, text=z["n"], fill="#aab4bf", font=("Segoe UI", 7, "italic"))

        for m in self.marks.values():
            x, y = S(m["x"], m["z"])
            if not (-5 < x < w + 5 and -5 < y < h + 5):
                continue
            col = MARK_STYLE[m["k"]][0]
            if m["k"] == "teleport":
                cv.create_polygon(x, y - 6, x + 6, y, x, y + 6, x - 6, y, fill=col, outline=BG)
                if scale >= 1 / 8:
                    cv.create_text(x, y + 9, text=m["n"], fill=col, font=("Segoe UI", 7), anchor="n")
            else:
                cv.create_rectangle(x - 3, y - 3, x + 3, y + 3, fill=col, outline=BG)
                if scale >= 1:
                    cv.create_text(x, y + 6, text=m["n"], fill=col, font=("Segoe UI", 7), anchor="n")

        checked, found = set(ms["checked"]), set(ms["found"])
        route, pos = ms["route"], ms["pos"]
        upcoming = route[pos:]
        upset = set(upcoming)
        areas = set(ms["areas"])
        for pid, p in self.points.items():
            if p["area"] not in areas or pid in upset:
                continue
            x, y = S(p["x"], p["z"])
            if not (-5 < x < w + 5 and -5 < y < h + 5):
                continue
            if pid in checked or p["gid"] in found:
                cv.create_oval(x - 3, y - 3, x + 3, y + 3, fill="", outline="#c0c6cc")
            else:
                cv.create_oval(x - 2, y - 2, x + 2, y + 2, fill=DIM, outline="")

        here = self.start_point()
        seq = ([here] if here else []) + [self.points[q] for q in upcoming[:12]]
        if len(seq) > 1:
            xy = [c for p in seq for c in S(p["x"], p["z"])]
            cv.create_line(*xy, fill=C_ROUTE, width=1, dash=(3, 3))
            if here:
                cv.create_line(*xy[:4], fill=C_TARGET, width=2, arrow="last")

        for i in range(len(upcoming) - 1, -1, -1):
            p = self.points[upcoming[i]]
            x, y = S(p["x"], p["z"])
            if not (-10 < x < w + 10 and -10 < y < h + 10):
                continue
            r, col = (7, C_TARGET) if i == 0 else (5, C_NEXT) if i == 1 else (3, C_ROUTE)
            cv.create_oval(x - r, y - r, x + r, y + r, fill=col,
                           outline=C_WATER if p["water"] else "", width=2)
            if i < 10:
                cv.create_text(x + r + 6, y - r, text=str(pos + i + 1), fill=col, font=("Segoe UI", 8, "bold"))

        if here is self.live and here:
            x, y = S(here["x"], here["z"])
            cv.create_oval(x - 9, y - 9, x + 9, y + 9, outline=C_HERE, width=2)
            cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=C_HERE, outline="")
        elif here:
            x, y = S(here["x"], here["z"])
            cv.create_polygon(x, y - 6, x - 5, y + 5, x + 5, y + 5, fill=C_HERE, outline="")

        cv.create_text(18, 12, text="С ↑", fill=FG, font=("Segoe UI", 8, "bold"))
        bar = next((m for m in (25, 50, 100, 250, 500, 1000) if m * scale >= 50), 2000)
        px = bar * scale
        cv.create_line(8, h - 8, 8 + px, h - 8, fill=FG, width=2)
        cv.create_text(8 + px / 2, h - 16, text=f"{bar} м", fill=FG, font=("Segoe UI", 7))

    def draw_relief(self, w, h):
        """Вырезает из подложки видимый кусок и масштабирует его целым множителем."""
        r = self.data["relief"]
        n, b, x0 = r["n"], r["block_m"], r["x0"]
        cx, cz, s = self.vw
        c0 = max(0, int((cx - w / 2 / s - x0) / b))
        c1 = min(n, int((cx + w / 2 / s - x0) / b) + 2)
        r0 = max(0, int((cz - h / 2 / s - x0) / b))
        r1 = min(n, int((cz + h / 2 / s - x0) / b) + 2)
        if c1 <= c0 or r1 <= r0:
            return
        k = s * b  # экранных пикселей на пиксель подложки
        img = tk.PhotoImage()
        opt = ("-zoom", round(k)) if k >= 1 else ("-subsample", round(1 / k))
        img.tk.call(img, "copy", self.relief, "-from", c0, r0, c1, r1, *opt)
        self.relief_view = img  # без ссылки Tk удалит картинку
        x, y = self.to_screen(x0 + c0 * b, x0 + r0 * b)
        self.canvas.create_image(x, y, image=img, anchor="nw")

    def nearest(self, ex, ey, limit=12):
        """Ближайшая к клику точка куба или ориентир."""
        areas = set(self.ms()["areas"])
        best, bd = None, limit
        for pid, p in list(self.points.items()) + list(self.marks.items()):
            if "area" in p and p["area"] not in areas:
                continue
            x, y = self.to_screen(p["x"], p["z"])
            d = math.hypot(x - ex, y - ey)
            if d < bd:
                best, bd = pid, d
        return best

    def canvas_press(self, e):
        self.drag = (e.x, e.y, list(self.pan), False)

    def canvas_motion(self, e):
        x0, y0, pan0, _ = self.drag
        if abs(e.x - x0) + abs(e.y - y0) > 4:
            s = ZOOMS[self.st["zoom"]]
            self.pan = [pan0[0] - (e.x - x0) / s, pan0[1] - (e.y - y0) / s]
            self.drag = (x0, y0, pan0, True)
            self.draw()

    def canvas_release(self, e):
        moved = self.drag and self.drag[3]
        self.drag = None
        if moved:
            return
        pid = self.nearest(e.x, e.y)
        if not pid:
            return
        ms = self.ms()
        # Клик по телепорту: «я здесь», маршрут от него.
        if pid in self.marks:
            ms["here"] = pid
            self.pin(self.marks[pid])
            self.rebuild()
            return
        # Клик по точке: «я у этой точки», маршрут начинается с неё.
        if pid in ms["checked"]:
            ms["checked"].remove(pid)
        gid = self.points[pid]["gid"]
        if gid in ms["found"]:
            ms["found"].remove(gid)
        self.pin(self.points[pid])
        self.rebuild(first=pid)

    def canvas_right(self, e):
        """Правый клик: отметить точку проверенной или снять отметку."""
        pid = self.nearest(e.x, e.y)
        if not pid or pid in self.marks:
            return
        ms = self.ms()
        if pid in ms["checked"]:
            ms["checked"].remove(pid)
            if self.target():
                self.rebuild(first=self.target())
            else:
                self.rebuild()
            return
        ms["checked"].append(pid)
        if pid in ms["route"]:
            i = ms["route"].index(pid)
            ms["route"].pop(i)
            if i < ms["pos"]:
                ms["pos"] -= 1
        self.refresh()

    # --- настройки ---------------------------------------------------------

    def open_settings(self):
        if getattr(self, "win", None) and self.win.winfo_exists():
            self.win.lift()
            return
        win = self.win = tk.Toplevel(self.root)
        win.title("Hidden Cube: районы")
        win.attributes("-topmost", True)
        win.geometry(f"+{self.root.winfo_x() + 40}+{self.root.winfo_y() + 40}")

        top = ttk.Frame(win, padding=8)
        top.pack(fill="both", expand=True)
        ttk.Label(top, text="Карта:").grid(row=0, column=0, sticky="w")
        mapvar = tk.StringVar(value=MAPS[self.st["map"]])
        box = ttk.Combobox(top, textvariable=mapvar, values=list(MAPS.values()), state="readonly", width=16)
        box.grid(row=0, column=1, sticky="w", pady=(0, 6))

        ttk.Label(top, text="Районы (можно несколько, Ctrl/Shift+клик):").grid(row=1, column=0, columnspan=2, sticky="w")
        lb = tk.Listbox(top, selectmode="extended", width=48, height=18, exportselection=False)
        lb.grid(row=2, column=0, columnspan=2, sticky="nsew")
        top.rowconfigure(2, weight=1)
        top.columnconfigure(1, weight=1)

        def fill():
            lb.delete(0, "end")
            ms = self.ms()
            checked, found = set(ms["checked"]), set(ms["found"])
            self.order = sorted(self.areas, key=lambda a: self.areas[a]["name"])
            for i, a in enumerate(self.order):
                info = self.areas[a]
                done = sum(1 for pid, p in self.points.items() if p["area"] == a and pid in checked)
                nf = sum(1 for g in info["groups"] if g in found)
                water = " · вода" if "water" in a.lower() else ""
                lb.insert("end", f"{info['name']}{water} — точек {info['n']}, кубов {len(info['groups'])}"
                                 + (f" · проверено {done}, найдено {nf}" if done or nf else ""))
                if a in ms["areas"]:
                    lb.selection_set(i)

        def on_map(_):
            key = next(k for k, v in MAPS.items() if v == mapvar.get())
            if key != self.st["map"]:
                self.set_map(key)
                fill()

        box.bind("<<ComboboxSelected>>", on_map)
        fill()

        def apply():
            ms = self.ms()
            ms["areas"] = [self.order[i] for i in lb.curselection()]
            ms["here"] = None
            self.rebuild()
            fill()

        def reset(areas_only):
            ms = self.ms()
            sel = {self.order[i] for i in lb.curselection()} if areas_only else set(self.areas)
            ms["checked"] = [p for p in ms["checked"] if self.points[p]["area"] not in sel]
            ms["found"] = [g for g in ms["found"]
                           if not any(g in self.areas[a]["groups"] for a in sel)]
            ms["here"] = None
            self.rebuild()
            fill()

        btns = ttk.Frame(top)
        btns.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="Построить маршрут", command=apply).pack(side="left")
        ttk.Button(btns, text="Сбросить отметки в выбранных", command=lambda: reset(True)).pack(side="left", padx=6)
        ttk.Button(btns, text="Сбросить всё", command=lambda: reset(False)).pack(side="left")

        al = ttk.Frame(top)
        al.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(al, text="Прозрачность оверлея:").pack(side="left")

        def on_alpha(v):
            self.st["alpha"] = round(float(v), 2)
            self.root.attributes("-alpha", self.st["alpha"])

        sc = ttk.Scale(al, from_=0.3, to=1.0, value=self.st["alpha"], command=on_alpha)
        sc.pack(side="left", fill="x", expand=True, padx=6)

        hide = tk.BooleanVar(value=self.st["auto_hide"])

        def on_hide():
            self.st["auto_hide"] = hide.get()
            self.save()

        ttk.Checkbutton(top, text="Показывать оверлей только поверх игры", variable=hide,
                        command=on_hide).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        help_text = ("Положение определяется само по миникарте игры, когда на ней видны телепорты.\n"
                     "Кнопка ⟳: перестроить маршрут от того места, где ты сейчас.\n"
                     "Клик по телепорту (голубой ромб): «я здесь», маршрут строится от него.\n"
                     "Клик по точке на миникарте: начать маршрут с неё. Правый клик: отметить или снять.\n"
                     "Перетаскивание: сдвинуть карту, колесо: масштаб.\n"
                     + "\n".join(f"{v[2]}: {v[3]}" for v in HOTKEYS.values()))
        ttk.Label(top, text=help_text, foreground="#555").grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        win.protocol("WM_DELETE_WINDOW", lambda: (self.save(), win.destroy()))

    # --- цикл --------------------------------------------------------------

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if isinstance(ev, tuple):
                    self.on_watch(ev)
                else:
                    self.act(ev)
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def on_watch(self, ev):
        if ev[0] == "fg":
            # Оверлей виден только поверх игры (и пока с ним работают).
            # До первого переключения в игру не прячемся: иначе окно исчезает сразу после запуска.
            if ev[1] == "game":
                self.seen_game = True
            if self.st["auto_hide"] and not self.hidden and self.seen_game:
                self.show(ev[1] is not None)
        elif ev[0] == "live":
            pos, self.live_mode = ev[1], ev[2]
            found = pos and not self.live
            self.live = {"x": pos[0], "z": pos[1], "y": None} if pos else None
            if found:
                self.on_found()
            else:
                self.update_info()
                self.draw()

    def on_found(self):
        """Положение только что нашлось: маршрут от игрока, при пустом выборе — ближайший район."""
        ms = self.ms()
        if not ms["areas"]:
            near = min(self.points.values(), key=lambda p: dist(p, self.live))
            ms["areas"] = [near["area"]]
        self.rebuild()

    def update_hint(self):
        """Подсказка локатору, где искать игрока, пока его положение неизвестно."""
        live = self.watcher.live
        if not live:
            return
        here = self.loc(self.ms()["here"])
        live.loc.hint = (here["x"], here["z"]) if here else None

    def pin(self, p):
        """Клик «я здесь»: сразу задаём положение, дальше локатор ведёт сам."""
        if self.watcher.live:
            self.watcher.live.loc.pin(p["x"], p["z"])
            self.live = {"x": p["x"], "z": p["z"], "y": None}
            self.live_mode = "по клику"

    def show(self, on):
        if on and self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        elif not on and self.root.state() != "withdrawn":
            self.root.withdraw()

    def quit(self):
        self.save()
        self.hotkeys.stop()
        self.watcher.running = False
        self.root.destroy()

    def run(self):
        if not self.ms()["areas"]:
            self.root.after(200, self.open_settings)
        self.root.mainloop()


def main():
    # Вторая копия заберёт горячие клавиши у первой и будет мешать: не запускаем её.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, "Aion2HiddenCubeOverlay")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "Оверлей уже запущен. Если его не видно, нажми Ctrl+Alt+H.",
                                         "Hidden Cube", 0x40)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    Overlay().run()


if __name__ == "__main__":
    main()
