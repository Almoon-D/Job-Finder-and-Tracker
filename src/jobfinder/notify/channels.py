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
from ..tracker.buttons import TEST_ID, job_id, keyboard
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
    if n.report is not None:
        return list(n.report.footer)
    lines = [n.sources_line]
    if n.also_elsewhere:
        lines.insert(0, t(n.lang, "elsewhere", n=n.also_elsewhere))
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

    def _grouped_parts(self, n: Notification) -> list[str]:
        """One part per job line; a section heading travels with its first line (never orphaned)."""
        e = html.escape
        parts = []
        for heading, jobs in n.sections():
            for i, job in enumerate(jobs):
                line = f'• <a href="{e(job.url, quote=True)}">{e(job.title[:150])}</a> — {e(n.compact_meta(job))}'
                parts.append(f"\n<b>{e(heading)}</b>\n{line}" if i == 0 else line)
        return parts

    def _job_html(self, n: Notification, job) -> str:
        e = html.escape
        head = f'<b>{e(n.job_heading(job))}</b>'
        meta = "\n".join(e(line) for line in n.job_meta(job))
        link = f'🔗 <a href="{e(job.url, quote=True)}">{e(t(n.lang, "view"))}</a>'
        return f"{head}\n{meta}\n{link}"

    def _report_parts(self, n: Notification) -> list[str]:
        e = html.escape
        assert n.report is not None
        return [f"<b>{e(heading)}</b>\n" + "\n".join(e(line) for line in lines) for heading, lines in n.report.sections]

    async def _send(self, token: str, chat: str, text: str, silent: bool, markup: dict | None = None) -> dict:
        """Send one message; returns Telegram's ``result`` (the sent message) or {}."""
        cfg = self.config.notify.telegram
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": cfg.disable_preview, "disable_notification": silent}
        if markup:
            payload["reply_markup"] = markup
        data = await self.http.post_json(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    async def send(self, n: Notification) -> None:
        cfg = self.config.notify.telegram
        token, chat = env(cfg.bot_token_env), env(cfg.chat_id_env)
        if not token or not chat:
            raise ChannelError(f"missing {cfg.bot_token_env} or {cfg.chat_id_env}")
        header = f"<b>{html.escape(n.title)}</b>"
        footer = "\n".join(html.escape(x) for x in footer_lines(n))
        silent = n.empty and n.priority != "high"
        if n.test:
            text, markup = f"{header}\n{html.escape(t(n.lang, 'test_body'))}", None
            if n.buttons:  # the tracker's buttons, to try the whole loop (press → sync → marked)
                text += f"\n\n{html.escape(t(n.lang, 'test_buttons'))}"
                markup = keyboard(TEST_ID, None, n.lang)
            await self._send(token, chat, text, False, markup)
            return
        if n.report is not None:
            for i, text in enumerate(chunk([p for p in (header, *self._report_parts(n), footer) if p], self.LIMIT)):
                if i:
                    await asyncio.sleep(1.1)
                await self._send(token, chat, text, False)
            return
        if n.format == "per_job" and not n.empty:
            await self._send(token, chat, header, False)
            for job in n.shown:
                await asyncio.sleep(1.1)  # Telegram allows ~1 message/second per chat
                jid = job_id(job.key) if n.buttons and job.kind == "job" else None
                markup = keyboard(jid, n.tracker_status.get(jid), n.lang) if jid else None
                sent = await self._send(token, chat, self._job_html(n, job), False, markup)
                if jid and sent.get("message_id"):
                    sent_chat = str((sent.get("chat") or {}).get("id") or chat)
                    n.sent.append((jid, job.key, sent_chat, int(sent["message_id"])))
            if n.hidden_count or n.problems:
                await self._send(token, chat, footer, True)
            return
        if n.format == "grouped" and not n.empty:
            messages = chunk([header, *self._grouped_parts(n), f"\n{footer}"], self.LIMIT, sep="\n")
        else:
            messages = chunk([header] + [self._job_html(n, j) for j in n.shown] + [footer], self.LIMIT)
        for i, text in enumerate(messages):
            if i:
                await asyncio.sleep(1.1)
            await self._send(token, chat, text, silent)


# ---------------------------------------------------------------------- Discord
def pack_embeds(embeds: list[dict], max_embeds: int = 10, max_chars: int = 6000) -> list[list[dict]]:
    """Split embeds into messages within Discord's limits (10 embeds and 6000 characters per message)."""

    def size(e: dict) -> int:
        return len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))

    out: list[list[dict]] = []
    cur: list[dict] = []
    total = 0
    for e in embeds:
        if cur and (len(cur) >= max_embeds or total + size(e) > max_chars):
            out.append(cur)
            cur, total = [], 0
        cur.append(e)
        total += size(e)
    if cur:
        out.append(cur)
    return out


class Discord(Channel):
    name = "discord"
    DESCRIPTION = 4096

    def enabled(self) -> bool:
        return self.config.notify.discord.enabled

    def _grouped_embeds(self, n: Notification, color: int) -> list[dict]:
        embeds = []
        for heading, jobs in n.sections():
            lines = [f"• [{j.title[:150].replace(']', ')')}]({j.url}) — {n.compact_meta(j)}"[:1000] for j in jobs]
            # A long section continues in another embed with the same title.
            for block in chunk(lines, self.DESCRIPTION, sep="\n"):
                embeds.append({"title": heading[:256], "description": block, "color": color})
        return embeds

    async def send(self, n: Notification) -> None:
        url = env(self.config.notify.discord.webhook_env)
        if not url:
            raise ChannelError(f"missing {self.config.notify.discord.webhook_env}")
        color = 0xF5A623 if n.priority == "high" else 0x2F80ED
        if n.test:
            await self.http.request("POST", url, json={"content": f"**{n.title}**\n{t(n.lang, 'test_body')}"})
            return
        if n.report is not None:
            embeds = [{"title": heading[:256], "description": ("\n".join(lines) or "—")[: self.DESCRIPTION],
                       "color": color} for heading, lines in n.report.sections]
        elif n.format == "grouped":
            embeds = self._grouped_embeds(n, color)
        else:
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
        footer = "\n".join(footer_lines(n))[:2048]
        if not embeds:
            await self.http.request("POST", url, json={"content": f"{content}\n{footer}"[:2000]})
            return
        embeds[-1]["footer"] = {"text": footer}
        for i, batch in enumerate(pack_embeds(embeds)):
            payload: dict = {"embeds": batch}
            if i == 0:
                payload["content"] = content[:2000]
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
        if n.report is not None:
            for heading, lines in n.report.sections:
                text_parts += [f"== {heading} ==", *lines, ""]
        elif n.format == "grouped":
            for heading, jobs in n.sections():
                text_parts += [f"== {heading} ==", ""]
                for j in jobs:
                    text_parts += [n.job_heading(j), *n.job_meta(j), j.url, ""]
        else:
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
        body = body.encode()[:3900].decode(errors="ignore")  # ntfy's limit is 4096 bytes, not characters
        payload = {"topic": topic, "title": title, "message": body, "priority": priority,
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
        if n.report is not None:
            # The footer (where the full report is) goes first: long bodies are cut at ntfy's 4 KB limit.
            lines = [*footer_lines(n), ""]
            for heading, rows in n.report.sections:
                lines += [f"**{heading}**", *[f"- {r}" for r in rows], ""]
            await self._post(server, topic, n.title, "\n".join(lines), 3, None, token)
            return
        if n.format == "per_job" and n.jobs and len(n.shown) <= 10:
            for j in n.shown:
                await self._post(server, topic, n.job_heading(j), "\n".join(n.job_meta(j)), prio, j.url, token)
            if n.hidden_count or n.problems:
                await self._post(server, topic, n.title, "\n".join(footer_lines(n)), 2, None, token)
            return
        if n.format == "grouped":
            lines = []
            for heading, jobs in n.sections():
                lines += [f"**{heading}**"] + [f"- [{j.title}]({j.url}) · {n.compact_meta(j)}" for j in jobs]
        else:
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
        if n.report is not None:
            for heading, lines in n.report.sections:
                body_lines += [f"### {heading}", *[f"- {line}" for line in lines], ""]
        elif n.format == "grouped":
            for heading, jobs in n.sections():
                body_lines += [f"### {heading}"] + [f"- [{j.title}]({j.url}) · {n.compact_meta(j)}" for j in jobs] + [""]
        else:
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
