import os
import json
import re
import logging
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, PollAnswerHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
OWNER_ID = int(os.environ["OWNER_ID"].strip())


def parse_chat_id(raw: str):
    """Accepts '@groupusername' as-is, or converts a numeric group id
    (e.g. '-1001234567890') to an int."""
    raw = raw.strip()
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        return raw


GROUP_ID = parse_chat_id(os.environ["GROUP_ID"])

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
STATE_FILE = DATA_DIR / "state.json"
RESPONSES_FILE = DATA_DIR / "responses.json"
REMINDER_FILE = DATA_DIR / "livestream_reminder.txt"

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".oga"}
MAX_FILE_MB = 49  # Telegram bots can't upload files larger than ~50MB

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- storage helpers ----------

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_state():
    return load_json(STATE_FILE, {"next_part_index": 1, "active_polls": {}, "skip_next_auto_send": False})

def save_state(state):
    save_json(STATE_FILE, state)

def _part_number(path: Path):
    m = re.search(r"part[-_](\d+)", path.stem)
    return int(m.group(1)) if m else None

def get_all_part_files():
    """Every file in parts/ named like part_1.txt, part_1.pdf, part_1.mp3, ..."""
    return [p for p in PARTS_DIR.iterdir() if p.is_file() and _part_number(p) is not None]

def get_day_numbers():
    return sorted({_part_number(p) for p in get_all_part_files()})

def get_day_files(idx):
    """All files for a given day, .txt first (used as the message body), then attachments."""
    files = [p for p in get_all_part_files() if _part_number(p) == idx]
    order = {".txt": 0, ".pdf": 1}
    files.sort(key=lambda p: order.get(p.suffix.lower(), 2))
    return files

# ---------- Arabic text ----------

START_TEXT = (
    "مرحبًا! 📚 هذا البوت ينشر أجزاء الكتاب والملفات الصوتية واستطلاعات القراءة "
    "مباشرة داخل مجموعة المقهى الثقافي. انضم إلى المجموعة لمتابعة كل شيء هناك."
)
POLL_QUESTION = "هل قرأت الجزء الذي تم نشره اليوم؟"
POLL_OPTIONS = ["✅ نعم، قرأته", "📖 لا أزال أقرأه", "❌ لم أبدأ بعد"]
NO_PARTS_LEFT_OWNER = "⚠️ لا توجد أجزاء متبقية لنشرها. أضف ملفات جديدة في مجلد parts/."
FILE_TOO_LARGE_OWNER = "⚠️ الملف {name} حجمه أكبر من {limit}MB، لم يتم إرساله. قلل حجم الملف وحاول مرة أخرى."
DEFAULT_REMINDER_TEXT = "سيتم تحديد تفاصيل موعد ورابط البث المباشر قريبًا. تابعوا هنا للتحديثات."
REMINDER_HEADER = "🔴 تذكير: هناك بث مباشر لمناقشة الكتاب هذا الأسبوع!\n\n"

# ---------- helpers ----------

def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == OWNER_ID

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(OWNER_ID, text)
    except Exception:
        pass

async def send_file_to_group(context: ContextTypes.DEFAULT_TYPE, path: Path):
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        await notify_owner(context, FILE_TOO_LARGE_OWNER.format(name=path.name, limit=MAX_FILE_MB))
        return False
    ext = path.suffix.lower()
    data = path.read_bytes()
    try:
        if ext == ".pdf":
            await context.bot.send_document(GROUP_ID, document=data, filename=path.name)
        elif ext in AUDIO_EXTS:
            await context.bot.send_audio(
                GROUP_ID, audio=data, filename=path.name, title=f"جزء {_part_number(path)}"
            )
        else:
            return False
        return True
    except Exception as e:
        log.warning(f"Failed to send {path.name} to group: {e}")
        await notify_owner(context, f"⚠️ فشل إرسال {path.name} إلى المجموعة.")
        return False

# ---------- fallback if someone DMs the bot directly ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

# ---------- owner-only handlers ----------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    state = get_state()
    await update.message.reply_text(
        f"الجزء القادم: {state['next_part_index']}\n"
        f"إجمالي الأيام المتوفرة: {len(get_day_numbers())}\n"
        f"المجموعة: {GROUP_ID}"
    )

async def do_send_part(context: ContextTypes.DEFAULT_TYPE):
    state = get_state()
    idx = state["next_part_index"]
    files = get_day_files(idx)
    if not files:
        await notify_owner(context, NO_PARTS_LEFT_OWNER)
        return

    text_file = next((f for f in files if f.suffix.lower() == ".txt"), None)
    attachments = [f for f in files if f.suffix.lower() != ".txt"]
    header = f"📖 جزء اليوم ({idx})"
    message = f"{header}:\n\n{text_file.read_text(encoding='utf-8')}" if text_file else header

    try:
        await context.bot.send_message(GROUP_ID, message)
    except Exception as e:
        log.warning(f"Failed to post part to group: {e}")
        await notify_owner(context, f"⚠️ فشل نشر الجزء {idx} في المجموعة. تأكد أن البوت عضو فيها ولديه صلاحية الإرسال.")
        return

    for f in attachments:
        await send_file_to_group(context, f)

    state["next_part_index"] = idx + 1
    save_state(state)
    formats = ", ".join(sorted({f.suffix.lower().lstrip(".") for f in files}))
    await notify_owner(context, f"✅ تم نشر الجزء {idx} ({formats}) في المجموعة.")

async def do_send_poll(context: ContextTypes.DEFAULT_TYPE):
    state = get_state()
    try:
        msg = await context.bot.send_poll(
            chat_id=GROUP_ID,
            question=POLL_QUESTION,
            options=POLL_OPTIONS,
            is_anonymous=False,  # groups allow this, so we can track who answered
        )
        state["active_polls"] = {str(msg.poll.id): {"part_index": state["next_part_index"] - 1}}
        save_state(state)
        await notify_owner(context, "✅ تم نشر استطلاع القراءة في المجموعة.")
    except Exception as e:
        log.warning(f"Failed to post poll to group: {e}")
        await notify_owner(context, "⚠️ فشل نشر الاستطلاع في المجموعة.")

def get_reminder_text():
    if REMINDER_FILE.exists():
        return REMINDER_FILE.read_text(encoding="utf-8").strip()
    return DEFAULT_REMINDER_TEXT

async def do_send_reminder(context: ContextTypes.DEFAULT_TYPE):
    message = REMINDER_HEADER + get_reminder_text()
    try:
        await context.bot.send_message(GROUP_ID, message)
        await notify_owner(context, "✅ تم نشر تذكير البث المباشر في المجموعة.")
    except Exception as e:
        log.warning(f"Failed to post reminder to group: {e}")
        await notify_owner(context, "⚠️ فشل نشر تذكير البث المباشر في المجموعة.")

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
    state = get_state()
    if state.get("skip_next_auto_send"):
        state["skip_next_auto_send"] = False
        save_state(state)
        await notify_owner(context, "⏭️ تم تخطي الإرسال التلقائي المجدول لهذا اليوم بناءً على طلبك.")
        return
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

async def skip_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    state = get_state()
    state["skip_next_auto_send"] = True
    save_state(state)
    await update.message.reply_text(
        "تم إلغاء الإرسال التلقائي القادم اليوم. سيتم إرسال الجزء التالي في موعده المعتاد غدًا."
    )

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
        "part_index": poll_info["part_index"],
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
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("send_now", send_now))
    app.add_handler(CommandHandler("poll_now", poll_now))
    app.add_handler(CommandHandler("skip_part", skip_part))
    app.add_handler(CommandHandler("skip_today", skip_today))
    app.add_handler(CommandHandler("reminder_now", reminder_now))
    app.add_handler(CommandHandler("set_livestream", set_livestream))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(PollAnswerHandler(poll_answer))

    app.job_queue.run_daily(send_part_job, time=parse_hhmm(SEND_TIME))
    app.job_queue.run_daily(send_poll_job, time=parse_hhmm(POLL_TIME))

    reminder_day_int = DAY_NAME_TO_INT.get(REMINDER_DAY, 4)
    app.job_queue.run_daily(reminder_job, time=parse_hhmm(REMINDER_TIME), days=(reminder_day_int,))

    log.info(
        "Bot started. Posting to group %s. Parts at %s, polls at %s, reminder %s at %s (%s)",
        GROUP_ID, SEND_TIME, POLL_TIME, REMINDER_DAY, REMINDER_TIME, TIMEZONE,
    )
    app.run_polling()

if __name__ == "__main__":
    main()
