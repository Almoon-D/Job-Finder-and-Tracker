"""Privacy-aware logging.

GitHub Actions logs of public repositories are public. Anything that could reveal
the user's interests (company names, job titles, search queries, URLs) must never
be printed there. Code therefore uses two functions:

* ``info()``   – safe, generic messages (counts, source indexes, adapter types).
* ``detail()`` – private details; printed only when running locally with --verbose.
"""

from __future__ import annotations

import os
import sys

_verbose = False


def in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI", "").lower() in ("1", "true")


def set_verbose(value: bool) -> None:
    global _verbose
    _verbose = value


def details_allowed() -> bool:
    return _verbose and not in_ci()


def info(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    prefix = "::warning::" if in_ci() else "WARNING: "
    print(prefix + msg, file=sys.stderr, flush=True)


def detail(msg: str) -> None:
    if details_allowed():
        print("  · " + msg, file=sys.stderr, flush=True)
