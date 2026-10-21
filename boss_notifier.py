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
import time
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
# По умолчанию: вся Бездна + Гартуа, Рагт, Кашапа, Тартан в Альтгарде.
# GitHub Variables MAPS / ONLY, если заданы, перекрывают эти значения.
MAPS = {int(x) for x in (os.environ.get("MAPS") or "1110,20").split(",") if x.strip()}
ONLY = {int(x) for x in (os.environ.get("ONLY") or
        "2400800,2400853,2400854,2400855,"                       # Гартуа, Рагт, Кашапа, Тартан
        "2600068,2600089,2600084,2600093,2600094,2600096,2600097,2600098"  # Бездна
        ).split(",") if x.strip()}
ALERTS = [15, 5]       # за сколько минут предупреждать
RUN_MINUTES = 5.5      # сколько работает один запуск: ждёт уведомлений и отвечает на команды
CACHE_REFRESH_SEC = 180
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


def http_json(url, payload=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "aion2-boss-tg-bot", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def tg(method, http_timeout=20, **params):
    if DRY_RUN and method != "getUpdates":
        print(f"[{method}]", params.get("text", params))
        return {"ok": True, "result": {"message_id": 1}}
    try:
        return http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", params, http_timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"Telegram {method} error {e.code}: {body}")
        return {"ok": False, "description": body}
    except Exception as e:
        print(f"Telegram {method} error: {e}")
        return {"ok": False, "description": str(e)}


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
    since = now
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


def boss_key(code):
    # у Нахмы три кода на одно появление - считаем их одним боссом
    return "|".join(map(str, BOSSES[code]))


def already(state, key, kind, target):
    ts = state["sent"].get(key, {}).get(kind)
    return ts is not None and abs(ts - target.timestamp()) < 600   # ±10 мин = тот же респ


def mark(state, key, kind, target):
    state["sent"].setdefault(key, {})[kind] = target.timestamp()


def alert(state, code, target, lead):
    key = boss_key(code)
    if already(state, key, f"w{lead}", target):
        return
    mins = max(1, round((target - datetime.now(timezone.utc)).total_seconds() / 60))
    tg("sendMessage", chat_id=CHANNEL_ID,
       text=f"⏰ Через {mins} мин: {label(code)}\nПоявление: {fmt(target)} МСК")
    for l in ALERTS:            # более ранние уведомления уже не нужны
        if l >= lead:
            mark(state, key, f"w{l}", target)


def load_cache(now):
    try:
        return http_json(f"{CACHE_URL}?v={int(now.timestamp() // 60)}")
    except Exception as exc:
        print("NotMeter cache error:", exc)
        return {}


def build_events(targets, now, until):
    """Моменты уведомлений до конца запуска: (когда, код, время появления, за сколько минут)."""
    events = []
    for code, target in targets.items():
        for lead in ALERTS:
            moment = target - timedelta(minutes=lead)
            smaller = max([l for l in ALERTS if l < lead], default=0)
            # опоздали к моменту: шлём сейчас, если ещё не пора слать следующее уведомление
            due_now = moment <= now < target - timedelta(minutes=smaller)
            if due_now or now < moment <= until:
                events.append((max(moment, now), code, target, lead))
    return sorted(events)


def boss_list(targets, cache, limit=None):
    now = datetime.now(timezone.utc)
    seen, lines = set(), []
    for code, t in sorted(targets.items(), key=lambda x: x[1]):
        if t <= now or boss_key(code) in seen:
            continue
        seen.add(boss_key(code))
        left = int((t - now).total_seconds() // 60)
        left_txt = f"{left // 60} ч {left % 60} мин" if left >= 60 else f"{left} мин"
        lines.append(f"{fmt(t)} (через {left_txt}) - {label(code)}")
    if limit:
        lines = lines[:limit]
    gen = cache.get("generatedAt")
    head = "🗓 Таймеры боссов Kromede (МСК)"
    if gen:
        head += f"\nДанные NotMeter от {fmt(datetime.fromtimestamp(gen, timezone.utc))}"
    return head + "\n\n" + ("\n".join(lines) or "Нет данных")


def handle_updates(updates, state, targets, cache):
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/bosses", "/timers", "/boss"):
            reply = boss_list(targets, cache)
        elif cmd == "/next":
            reply = boss_list(targets, cache, limit=3)
        elif cmd in ("/start", "/help"):
            reply = "Команды:\n/bosses - все таймеры\n/next - ближайшие 3 босса"
        else:
            continue
        tg("sendMessage", chat_id=msg["chat"]["id"], text=reply,
           reply_to_message_id=msg.get("message_id"))


def main():
    started = datetime.now(timezone.utc)
    deadline = started + timedelta(minutes=RUN_MINUTES)
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("sent", {})

    # убрать закреплённый список из старой версии бота
    if state.get("board_id"):
        tg("unpinChatMessage", chat_id=CHANNEL_ID, message_id=state["board_id"])
        tg("deleteMessage", chat_id=CHANNEL_ID, message_id=state["board_id"])
        state.pop("board_id")
    if state.get("commands_v") != 1:          # меню команд в Telegram
        tg("setMyCommands", commands=[
            {"command": "bosses", "description": "Все таймеры боссов"},
            {"command": "next", "description": "Ближайшие 3 босса"}])
        state["commands_v"] = 1

    cache, cache_at, targets, events = {}, None, {}, []
    while True:
        now = datetime.now(timezone.utc)
        if cache_at is None or (now - cache_at).total_seconds() >= CACHE_REFRESH_SEC:
            cache, cache_at = load_cache(now), now
            targets = collect_targets(cache, now)
            events = build_events(targets, now, deadline)

        # отправить уведомления, время которых пришло
        while events and events[0][0] <= now:
            _, code, target, lead = events.pop(0)
            alert(state, code, target, lead)
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"))
        if now >= deadline or DRY_RUN:
            break

        # ждём команды до ближайшего события (long polling)
        until = min([deadline] + [e[0] for e in events[:1]])
        wait = int(max(1, min(50, (until - now).total_seconds())))
        r = tg("getUpdates", http_timeout=wait + 15, timeout=wait,
               offset=state.get("offset"), allowed_updates=["message"])
        if r.get("ok"):
            handle_updates(r["result"], state, targets, cache)
        else:
            time.sleep(min(wait, 10))


if __name__ == "__main__":
    main()
