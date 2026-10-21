"""Скачивает точки Hidden Cube с aion2maps.com и сохраняет их в data/<карта>.json.

Запуск: py cube_overlay/fetch_cubes.py
"""

import json
import re
import urllib.request
from pathlib import Path

BASE = "https://aion2maps.com/map/assets/maps"
DATA_DIR = Path(__file__).with_name("data")

# Карты, открытые на Global, где есть Hidden Cube.
MAPS = {
    "World_D_A": "Altgard",
    "World_L_A": "Verteron",
}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "aion2-cube-overlay"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


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

    out = {
        "map": key,
        "name": name,
        "source": f"https://aion2maps.com ({BASE}/{key}/mapdata.json)",
        "subzones": [{"n": s["n"], "c": s["c"], "poly": s["poly"]} for s in zones],
        "groups": groups,
    }
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{key}.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    total = sum(len(g["points"]) for g in groups)
    print(f"{name}: {len(groups)} групп, {total} точек -> {path}")


def main():
    for key, name in MAPS.items():
        build(key, name)


if __name__ == "__main__":
    main()
