"""
AION 2 Global, Kromede - уведомления о боссах в Telegram-канал через GitHub Actions.

Скрипт запускается по расписанию (раз в ~5 минут), читает таймеры NotMeter,
отправляет уведомления и обновляет закреплённое сообщение со списком боссов.
Что уже отправлено, хранится в state.json (его сохраняет workflow между запусками).

Переменные окружения:
    BOT_TOKEN, CHANNEL_ID  - обязательно (GitHub Secrets)
    MAPS   - локации: 1010 Бертрон, 1110 Альтгард, 20 Решанта (по умолчанию все)
    ONLY   - коды нужных боссов через запятую (по умолчанию все)
    DRY_RUN=1 - ничего не отправлять, только печатать
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
SERVER_ID = 2316                                   # Kromede
CACHE_URL = ("https://raw.githubusercontent.com/Not4You-Dev/NotMeter-Cache/main/"
             "presence/notmeter-field-boss-public-ww-eu-expanded.json")
MAPS = {int(x) for x in (os.environ.get("MAPS") or "1010,1110,20").split(",") if x.strip()}
ONLY = {int(x) for x in (os.environ.get("ONLY") or "").split(",") if x.strip()}
WARN_LEAD_MIN = 20     # предупреждать, когда до появления осталось не больше N минут
SPAWN_GRACE_MIN = 15   # сообщить «появился», если запуск опоздал не больше чем на N минут
BOARD_SIZE = 15        # сколько боссов показывать в закреплённом сообщении
STATE_FILE = "state.json"
MSK = ZoneInfo("Europe/Moscow")
REGION_TZ = ZoneInfo("Europe/Berlin")

MAP_NAMES = {1010: "Бертрон", 1110: "Альтгард", 20: "Решанта"}

BOSSES = {
    # Бертрон
    2100003: (1010, "Нейкель востока"), 2100040: (1010, "Гнилой Кутар"),
    2100050: (1010, "Кернон запада"), 2100076: (1010, "Безумный берсерк Кусан"),
    2100077: (1010, "Ритуалист Гаршим"), 2100079: (1010, "Стражник Тигант"),
    2100141: (1010, "Расцветший Корин"), 2100177: (1010, "Разъяренная Сарус"),
    2100178: (1010, "Кровоклык Фнин"), 2100582: (1010, "Отступница Лейла"),
    2100617: (1010, "Черное щупальце Лава"), 2100661: (1010, "Касия фантазии"),
    2100708: (1010, "Центурион Демирос"), 2100718: (1010, "Священный Ансас"),
    2100876: (1010, "Управляющий сбором Мошав"), 2100877: (1010, "Страж Кнаш"),
    2100988: (1010, "Ученый Раулла"), 2100989: (1010, "Лесной воин Ураму"),
    2100991: (1010, "Гонитель Тауло"), 2101016: (1010, "Исследователь Сетрам"),
    2101074: (1010, "Вечный Гартуа"), 2101120: (1010, "Немая Тартан"),
    2101122: (1010, "Властелин душ Кашапа"), 2101131: (1010, "Глава армии Рагт"),
    # Альтгард
    2400017: (1110, "Расплавленный Данар"), 2400074: (1110, "Черный воин Аэд"),
    2400140: (1110, "Верный Ладжит"), 2400141: (1110, "Берсерк Вальг"),
    2400212: (1110, "Хищник Гарсан"), 2400223: (1110, "Воин крови Ланнар"),
    2400274: (1110, "Притворщица Трида"), 2400335: (1110, "Келпина лазурной волны"),
    2400353: (1110, "Главный надзиратель Нута"), 2400358: (1110, "Советник Рсан"),
    2400419: (1110, "Капитан спецотряда Линкс"), 2400424: (1110, "Осквернитель Ноблуд"),
    2400425: (1110, "Аксиос"), 2400474: (1110, "Одурманенный Хадирун"),
    2400504: (1110, "Эмиссар Варсиен"), 2400593: (1110, "Биооружие Гурута"),
    2400607: (1110, "Ветеран Шужакан"), 2400608: (1110, "Визионер Карука"),
    2400659: (1110, "Вишвада темной тени"), 2400709: (1110, "Острый Шурак"),
    2400800: (1110, "Бессмертный Гартуа"), 2400853: (1110, "Глава армии Рагт"),
    2400854: (1110, "Властелин душ Кашапа"), 2400855: (1110, "Немая Тартан"),
    # Решанта
    2600068: (20, "Король духов Агро"), 2600089: (20, "Дозорщик Кайра"),
    2600084: (20, "Лорд-хранитель Нахма"), 2600093: (20, "Лорд-хранитель Нахма"),
    2600094: (20, "Лорд-хранитель Нахма"), 2600096: (20, "Исполнитель Тамаса"),
    2600097: (20, "Исполнитель Агро"), 2600098: (20, "Исполнитель Кайра"),
}

# (код, первая минута суток, интервал (0 = один раз), дни: 0=вс ... 6=сб), время Europe/Berlin
SCHEDULED = [
    (2600089, 60, 180, [0, 1, 2, 3, 4, 5, 6]),     # Кайра: каждые 3 ч с 01:00
    (2600084, 1260, 0, [0, 5]),                    # Нахма: вс, пт 21:00
    (2600096, 1290, 0, [1, 4, 6]),                 # Исполнители: пн, чт, сб 21:30
    (2600097, 1290, 0, [1, 4, 6]),
    (2600098, 1290, 0, [1, 4, 6]),
]
DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def wanted(code):
    return code in BOSSES and BOSSES[code][0] in MAPS and (not ONLY or code in ONLY)


def label(code):
    m, name = BOSSES[code]
    return f"{name} ({MAP_NAMES[m]})"


def fmt(dt):
    d = dt.astimezone(MSK)
    return f"{DAYS[d.weekday()]} {d:%H:%M}"


def http_json(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "aion2-boss-tg-bot", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def tg(method, **params):
    if DRY_RUN:
        print(f"[{method}]", params.get("text", params))
        return {"ok": True, "result": {"message_id": 1}}
    try:
        return http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", params)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"Telegram {method} error {e.code}: {body}")
        return {"ok": False, "description": body}


def next_scheduled(start, interval, days, after):
    local = after.astimezone(REGION_TZ)
    for d in range(8):
        day = (local + timedelta(days=d)).date()
        if (day.weekday() + 1) % 7 not in days:
            continue
        for m in (range(start, 1440, interval) if interval else [start]):
            t = datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=REGION_TZ)
            if t > after:
                return t.astimezone(timezone.utc)
    return None


def collect_targets(cache, now):
    """код -> ближайшее появление (UTC). Берём и недавно прошедшие, чтобы не пропустить «появился»."""
    since = now - timedelta(minutes=SPAWN_GRACE_MIN)
    targets = {}
    server = next((s for s in cache.get("servers", []) if s["serverId"] == SERVER_ID), {})
    for mp in server.get("maps", []):
        for e in mp["entries"]:
            code = e["bossCode"]
            t = datetime.fromtimestamp(e["targetAt"] / 1000, timezone.utc)
            if wanted(code) and t > since:
                targets[code] = t
    for code, start, interval, days in SCHEDULED:
        if wanted(code) and code not in targets:
            t = next_scheduled(start, interval, days, since)
            if t:
                targets[code] = t
    return targets


def already(state, code, kind, target):
    ts = state["sent"].get(str(code), {}).get(kind)
    return ts is not None and abs(ts - target.timestamp()) < 600   # ±10 мин = тот же респ


def mark(state, code, kind, target):
    state["sent"].setdefault(str(code), {})[kind] = target.timestamp()


def main():
    now = datetime.now(timezone.utc)
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("sent", {})

    try:
        cache = http_json(f"{CACHE_URL}?v={int(now.timestamp() // 60)}")
    except Exception as exc:
        print("NotMeter cache error:", exc)
        cache = {}
    targets = collect_targets(cache, now)

    for code, target in sorted(targets.items(), key=lambda x: x[1]):
        mins = (target - now).total_seconds() / 60
        if 0 < mins <= WARN_LEAD_MIN and not already(state, code, "warn", target):
            tg("sendMessage", chat_id=CHANNEL_ID,
               text=f"⏰ Через {max(1, round(mins))} мин: {label(code)}\nПоявление: {fmt(target)} МСК")
            mark(state, code, "warn", target)
        elif -SPAWN_GRACE_MIN <= mins <= 0 and not already(state, code, "spawn", target):
            tg("sendMessage", chat_id=CHANNEL_ID, text=f"🔥 Появился: {label(code)}")
            mark(state, code, "spawn", target)
            mark(state, code, "warn", target)

    # закреплённое сообщение со списком ближайших боссов
    upcoming = sorted((t, c) for c, t in targets.items() if t > now)[:BOARD_SIZE]
    gen = cache.get("generatedAt")
    lines = [f"🗓 Боссы Kromede (обновлено {fmt(now)} МСК)"]
    if gen:
        lines.append(f"Данные NotMeter от {fmt(datetime.fromtimestamp(gen, timezone.utc))}")
    lines.append("")
    lines += [f"{fmt(t)} - {label(c)}" for t, c in upcoming] or ["Нет данных"]
    text = "\n".join(lines)
    board_id = state.get("board_id")
    ok = False
    if board_id:
        r = tg("editMessageText", chat_id=CHANNEL_ID, message_id=board_id, text=text)
        ok = r.get("ok") or "not modified" in r.get("description", "")
    if not ok:
        r = tg("sendMessage", chat_id=CHANNEL_ID, text=text, disable_notification=True)
        if r.get("ok"):
            state["board_id"] = r["result"]["message_id"]
            tg("pinChatMessage", chat_id=CHANNEL_ID, message_id=state["board_id"],
               disable_notification=True)

    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"))


if __name__ == "__main__":
    main()
