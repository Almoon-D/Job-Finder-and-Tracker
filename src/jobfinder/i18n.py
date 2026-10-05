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
        "other": "Otros puestos",
        "coverage_place": "Cobertura",
        "elsewhere": "🔁 {n} ya avisadas en otros grupos (no se repiten)",
        # Tracker
        "st_interested": "⭐ Interesa",
        "st_applied": "✅ Aplicado",
        "st_interview": "🗣 Entrevista",
        "st_offer": "🎉 Oferta",
        "st_rejected": "🚫 Rechazado",
        "st_discarded": "❌ Descartado",
        "btn_discarded": "❌ Descartar",
        "tracker_saved": "Guardado: {status}",
        "tracker_test": "Prueba: {status} (no se guarda)",
        "test_buttons": "Botones de prueba: pulsa uno y, en la siguiente sincronización (tracker-sync o la "
                        "próxima ejecución), el botón aparecerá marcado.",
        "tracker_title": "📋 Tracker",
        "tracker_empty": "Todavía no hay ofertas marcadas. Usa los botones de cada aviso.",
        "tracker_recent": "Últimos cambios",
        "pending_title": "⏳ Pendientes",
        "pending_apply": "⭐ Por aplicar ({n})",
        "pending_follow": "📨 Aplicadas sin novedades desde hace {days}+ días ({n})",
        "pending_none": "Nada pendiente 🎉",
        "since": "desde",
        "tracker_help": "Botones de cada oferta: ⭐ Interesa · ✅ Aplicado · 🗣 Entrevista · ❌ Descartar.\n"
                        "Comandos: /estado (embudo y últimos cambios) · /pendientes (por aplicar y sin respuesta).\n"
                        "Las pulsaciones y comandos se procesan en la siguiente sincronización. El CSV "
                        "tracker/applications.csv del repo privado se puede editar a mano.",
        # Weekly summary
        "w_new": "🆕 Ofertas nuevas: {n}",
        "w_by_group": "Por grupo",
        "w_by_company": "Por empresa",
        "w_by_family": "Por familia",
        "w_others": "otras {n}",
        "w_funnel": "🎯 Coincidencias y descartes",
        "w_reviewed": "{matches} coincidencias de {fetched} ofertas revisadas ({candidates} candidatas tras filtros)",
        "w_discards": "Descartes",
        "w_tracker": "📋 Tracker",
        "w_moves": "Esta semana",
        "w_sources": "🩺 Fuentes",
        "w_sources_ok": "{ok}/{total} fuentes funcionando",
        "w_top": "Más coincidencias",
        "w_problems": "Con problemas",
        "w_report": "Informe completo en {path} (repo privado)",
        "w_none": "sin datos",
        "w_col_source": "Fuente",
        "w_col_runs": "Ejecuciones",
        "w_col_ok": "OK",
        "w_col_jobs": "Ofertas/ejec.",
        "w_col_matches": "Coincidencias",
        "w_col_seconds": "Segundos/ejec.",
        "w_col_health": "Salud",
        "w_col_count": "Nº",
        "r_location": "ubicación",
        "r_too_old": "antigüedad",
        "r_excluded": "exclusiones",
        "r_experience": "experiencia",
        "r_no_keyword": "sin keyword",
        "r_ai_score": "encaje IA bajo",
        "r_source_family": "familia no permitida en la fuente",
        "r_duplicate": "duplicadas",
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
        "other": "Other roles",
        "coverage_place": "Coverage",
        "elsewhere": "🔁 {n} already sent in other groups (not repeated)",
        # Tracker
        "st_interested": "⭐ Interested",
        "st_applied": "✅ Applied",
        "st_interview": "🗣 Interview",
        "st_offer": "🎉 Offer",
        "st_rejected": "🚫 Rejected",
        "st_discarded": "❌ Discarded",
        "btn_discarded": "❌ Discard",
        "tracker_saved": "Saved: {status}",
        "tracker_test": "Test: {status} (not saved)",
        "test_buttons": "Test buttons: press one and, at the next sync (tracker-sync or the next run), the button "
                        "shows as selected.",
        "tracker_title": "📋 Tracker",
        "tracker_empty": "Nothing tracked yet. Use the buttons on each alert.",
        "tracker_recent": "Latest changes",
        "pending_title": "⏳ Pending",
        "pending_apply": "⭐ To apply ({n})",
        "pending_follow": "📨 Applied, no news for {days}+ days ({n})",
        "pending_none": "Nothing pending 🎉",
        "since": "since",
        "tracker_help": "Buttons on each job: ⭐ Interested · ✅ Applied · 🗣 Interview · ❌ Discard.\n"
                        "Commands: /status (funnel and latest changes) · /pending (to apply and awaiting reply).\n"
                        "Button presses and commands are processed at the next sync. The CSV "
                        "tracker/applications.csv in the private repo can be edited by hand.",
        # Weekly summary
        "w_new": "🆕 New jobs: {n}",
        "w_by_group": "By group",
        "w_by_company": "By company",
        "w_by_family": "By family",
        "w_others": "{n} others",
        "w_funnel": "🎯 Matches and discards",
        "w_reviewed": "{matches} matches out of {fetched} jobs checked ({candidates} candidates after filters)",
        "w_discards": "Discarded",
        "w_tracker": "📋 Tracker",
        "w_moves": "This week",
        "w_sources": "🩺 Sources",
        "w_sources_ok": "{ok}/{total} sources working",
        "w_top": "Most matches",
        "w_problems": "With problems",
        "w_report": "Full report in {path} (private repo)",
        "w_none": "no data",
        "w_col_source": "Source",
        "w_col_runs": "Runs",
        "w_col_ok": "OK",
        "w_col_jobs": "Jobs/run",
        "w_col_matches": "Matches",
        "w_col_seconds": "Seconds/run",
        "w_col_health": "Health",
        "w_col_count": "Count",
        "r_location": "location",
        "r_too_old": "too old",
        "r_excluded": "exclusions",
        "r_experience": "experience",
        "r_no_keyword": "no keyword",
        "r_ai_score": "low AI fit",
        "r_source_family": "family not allowed for the source",
        "r_duplicate": "duplicates",
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
