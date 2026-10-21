"""Короткий обход точек: ближайший сосед, затем улучшение 2-opt.

Путь открытый: начинается в заданной точке и никуда не возвращается.
"""

import math

# Подъём и спуск обходятся дороже, чем ход по ровному.
HEIGHT_WEIGHT = 1.5


def dist(a, b):
    # Высота живой позиции неизвестна: тогда считаем по плоскости.
    dy = a["y"] - b["y"] if a["y"] is not None and b["y"] is not None else 0
    return math.sqrt((a["x"] - b["x"]) ** 2 + (a["z"] - b["z"]) ** 2 + (HEIGHT_WEIGHT * dy) ** 2)


def build_route(points, start=None):
    """Возвращает индексы points в порядке обхода.

    start: точка (dict с x/y/z), от которой начинаем; сама в маршрут не входит.
    """
    n = len(points)
    if n == 0:
        return []
    nodes = ([start] if start else []) + list(points)
    off = 1 if start else 0
    m = len(nodes)
    d = [[dist(a, b) for b in nodes] for a in nodes]

    # Ближайший сосед. Без стартовой точки начинаем с самой западной.
    first = 0 if start else min(range(m), key=lambda i: nodes[i]["x"])
    order = [first]
    left = set(range(m)) - {first}
    while left:
        cur = order[-1]
        nxt = min(left, key=lambda j: d[cur][j])
        order.append(nxt)
        left.remove(nxt)

    # 2-opt для открытого пути: первая вершина закреплена.
    improved = True
    while improved:
        improved = False
        for i in range(1, m - 1):
            a, b = order[i - 1], order[i]
            for k in range(i + 1, m):
                c = order[k]
                e = order[k + 1] if k + 1 < m else None
                old = d[a][b] + (d[c][e] if e is not None else 0)
                new = d[a][c] + (d[b][e] if e is not None else 0)
                if new < old - 1e-6:
                    order[i:k + 1] = reversed(order[i:k + 1])
                    improved = True
                    b = order[i]

    return [i - off for i in order if i >= off]


def route_length(points, order, start=None):
    seq = ([start] if start else []) + [points[i] for i in order]
    return sum(dist(a, b) for a, b in zip(seq, seq[1:]))
