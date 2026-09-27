"""Tiny message catalogue for notifications (es/en)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Job

MESSAGES = {
    "es": {
        "new_jobs": "{n} ofertas nuevas",
        "new_job": "1 oferta nueva",
        "no_news": "Sin novedades",
        "sources": "{ok}/{total} fuentes revisadas",
        "posted": "Publicada",
        "detected": "Detectada",
        "today": "hoy",
        "approx": "aprox.",
        "fit": "Encaje",
        "exp": "Exp.",
        "years": "años",
        "also_on": "También en",
        "already": "ya avisada",
        "coverage": "cobertura",
        "more": "… y {n} más en el feed",
        "health": "Fuentes con problemas",
        "zero": "0 resultados (¿ha cambiado la web?)",
        "page_changed": "Ha cambiado la página de empleo",
        "favorite_alert": "Alerta favorita",
        "test_title": "Prueba de notificación",
        "test_body": "Si lees esto, el canal funciona correctamente.",
        "view": "Ver oferta",
    },
    "en": {
        "new_jobs": "{n} new jobs",
        "new_job": "1 new job",
        "no_news": "No new jobs",
        "sources": "{ok}/{total} sources checked",
        "posted": "Posted",
        "detected": "Detected",
        "today": "today",
        "approx": "approx.",
        "fit": "Fit",
        "exp": "Exp.",
        "years": "years",
        "also_on": "Also on",
        "already": "already alerted",
        "coverage": "coverage",
        "more": "… and {n} more in the feed",
        "health": "Sources with problems",
        "zero": "0 results (did the site change?)",
        "page_changed": "Careers page changed",
        "favorite_alert": "Favourite alert",
        "test_title": "Notification test",
        "test_body": "If you can read this, the channel works.",
        "view": "View job",
    },
}


def t(lang: str, key: str, **kwargs: object) -> str:
    text = MESSAGES.get(lang, MESSAGES["en"]).get(key) or MESSAGES["en"][key]
    return text.format(**kwargs) if kwargs else text


def when(job: Job, tz: ZoneInfo, lang: str, now: datetime) -> str:
    """'Publicada 27/09 08:15', 'Publicada ~25/09', 'Detectada 27/09 09:03'."""
    if job.posted_at is None:
        seen = (job.first_seen or now).astimezone(tz)
        return f"{t(lang, 'detected')} {seen:%d/%m %H:%M}"
    local = job.posted_at.astimezone(tz)
    if job.posted_precision == "datetime":
        return f"{t(lang, 'posted')} {local:%d/%m %H:%M}"
    if job.posted_precision == "relative":
        if local.date() == now.astimezone(tz).date():
            return f"{t(lang, 'posted')} {t(lang, 'today')} ({t(lang, 'approx')})"
        return f"{t(lang, 'posted')} ~{local:%d/%m}"
    return f"{t(lang, 'posted')} {local:%d/%m}"
