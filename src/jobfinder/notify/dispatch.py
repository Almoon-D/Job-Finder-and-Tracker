"""Send a notification through every enabled channel."""

from __future__ import annotations

from .. import log
from ..config.schema import Config
from ..sources.http import Http, HttpError
from .base import ChannelError, Notification
from .channels import ALL_CHANNELS


async def send_all(config: Config, http: Http, n: Notification) -> tuple[int, int]:
    """Returns (channels attempted, channels that succeeded)."""
    attempted = succeeded = 0
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
                why = str(exc)  # only mentions env var names
            else:
                why = type(exc).__name__
            log.warn(f"notify/{channel.name}: failed ({why})")
            log.detail(f"{channel.name}: {exc!r}")
    return attempted, succeeded
