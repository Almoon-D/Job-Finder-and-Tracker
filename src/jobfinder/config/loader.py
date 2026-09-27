"""Load and validate the private configuration."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .schema import Config


class ConfigError(Exception):
    """Invalid or missing configuration.

    ``public`` is safe to print in public CI logs (no values from the config);
    ``detail`` may contain private values and must only go to private places.
    """

    def __init__(self, public: str, detail: str):
        super().__init__(public)
        self.public = public
        self.detail = detail


def _parse(text: str, origin: str) -> Config:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ConfigError(f"config is not valid YAML{where}", f"{origin}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping", origin)
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        public_lines = [
            f"  - at {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}" for err in exc.errors()
        ]
        public = f"config has {exc.error_count()} error(s):\n" + "\n".join(public_lines)
        raise ConfigError(public, f"{origin}:\n{exc}") from exc


def load_config(path: str | Path | None = None, data_dir: str | Path | None = None) -> Config:
    """Load config from an explicit path, the JOBFINDER_CONFIG env var, or <data_dir>/config.yaml."""
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError("config file not found", str(p))
        return _parse(p.read_text(encoding="utf-8"), str(p))
    env = os.environ.get("JOBFINDER_CONFIG", "").strip()
    if env:
        return _parse(env, "JOBFINDER_CONFIG")
    if data_dir:
        p = Path(data_dir) / "config.yaml"
        if p.exists():
            return _parse(p.read_text(encoding="utf-8"), str(p))
    raise ConfigError(
        "no config found: pass --config, set JOBFINDER_CONFIG or put config.yaml in the data dir",
        f"data_dir={data_dir}",
    )
