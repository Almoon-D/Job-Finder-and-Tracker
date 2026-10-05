"""Inline keyboard of per-job Telegram messages and its compact callback data."""

from __future__ import annotations

import hashlib

from ..i18n import t

PREFIX = "jf1"
BUTTONS = ("interested", "applied", "interview", "discarded")  # 2×2 grid, in this order
DISCORD_EMOJI = {"interested": "⭐", "applied": "✅", "interview": "🗣️", "discarded": "❌"}  # Discord reactions
TEST_ID = "test"  # buttons of the test-notify message: the sync edits them but saves nothing


def job_id(key: str) -> str:
    """Short, stable id of a state key ("source:native id"): 12 hex characters."""
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def callback_data(status: str, jid: str) -> str:
    """'jf1:applied:3f9c1e0b7a2d' (at most 27 bytes; Telegram allows 64)."""
    return f"{PREFIX}:{status}:{jid}"


def parse_callback(data: str | None) -> tuple[str, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != PREFIX or parts[1] not in BUTTONS or not parts[2]:
        return None
    return parts[1], parts[2]


def emoji_status(emoji: str | None) -> str | None:
    """Status of a Discord reaction ('✅' → 'applied'); other emojis (or a missing variation selector) work too."""
    plain = (emoji or "").replace("\ufe0f", "")
    return next((s for s, e in DISCORD_EMOJI.items() if e.replace("\ufe0f", "") == plain), None)


def button_label(status: str, lang: str) -> str:
    return t(lang, "btn_discarded") if status == "discarded" else t(lang, f"st_{status}")


def keyboard(jid: str, current: str | None, lang: str) -> dict:
    """reply_markup with the four buttons; the current status is shown as '» ✅ Applied «'."""

    def button(status: str) -> dict:
        label = button_label(status, lang)
        if status == current:
            label = f"» {label} «"
        return {"text": label, "callback_data": callback_data(status, jid)}

    return {"inline_keyboard": [[button(BUTTONS[0]), button(BUTTONS[1])], [button(BUTTONS[2]), button(BUTTONS[3])]]}
