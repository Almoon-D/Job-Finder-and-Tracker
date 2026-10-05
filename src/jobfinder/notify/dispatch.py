"""Send a notification through every enabled channel."""

from __future__ import annotations

from .. import log
from ..config.schema import Config
from ..sources.http import Http, HttpError
from .base import ChannelError, Notification
from .channels import ALL_CHANNELS, env


async def send_all(config: Config, http: Http, n: Notification) -> tuple[int, int]:
    """Returns (channels attempted, channels that succeeded)."""
    attempted = succeeded = 0
    if not any(cls(config, http).enabled() for cls in ALL_CHANNELS):
        log.warn("notify: no channel is enabled in config.yaml (set e.g. `notify: {telegram: {enabled: true}}`)")
    for cls in ALL_CHANNELS:
        channel = cls(config, http)
        if not channel.enabled():
            continue
        attempted += 1
        try:
            await channel.send(n)
            succeeded += 1
            log.info(f"  notify/{channel.name}: sent")
        except Exception as exc:  # a failing channel must not stop the others
            # Error text may include the chat/webhook id in URLs: print the type only.
            if isinstance(exc, HttpError):
                why = f"HTTP {exc.status}"
            elif isinstance(exc, ChannelError):
                why = str(exc)  # env var names, or the API's own error text (no ids)
            else:
                why = type(exc).__name__
            log.warn(f"notify/{channel.name}: failed ({why})")
            log.detail(f"{channel.name}: {exc!r}")
    return attempted, succeeded


def credentials_checklist(config: Config) -> list[str]:
    """One line per channel: is it enabled, and which of its secrets are set (names and yes/no only)."""
    n = config.notify

    def have(*names: str) -> str:
        return ", ".join(f"{name}={'set' if env(name) else 'MISSING'}" for name in names)

    lines = [
        ("telegram", n.telegram.enabled, have(n.telegram.bot_token_env, n.telegram.chat_id_env)),
        ("discord", n.discord.enabled, have(n.discord.webhook_env) + " | bot: "
         + have(n.discord.bot_token_env, n.discord.channel_id_env)),
        ("email", n.email.enabled, have(n.email.host_env) + (", to=config" if n.email.to else f", {have(n.email.to_env)}")),
        ("ntfy", n.ntfy.enabled, have(n.ntfy.topic_env)),
        ("apprise", n.apprise.enabled, have(n.apprise.urls_env)),
    ]
    return [f"  {name}: {'enabled' if on else 'disabled in config.yaml'}" + (f" ({secrets})" if on else "")
            for name, on, secrets in lines]
