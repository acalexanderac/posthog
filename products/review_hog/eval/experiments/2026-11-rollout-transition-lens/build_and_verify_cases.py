"""Build and verify cases.json programmatically from GitHub PR metadata.

Fetches exact `headRefOid` and `baseRefOid` directly from GitHub GraphQL for every defect and
clean-control PR, verifies zero missing SHAs, and writes verified `cases.json`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent

# Each defect entry specifies the introducing PR, verified public fix/revert PR, category,
# split (`tune`, `holdout_recent`, or `golden_set_overlap`), and concrete labeled claim(s).
# NOTE:
# - `tune` is used for Step 1 stop-rule checks and prompt iteration.
# - `holdout_recent` contains recent (Sep-Nov 2026) PRs kept untouched during prompt tuning.
# - `golden_set_overlap` contains older (2023-2026) public golden-set PRs scored separately
#   because older PRs may appear in model training data and some were already Deep hits.
DEFECT_SPECS = [
    # --- TUNE (recent Sep-Nov 2026 + 2 golden-set partials used to ground the problem) ---
    {
        "pr": 134867,
        "fix_pr": 136056,
        "category": "unbackfilled_legacy_rows",
        "split": "tune",
        "issues": [
            {
                "id": "134867-R1",
                "claim": "Existing behavioral cohorts saved before condition_type existed remain NULL with no backfill; once realtime evaluation trusts the column/readiness stamp on deploy, those existing cohorts evaluate dynamically and match nobody until manually resaved.",
            }
        ],
    },
    {
        "pr": 135345,
        "fix_pr": 137370,
        "category": "deploy_migration_hazard",
        "split": "tune",
        "issues": [
            {
                "id": "135345-R1",
                "claim": "posthog/migrations/1440_rename_code_review_scopes.py runs in the deploy hook and rewrites millions of rows on large OAuth tables (posthog_oauthapplication / oauth2_provider_accesstoken) with GIN indexes on scope, blocking production deploys.",
            }
        ],
    },
    {
        "pr": 134208,
        "fix_pr": 135120,
        "category": "deploy_migration_hazard",
        "split": "tune",
        "issues": [
            {
                "id": "134208-R1",
                "claim": "posthog/clickhouse/migrations/0381_drop_legacy_metrics_on_logs.py drops metrics2_backfill_series before dropping its dependent dictionary metrics2_backfill_series_dict, failing migrate_clickhouse in production and blocking deploys.",
            }
        ],
    },
    {
        "pr": 103937,
        "fix_pr": 105316,
        "category": "deploy_migration_hazard",
        "split": "tune",
        "issues": [
            {
                "id": "103937-R1",
                "claim": "posthog/migrations/1376_taggeditem_untrack_legacy_keys.py drops foreign keys to 13 parent tables in a single transaction, acquiring AccessExclusiveLock on posthog_taggeditem then parent tables in opposite order from live readers and deadlocking production deploys.",
            }
        ],
    },
    {
        "pr": 120180,
        "fix_pr": 120399,
        "category": "rolling_deploy_cutover",
        "split": "tune",
        "issues": [
            {
                "id": "120180-R1",
                "claim": "nodejs/src/session-replay/recording-api/auth.ts requires a scoped JWT on the new encrypt route with no legacy-secret fallback, so every caller fails with 401 in production until the JWT signing key is deployed.",
            }
        ],
    },
    {
        "pr": 83059,
        "fix_pr": 84891,
        "category": "unbackfilled_legacy_rows",
        "split": "tune",
        "issues": [
            {
                "id": "83059-R1",
                "claim": "Making the email step's From override load-bearing in resolveFromEmailAddress breaks existing production steps that already hold a stored placeholder sender address off the integration's verified domain, failing every send after deploy (INC-990).",
            }
        ],
    },
    # --- HOLDOUT (recent Sep-Nov 2026 only; pilot holdout — expand to >=25 before flag-on) ---
    {
        "pr": 82610,
        "fix_pr": 133109,
        "category": "deploy_migration_hazard",
        "split": "holdout_recent",
        "issues": [
            {
                "id": "82610-R1",
                "claim": "posthog/management/commands/backfill_insights_query_metadata.py locks each batch of insight rows during query-metadata extraction, blocking live user insight saves while the backfill runs in production.",
            }
        ],
    },
    {
        "pr": 136756,
        "fix_pr": 137598,
        "category": "rolling_deploy_cutover",
        "split": "holdout_recent",
        "issues": [
            {
                "id": "136756-R1",
                "claim": "products/logs/backend/destination_conversion.py retires legacy HogFunction destinations during cutover without draining or fencing already-queued legacy alert events, dropping in-flight notifications.",
            }
        ],
    },
    {
        "pr": 134689,
        "fix_pr": 138993,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_recent",
        "issues": [
            {
                "id": "134689-R1",
                "claim": "products/batch_exports/backend/custom_events_schema.py switches custom events schemas to recompile against events_json, breaking already-saved schemas created by users with property restrictions whose stored hogql_query was previously dropped.",
            }
        ],
    },
    # --- GOLDEN SET OVERLAP (older public incident PRs, scored separately due to training-data risk) ---
    {
        "pr": 72913,
        "fix_pr": 83804,
        "category": "unbackfilled_legacy_rows",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "72913-R1",
                "claim": "Moving secret HogFlow action inputs to an encrypted column without backfilling or migrating existing rows causes an ordinary re-save of a pre-existing legacy flow to drop its stored secret (INC-987).",
            }
        ],
    },
    {
        "pr": 19938,
        "fix_pr": 20232,
        "category": "first_run_cache_quota",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "19938-R1",
                "claim": "ee/billing/quota_limiting.py moves already over-quota tokens into a new grace-period Redis set on the first post-deploy run while capture still reads the old limiter set, lifting quota enforcement on existing over-quota orgs at deploy time (INC-154).",
            }
        ],
    },
    {
        "pr": 25129,
        "fix_pr": 25199,
        "category": "first_run_cache_quota",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "25129-R1",
                "claim": "posthog/tasks/scheduled.py registers day/night cohort beat entries by passing a five-field cron string to crontab() as the minute argument instead of get_crontab, raising during Celery beat boot and leaving all later periodic tasks unregistered on deploy (INC-242).",
            }
        ],
    },
    {
        "pr": 36834,
        "fix_pr": 37167,
        "category": "rolling_deploy_cutover",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "36834-R1",
                "claim": "SessionAuthentication enforces 2FA on orgs with enforce_2fa enabled without exempting SSO-authenticated sessions, which never set the 2FA verified flag, locking existing SSO users into 403s on deploy (INC-496).",
            }
        ],
    },
    {
        "pr": 17466,
        "fix_pr": 19110,
        "category": "deploy_migration_hazard",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "17466-R1",
                "claim": "posthog/clickhouse/migrations/0049_add_inserted_at_projection.py adds a SELECT * projection and materializes it for the live events partition during deploy-time migration, causing heavy production cluster load (INC-120).",
            }
        ],
    },
    {
        "pr": 39299,
        "fix_pr": 39497,
        "category": "deploy_migration_hazard",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "39299-R1",
                "claim": "products/tasks/backend/migrations/0006_remove_workflowstage_agent_alter_task_workflow_and_more.py edits an already-applied Django migration instead of creating a new migration, so production databases never apply the schema change (INC-542).",
            }
        ],
    },
    {
        "pr": 55662,
        "fix_pr": 56117,
        "category": "inflight_workflow_replay",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "55662-R1",
                "claim": "products/tasks/backend/temporal/process_task/workflow.py schedules a new Temporal activity ahead of an existing one without workflow.patched(), causing replay nondeterminism for already in-flight workflows on deploy (INC-815).",
            }
        ],
    },
    {
        "pr": 44198,
        "fix_pr": 46696,
        "category": "rolling_deploy_cutover",
        "split": "golden_set_overlap",
        "issues": [
            {
                "id": "44198-R1",
                "claim": "posthog/auth.py adds a phs_ guard in the shared personal-API-key lookup that raises instead of returning None, breaking existing callers authenticating with legacy secret tokens on local-evaluation endpoints (INC-702).",
            }
        ],
    },
]

# Diverse hard-negative clean controls that exercise the same trigger categories safely.
CLEAN_SPECS = [
    {
        "pr": 124637,
        "why_safe": "Pure additive nullable column (hard_bounced_at) on MessageSuppression; no reader depends on it yet.",
    },
    {
        "pr": 131161,
        "why_safe": "Stage 2 additive Task status columns + dual-write save hook after commit; nothing reads the new columns yet.",
    },
    {
        "pr": 125374,
        "why_safe": "Populates hard_bounced_at on suppression writers while #124638 keeps legacy reason-text fallback for older rows.",
    },
    {
        "pr": 131165,
        "why_safe": "Flag-gated task-done rollout (off/shadow/enforce, default off) with safe rollback and Temporal patching.",
    },
    {
        "pr": 127699,
        "why_safe": "Additive supports_step_up field on OAuth token introspection; ID-JAG tokens default safely to false.",
    },
    {
        "pr": 109984,
        "why_safe": "Properly sequenced PSAK backfill migration that explicitly waited for #111078 compatibility fixes to land first.",
    },
    {
        "pr": 124425,
        "why_safe": "Additive new ProviderImportRun model and initial table migration; no pre-existing rows affected.",
    },
    {
        "pr": 126927,
        "why_safe": "Small bounded repin migration for deprecated Profound API v1 sources.",
    },
    {
        "pr": 128353,
        "why_safe": "Clean removal of already-retired in-workflow Trino shadow comparison in Temporal workflow.",
    },
    {
        "pr": 129897,
        "why_safe": "New Temporal onboarding reminder workflow/activity; does not mutate existing in-flight workflow histories.",
    },
    {
        "pr": 127259,
        "why_safe": "Explicitly scoped operator management command to resend weekly digest for chosen org IDs.",
    },
    {
        "pr": 127455,
        "why_safe": "Bounded ranking update for error tracking backfill without altering persisted schema or lock scope.",
    },
]


def fetch_github_metadata(pr_numbers: list[int]) -> dict[int, dict]:
    fields = "\n".join(
        f"pr{n}: pullRequest(number: {n}) {{ number title mergedAt headRefOid baseRefOid additions deletions changedFiles files(first: 100) {{ nodes {{ path }} }} }}"
        for n in pr_numbers
    )
    query = f'query {{ repository(owner: "PostHog", name: "posthog") {{ {fields} }} }}'
    out = subprocess.check_output(["gh", "api", "graphql", "-f", f"query={query}"], text=True, encoding="utf-8")
    raw = json.loads(out)["data"]["repository"]
    return {n: raw[f"pr{n}"] for n in pr_numbers}


def main() -> None:
    all_nums = sorted({c["pr"] for c in DEFECT_SPECS} | {c["pr"] for c in CLEAN_SPECS})
    meta = fetch_github_metadata(all_nums)

    defects_out = []
    for spec in DEFECT_SPECS:
        m = meta[spec["pr"]]
        assert m and len(m["headRefOid"]) == 40 and len(m["baseRefOid"]) == 40, f"Bad SHAs for #{spec['pr']}"
        defects_out.append(
            {
                "pr": spec["pr"],
                "fix_pr": spec["fix_pr"],
                "title": m["title"],
                "merged_at": m["mergedAt"][:10],
                "head_sha": m["headRefOid"],
                "base_sha": m["baseRefOid"],
                "category": spec["category"],
                "split": spec["split"],
                "changed_files": m["changedFiles"],
                "additions": m["additions"],
                "deletions": m["deletions"],
                "issues": spec["issues"],
            }
        )

    clean_out = []
    for spec in CLEAN_SPECS:
        m = meta[spec["pr"]]
        assert m and len(m["headRefOid"]) == 40 and len(m["baseRefOid"]) == 40, f"Bad SHAs for #{spec['pr']}"
        clean_out.append(
            {
                "pr": spec["pr"],
                "title": m["title"],
                "merged_at": m["mergedAt"][:10],
                "head_sha": m["headRefOid"],
                "base_sha": m["baseRefOid"],
                "changed_files": m["changedFiles"],
                "additions": m["additions"],
                "deletions": m["deletions"],
                "why_safe": spec["why_safe"],
            }
        )

    payload = {"defects": defects_out, "clean": clean_out}
    out_path = EXP_DIR / "cases.json"
    out_path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    print(f"Wrote verified {out_path}: {len(defects_out)} defects, {len(clean_out)} clean controls.")  # noqa: T201


if __name__ == "__main__":
    main()
