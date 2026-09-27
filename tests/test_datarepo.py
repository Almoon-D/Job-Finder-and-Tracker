"""commit_and_push against real git repositories (a bare remote and two clones)."""

from __future__ import annotations

import subprocess
from datetime import timedelta

from jobfinder.models import Job
from jobfinder.state import State
from jobfinder.storage.datarepo import commit_and_push

from .conftest import NOW


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def setup_repos(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "config.yaml").write_text("x: 1\n")
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "HEAD:main")
    clones = []
    for name in ("a", "b"):
        path = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True, capture_output=True)
        clones.append(path)
    return remote, clones


def test_no_changes_no_commit(tmp_path):
    _, (a, _) = setup_repos(tmp_path)
    assert commit_and_push(a, "msg") is False


def test_concurrent_push_is_merged(tmp_path):
    _, (a, b) = setup_repos(tmp_path)
    sa, sb = State(a), State(b)
    ja, jb = Job("s", "1", "PB", "https://x/1", "Acme"), Job("s", "2", "IR", "https://x/2", "Acme")
    sa.observe(ja, NOW)
    sa.mark_notified(ja, "favorites", NOW)
    sa.save()
    assert commit_and_push(a, "run a")

    sb.observe(jb, NOW)
    sb.mark_notified(jb, "company_sites", NOW + timedelta(minutes=1))
    sb.save()

    def rewrite():
        sb.merge_with_disk()
        sb.save()

    assert commit_and_push(b, "run b", rewrite=rewrite)  # first push is rejected, then merged
    git(a, "pull", "-q")
    final = State(a)
    assert final.notified_in(ja, "favorites") and final.notified_in(jb, "company_sites")
