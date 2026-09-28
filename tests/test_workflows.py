"""Workflow files: secrets are passed one by one and cover every env var the code reads by default."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from jobfinder.config.schema import NotifyConfig

ROOT = Path(__file__).parent.parent
WORKFLOWS = ["jobfinder.yml", "favorites.yml"]


def _default_env_names() -> set[str]:
    names = set()
    for channel in NotifyConfig().model_dump().values():
        if isinstance(channel, dict):
            names |= {v for k, v in channel.items() if k.endswith("_env")}
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    names |= {p["api_key_env"] for p in example["llm"]["providers"]}
    return names


def _run_env(workflow: str) -> dict[str, str]:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
    steps = data["jobs"]["run"]["steps"]
    return next(s for s in steps if "jobfinder" in s.get("run", "") and "env" in s)["env"]


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_secrets_are_not_serialised_at_once(workflow):
    # GitHub holds workflows that dump every secret at once as "possibly malicious" (they never run).
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert not re.search(r"toJSON\(\s*secrets\s*\)", text, re.I)


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_default_env_names_are_passed(workflow):
    env = _run_env(workflow)
    missing = _default_env_names() - set(env)
    assert not missing, missing
    for name, value in env.items():
        if value.startswith("${{ secrets."):
            assert value == f"${{{{ secrets.{name} }}}}", name
