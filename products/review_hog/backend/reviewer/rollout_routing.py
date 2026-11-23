"""Deterministic, repo-agnostic routing for the Standard rollout-transition lens.

Decides whether a PR touches files where new code meets pre-existing production state
(migrations, persisted models/schemas, durable workflows, scheduled jobs, backfill/migration
commands, billing/quota enforcement, auth/secret verification, or cache/Redis layers).

Zero network calls and zero LLM calls: runs purely on changed file paths (and file status when
available). Kept free of repository-specific names so it works across any repository ReviewHog
reviews.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from products.review_hog.backend.reviewer.constants import FLASH_LENS_CHUNK_MAX_LINES
from products.review_hog.backend.reviewer.models.github_meta import PRFile

# Non-code, test, generated, doc, and UI-only paths never trigger the rollout-transition lens.
_EXCLUDED_DIR_PARTS = frozenset(
    {
        "docs",
        "doc",
        "tests",
        "test",
        "__tests__",
        "__snapshots__",
        "mocks",
        "mock",
        "fixtures",
        "fixture",
        "stories",
        "frontend",
        "generated",
        "migrations_analysis",
        "migration_analysis",
    }
)

_EXCLUDED_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Generated or query-only schema files that do not represent persisted storage contracts
    re.compile(r"(^|/)services/mcp/schema/"),
    re.compile(r"(^|/)hogql/database/schema/"),
    re.compile(r"^posthog/schema\.py$"),
    # Source-connector packages under temporal/ that are not workflow definitions
    re.compile(r"(^|/)temporal/data_imports/sources/"),
    # Test, snapshot, doc, and config/text extensions
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(
        r"(_test\.py|_test\.go|\.test\.(ts|tsx|js|jsx)|\.spec\.(ts|tsx|js|jsx)|"
        r"\.stories\.(ts|tsx|js|jsx)|\.ambr|\.snap|\.md|\.txt|\.yml|\.yaml|\.json|\.lock)$"
    ),
)

_CODE_EXT_RE = re.compile(r"\.(py|ts|js|rs|go|sql|prisma|hcl)$")

# Generic, repo-agnostic path triggers for rollout-transition risk.
_TRIGGER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Database & warehouse migrations (Django, ClickHouse, Alembic, Prisma, Rails, raw SQL)
    re.compile(r"(^|/)migrations/[^/]+\.(py|sql|ts|js|rb)$"),
    re.compile(r"(^|/)alembic/versions/[^/]+\.py$"),
    re.compile(r"(^|/)prisma/migrations/"),
    # Management / operational backfill & data-migration commands
    re.compile(r"(^|/)management/commands/[^/]+\.py$"),
    re.compile(r"(^|/)[^/]*(backfill|cutover)[^/]*\.(py|ts|js|rs|go|sql)$"),
    # Persisted models & backend storage/wire schemas (not generated or query schemas)
    re.compile(r"(^|/)models\.py$"),
    re.compile(r"(^|/)models/[^/]+\.py$"),
    re.compile(r"(^|/)schema/[^/]+\.(py|ts|js)$"),
    re.compile(r"(^|/)schemas?\.(py|ts)$"),
    # Durable workflow definitions & periodic schedules
    re.compile(r"(^|/)temporal/(.*/)?(workflows?|schedules?)/[^/]+\.(py|ts|go)$"),
    re.compile(r"(^|/)temporal/.*(workflow|workflows|schedule|schedules)[^/]*\.(py|ts|go)$"),
    re.compile(r"(^|/)(workflow|workflows|schedules|beat_schedule|scheduled)\.py$"),
    # Quota, billing, authentication/secrets, and explicit cache/Redis modules (whole-word segments)
    re.compile(
        r"(^|/)([^/]*_)?(quota|billing|auth|authenticator|authentication|secret|encrypt|redis|cache)(_[^/]*)?\.(py|ts|rs|go)$"
    ),
)


def is_rollout_trigger_path(path: str) -> bool:
    """Whether a single repository-relative path is a rollout-transition trigger file."""
    posix = PurePosixPath(path)
    if any(part in _EXCLUDED_DIR_PARTS for part in posix.parts[:-1]):
        return False
    if any(rx.search(path) for rx in _EXCLUDED_PATH_PATTERNS):
        return False
    if not _CODE_EXT_RE.search(path):
        return False
    return any(rx.search(path) for rx in _TRIGGER_PATTERNS)


def rollout_transition_trigger_files(paths: Sequence[str]) -> list[str]:
    """Changed paths that trigger the rollout-transition lens, preserving input order."""
    return [path for path in paths if is_rollout_trigger_path(path)]


def _stem_key(path: str) -> str:
    return PurePosixPath(path).stem.lower()


def _parent_dir(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _changed_lines(pr_file: PRFile) -> int:
    return pr_file.additions + pr_file.deletions


def rollout_transition_scope_files(
    pr_files: Sequence[PRFile],
    *,
    max_lines: int = FLASH_LENS_CHUNK_MAX_LINES,
) -> tuple[list[str], list[str]]:
    """Return `(trigger_files, scope_files)` for a Standard rollout-transition lens session.

    `trigger_files` are the matched rollout-risk paths. When empty, the lens does not run.
    `scope_files` starts from `trigger_files` and adds non-test changed files in the same directory
    or sharing a filename stem with a trigger, as long as the combined diff stays within `max_lines`
    (matching the shipped lens chunk budget). Past the cap, `scope_files` stays at `trigger_files`
    so the session stays within the shipped-lens token budget while still seeing the full PR file
    list to follow state into sibling files via git.
    """
    triggers = [f for f in pr_files if is_rollout_trigger_path(f.filename)]
    if not triggers:
        return [], []

    trigger_names = [f.filename for f in triggers]
    trigger_set = set(trigger_names)
    trigger_dirs = {_parent_dir(f.filename) for f in triggers}
    trigger_stems = {_stem_key(f.filename) for f in triggers if len(_stem_key(f.filename)) >= 4}

    total_lines = sum(_changed_lines(f) for f in triggers)
    if total_lines > max_lines:
        return trigger_names, trigger_names

    scope = list(trigger_names)
    for f in pr_files:
        if f.filename in trigger_set:
            continue
        posix = PurePosixPath(f.filename)
        if any(part in _EXCLUDED_DIR_PARTS for part in posix.parts[:-1]):
            continue
        if any(rx.search(f.filename) for rx in _EXCLUDED_PATH_PATTERNS):
            continue
        if _parent_dir(f.filename) in trigger_dirs or _stem_key(f.filename) in trigger_stems:
            lines = _changed_lines(f)
            if total_lines + lines <= max_lines:
                scope.append(f.filename)
                total_lines += lines

    return trigger_names, scope
