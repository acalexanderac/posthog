import importlib.util
from dataclasses import dataclass
from pathlib import Path

import pytest

# The experiment folder name is not a valid package name, so the module is loaded by path.
_spec = importlib.util.spec_from_file_location("rollout_routing", Path(__file__).parent / "rollout_routing.py")
assert _spec and _spec.loader
_routing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_routing)

is_rollout_trigger_path = _routing.is_rollout_trigger_path
rollout_transition_scope_files = _routing.rollout_transition_scope_files
rollout_transition_trigger_files = _routing.rollout_transition_trigger_files


@dataclass
class _File:
    filename: str
    additions: int
    deletions: int = 0


def _file(filename: str, lines: int = 10) -> _File:
    return _File(filename=filename, additions=lines)


@pytest.mark.parametrize(
    "path",
    [
        "posthog/migrations/1440_rename_code_review_scopes.py",
        "posthog/clickhouse/migrations/0381_drop_legacy_metrics_on_logs.py",
        "products/cohorts/backend/migrations/0023_turn_on_realtime_for_processed_teams.py",
        "posthog/management/commands/backfill_insights_query_metadata.py",
        "products/logs/backend/destination_cutover.py",
        "products/tasks/backend/models.py",
        "posthog/models/team.py",
        "products/tasks/backend/temporal/process_task/workflow.py",
        "posthog/tasks/scheduled.py",
        "ee/billing/quota_limiting.py",
        "posthog/auth.py",
        "nodejs/src/session-replay/recording-api/auth.ts",
        "rust/feature-flags/src/redis.rs",
        "posthog/api/organization_access_cache.py",
    ],
)
def test_state_touching_paths_trigger_the_lens(path: str) -> None:
    assert is_rollout_trigger_path(path)


@pytest.mark.parametrize(
    "path",
    [
        # Excluded directories win over a matching file name.
        "frontend/src/scenes/billing/billing.ts",
        "posthog/api/test/test_auth.py",
        "docs/internal/migrations/0001_example.py",
        "posthog/management/migration_analysis/models.py",
        # Generated or query-only schemas are not storage contracts.
        "posthog/schema.py",
        "posthog/hogql/database/schema/sessions_v3.py",
        "services/mcp/schema/tool-inputs.ts",
        # Source connectors live under temporal/ but define no workflow.
        "products/warehouse_sources/backend/temporal/data_imports/sources/depot/schedule.py",
        # Not code.
        "posthog/migrations/max_migration.txt",
        "products/tasks/backend/migrations/README.md",
        # A trigger word must be a whole underscore-separated segment of the file name.
        "products/signals/backend/logic/author.py",
        "posthog/caching/utils.py",
        "products/endpoints/backend/materialization_transforms.py",
    ],
)
def test_other_paths_do_not_trigger_the_lens(path: str) -> None:
    assert not is_rollout_trigger_path(path)


def test_trigger_files_keep_the_input_order() -> None:
    paths = ["posthog/auth.py", "posthog/api/cohort.py", "posthog/migrations/0001_initial.py"]
    assert rollout_transition_trigger_files(paths) == ["posthog/auth.py", "posthog/migrations/0001_initial.py"]


def test_scope_is_empty_when_nothing_triggers() -> None:
    assert rollout_transition_scope_files([_file("posthog/api/cohort.py")]) == ([], [])


def test_scope_adds_same_directory_and_same_stem_files_but_not_tests_or_unrelated_files() -> None:
    files = [
        _file("products/cohorts/backend/models/cohort.py"),
        _file("products/cohorts/backend/models/util.py"),
        _file("posthog/api/cohort.py"),
        _file("posthog/api/test/test_cohort.py"),
        _file("posthog/api/insight.py"),
    ]
    triggers, scope = rollout_transition_scope_files(files)
    assert triggers == ["products/cohorts/backend/models/cohort.py", "products/cohorts/backend/models/util.py"]
    assert scope == [*triggers, "posthog/api/cohort.py"]


def test_scope_falls_back_to_the_triggers_when_they_already_exceed_the_line_budget() -> None:
    files = [
        _file("posthog/auth.py", lines=60),
        _file("posthog/quota.py", lines=60),
        _file("posthog/helpers.py", lines=1),
    ]
    triggers, scope = rollout_transition_scope_files(files, max_lines=100)
    assert triggers == scope == ["posthog/auth.py", "posthog/quota.py"]


def test_scope_skips_a_file_that_would_exceed_the_line_budget_and_keeps_adding_smaller_ones() -> None:
    files = [
        _file("posthog/billing/quota.py", lines=40),
        _file("posthog/billing/large_sibling.py", lines=80),
        _file("posthog/billing/small_sibling.py", lines=20),
    ]
    triggers, scope = rollout_transition_scope_files(files, max_lines=100)
    assert triggers == ["posthog/billing/quota.py"]
    assert scope == ["posthog/billing/quota.py", "posthog/billing/small_sibling.py"]
