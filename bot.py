import os
import json
import re
import logging
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, PollAnswerHandler, ContextTypes
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
OWNER_ID = int(os.environ["OWNER_ID"])
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Riyadh"))
SEND_TIME = os.environ.get("SEND_TIME", "09:00")
POLL_TIME = os.environ.get("POLL_TIME", "20:00")

DAY_NAME_TO_INT = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
REMINDER_DAY = os.environ.get("REMINDER_DAY", "friday").lower()
REMINDER_TIME = os.environ.get("REMINDER_TIME", "20:00")

BASE_DIR = Path(__file__).parent
PARTS_DIR = BASE_DIR / "parts"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
STATE_FILE = DATA_DIR / "state.json"
RESPONSES_FILE = DATA_DIR / "responses.json"
REMINDER_FILE = DATA_DIR / "livestream_reminder.txt"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- storage helpers ----------

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_subscribers():
    return load_json(SUBSCRIBERS_FILE, [])

def add_subscriber(chat_id):
    subs = get_subscribers()
    if chat_id not in subs:
        subs.append(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)

def remove_subscriber(chat_id):
    subs = get_subscribers()
    if chat_id in subs:
        subs.remove(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)

def get_state():
    return load_json(STATE_FILE, {"next_part_index": 1, "active_polls": {}})

def save_state(state):
    save_json(STATE_FILE, state)

def _part_number(path: Path):
    m = re.search(r"part_(\d+)", path.stem)
    return int(m.group(1)) if m else None

def get_parts():
    """Sorted list of files named part_1.txt, part_2.txt, ..."""
    files = [p for p in PARTS_DIR.glob("part_*.txt") if _part_number(p) is not None]
    files.sort(key=_part_number)
    return files

def find_part(idx):
    for p in get_parts():
        if _part_number(p) == idx:
            return p
    return None

# ---------- Arabic text ----------

WELCOME = (
    "مرحبًا بك في نادي القراءة! 📚\n\n"
    "سيصلك هنا كل يوم جزء جديد من الكتاب لتقرأه، "
    "ثم استطلاع رأي بسيط لمعرفة مدى تقدمك في القراءة.\n\n"
    "أرسل /help لعرض الأوامر المتاحة."
)
GOODBYE = "تم إلغاء اشتراكك. نتمنى رؤيتك قريبًا! 👋 يمكنك الاشتراك مجددًا في أي وقت عبر /start"
HELP_TEXT = (
    "الأوامر المتاحة:\n"
    "/start - الاشتراك في نادي القراءة\n"
    "/stop - إلغاء الاشتراك\n"
    "/help - عرض هذه الرسالة"
)
POLL_QUESTION = "هل قرأت الجزء الذي تم إرساله اليوم؟"
POLL_OPTIONS = ["✅ نعم، قرأته", "📖 لا أزال أقرأه", "❌ لم أبدأ بعد"]
NO_PARTS_LEFT_OWNER = "⚠️ لا توجد أجزاء متبقية لإرسالها. أضف ملفات جديدة في مجلد parts/."
DEFAULT_REMINDER_TEXT = "سيتم تحديد تفاصيل موعد ورابط البث المباشر قريبًا. تابعوا هنا للتحديثات."
REMINDER_HEADER = "🔴 تذكير: هناك بث مباشر لمناقشة الكتاب هذا الأسبوع!\n\n"

# ---------- subscriber-facing handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text(WELCOME)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text(GOODBYE)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

# ---------- owner-only handlers ----------

def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == OWNER_ID

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    state = get_state()
    await update.message.reply_text(
        f"الجزء القادم: {state['next_part_index']}\n"
        f"إجمالي الأجزاء المتوفرة: {len(get_parts())}\n"
        f"عدد المشتركين: {len(get_subscribers())}"
    )

async def do_send_part(context: ContextTypes.DEFAULT_TYPE):
    state = get_state()
    idx = state["next_part_index"]
    match = find_part(idx)
    if not match:
        try:
            await context.bot.send_message(OWNER_ID, NO_PARTS_LEFT_OWNER)
        except Exception:
            pass
        return
    text = match.read_text(encoding="utf-8")
    header = f"📖 جزء اليوم ({idx}):\n\n"
    subs = get_subscribers()
    sent = 0
    for chat_id in subs:
        try:
            await context.bot.send_message(chat_id, header + text)
            sent += 1
        except Exception as e:
            log.warning(f"Failed to send part to {chat_id}: {e}")
    state["next_part_index"] = idx + 1
    save_state(state)
    try:
        await context.bot.send_message(OWNER_ID, f"✅ تم إرسال الجزء {idx} إلى {sent} مشترك.")
    except Exception:
        pass

async def do_send_poll(context: ContextTypes.DEFAULT_TYPE):
    subs = get_subscribers()
    state = get_state()
    poll_ids = {}
    for chat_id in subs:
        try:
            msg = await context.bot.send_poll(
                chat_id=chat_id,
                question=POLL_QUESTION,
                options=POLL_OPTIONS,
                is_anonymous=False,
            )
            poll_ids[str(msg.poll.id)] = {"chat_id": chat_id}
        except Exception as e:
            log.warning(f"Failed to send poll to {chat_id}: {e}")
    state["active_polls"] = poll_ids
    save_state(state)

def get_reminder_text():
    if REMINDER_FILE.exists():
        return REMINDER_FILE.read_text(encoding="utf-8").strip()
    return DEFAULT_REMINDER_TEXT

async def do_send_reminder(context: ContextTypes.DEFAULT_TYPE):
    message = REMINDER_HEADER + get_reminder_text()
    subs = get_subscribers()
    sent = 0
    for chat_id in subs:
        try:
            await context.bot.send_message(chat_id, message)
            sent += 1
        except Exception as e:
            log.warning(f"Failed to send reminder to {chat_id}: {e}")
    try:
        await context.bot.send_message(OWNER_ID, f"✅ تم إرسال تذكير البث المباشر إلى {sent} مشترك.")
    except Exception:
        pass

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    await do_send_reminder(context)

async def reminder_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await do_send_reminder(context)

async def set_livestream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "استخدم الأمر هكذا:\n"
            "/set_livestream الموعد: السبت الساعة ٨ مساءً بتوقيت الرياض. الرابط: https://...\n\n"
            f"النص الحالي:\n{get_reminder_text()}"
        )
        return
    REMINDER_FILE.write_text(text, encoding="utf-8")
    await update.message.reply_text("تم تحديث نص تذكير البث المباشر ✅")

async def send_part_job(context: ContextTypes.DEFAULT_TYPE):
    await do_send_part(context)

async def send_poll_job(context: ContextTypes.DEFAULT_TYPE):
    await do_send_poll(context)

async def send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await do_send_part(context)

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await do_send_poll(context)

async def skip_part(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    state = get_state()
    state["next_part_index"] += 1
    save_state(state)
    await update.message.reply_text(f"تم التخطي. الجزء القادم الآن: {state['next_part_index']}")

async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    user = answer.user
    state = get_state()
    poll_info = state.get("active_polls", {}).get(str(answer.poll_id))
    if not poll_info:
        return
    responses = load_json(RESPONSES_FILE, {})
    chosen = [POLL_OPTIONS[i] for i in answer.option_ids] if answer.option_ids else ["لم يُجب"]
    responses[str(user.id)] = {
        "name": user.full_name,
        "username": user.username,
        "answer": chosen,
        "part_index": state["next_part_index"] - 1,
    }
    save_json(RESPONSES_FILE, responses)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    responses = load_json(RESPONSES_FILE, {})
    if not responses:
        await update.message.reply_text("لا توجد إجابات بعد.")
        return
    lines = [f"- {info.get('name') or info.get('username') or uid}: {', '.join(info['answer'])}"
             for uid, info in responses.items()]
    await update.message.reply_text("نتائج آخر استطلاع:\n" + "\n".join(lines))

# ---------- main ----------

def parse_hhmm(s):
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m), tzinfo=TIMEZONE)

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("send_now", send_now))
    app.add_handler(CommandHandler("poll_now", poll_now))
    app.add_handler(CommandHandler("skip_part", skip_part))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("reminder_now", reminder_now))
    app.add_handler(CommandHandler("set_livestream", set_livestream))
    app.add_handler(PollAnswerHandler(poll_answer))

    app.job_queue.run_daily(send_part_job, time=parse_hhmm(SEND_TIME))
    app.job_queue.run_daily(send_poll_job, time=parse_hhmm(POLL_TIME))

    reminder_day_int = DAY_NAME_TO_INT.get(REMINDER_DAY, 4)
    app.job_queue.run_daily(reminder_job, time=parse_hhmm(REMINDER_TIME), days=(reminder_day_int,))

    log.info(
        "Bot started. Parts at %s, polls at %s, livestream reminder %s at %s (%s)",
        SEND_TIME, POLL_TIME, REMINDER_DAY, REMINDER_TIME, TIMEZONE,
    )
    app.run_polling()

if __name__ == "__main__":
    main()
