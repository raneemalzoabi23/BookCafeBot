"""Deletes spam / ads in the whole group (all topics + General).

Rules live in spam_words.json, so you can edit them on GitHub without
touching code. Admins and the owner are never touched.

Env var (optional): SPAM_DRY_RUN=1  -> only DMs the owner, deletes nothing.
"""
import json
import logging
import os
import re
import unicodedata
from pathlib import Path

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger(__name__)

WORDS_FILE = Path(__file__).parent / "spam_words.json"
DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
URL_RE = re.compile(r"(https?://|www\.|t\.me/|wa\.me/|bit\.ly/)", re.IGNORECASE)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return DIACRITICS.sub("", text).lower()


def _load_rules() -> dict:
    try:
        return json.loads(WORDS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not read spam_words.json: {e}")
        return {"keywords": [], "block_links": True}


def _spam_reason(msg) -> str | None:
    rules = _load_rules()
    text = _norm(msg.text or msg.caption or "")

    for kw in rules.get("keywords", []):
        k = _norm(kw.strip())
        if k and k in text:
            return f"كلمة: {kw}"

    if rules.get("block_links", True):
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        if any(e.type in ("url", "text_link") for e in entities) or URL_RE.search(text):
            return "رابط"

    return None


def setup_spam_filter(app: Application, group_id, owner_id: int) -> None:
    dry_run = os.environ.get("SPAM_DRY_RUN", "").strip() == "1"

    async def spam_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        user = update.effective_user
        if not msg or not user or user.is_bot or msg.sender_chat:
            return
        if isinstance(group_id, int) and msg.chat_id != group_id:
            return
        if user.id == owner_id:
            return

        reason = _spam_reason(msg)
        if not reason:
            return

        # Never touch group admins
        try:
            member = await context.bot.get_chat_member(msg.chat_id, user.id)
            if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                return
        except Exception as e:
            log.warning(f"Could not check admin status: {e}")
            return

        preview = (msg.text or msg.caption or "")[:200]
        who = f"{user.full_name} (@{user.username or 'بدون يوزر'})"

        if dry_run:
            await _notify(context, owner_id, f"🧪 (تجريبي) كانت ستُحذف رسالة من {who}\nالسبب: {reason}\n\n{preview}")
            return

        try:
            await msg.delete()
            await _notify(context, owner_id, f"🗑️ حُذفت رسالة من {who}\nالسبب: {reason}\n\n{preview}")
        except Exception as e:
            log.warning(f"Could not delete message: {e}")
            await _notify(
                context, owner_id,
                "⚠️ تعذر حذف رسالة مخالفة. تأكد أن البوت مشرف ولديه صلاحية حذف الرسائل.",
            )
        # Stop other handlers (e.g. the FAQ) from replying to the spam
        raise ApplicationHandlerStop

    # group=-1 -> runs before all your other handlers
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, spam_handler),
        group=-1,
    )


async def _notify(context, owner_id, text):
    try:
        await context.bot.send_message(owner_id, text)
    except Exception:
        pass
