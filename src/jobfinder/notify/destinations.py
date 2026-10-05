"""Chat name -> id: the Discord channel / Telegram topic behind each name of ``notify.routing``.

Order: an id written in the config, an id the bot remembered (``state/runs.json``), a chat made now (a Discord
channel of the same name is adopted instead; Telegram cannot list topics, so write the id in the config if the
state is ever lost). A name that cannot be resolved falls back to the ``other`` chat and then to the default one.
Public CI logs show counts and the API's own reason, never a chat name or an id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import log
from ..config.schema import Config, route_slug
from ..sources.http import Http, HttpError
from ..state import State
from .channels import api_reason, env

DISCORD_API = "https://discord.com/api/v10"
TEXT_CHANNEL, CATEGORY = 0, 4  # Discord channel types
_UNSET = object()


class Destinations:
    """One per run: it caches what it looked up, and saves what it created (``on_created`` commits it)."""

    def __init__(self, config: Config, http: Http, state: State, on_created: Callable[[], None] | None = None):
        self.config, self.http, self.state, self.on_created = config, http, state, on_created
        self._discord: dict[str, str | None] = {}
        self._telegram: dict[str, int | None] = {}
        self._guild: dict[str, Any] | None = None
        self._forum: bool | None = None
        self._warned: set[str] = set()
        self.created = 0

    # ------------------------------------------------------------------ shared
    def _fail(self, channel: str, exc: Exception, doing: str) -> None:
        why = api_reason(exc) if isinstance(exc, HttpError) else type(exc).__name__
        if why not in self._warned:  # once per cause: a missing permission would repeat for every chat
            self._warned.add(why)
            log.warn(f"notify/{channel}: could not {doing} ({why}); the chat falls back to the catch-all one")

    def _remember(self, channel: str, route: str, ident: str | int) -> None:
        self.created += 1
        self.state.set_destination(channel, route, ident)
        if self.on_created:
            self.on_created()  # saved at once: a chat lost from the state would be created again

    # ----------------------------------------------------------------- Discord
    @property
    def discord_ready(self) -> bool:
        """Routing needs the bot: webhooks cannot choose a channel."""
        cfg = self.config.notify.discord
        return bool(cfg.bot and env(cfg.bot_token_env) and env(cfg.channel_id_env))

    async def _discord_call(self, method: str, path: str, **kwargs: Any) -> Any:
        token = env(self.config.notify.discord.bot_token_env)
        resp = await self.http.request(method, f"{DISCORD_API}{path}", headers={"Authorization": f"Bot {token}"},
                                       **kwargs)
        return resp.json()

    async def discord_channel(self, route: str | None) -> str:
        """Channel id for a chat name; ``None`` is the default channel, and so is anything unresolvable."""
        default = env(self.config.notify.discord.channel_id_env)
        if route is None:
            return default
        for name in dict.fromkeys([route, self.config.notify.routing.other]):
            found = await self._discord_resolve(name) if name else None
            if found:
                return found
        return default

    async def _discord_resolve(self, route: str) -> str | None:
        if route not in self._discord:
            ident = self.config.notify.discord.channels.get(route) or self.state.destination("discord", route)
            self._discord[route] = str(ident) if ident else await self._discord_create(route)
        return self._discord[route]

    async def _discord_guild(self) -> dict[str, Any]:
        if self._guild is None:
            cfg = self.config.notify.discord
            guild, parent = cfg.guild_id, None
            if not guild or not cfg.category:  # where the default channel lives: same server, same category
                info = await self._discord_call("GET", f"/channels/{env(cfg.channel_id_env)}")
                guild, parent = guild or info["guild_id"], info.get("parent_id")
            channels = await self._discord_call("GET", f"/guilds/{guild}/channels")
            if cfg.category:
                wanted = cfg.category.strip().lower()
                found = next((c for c in channels if c.get("type") == CATEGORY and c.get("name", "").lower() == wanted),
                             None)
                if found is None:
                    found = await self._discord_call("POST", f"/guilds/{guild}/channels", retries=0,
                                                     json={"name": cfg.category.strip(), "type": CATEGORY})
                    channels.append(found)
                parent = found["id"]
            self._guild = {"id": guild, "category": parent, "channels": channels}
        return self._guild

    async def _discord_create(self, route: str) -> str | None:
        """Adopt the text channel that already has this name, else create it next to the default channel."""
        try:
            guild = await self._discord_guild()
            slug = route_slug(route)
            same = [c for c in guild["channels"] if c.get("type") == TEXT_CHANNEL and route_slug(c.get("name", "")) == slug]
            found = next((c for c in same if c.get("parent_id") == guild["category"]), same[0] if same else None)
            if found is None:
                body: dict[str, Any] = {"name": slug, "type": TEXT_CHANNEL}
                if guild["category"]:
                    body["parent_id"] = guild["category"]
                # retries=0: a timeout after Discord created it must not make a second channel
                found = await self._discord_call("POST", f"/guilds/{guild['id']}/channels", retries=0, json=body)
                guild["channels"].append(found)
            ident = str(found["id"])
        except (HttpError, KeyError, TypeError, ValueError, AttributeError) as exc:
            self._fail("discord", exc, "find or create a channel")
            return None
        self._remember("discord", route, ident)
        return ident

    # ---------------------------------------------------------------- Telegram
    async def telegram_ready(self) -> bool:
        """Routing needs a supergroup with Topics enabled (the bot is told by ``getChat``)."""
        if self._forum is None:
            cfg = self.config.notify.telegram
            token, chat = env(cfg.bot_token_env), env(cfg.chat_id_env)
            self._forum = False
            if token and chat:
                try:
                    data = await self.http.post_json(f"https://api.telegram.org/bot{token}/getChat",
                                                     json={"chat_id": chat})
                    self._forum = bool(((data or {}).get("result") or {}).get("is_forum"))
                except HttpError as exc:
                    self._fail("telegram", exc, "read the group")
                if not self._forum:
                    log.warn("notify/telegram: routing needs a group with Topics enabled; sending to the one chat")
        return self._forum

    async def telegram_topic(self, route: str | None) -> int | None:
        """Topic id (``message_thread_id``) for a chat name; ``None`` is the General topic, and so is anything
        unresolvable."""
        if route is None:
            return None
        for name in dict.fromkeys([route, self.config.notify.routing.other]):
            if not name:
                continue
            found = await self._telegram_resolve(name)
            if found:  # 0 is the General topic: no thread id
                return found
            if found == 0:
                return None
        return None

    async def _telegram_resolve(self, route: str) -> int | None:
        if route not in self._telegram:
            ident: Any = self.config.notify.telegram.topics.get(route, _UNSET)
            if ident is _UNSET:
                ident = self.state.destination("telegram", route)
            self._telegram[route] = await self._telegram_create(route) if ident is None else int(ident)
        return self._telegram[route]

    async def _telegram_create(self, route: str) -> int | None:
        cfg = self.config.notify.telegram
        try:
            data = await self.http.post_json(
                f"https://api.telegram.org/bot{env(cfg.bot_token_env)}/createForumTopic", retries=0,
                json={"chat_id": env(cfg.chat_id_env), "name": route[:128]})
            ident = int(data["result"]["message_thread_id"])
        except (HttpError, KeyError, TypeError, ValueError) as exc:
            self._fail("telegram", exc, "create a topic")
            return None
        self._remember("telegram", route, ident)
        return ident
