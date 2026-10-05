"""Read button presses and commands from Telegram (getUpdates) and apply them to the tracker.

GitHub Actions cannot receive webhooks, so the bot is polled: at the start of every normal run
and from ``jobfinder tracker-sync`` (cron-job.org, every 1–3 h). Telegram keeps updates for 24
hours only. Button presses are answered late (the spinner stops and the button shows as selected
at the next sync). Logs show counts only: update contents never reach the public log.
"""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from datetime import datetime

from .. import log
from ..config.schema import Config
from ..i18n import t
from ..notify.channels import chunk, env
from ..sources.http import Http, HttpError
from ..state import State
from .buttons import TEST_ID, job_id, keyboard, parse_callback
from .store import STATUSES, Tracker, TrackerOp, parse_status, status_label

MAX_PAGES = 5  # 100 updates per page
COMMANDS = {"estado": "status", "status": "status", "pendientes": "pending", "pending": "pending",
            "ayuda": "help", "help": "help", "start": "help"}


@dataclass
class SyncResult:
    updates: int = 0
    buttons: int = 0
    commands: int = 0
    ignored: int = 0
    rows_changed: int = 0
    failed: bool = False


class TelegramSync:
    def __init__(self, config: Config, http: Http, tracker: Tracker, state: State, now: datetime):
        self.config = config
        self.http = http
        self.tracker = tracker
        self.state = state
        self.now = now
        self.lang = config.language
        cfg = config.notify.telegram
        self.token, self.chat = env(cfg.bot_token_env), env(cfg.chat_id_env)
        self.today = tracker.today(now).isoformat()
        self._keys: dict[str, str] | None = None  # job id -> state key (reverse index of seen jobs)
        self.result = SyncResult()

    # ------------------------------------------------------------ helpers
    async def _call(self, method: str, payload: dict) -> dict:
        data = await self.http.post_json(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
        return data if isinstance(data, dict) else {}

    async def _quiet(self, method: str, payload: dict) -> bool:
        """Best-effort call: 400s ('query is too old', 'message is not modified'...) are expected."""
        try:
            await self._call(method, payload)
            return True
        except HttpError as exc:
            log.detail(f"tracker: {method} HTTP {exc.status}")
            return False

    def _our_chat(self, chat: dict | None) -> bool:
        chat = chat or {}
        mine = self.chat.strip()
        return str(chat.get("id")) == mine or (bool(chat.get("username")) and f"@{chat['username']}" == mine)

    def _key_for(self, jid: str) -> str | None:
        key = self.tracker.messages.get(jid, {}).get("key")
        if key:
            return key
        if self._keys is None:
            self._keys = {job_id(k): k for k in self.state.seen}
        return self._keys.get(jid)

    def _info(self, jid: str, key: str | None, message: dict) -> dict[str, str]:
        entry = self.state.seen.get(key or "", {})
        if entry:
            return {"company": entry.get("company") or "", "title": entry.get("title") or "",
                    "location": entry.get("loc") or "", "url": entry.get("url") or ""}
        # Not in the state any more: read it from the message itself ("Company — Title", first link).
        first = (message.get("text") or "").split("\n", 1)[0]
        company, _, title = first.partition(" — ")
        url = next((e.get("url") for e in message.get("entities", []) if e.get("type") == "text_link"), "")
        return {"company": company.strip(), "title": title.strip(), "location": "", "url": url or ""}

    async def _refresh(self, jid: str, extra: tuple[str, int] | None = None) -> None:
        """Show the current status on every message that carries this job's buttons."""
        markup = keyboard(jid, self.tracker.status_of(jid), self.lang)
        targets = list(dict.fromkeys([*self.tracker.refs(jid), *([extra] if extra else [])]))
        for chat, message_id in targets:
            await self._quiet("editMessageReplyMarkup",
                              {"chat_id": chat, "message_id": message_id, "reply_markup": markup})

    # ----------------------------------------------------------- updates
    async def _on_button(self, query: dict) -> None:
        message = query.get("message") or {}
        parsed = parse_callback(query.get("data"))
        if not self._our_chat(message.get("chat")) or parsed is None:
            self.result.ignored += 1
            return
        self.result.buttons += 1
        status, jid = parsed
        where = (str(message["chat"]["id"]), int(message["message_id"])) if message.get("message_id") else None
        label = status_label(status, self.lang)
        if jid == TEST_ID:
            await self._quiet("answerCallbackQuery", {"callback_query_id": query.get("id"),
                                                      "text": t(self.lang, "tracker_test", status=label)})
            if where:
                await self._quiet("editMessageReplyMarkup", {"chat_id": where[0], "message_id": where[1],
                                                             "reply_markup": keyboard(TEST_ID, status, self.lang)})
            return
        key = self._key_for(jid)
        if self.tracker.apply(TrackerOp(jid, status, self.today, self._info(jid, key, message))):
            self.result.rows_changed += 1
        if where:
            self.tracker.remember(jid, key, where[0], where[1], self.today)
        await self._quiet("answerCallbackQuery", {"callback_query_id": query.get("id"),
                                                  "text": t(self.lang, "tracker_saved", status=label)})
        await self._refresh(jid, where)

    async def _on_message(self, message: dict) -> None:
        text = (message.get("text") or "").strip()
        if not self._our_chat(message.get("chat")) or not text.startswith("/"):
            self.result.ignored += 1
            return
        command = COMMANDS.get(text.split()[0][1:].split("@")[0].lower(), "help")
        self.result.commands += 1
        body = {"status": self.status_text, "pending": self.pending_text, "help": self.help_text}[command]()
        for i, part in enumerate(chunk(body.split("\n\n"), 4000)):
            if i:
                await asyncio.sleep(1.1)
            await self._quiet("sendMessage", {"chat_id": message["chat"]["id"], "text": part, "parse_mode": "HTML",
                                              "disable_web_page_preview": True})

    # ------------------------------------------------------------ replies
    def _line(self, row: dict[str, str], suffix: str = "") -> str:
        e = html.escape
        name = " — ".join(x for x in (row.get("company"), row.get("title")) if x) or row.get("id", "?")
        link = f'<a href="{e(row["url"], quote=True)}">{e(name)}</a>' if row.get("url") else e(name)
        return f"• {link}{e(suffix)}"

    def status_text(self) -> str:
        counts = self.tracker.counts()
        head = f"<b>{html.escape(t(self.lang, 'tracker_title'))}</b>"
        if not counts:
            return f"{head}\n{html.escape(t(self.lang, 'tracker_empty'))}"
        funnel = " · ".join(f"{status_label(s, self.lang)} {counts[s]}" for s in (*STATUSES, "other") if s in counts)
        recent = self.tracker.changes()[-10:][::-1]
        lines = [self._line(row, f" ({day[8:10]}/{day[5:7]}: {status_label(parse_status(st) or st, self.lang)})")
                 for day, st, row in recent]
        return f"{head}\n{html.escape(funnel)}\n\n<b>{html.escape(t(self.lang, 'tracker_recent'))}</b>\n" + \
            "\n".join(lines)

    def pending_text(self) -> str:
        days = self.config.tracker.follow_up_days
        to_apply, follow = self.tracker.pending(self.tracker.today(self.now), days)
        parts = [f"<b>{html.escape(t(self.lang, 'pending_title'))}</b>"]
        if not to_apply and not follow:
            parts.append(html.escape(t(self.lang, "pending_none")))
        since = t(self.lang, "since")
        if to_apply:
            parts.append(f"<b>{html.escape(t(self.lang, 'pending_apply', n=len(to_apply)))}</b>\n" + "\n".join(
                self._line(r, f" ({since} {self.tracker.last_change(r) or '?'})") for r in to_apply))
        if follow:
            parts.append(f"<b>{html.escape(t(self.lang, 'pending_follow', n=len(follow), days=days))}</b>\n" +
                         "\n".join(self._line(r, f" ({since} {self.tracker.last_change(r) or '?'})") for r in follow))
        return "\n\n".join(parts)

    def help_text(self) -> str:
        return f"<b>{html.escape(t(self.lang, 'tracker_title'))}</b>\n{html.escape(t(self.lang, 'tracker_help'))}"

    # ---------------------------------------------------------------- run
    async def run(self) -> SyncResult:
        if not self.token or not self.chat:
            log.info("tracker: Telegram credentials missing; sync skipped")
            return self.result
        for jid in self.tracker.detect_manual(self.today):  # rows changed by hand on the web
            self.result.rows_changed += 1
            await self._refresh(jid)
        for _ in range(MAX_PAGES):
            payload: dict = {"timeout": 0, "limit": 100,
                             "allowed_updates": ["message", "channel_post", "callback_query"]}
            if self.tracker.offset is not None:
                payload["offset"] = self.tracker.offset
            try:
                data = await self._call("getUpdates", payload)
            except HttpError as exc:
                if exc.status == 409:
                    # Either a webhook is set (getUpdates never works: call deleteWebhook) or another run
                    # (favourites, main, tracker-sync) was reading updates at that moment: the next sync reads them.
                    log.warn("tracker: Telegram answered 409 (a webhook is set on the bot, or another run was "
                             "reading updates at the same time)")
                else:
                    log.warn(f"tracker: getUpdates failed ({exc})")
                    self.result.failed = True
                break
            updates = data.get("result") or []
            for update in updates:
                self.result.updates += 1
                # Moving the offset past an update confirms it: Telegram drops it at the next call.
                self.tracker.set_offset(int(update["update_id"]) + 1)
                try:
                    if "callback_query" in update:
                        await self._on_button(update["callback_query"])
                    elif "message" in update or "channel_post" in update:
                        await self._on_message(update.get("message") or update["channel_post"])
                    else:
                        self.result.ignored += 1
                except (KeyError, TypeError, ValueError) as exc:  # malformed update: skip it, keep the rest
                    self.result.ignored += 1
                    log.detail(f"tracker: bad update {exc!r}")
            if len(updates) < 100:
                break
        r = self.result
        log.info(f"tracker: {r.updates} updates ({r.buttons} buttons, {r.commands} commands, {r.ignored} ignored), "
                 f"{r.rows_changed} rows changed")
        return r


async def sync(config: Config, http: Http, tracker: Tracker, state: State, now: datetime) -> SyncResult:
    return await TelegramSync(config, http, tracker, state, now).run()
