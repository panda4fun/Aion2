"""Скачивает точки Hidden Cube с aion2maps.com и сохраняет их в data/<карта>.json.

Заодно рисует по карте высот подложку с рельефом: data/<карта>_relief.png.

Запуск: py cube_overlay/fetch_cubes.py
"""

import json
import math
import re
import struct
import urllib.request
import zlib
from pathlib import Path

BASE = "https://aion2maps.com/map/assets/maps"
DATA_DIR = Path(__file__).with_name("data")

# Карты, открытые на Global, где есть Hidden Cube.
MAPS = {
    "World_D_A": "Altgard",
    "World_L_A": "Verteron",
}


LANDMARK_KINDS = {"teleport", "dungeon", "monolith"}

# Цвет суши по высоте (м): тёмные тона, чтобы точки маршрута читались поверх.
LAND_STOPS = [(0, (70, 92, 52)), (80, (92, 104, 60)), (200, (104, 92, 64)),
              (350, (112, 106, 96)), (600, (170, 170, 170))]
SEA = (28, 62, 84)


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "aion2-cube-overlay"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch_bytes(url))


def land_colour(h):
    for (h0, c0), (h1, c1) in zip(LAND_STOPS, LAND_STOPS[1:]):
        if h <= h1:
            t = max(0.0, (h - h0) / (h1 - h0))
            return [a + (b - a) * t for a, b in zip(c0, c1)]
    return list(LAND_STOPS[-1][1])


def blur(v, n):
    """Размытие 3x3 в два прохода, чтобы ступеньки высот по 4 м не рябили."""
    out = v[:]
    for r in range(n):
        row = r * n
        for c in range(1, n - 1):
            out[row + c] = (v[row + c - 1] + v[row + c] + v[row + c + 1]) / 3
    v, out = out, out[:]
    for r in range(1, n - 1):
        for c in range(n):
            i = r * n + c
            out[i] = (v[i - n] + v[i] + v[i + n]) / 3
    return out


def write_png(path, w, h, rgb):
    raw = b"".join(b"\0" + rgb[r * w * 3:(r + 1) * w * 3] for r in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def i16s(buf, cnt):
    """int16, записанные «сначала младшие байты, потом старшие»."""
    return [(lo | hi << 8) - (65536 if hi > 127 else 0) for lo, hi in zip(buf[:cnt], buf[cnt:2 * cnt])]


def extras_top(key, vi, n):
    """Верх скал, построек и деревьев над ландшафтом по столбцам (уровни по block_m) или None.

    Формат cols4.bin описан в vox/index.json: на столбец nrun отрезков (gap, len),
    первый отсчитывается от верха ландшафта terr, следующие от конца предыдущего.
    """
    cols = vi.get("cols")
    if not cols or cols["n"] != n:
        return None
    b = zlib.decompress(fetch_bytes(f"{BASE}/{key}/vox/{cols['file']}"))
    R, nn = cols["runs"], n * n
    nrun = b[:nn]
    p = nn
    gap = i16s(b[p:p + 2 * R], R)
    p += 2 * R
    ln = i16s(b[p:p + 2 * R], R)
    p += 2 * R
    terr = i16s(b[p:p + 2 * nn], nn)
    top = [None] * nn
    j = 0
    for i in range(nn):
        k = nrun[i]
        if k:
            cur = terr[i]
            for g, l in zip(gap[j:j + k], ln[j:j + k]):
                cur += g + l
            top[i] = cur
            j += k
    return top


def render_relief(key):
    """Подложка: цвет по высоте и тени от рельефа. Пиксель = block_m метров, север сверху."""
    vi = fetch_json(f"{BASE}/{key}/vox/index.json")
    tops = vi["tops"]
    n, step, off, sea = tops["n"], tops["block_m"], vi["off"], vi["ocean_y"]
    raw = zlib.decompress(fetch_bytes(f"{BASE}/{key}/vox/{tops['file']}"))
    h = [off + (b - 256 if b > 127 else b) * step for b in raw]
    # Скалы и руины в игре часто отдельные объекты, а не ландшафт: без них на их месте «море».
    ext = extras_top(key, vi, n)
    if ext:
        h = [max(a, off + e * step) if e is not None else a for a, e in zip(h, ext)]
    hs = blur(blur(h, n), n)
    sx, sy, sz = vi.get("sun", [-0.5, 0.74, 0.45])
    rgb = bytearray(n * n * 3)
    for r in range(n):
        for c in range(n):
            i = r * n + c
            if h[i] <= sea + 0.5:
                col, k = SEA, 1.0
            else:
                dx = (hs[i + 1] - hs[i - 1]) / (2 * step) if 0 < c < n - 1 else 0
                dz = (hs[i + n] - hs[i - n]) / (2 * step) if 0 < r < n - 1 else 0
                lit = (-dx * sx + sy - dz * sz) / math.sqrt(dx * dx + 1 + dz * dz)
                col, k = land_colour(h[i]), 0.55 + 0.6 * max(0.0, lit)
            rgb[i * 3:i * 3 + 3] = bytes(min(255, int(v * k)) for v in col)
    path = DATA_DIR / f"{key}_relief.png"
    write_png(path, n, n, bytes(rgb))
    return {"file": path.name, "n": n, "block_m": step, "x0": vi["x0"]}


def poly_area(poly):
    return abs(sum(x1 * z2 - x2 * z1 for (x1, z1), (x2, z2) in zip(poly, poly[1:] + poly[:1]))) / 2


def inside(poly, x, z):
    hit = False
    for (x1, z1), (x2, z2) in zip(poly, poly[1:] + poly[:1]):
        if (z1 > z) != (z2 > z) and x < x1 + (z - z1) * (x2 - x1) / (z2 - z1):
            hit = not hit
    return hit


def area_key(spawner):
    """EnvObj_D1_BasfeltRuins_HiddenBox_01_B1 -> BasfeltRuins."""
    m = re.match(r"EnvObj_[A-Z]\d_(.+?)_Hidden(?:Box|Cube)", spawner)
    return m.group(1) if m else spawner


def split_camel(s):
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s.replace("_", " ")).strip()


def build(key, name):
    d = fetch_json(f"{BASE}/{key}/mapdata.json")
    # Самая мелкая подзона, в которую попадает точка, без подзоны всего региона.
    zones = sorted(
        (s for s in d["subzones"] if s["n"] and s["n"] != name and len(s["poly"]) > 2),
        key=lambda s: poly_area(s["poly"]),
    )

    def zone_at(x, z):
        return next((s["n"] for s in zones if inside(s["poly"], x, z)), None)

    groups = []
    for c in d["cubes"]:
        water = bool(c.get("w")) or "water" in c["s"].lower() or "lake" in c["s"].lower()
        points = []
        for i, (x, y, z) in enumerate(c["pos"]):
            p = {"x": x, "y": y, "z": z}
            if c.get("dep"):
                p["dep"] = c["dep"][i]
            points.append(p)
        groups.append({
            "id": c["s"].removeprefix("EnvObj_"),
            "area": area_key(c["s"]),
            "water": water,
            "points": points,
        })

    # Название района: подзона, в которую попадает больше всего точек района.
    names = {}
    for area in {g["area"] for g in groups}:
        votes = {}
        for g in groups:
            if g["area"] == area:
                for p in g["points"]:
                    zn = zone_at(p["x"], p["z"])
                    if zn:
                        votes[zn] = votes.get(zn, 0) + 1
        names[area] = max(votes, key=votes.get) if votes else split_camel(area)
    # Два района с одной подзоной различаем по коду спавнера.
    for area, n in list(names.items()):
        if list(names.values()).count(n) > 1:
            names[area] = f"{n} ({split_camel(area)})"
    for g in groups:
        g["area_name"] = names[g["area"]]

    landmarks = [{"k": o["k"], "n": o["n"], "x": o["x"], "y": o["y"], "z": o["z"]}
                 for o in d["objects"] if o["k"] in LANDMARK_KINDS and o.get("n")]

    DATA_DIR.mkdir(exist_ok=True)
    out = {
        "map": key,
        "name": name,
        "source": f"https://aion2maps.com ({BASE}/{key}/mapdata.json)",
        "subzones": [{"n": s["n"], "c": s["c"], "poly": s["poly"]} for s in zones],
        "landmarks": landmarks,
        "relief": render_relief(key),
        "groups": groups,
    }
    path = DATA_DIR / f"{key}.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    total = sum(len(g["points"]) for g in groups)
    print(f"{name}: {len(groups)} групп, {total} точек -> {path}")


def main():
    for key, name in MAPS.items():
        build(key, name)


if __name__ == "__main__":
    main()
