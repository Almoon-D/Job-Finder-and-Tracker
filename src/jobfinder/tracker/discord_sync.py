"""Read ⭐✅🗣❌ reactions from the Discord bot's messages and apply them to the tracker.

Like Telegram, this is polling (GitHub Actions cannot host a Discord gateway): at the start of every
normal run and from ``jobfinder tracker-sync``. Each tracked message is read with one REST call
(``GET /channels/{id}/messages/{id}`` lists its reactions with a count and ``me``); a reaction made
by someone other than the bot is ``count - 1`` when the bot reacted too. No privileged intents.
Logs show counts only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from .. import log
from ..config.schema import Config
from ..notify.channels import DISCORD_API
from ..sources.http import Http, HttpError
from ..state import State
from .buttons import BUTTONS, TEST_ID, emoji_status, job_id
from .store import REF_DAYS, Tracker, TrackerOp, status_label
from .sync import SyncResult

MAX_MESSAGES = 200  # newest first; at ~0.25 s each a sync stays under a minute


def marker(status: str, lang: str) -> str:
    """First line of the bot's message once a status is set: '» ✅ Applied «'."""
    return f"» {status_label(status, lang)} «"


def with_marker(content: str, status: str, lang: str) -> str:
    """``content`` with its marker line (if any) replaced by the one of ``status``."""
    body = content
    if body.startswith("» ") and "«" in body.split("\n", 1)[0]:
        body = body.split("\n", 1)[1] if "\n" in body else ""
    line = marker(status, lang)
    return f"{line}\n{body}" if body else line


class DiscordSync:
    def __init__(self, config: Config, http: Http, tracker: Tracker, state: State, now: datetime):
        self.config = config
        self.http = http
        self.tracker = tracker
        self.state = state
        self.now = now
        self.lang = config.language
        creds = config.notify.discord.bot_credentials()
        self.token = creds[0] if creds else ""
        self.today = tracker.today(now).isoformat()
        self._keys: dict[str, str] | None = None
        self.result = SyncResult()

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        resp = await self.http.request(method, f"{DISCORD_API}{path}", headers={"Authorization": f"Bot {self.token}"},
                                       **kwargs)
        data = resp.json() if resp.content else {}
        return data if isinstance(data, dict) else {}

    def _key_for(self, jid: str, ref: dict) -> str | None:
        if ref.get("key"):
            return ref["key"]
        if self._keys is None:
            self._keys = {job_id(k): k for k in self.state.seen}
        return self._keys.get(jid)

    def _info(self, key: str | None, message: dict) -> dict[str, str]:
        entry = self.state.seen.get(key or "", {})
        if entry:
            return {"company": entry.get("company") or "", "title": entry.get("title") or "",
                    "location": entry.get("loc") or "", "url": entry.get("url") or ""}
        # Not in the state any more: read it from the embed ("Company — Title" and its link).
        embed = (message.get("embeds") or [{}])[0]
        company, _, title = (embed.get("title") or "").partition(" — ")
        return {"company": company.strip(), "title": title.strip(), "location": "", "url": embed.get("url") or ""}

    @staticmethod
    def _human_reactions(message: dict) -> set[str]:
        """Statuses somebody (not the bot) reacted with."""
        out = set()
        for reaction in message.get("reactions") or []:
            humans = int(reaction.get("count") or 0) - (1 if reaction.get("me") else 0)
            status = emoji_status((reaction.get("emoji") or {}).get("name"))
            if humans > 0 and status:
                out.add(status)
        return out

    async def _read(self, mid: str, ref: dict) -> dict | None:
        """The message, or None when it cannot be read (deleted, or the bot lost access)."""
        try:
            return await self._call("GET", f"/channels/{ref['channel']}/messages/{mid}")
        except HttpError as exc:
            if exc.status == 404:  # deleted by hand: stop tracking it
                self.tracker.forget_discord(mid)
            elif exc.status in (401, 403):
                log.warn(f"tracker: Discord answered HTTP {exc.status} (check the bot token and that it can "
                         "'View Channel' and 'Read Message History')")
                self.result.failed = True
            else:
                log.detail(f"tracker: Discord GET HTTP {exc.status}")
            return None

    async def run(self) -> SyncResult:
        cutoff = (self.tracker.today(self.now) - timedelta(days=REF_DAYS)).isoformat()
        refs = sorted(((mid, ref) for mid, ref in self.tracker.discord.items() if ref.get("day", "") >= cutoff),
                      key=lambda item: item[1].get("day", ""), reverse=True)[:MAX_MESSAGES]
        self.result.rows_changed += len(self.tracker.detect_manual(self.today))  # rows changed by hand on the web
        read: list[tuple[str, dict, dict, str | None]] = []  # (message id, ref, message, test status)
        for mid, ref in refs:
            if self.result.failed:
                break
            message = await self._read(mid, ref)
            await asyncio.sleep(0.25)
            if message is None:
                continue
            self.result.updates += 1
            humans = self._human_reactions(message)
            seen = set(ref.get("seen") or [])
            self.tracker.set_discord_seen(mid, sorted(humans))
            new = humans - seen
            status = next((s for s in reversed(BUTTONS) if s in new), None)
            if status is None:
                read.append((mid, ref, message, None))
                continue
            self.result.buttons += 1
            if ref["jid"] == TEST_ID:
                read.append((mid, ref, message, status))
                continue
            key = self._key_for(ref["jid"], ref)
            if self.tracker.apply(TrackerOp(ref["jid"], status, self.today, self._info(key, message))):
                self.result.rows_changed += 1
            read.append((mid, ref, message, None))
        # Second pass, once every reaction is applied: show the current status on each message.
        for mid, ref, message, test_status in read:
            status = test_status or (self.tracker.status_of(ref["jid"]) if ref["jid"] != TEST_ID else None)
            if not status:
                continue
            content = message.get("content") or ""
            wanted = with_marker(content, status, self.lang)
            if wanted == content:
                continue
            try:
                await self._call("PATCH", f"/channels/{ref['channel']}/messages/{mid}", json={"content": wanted[:2000]})
            except HttpError as exc:  # best effort: the status is saved either way
                log.detail(f"tracker: Discord PATCH HTTP {exc.status}")
            await asyncio.sleep(0.25)
        r = self.result
        log.info(f"tracker: discord {r.updates} messages read ({r.buttons} reactions), {r.rows_changed} rows changed")
        return r
