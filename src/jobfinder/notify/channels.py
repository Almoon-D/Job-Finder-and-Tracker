"""Notification channels: Telegram, Discord, e-mail (SMTP), ntfy and Apprise."""

from __future__ import annotations

import asyncio
import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..i18n import t
from .base import Channel, ChannelError, Notification


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def chunk(parts: list[str], limit: int, sep: str = "\n\n") -> list[str]:
    """Join parts into messages no longer than ``limit`` characters."""
    out: list[str] = []
    cur = ""
    for p in parts:
        p = p[:limit]
        candidate = f"{cur}{sep}{p}" if cur else p
        if len(candidate) > limit:
            out.append(cur)
            cur = p
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def footer_lines(n: Notification) -> list[str]:
    lines = [n.sources_line]
    if n.hidden_count:
        lines.insert(0, t(n.lang, "more", n=n.hidden_count))
    if n.problems:
        lines.append(f"⚠️ {t(n.lang, 'health')}: " + "; ".join(n.problems))
    return lines


# --------------------------------------------------------------------- Telegram
class Telegram(Channel):
    name = "telegram"
    LIMIT = 4000

    def enabled(self) -> bool:
        return self.config.notify.telegram.enabled

    def _job_html(self, n: Notification, job) -> str:
        e = html.escape
        head = f'<b>{e(n.job_heading(job))}</b>'
        meta = "\n".join(e(line) for line in n.job_meta(job))
        link = f'🔗 <a href="{e(job.url, quote=True)}">{e(t(n.lang, "view"))}</a>'
        return f"{head}\n{meta}\n{link}"

    async def _send(self, token: str, chat: str, text: str, silent: bool) -> None:
        cfg = self.config.notify.telegram
        await self.http.post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": cfg.disable_preview, "disable_notification": silent},
        )

    async def send(self, n: Notification) -> None:
        cfg = self.config.notify.telegram
        token, chat = env(cfg.bot_token_env), env(cfg.chat_id_env)
        if not token or not chat:
            raise ChannelError(f"missing {cfg.bot_token_env} or {cfg.chat_id_env}")
        header = f"<b>{html.escape(n.title)}</b>"
        footer = "\n".join(html.escape(x) for x in footer_lines(n))
        silent = n.empty and n.priority != "high"
        if n.test:
            await self._send(token, chat, f"{header}\n{html.escape(t(n.lang, 'test_body'))}", False)
            return
        if n.format == "per_job" and not n.empty:
            await self._send(token, chat, header, False)
            for job in n.shown:
                await asyncio.sleep(1.1)  # Telegram allows ~1 message/second per chat
                await self._send(token, chat, self._job_html(n, job), False)
            if n.hidden_count or n.problems:
                await self._send(token, chat, footer, True)
            return
        parts = [header] + [self._job_html(n, j) for j in n.shown] + [footer]
        for i, text in enumerate(chunk(parts, self.LIMIT)):
            if i:
                await asyncio.sleep(1.1)
            await self._send(token, chat, text, silent)


# ---------------------------------------------------------------------- Discord
class Discord(Channel):
    name = "discord"

    def enabled(self) -> bool:
        return self.config.notify.discord.enabled

    async def send(self, n: Notification) -> None:
        url = env(self.config.notify.discord.webhook_env)
        if not url:
            raise ChannelError(f"missing {self.config.notify.discord.webhook_env}")
        color = 0xF5A623 if n.priority == "high" else 0x2F80ED
        if n.test:
            await self.http.request("POST", url, json={"content": f"**{n.title}**\n{t(n.lang, 'test_body')}"})
            return
        embeds = [
            {
                "title": n.job_heading(j)[:256],
                "url": j.url,
                "description": "\n".join(n.job_meta(j))[:4000],
                "color": color,
            }
            for j in n.shown
        ]
        content = f"**{n.title}**"
        footer = "\n".join(footer_lines(n))
        if not embeds:
            await self.http.request("POST", url, json={"content": f"{content}\n{footer}"[:2000]})
            return
        for i in range(0, len(embeds), 10):
            payload = {"embeds": embeds[i:i + 10]}
            if i == 0:
                payload["content"] = content[:2000]
            if i + 10 >= len(embeds):
                payload["embeds"][-1]["footer"] = {"text": footer[:2000]}
            await self.http.request("POST", url, json=payload)
            await asyncio.sleep(0.6)


# ------------------------------------------------------------------------ Email
_TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


class Email(Channel):
    name = "email"

    def enabled(self) -> bool:
        return self.config.notify.email.enabled

    def render(self, n: Notification) -> tuple[str, str]:
        text_parts = [n.title, ""]
        for j in n.shown:
            text_parts += [n.job_heading(j), *n.job_meta(j), j.url, ""]
        text_parts += footer_lines(n)
        if n.test:
            text_parts = [n.title, t(n.lang, "test_body")]
        html_body = _TEMPLATES.get_template("email.html").render(n=n, t=lambda k, **kw: t(n.lang, k, **kw),
                                                                footer=footer_lines(n))
        return "\n".join(text_parts), html_body

    def _send_sync(self, msg: EmailMessage, host: str, port: int, user: str, password: str) -> None:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
            return
        with smtplib.SMTP(host, port, timeout=60) as s:
            if self.config.notify.email.starttls:
                s.starttls(context=context)
            if user:
                s.login(user, password)
            s.send_message(msg)

    async def send(self, n: Notification) -> None:
        cfg = self.config.notify.email
        host, user, password = env(cfg.host_env), env(cfg.user_env), env(cfg.password_env)
        port = int(env(cfg.port_env) or 587)
        to = cfg.to or [x.strip() for x in env(cfg.to_env).split(",") if x.strip()]
        if not host or not to:
            raise ChannelError(f"missing {cfg.host_env} or recipients")
        text, html_body = self.render(n)
        msg = EmailMessage()
        msg["Subject"] = n.title
        msg["From"] = cfg.from_addr or user
        msg["To"] = ", ".join(to)
        if n.priority == "high":
            msg["X-Priority"] = "1"
            msg["Importance"] = "high"
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")
        await asyncio.to_thread(self._send_sync, msg, host, port, user, password)


# ------------------------------------------------------------------------- ntfy
class Ntfy(Channel):
    name = "ntfy"

    def enabled(self) -> bool:
        return self.config.notify.ntfy.enabled

    async def _post(self, server: str, topic: str, title: str, body: str, priority: int, click: str | None,
                    token: str) -> None:
        """Publish via ntfy's JSON API (supports UTF-8 titles and Markdown)."""
        payload = {"topic": topic, "title": title, "message": body[:3900], "priority": priority,
                   "markdown": True, "tags": ["briefcase"]}
        if click:
            payload["click"] = click
        auth = {"Authorization": f"Bearer {token}"} if token else {}
        await self.http.request("POST", server, json=payload, headers=auth)

    async def send(self, n: Notification) -> None:
        cfg = self.config.notify.ntfy
        topic, token = env(cfg.topic_env), env(cfg.token_env)
        if not topic:
            raise ChannelError(f"missing {cfg.topic_env}")
        server = cfg.server.rstrip("/")
        prio = 5 if n.priority == "high" else 3
        if n.test:
            await self._post(server, topic, n.title, t(n.lang, "test_body"), prio, None, token)
            return
        if n.format == "per_job" and n.jobs and len(n.shown) <= 10:
            for j in n.shown:
                await self._post(server, topic, n.job_heading(j), "\n".join(n.job_meta(j)), prio, j.url, token)
            if n.hidden_count or n.problems:
                await self._post(server, topic, n.title, "\n".join(footer_lines(n)), 2, None, token)
            return
        lines = [f"- [{n.job_heading(j)}]({j.url}) · {n.when(j)}" for j in n.shown]
        body = "\n".join(lines + [""] + footer_lines(n))
        await self._post(server, topic, n.title, body, 2 if n.empty else prio,
                         n.jobs[0].url if len(n.jobs) == 1 else None, token)


# ---------------------------------------------------------------------- Apprise
class AppriseChannel(Channel):
    name = "apprise"

    def enabled(self) -> bool:
        return self.config.notify.apprise.enabled

    async def send(self, n: Notification) -> None:
        urls = [u.strip() for u in env(self.config.notify.apprise.urls_env).split() if u.strip()]
        if not urls:
            raise ChannelError(f"missing {self.config.notify.apprise.urls_env}")
        try:
            import apprise
        except ImportError as exc:
            raise ChannelError("install the 'apprise' extra") from exc
        app = apprise.Apprise()
        for u in urls:
            app.add(u)
        body_lines = []
        for j in n.shown:
            body_lines += [f"**[{n.job_heading(j)}]({j.url})**", *n.job_meta(j), ""]
        body_lines += footer_lines(n)
        if n.test:
            body_lines = [t(n.lang, "test_body")]
        ok = await asyncio.to_thread(app.notify, title=n.title, body="\n".join(body_lines),
                                     body_format=apprise.NotifyFormat.MARKDOWN)
        if not ok:
            raise ChannelError("apprise reported a failure")


ALL_CHANNELS = [Telegram, Discord, Email, Ntfy, AppriseChannel]
