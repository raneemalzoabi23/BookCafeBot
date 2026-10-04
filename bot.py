import os
import json
import re
import asyncio
import logging
import unicodedata
from datetime import time, date
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, filters, PollAnswerHandler, ContextTypes

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
CHANNEL_ID = parse_chat_id(os.environ["CHANNEL_ID"].strip()) if os.environ.get("CHANNEL_ID", "").strip() else None
ALL_TARGETS = [t for t in [GROUP_ID, CHANNEL_ID] if t]

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Riyadh"))
SEND_TIME = os.environ.get("SEND_TIME", "09:00")
POLL_TIME = os.environ.get("POLL_TIME", "20:00")
QUESTIONS_TIME = os.environ.get("QUESTIONS_TIME", "").strip()  # empty/unset = send right after the part, as before
# The date day 1 was (or will be) sent. Day number is computed from today's
# date relative to this, so progress survives redeploys — nothing is "counted"
# or stored that could get reset.
START_DATE = date.fromisoformat(os.environ.get("START_DATE", str(date.today())).strip())

DAY_NAME_TO_INT = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
REMINDER_DAY = os.environ.get("REMINDER_DAY", "").strip().lower()  # empty/unset = reminder disabled
REMINDER_TIME = os.environ.get("REMINDER_TIME", "20:00")
CONTACT_NUMBER = os.environ.get("CONTACT_NUMBER", "").strip()

BASE_DIR = Path(__file__).parent
PARTS_DIR = BASE_DIR / "parts"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
RESPONSES_FILE = DATA_DIR / "responses.json"
REMINDER_FILE = DATA_DIR / "livestream_reminder.txt"
FAQ_FILE = BASE_DIR / "faq.json"  # lives in the repo, edited via GitHub like parts/
QUESTIONS_FILE = BASE_DIR / "questions.json"  # discussion/quiz questions per part

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
    return load_json(STATE_FILE, {"day_offset": 0, "active_polls": {}, "skip_next_auto_send": False})

def save_state(state):
    save_json(STATE_FILE, state)

def get_today_day_index():
    """Day number is always derived from today's date + START_DATE, plus any
    manual offset from /skip_part. Never a stored counter, so it can't reset."""
    today = date.today()
    state = get_state()
    return (today - START_DATE).days + 1 + state.get("day_offset", 0)

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
    "مباشرة داخل مجموعة قناة جمعية النعيم للتعليم. انضم إلى المجموعة لمتابعة كل شيء هناك."
)
POLL_QUESTION = "هل قرأت الجزء الذي تم نشره اليوم؟"
POLL_OPTIONS = ["✅ نعم، قرأته", "📖 لا أزال أقرأه", "❌ لم أبدأ بعد"]
NO_PARTS_LEFT_OWNER = "⚠️ لا توجد أجزاء متبقية لنشرها. أضف ملفات جديدة في مجلد parts/."
FILE_TOO_LARGE_OWNER = "⚠️ الملف {name} حجمه أكبر من {limit}MB، لم يتم إرساله. قلل حجم الملف وحاول مرة أخرى."
DEFAULT_REMINDER_TEXT = "سيتم تحديد تفاصيل موعد ورابط البث المباشر قريبًا. تابعوا هنا للتحديثات."
REMINDER_HEADER = "🔴 تذكير: هناك بث مباشر لمناقشة الكتاب هذا الأسبوع!\n\n"
FAQ_FALLBACK = (
    "عذرًا، لم أجد إجابة لسؤالك 🤔\n"
    + (f"للمزيد من المعلومات تواصل عبر: {CONTACT_NUMBER}" if CONTACT_NUMBER
       else "يرجى التواصل مع مسؤول المجموعة للمزيد من المعلومات.")
)

# ---------- helpers ----------

def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == OWNER_ID

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(OWNER_ID, text)
    except Exception:
        pass

def load_questions():
    return load_json(QUESTIONS_FILE, [])

def get_questions_for_day(idx):
    for entry in load_questions():
        if entry.get("part") == idx:
            return entry.get("questions", [])
    return []

ARABIC_LETTER_LABELS = ["أ", "ب", "ت", "ث", "ج", "ح", "خ", "د"]

async def _send_one_quiz(context, chat_id, is_anonymous, raw_question, raw_options, correct):
    too_long = len(raw_question) > 300 or any(len(o) > 100 for o in raw_options)
    if too_long:
        labels = ARABIC_LETTER_LABELS[: len(raw_options)]
        lines = [f"❓ {raw_question}", ""]
        lines += [f"{labels[i]}) {opt}" for i, opt in enumerate(raw_options)]
        await context.bot.send_message(chat_id, "\n".join(lines))
        await context.bot.send_poll(
            chat_id=chat_id,
            question="اختر الإجابة الصحيحة (بحسب الخيارات أعلاه):",
            options=labels,
            type=Poll.QUIZ,
            correct_option_id=correct,
            is_anonymous=is_anonymous,
        )
    else:
        await context.bot.send_poll(
            chat_id=chat_id,
            question=raw_question,
            options=raw_options,
            type=Poll.QUIZ,
            correct_option_id=correct,
            is_anonymous=is_anonymous,
        )

async def send_day_questions(context: ContextTypes.DEFAULT_TYPE, idx: int):
    questions = get_questions_for_day(idx)
    if not questions:
        return
    sent = 0
    for q in questions:
        raw_question = str(q.get("question", ""))
        raw_options = [str(o) for o in q.get("options", [])]
        correct = q.get("correct_index")
        if len(raw_options) < 2 or correct is None or correct >= len(raw_options):
            continue
        try:
            # Group: non-anonymous (tracked). Channel: must be anonymous (Telegram rule).
            await _send_one_quiz(context, GROUP_ID, False, raw_question, raw_options, correct)
            if CHANNEL_ID:
                await _send_one_quiz(context, CHANNEL_ID, True, raw_question, raw_options, correct)
            sent += 1
            await asyncio.sleep(1)  # avoid flooding / rate limits
        except Exception as e:
            log.warning(f"Failed to send quiz question: {e}")
    if sent:
        await notify_owner(context, f"📝 تم إرسال {sent} سؤال/أسئلة مراجعة للجزء {idx}.")

async def send_file_to_group(context: ContextTypes.DEFAULT_TYPE, path: Path):
    """Sends one attachment to every configured target (group and/or channel).
    Uploads once and reuses the resulting file_id for the rest, instead of
    re-uploading the whole file per target."""
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        await notify_owner(context, FILE_TOO_LARGE_OWNER.format(name=path.name, limit=MAX_FILE_MB))
        return False
    ext = path.suffix.lower()
    data = path.read_bytes()
    file_id = None
    ok = False
    for chat_id in ALL_TARGETS:
        try:
            source = file_id if file_id else data
            if ext == ".pdf":
                msg = await context.bot.send_document(
                    chat_id, document=source, filename=path.name if not file_id else None
                )
                file_id = file_id or msg.document.file_id
            elif ext in AUDIO_EXTS:
                msg = await context.bot.send_audio(
                    chat_id, audio=source, filename=path.name if not file_id else None,
                    title=f"جزء {_part_number(path)}",
                )
                file_id = file_id or msg.audio.file_id
            else:
                continue
            ok = True
        except Exception as e:
            log.warning(f"Failed to send {path.name} to {chat_id}: {e}")
            await notify_owner(context, f"⚠️ فشل إرسال {path.name} إلى {chat_id}.")
    return ok

# ---------- fallback if someone DMs the bot directly ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

# ---------- FAQ ----------

def load_faq():
    return load_json(FAQ_FILE, [])

ARABIC_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")  # harakat, dagger alef, tatweel

def normalize_ar(text: str) -> str:
    """Makes Arabic matching forgiving of invisible differences — diacritics,
    elongation marks, and different Unicode representations of the same
    visible letter (all common side-effects of mobile keyboards)."""
    text = unicodedata.normalize("NFKC", text)
    text = ARABIC_DIACRITICS.sub("", text)
    return text

def find_faq_answer(query: str):
    query_norm = normalize_ar(query.strip())
    if not query_norm:
        return None
    for entry in load_faq():
        for kw in entry.get("keywords", []):
            kw_norm = normalize_ar(kw.strip())
            if kw_norm and kw_norm in query_norm:
                answer = entry.get("answer", "")
                if "__CONTACT_PLACEHOLDER__" in answer:
                    contact = CONTACT_NUMBER or "تواصل مع مسؤول المجموعة"
                    answer = answer.replace("__CONTACT_PLACEHOLDER__", contact)
                return answer
    return None

async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.partition(" ")[2].strip()
    if not query:
        faqs = load_faq()
        if not faqs:
            await update.message.reply_text("لا توجد أسئلة شائعة مضافة بعد.")
            return
        lines = ["📋 الأسئلة الشائعة المتاحة:"]
        for entry in faqs:
            if entry.get("keywords"):
                lines.append(f"- {entry['keywords'][0]}")
        lines.append("\nللسؤال، اكتب: /faq متبوعًا بسؤالك\nمثال: /faq متى يتم إرسال الجزء اليومي؟")
        await update.message.reply_text("\n".join(lines))
        return
    await update.message.reply_text(find_faq_answer(query) or FAQ_FALLBACK)

async def faq_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Watches every group message: replies with the FAQ answer if the text
    mentions the bot (always replies, with fallback) or simply contains a
    matching keyword (replies only on an actual match, otherwise stays silent)."""
    text = update.message.text or ""
    if not text:
        return
    log.info(f"faq_watch received: {text!r}")
    log.info(f"faq_watch normalized: {normalize_ar(text)!r}")
    bot_username = context.bot.username
    mentioned = bool(bot_username) and f"@{bot_username}" in text
    if mentioned:
        query = text.replace(f"@{bot_username}", "").strip()
        answer = find_faq_answer(query) or FAQ_FALLBACK
        log.info(f"faq_watch mention match: {answer!r}")
        await update.message.reply_text(answer)
        return
    answer = find_faq_answer(text)
    log.info(f"faq_watch keyword match: {answer!r}")
    all_keywords = [kw for e in load_faq() for kw in e.get("keywords", [])]
    log.info(f"faq_watch loaded keywords: {all_keywords!r}")
    if answer:
        await update.message.reply_text(answer)

# ---------- owner-only handlers ----------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    idx = get_today_day_index()
    targets = f"المجموعة: {GROUP_ID}" + (f"\nالقناة: {CHANNEL_ID}" if CHANNEL_ID else "")
    await update.message.reply_text(
        f"يوم اليوم: {idx}\n"
        f"إجمالي الأيام المتوفرة: {len(get_day_numbers())}\n"
        f"{targets}"
    )

async def do_send_part(context: ContextTypes.DEFAULT_TYPE):
    idx = get_today_day_index()
    if idx < 1:
        return  # start date is in the future, nothing to send yet
    files = get_day_files(idx)
    if not files:
        await notify_owner(context, NO_PARTS_LEFT_OWNER)
        return

    text_file = next((f for f in files if f.suffix.lower() == ".txt"), None)
    attachments = [f for f in files if f.suffix.lower() != ".txt"]
    header = f"📖 جزء اليوم ({idx})"
    message = f"{header}:\n\n{text_file.read_text(encoding='utf-8')}" if text_file else header

    sent_ok = False
    for chat_id in ALL_TARGETS:
        try:
            await context.bot.send_message(chat_id, message)
            sent_ok = True
        except Exception as e:
            log.warning(f"Failed to post part to {chat_id}: {e}")
            await notify_owner(context, f"⚠️ فشل نشر الجزء {idx} في {chat_id}. تأكد أن البوت عضو/مشرف فيها ولديه صلاحية الإرسال.")
    if not sent_ok:
        return

    for f in attachments:
        await send_file_to_group(context, f)

    if not QUESTIONS_TIME:
        # No separate time configured — keep the old behavior of sending
        # questions right after the part itself.
        await send_day_questions(context, idx)

    formats = ", ".join(sorted({f.suffix.lower().lstrip(".") for f in files}))
    await notify_owner(context, f"✅ تم نشر الجزء {idx} ({formats}) في المجموعة.")

async def do_send_poll(context: ContextTypes.DEFAULT_TYPE):
    state = get_state()
    try:
        # Group: non-anonymous so we can track who answered (used by /report)
        msg = await context.bot.send_poll(
            chat_id=GROUP_ID,
            question=POLL_QUESTION,
            options=POLL_OPTIONS,
            is_anonymous=False,
        )
        state["active_polls"] = {str(msg.poll.id): {"part_index": get_today_day_index()}}
        save_state(state)
    except Exception as e:
        log.warning(f"Failed to post poll to group: {e}")
        await notify_owner(context, "⚠️ فشل نشر الاستطلاع في المجموعة.")
        return
    if CHANNEL_ID:
        try:
            # Channels only allow anonymous polls — a Telegram platform rule
            await context.bot.send_poll(
                chat_id=CHANNEL_ID,
                question=POLL_QUESTION,
                options=POLL_OPTIONS,
                is_anonymous=True,
            )
        except Exception as e:
            log.warning(f"Failed to post poll to channel: {e}")
            await notify_owner(context, "⚠️ فشل نشر الاستطلاع في القناة.")
    await notify_owner(context, "✅ تم نشر استطلاع القراءة.")

def get_reminder_text():
    if REMINDER_FILE.exists():
        return REMINDER_FILE.read_text(encoding="utf-8").strip()
    return DEFAULT_REMINDER_TEXT

async def do_send_reminder(context: ContextTypes.DEFAULT_TYPE):
    message = REMINDER_HEADER + get_reminder_text()
    sent_ok = False
    for chat_id in ALL_TARGETS:
        try:
            await context.bot.send_message(chat_id, message)
            sent_ok = True
        except Exception as e:
            log.warning(f"Failed to post reminder to {chat_id}: {e}")
            await notify_owner(context, f"⚠️ فشل نشر تذكير البث المباشر في {chat_id}.")
    if sent_ok:
        await notify_owner(context, "✅ تم نشر تذكير البث المباشر.")

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

async def questions_job(context: ContextTypes.DEFAULT_TYPE):
    idx = get_today_day_index()
    if idx >= 1:
        await send_day_questions(context, idx)

async def send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await do_send_part(context)

async def questions_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    idx = get_today_day_index()
    await send_day_questions(context, idx)

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await do_send_poll(context)

async def skip_part(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    state = get_state()
    state["day_offset"] = state.get("day_offset", 0) + 1
    save_state(state)
    await update.message.reply_text(f"تم التخطي. يوم اليوم أصبح: {get_today_day_index()}")

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

# ---------- ask an admin (private reply via the bot) ----------

ASK_USAGE = "استخدم: /ask متبوعًا بسؤالك، مثال:\n/ask هل هناك جلسة نقاش هذا الأسبوع؟"
ASK_RECEIVED_GROUP = "✅ تم إرسال سؤالك، سيصلك الرد في الخاص من البوت قريبًا."
ASK_NEED_START_GROUP = (
    "⚠️ لإرسال الرد لك في الخاص، افتح محادثة خاصة مع البوت أولًا (@{bot_username})، "
    "أرسل /start هناك، ثم أعد إرسال سؤالك هنا بـ /ask."
)

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text.partition(" ")[2].strip()
    user = update.effective_user
    if not question:
        await update.message.reply_text(ASK_USAGE)
        return
    try:
        await context.bot.send_message(
            user.id, f"📩 سؤالك:\n{question}\n\nسيصلك الرد هنا قريبًا بإذن الله."
        )
    except Exception:
        bot_username = context.bot.username
        await update.message.reply_text(ASK_NEED_START_GROUP.format(bot_username=bot_username))
        return
    try:
        await context.bot.send_message(
            OWNER_ID,
            f"❓ سؤال جديد من {user.full_name} (@{user.username or 'بدون يوزر'}):\n{question}\n\n"
            f"للرد: /reply {user.id} نص الرد",
        )
    except Exception:
        pass
    await update.message.reply_text(ASK_RECEIVED_GROUP)

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("استخدم: /reply <user_id> <نص الرد>")
        return
    _, uid_str, reply_text = parts
    try:
        uid = int(uid_str)
    except ValueError:
        await update.message.reply_text("معرف المستخدم غير صحيح.")
        return
    try:
        await context.bot.send_message(uid, f"💬 رد من المشرف:\n{reply_text}")
        await update.message.reply_text("✅ تم إرسال الرد.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ تعذر إرسال الرد: {e}")

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
    app.add_handler(CommandHandler("questions_now", questions_now))
    app.add_handler(CommandHandler("poll_now", poll_now))
    app.add_handler(CommandHandler("skip_part", skip_part))
    app.add_handler(CommandHandler("skip_today", skip_today))
    app.add_handler(CommandHandler("reminder_now", reminder_now))
    app.add_handler(CommandHandler("set_livestream", set_livestream))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(CommandHandler("faq", faq_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, faq_watch))
    app.add_handler(PollAnswerHandler(poll_answer))

    app.job_queue.run_daily(send_part_job, time=parse_hhmm(SEND_TIME))
    app.job_queue.run_daily(send_poll_job, time=parse_hhmm(POLL_TIME))
    if QUESTIONS_TIME:
        app.job_queue.run_daily(questions_job, time=parse_hhmm(QUESTIONS_TIME))

    if REMINDER_DAY in DAY_NAME_TO_INT:
        app.job_queue.run_daily(
            reminder_job, time=parse_hhmm(REMINDER_TIME), days=(DAY_NAME_TO_INT[REMINDER_DAY],)
        )
        reminder_status = f"reminder {REMINDER_DAY} at {REMINDER_TIME}"
    else:
        reminder_status = "reminder disabled"

    questions_status = f"questions at {QUESTIONS_TIME}" if QUESTIONS_TIME else "questions right after parts"
    log.info(
        "Bot started. Posting to group %s. Parts at %s, polls at %s, %s, %s (%s)",
        GROUP_ID, SEND_TIME, POLL_TIME, questions_status, reminder_status, TIMEZONE,
    )
    app.run_polling()

if __name__ == "__main__":
    main()
