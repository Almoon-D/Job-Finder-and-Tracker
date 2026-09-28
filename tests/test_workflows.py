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


def test_tracker_sync_mode_has_its_own_queue():
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "jobfinder.yml").read_text(encoding="utf-8"))
    on = data.get("on") or data.get(True)  # YAML 1.1 reads the key `on` as True
    assert "tracker-sync" in on["workflow_dispatch"]["inputs"]["mode"]["options"]
    # A queued tracker-sync must not replace a pending real run of the main queue.
    assert "tracker-sync" in data["concurrency"]["group"] and "main" in data["concurrency"]["group"]
    run_step = next(s for s in data["jobs"]["run"]["steps"] if s.get("name") == "Run")["run"]
    assert "tracker-sync) uv run --no-sync jobfinder tracker-sync --data-dir data" in run_step
