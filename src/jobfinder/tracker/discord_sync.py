"""Read ✅ ❌ reactions on the messages the Discord bot posted and apply them to the tracker.

Discord webhooks can neither carry buttons nor read reactions, so with ``notify.discord.bot`` the bot
posts one message per job (see ``notify.channels.Discord``) and this sync polls the channel history
(one request returns 100 messages with their reaction counts) at the start of every normal run and
from ``jobfinder tracker-sync``. Logs show counts only.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import log
from ..config.schema import Config
from ..notify.channels import Discord, env
from ..sources.http import Http, HttpError
from ..state import State
from .store import Tracker, TrackerOp
from .sync import SyncResult

WINDOW_DAYS = 14  # messages older than this are no longer read
MAX_PAGES = 6  # 100 messages per page
EMOJI = {"✅": "applied", "❌": "discarded"}
KEEP = {None, "interested", "applied", "discarded"}  # a reaction never overrides interview/offer/rejected


class DiscordSync:
    def __init__(self, config: Config, http: Http, tracker: Tracker, state: State, now: datetime):
        self.http, self.tracker, self.state, self.now = http, tracker, state, now
        cfg = config.notify.discord
        self.token, self.channel = env(cfg.bot_token_env), env(cfg.channel_id_env)
        self.today = tracker.today(now).isoformat()
        self.since = (tracker.today(now) - timedelta(days=WINDOW_DAYS)).isoformat()
        self.result = SyncResult()

    async def _history(self, wanted: set[int]) -> dict[int, dict]:
        """The tracked messages found in the channel history, newest first, until the oldest one is reached."""
        found: dict[int, dict] = {}
        before = ""
        for _ in range(MAX_PAGES):
            path = f"/channels/{self.channel}/messages?limit=100" + (f"&before={before}" if before else "")
            resp = await self.http.request("GET", f"{Discord.API}{path}",
                                           headers={"Authorization": f"Bot {self.token}"})
            page = resp.json()
            for message in page:
                if int(message["id"]) in wanted:
                    found[int(message["id"])] = message
            if len(page) < 100 or not wanted - set(found) or int(page[-1]["id"]) <= min(wanted):
                break
            before = page[-1]["id"]
        return found

    @staticmethod
    def _humans(message: dict) -> str:
        """'a', 'd' or 'ad': which of ✅ ❌ somebody other than the bot has added."""
        out = ""
        for reaction in message.get("reactions") or []:
            status = EMOJI.get((reaction.get("emoji") or {}).get("name", ""))
            humans = int(reaction.get("count", 0)) - (1 if reaction.get("me") else 0)
            if status and humans > 0:
                out += status[0]
        return "".join(sorted(out))

    def _apply(self, jid: str, seen: str) -> bool:
        current = self.tracker.status_of(jid)
        if current not in KEEP:
            return False
        if seen == "ad":  # both: the one that is not the current status is the newer choice
            target = "discarded" if current == "applied" else "applied"
        else:
            target = "applied" if seen == "a" else "discarded"
        if target == current:
            return False
        entry = self.state.seen.get(self.tracker.messages.get(jid, {}).get("key") or "", {})
        info = {"company": entry.get("company") or "", "title": entry.get("title") or "",
                "location": entry.get("loc") or "", "url": entry.get("url") or ""}
        return self.tracker.apply(TrackerOp(jid, target, self.today, info))

    async def run(self) -> SyncResult:
        if not self.token or not self.channel:
            log.info("tracker: Discord bot credentials missing; reaction sync skipped")
            return self.result
        refs = self.tracker.discord_refs(self.since)
        if not refs:
            return self.result
        self.tracker.detect_manual(self.today)
        try:
            history = await self._history({mid for _, _, mid in refs})
        except HttpError as exc:
            log.warn(f"tracker: Discord history failed (HTTP {exc.status})")
            self.result.failed = True
            return self.result
        by_job = {jid: (channel, mid) for jid, channel, mid in refs}
        for jid, (channel, mid) in by_job.items():
            message = history.get(mid)
            if message is None:
                continue
            seen = self._humans(message)
            if seen == self.tracker.reactions.get(f"{channel}:{mid}", ""):
                continue  # nothing new since the last sync: hand edits of the CSV are left alone
            self.result.updates += 1
            self.result.buttons += 1
            if seen and self._apply(jid, seen):
                self.result.rows_changed += 1
            self.tracker.set_reactions(channel, mid, seen)
        r = self.result
        log.info(f"tracker: Discord {len(history)}/{len(refs)} messages read, {r.buttons} reaction changes, "
                 f"{r.rows_changed} rows changed")
        return r
