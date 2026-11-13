"""Home Trainer Bot: домашній тренер у Telegram.

Нагадує про вправи між START і END, питає «Вийшло?», веде статистику.
Один користувач (OWNER_ID), дані в SQLite, налаштування в .env.
"""
import json
import logging
import os
import random
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

load_dotenv()
log = logging.getLogger("trainer")


def _hm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def _days(spec: str) -> set:
    """'1-5' або '1,3,5' (1 = понеділок) -> {1,2,3,4,5}."""
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Kyiv"))
START = _hm(os.getenv("START", "10:00"))
END = _hm(os.getenv("END", "17:00"))
SLOTS = int(os.getenv("SLOTS", "6"))
WORKDAYS = _days(os.getenv("WORKDAYS", "1-5"))
WINDOW_MIN = int(os.getenv("WINDOW_MIN", "30"))      # скільки часу на вправу
RETRY_MIN = int(os.getenv("RETRY_MIN", "15"))        # «те саме через…»
MAX_REMINDERS = int(os.getenv("MAX_REMINDERS", "2"))  # скільки разів повторювати
STALE_MIN = int(os.getenv("STALE_MIN", "20"))        # прострочене нагадування ігноруємо
DEMO_MIN = int(os.getenv("DEMO_MIN", "1"))           # у /demo все швидше
DB_PATH = os.getenv("DB_PATH", "trainer.db")

EXERCISES = {
    "squat": ("🦵", "Присідання", "20 разів"),
    "abs": ("🔥", "Прес", "20 скручувань"),
    "burpee": ("⚡", "Бьорпі", "10 разів"),
    "plank": ("🧱", "Планка", "1 хвилина"),
    "pushup": ("💪", "Віджимання", "10–15 разів"),
    "lunge": ("🚶", "Випади", "10 на кожну ногу"),
    "neck": ("🙆", "Розминка шиї й плечей", "2 хвилини"),
    "stretch": ("🧘", "Розтяжка", "3 хвилини"),
}

DB: sqlite3.Connection = None  # type: ignore


# ---------- час і база ----------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def hhmm(s: str) -> str:
    return parse(s).astimezone(TZ).strftime("%H:%M")


def init_db(path: str) -> None:
    global DB
    DB = sqlite3.connect(path)
    DB.row_factory = sqlite3.Row
    DB.isolation_level = None  # autocommit
    DB.executescript("""
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS slots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL, at TEXT NOT NULL, exercise TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'planned',
        reminders INTEGER NOT NULL DEFAULT 0,
        demo INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_id INTEGER, kind TEXT NOT NULL, day TEXT,
        due TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ev_due ON events(done, due);
    """)


def q(sql: str, args=()):
    return DB.execute(sql, args).fetchall()


def get_exercises() -> list:
    r = q("SELECT value FROM settings WHERE key='exercises'")
    return json.loads(r[0]["value"]) if r else []


def set_exercises(keys: list) -> None:
    DB.execute("INSERT OR REPLACE INTO settings VALUES('exercises', ?)",
               (json.dumps(keys),))


def add_event(kind: str, due: datetime, slot_id=None, day=None) -> None:
    DB.execute("INSERT INTO events(slot_id, kind, day, due) VALUES(?,?,?,?)",
               (slot_id, kind, day, iso(due)))


def title(key: str) -> str:
    e, name, _ = EXERCISES[key]
    return f"{e} {name}"


# ---------- планування ----------

def _round5(t: datetime) -> datetime:
    return t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)


def _assign(n: int, keys: list) -> list:
    pool = keys[:]
    random.shuffle(pool)
    return [pool[i % len(pool)] for i in range(n)]


def plan_today(now: datetime) -> int:
    """Створює слоти на сьогодні, якщо це робочий день і їх ще немає."""
    local = now.astimezone(TZ)
    d = local.date()
    keys = get_exercises()
    if d.isoweekday() not in WORKDAYS or not keys:
        return 0
    if q("SELECT 1 FROM slots WHERE day=? AND demo=0 LIMIT 1", (d.isoformat(),)):
        return 0
    start = datetime.combine(d, START, TZ)
    end = datetime.combine(d, END, TZ)
    step = (end - start) / SLOTS
    plan = _assign(SLOTS, keys)
    created = 0
    for i in range(SLOTS):
        t = _round5(start + step * i)
        if t < now - timedelta(minutes=2):
            continue
        cur = DB.execute(
            "INSERT INTO slots(day, at, exercise) VALUES(?,?,?)",
            (d.isoformat(), iso(t), plan[i]))
        add_event("remind", t, cur.lastrowid, d.isoformat())
        created += 1
    if created and end > now:
        add_event("summary", end, None, d.isoformat())
    return created


def retarget_future(now: datetime) -> None:
    """Користувач змінив набір вправ посеред дня: оновлюємо майбутні слоти."""
    keys = get_exercises()
    rows = q("SELECT id FROM slots WHERE status='planned' AND demo=0 AND at>? "
             "ORDER BY at", (iso(now),))
    for row, ex in zip(rows, _assign(len(rows), keys) if keys else []):
        DB.execute("UPDATE slots SET exercise=? WHERE id=?", (ex, row["id"]))


# ---------- статистика ----------

def day_stats(now: datetime) -> dict:
    """Рахує дні, серію і пропуски. Демо-слоти не враховуються."""
    today = now.astimezone(TZ).date()
    end_today = datetime.combine(today, END, TZ)
    by_day = {}
    for r in q("SELECT day, status FROM slots WHERE demo=0"):
        d = by_day.setdefault(r["day"], [0, 0])
        d[1] += 1
        if r["status"] == "done":
            d[0] += 1
    streak = 0
    for day in sorted(by_day, reverse=True):
        done, _total = by_day[day]
        finished = day < today.isoformat() or now >= end_today
        if day == today.isoformat() and not finished and done == 0:
            continue  # день ще триває, не ламаємо серію
        if done > 0:
            streak += 1
        elif finished:
            break
    week_from = (today - timedelta(days=6)).isoformat()
    w_done = w_total = w_skipped_days = w_active = 0
    for day, (done, total) in by_day.items():
        if day < week_from:
            continue
        w_done += done
        w_total += total
        finished = day < today.isoformat() or now >= end_today
        if done > 0:
            w_active += 1
        elif finished:
            w_skipped_days += 1
    return {"streak": streak, "w_done": w_done, "w_total": w_total,
            "w_active": w_active, "w_skipped_days": w_skipped_days,
            "all_active": sum(1 for v in by_day.values() if v[0] > 0)}


def stats_text(now: datetime) -> str:
    s = day_stats(now)
    return (f"🔥 Серія активних днів: <b>{s['streak']}</b>\n"
            f"📅 За 7 днів: активних днів <b>{s['w_active']}</b>, "
            f"пропущених <b>{s['w_skipped_days']}</b>\n"
            f"✅ Вправ за 7 днів: <b>{s['w_done']}</b> з {s['w_total']}\n"
            f"🏁 Усього активних днів: {s['all_active']}")


def today_lines(day: str) -> str:
    icon = {"done": "✅", "skipped": "⏭", "missed": "❌",
            "planned": "🕒", "active": "⏳"}
    rows = q("SELECT at, exercise, status FROM slots WHERE day=? AND demo=0 "
             "ORDER BY at", (day,))
    return "\n".join(f"{icon[r['status']]} {hhmm(r['at'])} {title(r['exercise'])}"
                     for r in rows)


def next_slot_text(now: datetime) -> str:
    r = q("SELECT at FROM slots WHERE status='planned' AND demo=0 AND at>? "
          "ORDER BY at LIMIT 1", (iso(now),))
    if r:
        return f"Наступна вправа о {hhmm(r[0]['at'])}."
    if now.astimezone(TZ).time() < END:
        return f"На сьогодні це все. Підсумок дня о {END:%H:%M}."
    return "На сьогодні це все."


# ---------- повідомлення ----------

def reminder_markup(slot_id: int) -> Markup:
    return Markup([[Btn("✅ Зроблено", callback_data=f"done:{slot_id}"),
                    Btn("⏭ Пропустити", callback_data=f"skip:{slot_id}")]])


def picker(selected: list):
    rows, row = [], []
    for key, (_e, name, _h) in EXERCISES.items():
        mark = "✅" if key in selected else "⬜"
        row.append(Btn(f"{mark} {name}", callback_data=f"ex:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("Готово ✔", callback_data="exok")])
    text = ("Обери вправи, з яких складатиму твій день. "
            "Можна змінити будь-коли: /exercises")
    return text, Markup(rows)


async def send_reminder(bot, slot_id: int, now: datetime) -> None:
    s = q("SELECT * FROM slots WHERE id=?", (slot_id,))[0]
    window = DEMO_MIN if s["demo"] else WINDOW_MIN
    DB.execute("UPDATE slots SET status='active', reminders=reminders+1 "
               "WHERE id=?", (slot_id,))
    _e, _n, hint = EXERCISES[s["exercise"]]
    text = (f"⏰ Час для вправи!\n\n<b>{title(s['exercise'])}</b>: {hint}\n\n"
            f"Часу є {window} хв. Без поспіху і без тиску 🙂")
    await bot.send_message(OWNER_ID, text, parse_mode="HTML",
                           reply_markup=reminder_markup(slot_id))
    add_event("ask", now + timedelta(minutes=window), slot_id, s["day"])


async def send_summary(bot, day: str, now: datetime) -> None:
    DB.execute("UPDATE slots SET status='missed' WHERE day=? AND demo=0 "
               "AND status IN ('planned','active')", (day,))
    rows = q("SELECT status FROM slots WHERE day=? AND demo=0", (day,))
    if not rows:
        return
    done = sum(1 for r in rows if r["status"] == "done")
    head = f"🏁 <b>Підсумок дня</b>: зроблено {done} з {len(rows)}\n\n"
    tail = "\n\n" + stats_text(now)
    if done == 0:
        tail += "\n\nСьогодні без вправ. Нічого страшного, завтра нова спроба."
    elif done == len(rows):
        tail += "\n\nПовний комплект! 🎉"
    await bot.send_message(OWNER_ID, head + today_lines(day) + tail,
                           parse_mode="HTML")


async def process_events(bot, now: datetime) -> None:
    due = q("SELECT * FROM events WHERE done=0 AND due<=? ORDER BY due, id",
            (iso(now),))
    for ev in due:
        DB.execute("UPDATE events SET done=1 WHERE id=?", (ev["id"],))
        late = now - parse(ev["due"])
        try:
            if ev["kind"] == "summary":
                if late <= timedelta(hours=3):
                    await send_summary(bot, ev["day"], now)
                else:
                    DB.execute("UPDATE slots SET status='missed' WHERE day=? "
                               "AND demo=0 AND status IN ('planned','active')",
                               (ev["day"],))
                continue
            slot = q("SELECT * FROM slots WHERE id=?", (ev["slot_id"],))
            if not slot or slot[0]["status"] not in ("planned", "active"):
                continue
            slot = slot[0]
            if ev["kind"] == "remind":
                if late > timedelta(minutes=STALE_MIN):
                    DB.execute("UPDATE slots SET status='missed' WHERE id=?",
                               (slot["id"],))
                else:
                    await send_reminder(bot, slot["id"], now)
            elif ev["kind"] == "ask" and slot["status"] == "active":
                await bot.send_message(
                    OWNER_ID, f"Ну що, вийшло? {title(slot['exercise'])}",
                    reply_markup=Markup([[
                        Btn("Так ✅", callback_data=f"yes:{slot['id']}"),
                        Btn("Ні ❌", callback_data=f"no:{slot['id']}")]]))
        except Exception:
            log.exception("event %s failed", ev["id"])


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = utcnow()
    try:
        plan_today(now)
        await process_events(context.bot, now)
    except Exception:
        log.exception("tick failed")


# ---------- кнопки ----------

async def handle_action(bot, data: str, now: datetime):
    """Повертає (новий текст повідомлення, нова клавіатура або None)."""
    action, _, arg = data.partition(":")

    if action == "ex":
        keys = get_exercises()
        if arg in EXERCISES:
            keys = [k for k in keys if k != arg] if arg in keys else keys + [arg]
            keys = [k for k in EXERCISES if k in keys]
            set_exercises(keys)
        return picker(keys)
    if action == "exok":
        keys = get_exercises()
        if not keys:
            return "Обери хоча б одну вправу 🙂", picker(keys)[1]
        made = plan_today(now)
        if not made:
            retarget_future(now)
        names = ", ".join(EXERCISES[k][1] for k in keys)
        return (f"Готово! Мої вправи: {names}.\n\n"
                f"Працюю в будні з {START:%H:%M} до {END:%H:%M}: "
                f"{SLOTS} вправ на день.\n{next_slot_text(now)}\n"
                "Спробувати зараз: /demo"), None

    slot_rows = q("SELECT * FROM slots WHERE id=?", (int(arg or 0),))
    if not slot_rows:
        return "Не знайшов це нагадування (можливо, воно старе).", None
    s = slot_rows[0]
    sid, name = s["id"], title(s["exercise"])
    if s["status"] in ("done", "skipped") and action in ("done", "yes", "skip"):
        return f"{name}: вже відмічено.", None

    if action in ("done", "yes"):
        DB.execute("UPDATE slots SET status='done' WHERE id=?", (sid,))
        return f"✅ {name}: зроблено. Молодець!\n{next_slot_text(now)}", None
    if action == "skip":
        DB.execute("UPDATE slots SET status='skipped' WHERE id=?", (sid,))
        return f"⏭ {name}: пропускаємо, ніхто не засуджує.\n{next_slot_text(now)}", None
    if action == "no":
        rows = []
        if s["reminders"] < MAX_REMINDERS:
            wait = DEMO_MIN if s["demo"] else RETRY_MIN
            rows.append([Btn(f"🔁 Те саме через {wait} хв",
                             callback_data=f"again:{sid}")])
        rows.append([Btn("🔀 Інша вправа", callback_data=f"swap:{sid}")])
        rows.append([Btn("⏭ Пропустити", callback_data=f"skip:{sid}")])
        return f"Буває. {name}: що робимо?", Markup(rows)
    if action == "again":
        wait = DEMO_MIN if s["demo"] else RETRY_MIN
        add_event("remind", now + timedelta(minutes=wait), sid, s["day"])
        DB.execute("UPDATE slots SET status='planned' WHERE id=?", (sid,))
        return f"Добре, нагадаю про {name} через {wait} хв.", None
    if action == "swap":
        keys = [k for k in get_exercises() if k != s["exercise"]] or \
               [k for k in EXERCISES if k != s["exercise"]]
        new = random.choice(keys)
        DB.execute("UPDATE slots SET exercise=?, reminders=0 WHERE id=?",
                   (new, sid))
        await send_reminder(bot, sid, now)
        return f"🔀 Міняю {name} на {title(new)}.", None
    return "Невідома дія.", None


# ---------- команди ----------

def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id != OWNER_ID:
            uid = user.id if user else "?"
            if update.callback_query:
                await update.callback_query.answer("Це приватний бот", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    f"Це приватний бот. Твій ID: {uid}")
            return
        return await fn(update, context)
    return wrapper


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твій Telegram ID: {update.effective_user.id}")


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я твій домашній тренер 💪\n\n"
        "Працюєш з дому, сидиш багато, а поворушитися треба. Я не змушую, "
        "тільки нагадую: протягом дня пропоную кілька коротких вправ, "
        "а ввечері показую підсумок.")
    text, kb = picker(get_exercises())
    await update.message.reply_text(text, reply_markup=kb)


@owner_only
async def cmd_exercises(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = picker(get_exercises())
    await update.message.reply_text(text, reply_markup=kb)


@owner_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = utcnow()
    plan_today(now)
    day = now.astimezone(TZ).date().isoformat()
    lines = today_lines(day)
    await update.message.reply_text(
        lines or "На сьогодні вправ немає (вихідний або ще не обрано вправи: /exercises).")


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text(utcnow()), parse_mode="HTML")


@owner_only
async def cmd_demo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = utcnow()
    keys = get_exercises() or list(EXERCISES)
    cur = DB.execute(
        "INSERT INTO slots(day, at, exercise, demo) VALUES(?,?,?,1)",
        (now.astimezone(TZ).date().isoformat(), iso(now), random.choice(keys)))
    await update.message.reply_text(
        f"Демо: це нагадування не потрапить у статистику, а запитання "
        f"«Вийшло?» прийде за {DEMO_MIN} хв.")
    await send_reminder(context.bot, cur.lastrowid, now)


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text, kb = await handle_action(context.bot, query.data, utcnow())
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        ("today", "Вправи на сьогодні"), ("stats", "Статистика"),
        ("exercises", "Обрати вправи"), ("demo", "Показати, як це працює"),
        ("id", "Мій Telegram ID")])


def build_app() -> Application:
    init_db(DB_PATH)
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("exercises", cmd_exercises))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("demo", cmd_demo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.job_queue.run_repeating(tick, interval=15, first=3)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not BOT_TOKEN:
        raise SystemExit("Не задано BOT_TOKEN (див. файл .env)")
    if not OWNER_ID:
        log.warning("OWNER_ID не заданий: бот відповідає лише на /id")
    build_app().run_polling()


if __name__ == "__main__":
    main()
