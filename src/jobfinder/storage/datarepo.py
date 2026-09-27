"""Commit and push changes of the private data repository (checked out in ./data)."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .. import log

_BOT = ["-c", "user.name=jobfinder-bot", "-c", "user.email=jobfinder-bot@users.noreply.github.com"]


def _git(data_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(data_dir), *args], capture_output=True, text=True, check=check)


def is_git_repo(data_dir: Path) -> bool:
    return (Path(data_dir) / ".git").exists()


def _commit(data_dir: Path, message: str) -> bool:
    _git(data_dir, "add", "-A")
    if _git(data_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(data_dir, *_BOT, "commit", "-q", "-m", message)
    return True


def commit_and_push(
    data_dir: Path,
    message: str,
    push: bool = True,
    rewrite: Callable[[], None] | None = None,
    attempts: int = 4,
) -> bool:
    """Commit all changes in the data repo and push them. Returns True if something was committed.

    If the push is rejected because another run pushed first, the local commit is
    dropped, the remote version is checked out and ``rewrite`` is called to write
    our changes again merged on top of it (see ``State.merge_with_disk``).
    Git output is never printed: it could contain private paths.
    """
    data_dir = Path(data_dir)
    if not is_git_repo(data_dir):
        log.info("data dir is not a git repository; state saved locally only")
        return False
    if not _commit(data_dir, message):
        log.info("data repo: no changes")
        return False
    if not push:
        log.info("data repo: committed (push disabled)")
        return True
    for attempt in range(1, attempts + 1):
        if _git(data_dir, "push", "-q", check=False).returncode == 0:
            log.info("data repo: pushed")
            return True
        log.info(f"data repo: push rejected (attempt {attempt}/{attempts}), merging with remote")
        time.sleep(attempt)
        _git(data_dir, "fetch", "-q", check=False)
        if rewrite is None:
            _git(data_dir, "pull", "-q", "--rebase", "-X", "theirs", check=False)
            continue
        _git(data_dir, "reset", "-q", "--hard", "@{u}", check=False)
        rewrite()
        if not _commit(data_dir, message):
            return True
    log.warn("data repo: could not push state after retries")
    return True
