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
# - `holdout_unseen` contains Jul-Nov 2026 pairs mined from fix PR descriptions after the prompt and router
#   were frozen; never use it to adjust either.
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
    # --- HOLDOUT UNSEEN (Jul-Nov 2026 fix/revert pairs mined after the prompt and router were frozen) ---
    {
        "pr": 70552,
        "fix_pr": 70799,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "70552-R1",
                "claim": "posthog/schema.py makes FunnelsQuery.series a union discriminated on `kind` (generated from FunnelsQuerySeriesNodeUnion in schema-general.ts), where kind-less items used to be coerced to EventsNode. Saved funnel insights whose stored series items have no `kind` now fail validation with union_tag_not_found when they are loaded.",
            }
        ],
    },
    {
        "pr": 72741,
        "fix_pr": 128856,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "72741-R1",
                "claim": "rust/personhog-leader/src/service.rs adds `assert_writeable`, which refuses any record whose `created_at` is below 0, on the assumption that every legitimately created person falls in [1970, 9999]. Persons already in Postgres with a pre-1970 `created_at` (year 1, from a client's zero DateTime) are refused as not writeable on every property write.",
            }
        ],
    },
    {
        "pr": 73266,
        "fix_pr": 75717,
        "category": "rolling_deploy_cutover",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "73266-R1",
                "claim": "rust/personhog-test-harness/src/scenarios/traffic.rs calls seed::cleanup_team, a delete of every row for the team, at startup, at each epoch's pool rotation and at shutdown. When a rolling restart briefly runs an old and a new traffic-bed pod against the same team, either pod deletes the other's live person pool, so in-flight writes fail with not-found and the dying pod's close-out reports false consistency violations.",
            }
        ],
    },
    {
        "pr": 81193,
        "fix_pr": 124417,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "81193-R1",
                "claim": "posthog/hogql/compiler/bytecode.py now raises QueryError when a name is used inside its own non-lambda declaration (`let x := f(x)`). Evaluation.save() recompiles the stored Hog source on every save, so for evaluations saved earlier with that pattern every save raises ValidationError and rolls back, including status-only set_status, pause and delete.",
            }
        ],
    },
    {
        "pr": 83784,
        "fix_pr": 86483,
        "category": "rolling_deploy_cutover",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "83784-R1",
                "claim": "posthog/settings/base_variables.py adds a per-process MANAGED_WAREHOUSE_DYNAMIC_SQL_EDITOR_AUTH_ENABLED env gate (default false), which provisioning in web workers and recovery/cleanup tasks in Celery workers each read independently. When the deployed web and task workers carry different values for this setting, one side creates static org_root sources while the other expects or schedules dynamic generation-fenced setup, so provisioning and recovery choose different authentication modes.",
            }
        ],
    },
    {
        "pr": 86477,
        "fix_pr": 99706,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "86477-R1",
                "claim": "products/signals/backend/implementation_pr.py adds pr_bearing_task_run_filter, which keys the SQL exclusion on the run's state.ai_stage stamp and lets runs without a stamp through, while the Python reader excludes by the association's run type. So on existing research tasks whose runs predate the stamp and recorded a PR URL, the has_implementation_pr and unclaimed filters count the report as having a PR even though the report reader shows none.",
            }
        ],
    },
    {
        "pr": 86493,
        "fix_pr": 91685,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "86493-R1",
                "claim": "posthog/api/email_verification.py decides the email-change code target with a truthiness check on `user.is_email_verified`, both in `send_code` and in `check_code`'s `expected_target`. For existing accounts from before email verification, where `is_email_verified` is NULL, the expected target becomes '' and the email-change code can never match, so the change cannot complete.",
            }
        ],
    },
    {
        "pr": 94017,
        "fix_pr": 95554,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "94017-R1",
                "claim": "products/signals/backend/implementation_pr.py makes close_implementation_pr_for_report look up only the new SignalReportAssignment row and return early when it is missing or has no pr_url. The same PR's webhook matching and PR-checks lookup also key on assignment repository and pr_number, so existing reports whose PR is linked only through task runs (which were never backfilled into assignments) are no longer closed on dismissal, matched by merge/close webhooks, or given PR checks.",
            }
        ],
    },
    {
        "pr": 95132,
        "fix_pr": 95623,
        "category": "rolling_deploy_cutover",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "95132-R1",
                "claim": "products/signals/backend/scout_harness/runner.py now dispatches every scout with `posthog_mcp_scopes=scope_posture`, a dict, instead of the preset string. A sandbox worker still on the previous deploy has `PosthogMcpScopes = McpScopePreset | list[str] | ScoutScopePosture`, so Temporal decodes the dict as `list[str]` of its keys. The token is then minted with only `preset` and `extra_write_scopes`, and the run loses every scout and read scope.",
            }
        ],
    },
    {
        "pr": 98358,
        "fix_pr": 100003,
        "category": "inflight_workflow_replay",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "98358-R1",
                "claim": "products/tasks/backend/temporal/process_task/workflow.py makes the `agent_state_changed` signal set `_turn_ended_received`, which wakes `_wait_for_task_external_event` and makes the run loop `continue` into a fresh wait, without any `workflow.patched` guard. When a ProcessTaskWorkflow started before the rollout replays an `agent_state_changed(False)` signal, it now issues commands its history never recorded, so replay fails with a nondeterminism error.",
            }
        ],
    },
    {
        "pr": 99183,
        "fix_pr": 100938,
        "category": "deploy_migration_hazard",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "99183-R1",
                "claim": "posthog/clickhouse/migrations/0322_add_snapshot_mode_v2_to_session_replay_events.py runs CHECK_REPLAY_SCHEMA `throwIf` guards on every data and ingestion node before any DDL. On existing ClickHouse tables that still hold the legacy `snapshot_mode` column or a `snapshot_source` aggregate of a different type, the guard throws and the migration runner stops.",
            }
        ],
    },
    {
        "pr": 99917,
        "fix_pr": 100779,
        "category": "deploy_migration_hazard",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "99917-R1",
                "claim": "posthog/migrations/1353_drop_retired_ducklake_tables.py runs a raw `DROP TABLE IF EXISTS posthog_ducklakecatalog, posthog_ducklakebackfill` under only `SET LOCAL lock_timeout = '2s'`. The drop takes ACCESS EXCLUSIVE one relation at a time on the hot parents those tables' foreign keys reference (posthog_team, posthog_organization, posthog_user), so on a live database its lock order crosses that of concurrent multi-table application reads, and Postgres's deadlock detector aborts those reads as victims.",
            }
        ],
    },
    {
        "pr": 101691,
        "fix_pr": 112659,
        "category": "deploy_interrupts_inflight_work",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "101691-R1",
                "claim": "products/warehouse_sources/backend/temporal/data_imports/sources/metronome/metronome.py adds `_parallel_usage_pages`, which commits the customer checkpoint only after the `yield` returns, advances it only when a batch is yielded, and blocks on `future.result()`. When a deploy drains the worker in the middle of a usage sync, the pipeline ends the attempt after writing a batch, so that batch's checkpoint and any customers with no usage are never recorded, and each new attempt restarts at the first customer until the resume retries run out.",
            }
        ],
    },
    {
        "pr": 104566,
        "fix_pr": 127889,
        "category": "deploy_migration_hazard",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "104566-R1",
                "claim": "posthog/clickhouse/migrations/0364_sessions_v3_more_ad_ids.py creates the v3 ingestion MVs, which now emit `uniq` states, on top of whatever v3 table already exists off cloud. On self-hosted installs that created raw_sessions_v3 before the shape change, the table still has `uniqExact` columns, so creating the MV fails and migrate_clickhouse stops at 0364.",
            }
        ],
    },
    {
        "pr": 106933,
        "fix_pr": 107735,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "106933-R1",
                "claim": "products/signals/backend/scout_harness/config_registry.py selects setup-flow pauses with `status_changed_by__isnull=True`, and the resume lock check refuses rows with a `status_changed_by_id`. The existing operational scout rows that the old setup flow paused over MCP carry the user in `status_changed_by`, so `resume_setup_paused_operational_scouts` selects none of them and they stay paused.",
            }
        ],
    },
    {
        "pr": 113788,
        "fix_pr": 114754,
        "category": "rolling_deploy_cutover",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "113788-R1",
                "claim": "products/review_hog/backend/internal_features.py makes has_internal_features evaluate the new per-project `review-hog-internal` feature flag, a missing flag reads as off, and automatic Flash reviews, the label trigger, manual Flash, resolution, Inbox reviews and the tiered arms are gated on it. Those parts previously ran for the internal project through the REVIEWHOG_TEAM_IDS gate, so in a production where the flag had never been created they all stopped silently after deploy.",
            }
        ],
    },
    {
        "pr": 115963,
        "fix_pr": 120726,
        "category": "rolling_deploy_cutover",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "115963-R1",
                "claim": "posthog/schema.py removes the `voice_mode` field from `MaxUIContext`, a model that forbids extra fields. Browser tabs still running the pre-deploy frontend keep sending `ui_context.voice_mode`, so their chat messages fail validation in the conversation serializers with HTTP 400 until the tab is reloaded.",
            }
        ],
    },
    {
        "pr": 116696,
        "fix_pr": 118764,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "116696-R1",
                "claim": "products/billing_alerts/backend/presentation/serializers.py stops accepting and returning minimum_value, evaluation_delay_hours and cooldown_hours, while the model columns and billing's legacy hourly evaluator keep reading them. Billing alerts saved earlier with non-default values keep changing evaluation, and their owners can no longer see or reset those values.",
            }
        ],
    },
    {
        "pr": 117313,
        "fix_pr": 118540,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "117313-R1",
                "claim": "products/today/backend/logic/highlight_sources/routine.py ranks insights only by the new `view_days` array and requires 8 distinct view days in the last 14, or 3 distinct weeks. Migration 1400 adds that column as nullable with no backfill, so existing FileSystemViewLog rows, including habitual users' recent views, read as `days or []` and score 0, leaving every routine pulse tile empty for about two weeks after deploy.",
            }
        ],
    },
    {
        "pr": 118657,
        "fix_pr": 129745,
        "category": "first_run_cache_quota",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "118657-R1",
                "claim": "products/engineering_analytics/backend/logic/views/incremental_floor.py turns the NULL `{incremental_window_start}` of a full build into an empty-string floor (`ifNull(..., '')`), so the first build of the stored CI views scans the team's entire existing job history in one ClickHouse query. On a busy repository's job history, that build exceeds the 600 second query limit and never completes.",
            }
        ],
    },
    {
        "pr": 120169,
        "fix_pr": 128241,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "120169-R1",
                "claim": "products/endpoints/backend/materialization_transforms.py now makes `_usage_rejection` reject variables used inside multi-argument calls such as `concat('x-', {variables.item})`, which analysis used to accept. But `build_endpoint_hogql` in the same file discards the rejection, so endpoint versions already materialized with those shapes are rebuilt before each run without the transform, and the refresh fails with `Unable to resolve field: variables` on the raw placeholders.",
            }
        ],
    },
    {
        "pr": 121086,
        "fix_pr": 125617,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "121086-R1",
                "claim": "products/workflows/frontend/Broadcasts/broadcastsLogic.ts adds a required `tags` field to the persisted `BroadcastsFilters` and reads `values.filters.tags.length`, but filters saved in the browser before this change have no `tags` and are restored as-is. On those stored filters, the URL handling and the tag picker dereference undefined, and the broadcasts list crashes.",
            }
        ],
    },
    {
        "pr": 122005,
        "fix_pr": 124043,
        "category": "deploy_interrupts_inflight_work",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "122005-R1",
                "claim": "products/warehouse_sources/backend/temporal/data_imports/person_property_backfill_job.py now catches the final ActivityError, including a WorkerShutdown from a deploy, and runs record_failed_person_property_backfill_runs_activity, which records a failed run through record_failed_runs. Each backfill a deploy stops mid-run therefore fails on its single attempt and counts toward consecutive_failures, so routine deploys can auto-disable a valid source.",
            }
        ],
    },
    {
        "pr": 122437,
        "fix_pr": 125911,
        "category": "deploy_interrupts_inflight_work",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "122437-R1",
                "claim": "products/aeo/backend/runner.py now runs up to AEO_MAX_PROMPTS_PER_RUN (2,000) prompts in a single task that takes hours, with the time limit in tasks.py raised to 10 hours, while beat still dispatches it only once a day. So when a deploy restarts the long-running Celery workers mid-run, the in-flight task dies, nothing dispatches it again that day, and most prompts go unchecked.",
            }
        ],
    },
    {
        "pr": 126129,
        "fix_pr": 129537,
        "category": "rolling_deploy_cutover",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "126129-R1",
                "claim": "frontend/src/queries/schema/schema-general.ts makes `WebVitalsItem.action` optional because the runner no longer writes it, and the PR drops `action` from every trends result row. Toolbar bundles already shipped with older posthog-js releases find each web vitals metric by `results[].action.custom_name`, so on those sites the `/web_vitals` panel throws `a.action is undefined` and shows nothing.",
            }
        ],
    },
    {
        "pr": 127448,
        "fix_pr": 131753,
        "category": "first_run_cache_quota",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "127448-R1",
                "claim": "products/tasks/backend/logic/services/compute_settlement.py adds an hourly sweep whose `_overdue_ended` selects every ended, user-attributed SandboxSession with null `compute_settled_at` and `compute_settle_conflict_at`, with no lower bound on `ended_at`. Migration 0145 creates those columns as null on every existing row, so the first sweep after GATEWAY_COMPUTE_DEBIT_ENABLED is turned on posts a compute debit to the gateway ledger for every historical sandbox session.",
            }
        ],
    },
    {
        "pr": 131167,
        "fix_pr": 131942,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "131167-R1",
                "claim": "products/warehouse_sources/backend/logic/source_credentials_rollout.py drops the job_inputs fallback, so a read takes the OAuth pointer only from the credentials_integration_id column and raises SourceCredentialsMissingError when that column is empty. On existing non-stripped OAuth sources that never had the column written and keep no pointer in job_inputs (Google Ads, HubSpot), syncs and schema discovery fail.",
            }
        ],
    },
    {
        "pr": 133413,
        "fix_pr": 137939,
        "category": "deploy_interrupts_inflight_work",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "133413-R1",
                "claim": "products/managed_warehouse/backend/tasks/tasks.py registers run_trino_compile_check_task as a single long_running Celery task with default early acks that checks every model of a team in one execution. When a deploy replaces the worker mid-run, the already-acked task is never redelivered, so the run never emits trino_compile_check_completed.",
            }
        ],
    },
    {
        "pr": 133491,
        "fix_pr": 134496,
        "category": "unbackfilled_legacy_rows",
        "split": "holdout_unseen",
        "issues": [
            {
                "id": "133491-R1",
                "claim": "products/signals/backend/activity_summary/prompt.py sets MIN_PROMPT_VERSION = 9, equal to the current PROMPT_VERSION, and render.py likewise sets MIN_RENDER_VERSION = 13. Stored summaries still on prompt 8 or render 12, left over from the version bump merged the day before, therefore fall below the floor, are forced into an immediate full rebuild instead of aging in, and stop being served when that rebuild skips.",
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
