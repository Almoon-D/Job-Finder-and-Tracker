"""Configuration schema.

Everything user-specific (companies, roles, locations, schedules) lives in a YAML
file that is kept OUT of the public repository (see docs/PRIVACY.md). Credentials
are never stored in the config: it only names the environment variables that hold
them.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "source"


class _Model(BaseModel):
    # coerce_numbers_to_str: YAML turns `2027` into an int; keyword lists expect strings.
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class RoleFamily(_Model):
    """A family of roles you are interested in (e.g. "product management")."""

    name: str
    label: str | None = Field(None, description="Name shown in grouped digests (default: the name).")
    description: str = Field("", description="Free text used by the AI matcher to understand the family.")
    include_any: list[str] = Field(
        default_factory=list, description="Title keywords (any language). Used as rule filter when AI is off."
    )


class Filters(_Model):
    exclude_title_any: list[str] = Field(default_factory=list, description="Hard exclusions on the job title.")
    exclude_text_any: list[str] = Field(
        default_factory=list, description="Hard exclusions on title+description (use sparingly)."
    )
    require_keyword_match: bool | None = Field(
        None,
        description="Require an include_any keyword in the title. Default: only when the AI matcher is disabled.",
    )


class Experience(_Model):
    min_years: float | None = None
    max_years: float | None = None
    hard: bool = Field(False, description="If true, drop jobs whose stated experience is clearly out of range.")


class LocationSpec(_Model):
    country: str | None = Field(None, description="ISO-3166 alpha-2 code or country name (e.g. DE, Germany).")
    city: str | None = None
    aliases: list[str] = Field(default_factory=list, description="Extra spellings for the city/country.")
    include_remote: bool = Field(True, description="Also match 'Remote' jobs in this country.")
    label: str | None = Field(None, description="Name shown in grouped digests (default: city or country name).")

    @model_validator(mode="after")
    def _need_something(self) -> LocationSpec:
        if not self.country and not self.city:
            raise ValueError("a location needs at least a country or a city")
        return self


class Coverage(_Model):
    """Jobs located elsewhere that mention your market (e.g. 'DACH coverage, based in London')."""

    enabled: bool = True
    text_any: list[str] = Field(
        default_factory=list, description="If the title/description mentions any of these, the location matches."
    )
    search_terms: list[str] = Field(
        default_factory=list,
        description="Extra keyword searches run WITHOUT location filter on sources that support search.",
    )
    only_cities: list[str] = Field(
        default_factory=list, description="Optional: restrict coverage matches to these cities (empty = anywhere)."
    )


class Group(_Model):
    """A notification group: which sources, when, and how the digest looks."""

    label: str | None = None
    kind: Literal["jobs", "summary"] = "jobs"
    times: list[str] = Field(default_factory=list, description="Local times HH:MM (config timezone).")
    weekdays: list[str] | None = Field(None, description="Restrict to weekdays: mon..sun.")
    every_days: int = Field(1, ge=1)
    interval_minutes: int | None = Field(None, ge=5, description="Polling group (e.g. favourites every 10 min).")
    quiet_hours: str | None = Field(None, description="HH:MM-HH:MM local window with no polling.")
    grace_hours: float = Field(
        12.0, gt=0,
        description="How late a missed scheduled slot may still run. GitHub's cron can skip half a day, "
                    "and a late run is harmless (seen jobs are never repeated), so keep it generous.",
    )
    max_age_days: float = Field(3.0, gt=0, description="Only jobs posted (or first seen) within this window.")
    notify_empty: bool = True
    format: Literal["per_job", "digest", "grouped"] = Field(
        "digest", description="per_job, digest, or grouped (compact digest by role family and location)."
    )
    priority: Literal["normal", "high"] = "normal"
    max_items: int = Field(50, ge=1, description="Max jobs listed in one notification (rest go to the feed).")

    @field_validator("times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        for t in v:
            if not _HHMM.match(t):
                raise ValueError(f"invalid time {t!r}, expected HH:MM")
        return v

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for d in v:
            k = d.strip().lower()[:3]
            if k not in _WEEKDAYS:
                raise ValueError(f"invalid weekday {d!r}")
            out.append(k)
        return out

    @field_validator("quiet_hours")
    @classmethod
    def _quiet(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parts = v.split("-")
        if len(parts) != 2 or not all(_HHMM.match(p.strip()) for p in parts):
            raise ValueError("quiet_hours must look like 23:00-07:00")
        return v

    @model_validator(mode="after")
    def _schedule(self) -> Group:
        if self.kind == "jobs" and not self.times and not self.interval_minutes:
            raise ValueError("a group needs 'times' or 'interval_minutes'")
        if self.kind == "summary" and not self.times:
            raise ValueError("a summary group needs 'times' (e.g. times: ['18:00'] and weekdays: [sun])")
        if self.interval_minutes and "notify_empty" not in self.model_fields_set:
            self.notify_empty = False  # a polling group must not send "no news" every few minutes
        return self

    def weekday_numbers(self) -> set[int] | None:
        return None if self.weekdays is None else {_WEEKDAYS[d] for d in self.weekdays}


class Source(BaseModel):
    """One place to look for jobs.

    Either give a ``url`` (the adapter is auto-detected for well-known ATS) or an
    explicit ``type`` plus its parameters. Unknown keys are passed to the adapter,
    so declarative recipes (json_api, html_list...) keep their parameters here.
    """

    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    name: str
    id: str | None = Field(None, description="Stable id for state; defaults to a slug of the name.")
    group: str = "company_sites"
    type: str | None = None
    url: str | None = None
    enabled: bool = True
    favorite: bool = False
    company: str | None = Field(None, description="Company shown in notifications (defaults to name).")
    queries: list[str] = Field(default_factory=list, description="Search keywords (sources that support search).")
    role_families: list[str] | None = Field(None, description="Restrict to these role family names.")
    include_title_any: list[str] = Field(default_factory=list)
    exclude_title_any: list[str] = Field(default_factory=list)
    skip_location_filter: bool = False
    use_coverage_search: bool = True
    max_pages: int = Field(10, ge=1)
    fetch_details: bool = True
    require_keyword_match: bool | None = Field(
        None,
        description="Require a role-family keyword in the title before the AI (saves quota on noisy sources). "
        "Default: the global filters.require_keyword_match.",
    )
    only_queries: bool = Field(False, description="ATS sources: skip the full listing, run only 'queries'.")
    use_ai: bool = Field(
        True,
        description="Send this source's jobs to the AI matcher. False: keyword matching only (e.g. sites whose "
        "robots.txt opts out of AI input).",
    )

    @property
    def key(self) -> str:
        return self.id or slugify(self.name)

    @property
    def display_company(self) -> str:
        return self.company or self.name

    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class LLMProvider(_Model):
    name: str
    base_url: str = Field(..., description="OpenAI-compatible base URL, e.g. https://api.groq.com/openai/v1")
    model: str
    api_key_env: str
    json_mode: bool = True
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(
        default_factory=dict, description="Extra request fields, e.g. {reasoning_effort: low}."
    )


class LLMConfig(_Model):
    enabled: bool = True
    providers: list[LLMProvider] = Field(default_factory=list)
    min_score: int = Field(60, ge=0, le=100)
    batch_size: int = Field(10, ge=1, le=40)
    max_calls_per_run: int = Field(30, ge=1)
    description_chars: int = Field(1800, ge=200)
    timeout_seconds: float = 90
    max_provider_failures: int = Field(
        2, ge=1, description="A provider that fails this many requests in a row is skipped for the rest of the run."
    )
    max_seconds_per_run: float = Field(
        600, gt=0, description="AI time budget of one run, checked between requests; after it the remaining jobs use keyword rules."
    )
    temperature: float = 0.1
    extra_instructions: str = ""


class TelegramConfig(_Model):
    enabled: bool = False
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    disable_preview: bool = True


class DiscordConfig(_Model):
    """A webhook posts alerts; a bot (token + channel id) also adds ⭐✅🗣❌ reactions that feed the tracker."""

    enabled: bool = False
    webhook_env: str = "DISCORD_WEBHOOK_URL"
    bot_token_env: str = "DISCORD_BOT_TOKEN"
    channel_id_env: str = "DISCORD_CHANNEL_ID"

    def bot_credentials(self) -> tuple[str, str] | None:
        """(bot token, channel id) when both secrets are set: the bot (reactions + tracker) replaces the webhook."""
        token = os.environ.get(self.bot_token_env, "").strip()
        channel = os.environ.get(self.channel_id_env, "").strip()
        return (token, channel) if token and channel else None


class EmailConfig(_Model):
    enabled: bool = False
    to: list[str] = Field(default_factory=list, description="Recipients (or set EMAIL_TO env var).")
    to_env: str = "EMAIL_TO"
    from_addr: str | None = None
    host_env: str = "SMTP_HOST"
    port_env: str = "SMTP_PORT"
    user_env: str = "SMTP_USER"
    password_env: str = "SMTP_PASSWORD"
    starttls: bool = True


class NtfyConfig(_Model):
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic_env: str = "NTFY_TOPIC"
    token_env: str = "NTFY_TOKEN"


class AppriseConfig(_Model):
    enabled: bool = False
    urls_env: str = "APPRISE_URLS"


class NotifyConfig(_Model):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    apprise: AppriseConfig = Field(default_factory=AppriseConfig)
    health_alert_after_failures: int = Field(2, ge=1)


class TrackerConfig(_Model):
    """Application tracker: Telegram buttons / Discord reactions on per-job messages, kept in tracker/applications.csv."""

    enabled: bool = Field(True, description="Buttons and sync (only with notify.telegram or a Discord bot).")
    follow_up_days: int = Field(
        14, ge=1, description="/pendientes also lists applications without changes for this many days."
    )


class FeedsConfig(_Model):
    enabled: bool = True
    max_items: int = 300
    title: str = "Job finder"


class HttpConfig(_Model):
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
    accept_language: str = "en-US,en;q=0.9,es;q=0.8,fr;q=0.7"
    timeout_seconds: float = 30
    max_per_host: int = 3
    retries: int = 2
    source_timeout_seconds: float = 240


class Config(_Model):
    version: int = 1
    timezone: str = "UTC"
    language: Literal["es", "en"] = "en"
    profile: str = Field("", description="Anonymous description of the candidate, used by the AI matcher.")
    role_families: list[RoleFamily] = Field(default_factory=list)
    filters: Filters = Field(default_factory=Filters)
    experience: Experience = Field(default_factory=Experience)
    locations: list[LocationSpec] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    groups: dict[str, Group] = Field(default_factory=dict)
    sources: list[Source] = Field(default_factory=list)
    llm: LLMConfig = Field(default_factory=lambda: LLMConfig(enabled=False))
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    feeds: FeedsConfig = Field(default_factory=FeedsConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        keys: set[str] = set()
        families = {f.name for f in self.role_families}
        for s in self.sources:
            if s.key in keys:
                raise ValueError(f"duplicate source id {s.key!r}; set a unique 'id'")
            keys.add(s.key)
            if s.group not in self.groups:
                raise ValueError(f"source {s.name!r} uses unknown group {s.group!r}")
            if s.favorite and "favorites" not in self.groups:
                raise ValueError(f"source {s.name!r} is a favorite but there is no 'favorites' group")
            if not s.url and not s.type:
                raise ValueError(f"source {s.name!r} needs a 'url' or a 'type'")
            for f in s.role_families or []:
                if f not in families:
                    raise ValueError(f"source {s.name!r} references unknown role family {f!r}")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def sources_for_group(self, group: str) -> list[Source]:
        out = []
        for s in self.sources:
            if not s.enabled:
                continue
            if s.group == group or (group == "favorites" and s.favorite):
                out.append(s)
        return out
