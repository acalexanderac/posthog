# PLAN: rollout-transition lens for changes that meet live production state

Status: revised after a 2-model adversarial review (Claude Opus 5.5 + Codex GPT-6.2 Sol); ready for Step 1 baseline on `tune` and expansion of the unseen holdout before any flag decision.

**Question:** on a PR that changes files meeting live production state (migrations, persisted models/schemas, durable workflows, scheduled jobs, backfill commands, billing/quota enforcement, auth/secret verification, or cache/Redis layers), does Standard with a rollout-transition lens find deploy-time and existing-state failures that Standard alone misses, without adding noise, exceeding shipped-lens token cost, or displacing Standard's findings?

Motivation: Recommendation 3 of [`2026-09-incident-golden-set/FINAL_REPORT.md`](../2026-09-incident-golden-set/FINAL_REPORT.md) noted that ReviewHog reasons well about steady-state code and poorly about the rollout transition (*"what does the first run after deploy see that the steady state does not"* — INC-990, INC-987, INC-154, INC-242, plus INC-120 and INC-542). Recent fix and revert commits in `PostHog/posthog` (Sep–Nov 2026) confirm the same failure classes still ship to production.

## Surface & architecture

- **Primary surface: Standard (Flash v2) routed lens** — mirrors `DEVEX_LENS` (`2026-11-17` own-lane design) so it cannot displace Standard findings or add cost to unrelated PRs:
  - Prompt file: `products/review_hog/backend/reviewer/prompts/single_agent_review/lens_rollout_transition.md`
  - Router module: `products/review_hog/backend/reviewer/rollout_routing.py`
  - Constant: `ROLLOUT_TRANSITION_LENS = FlashLens(pass_number=2004, prompt_file="lens_rollout_transition.md", source="flash-lens-rollout-transition")` (NOT added to `FLASH_LENSES`, which feeds every turn's fan-out, fingerprint, and `COORDINATOR_MAX_SUBAGENTS`)
  - PR-keyed feature flag: `reviewhog-rollout-transition-lens` (default off; matched control `rollout_transition_lens_routed` resolved even when flag is off)
  - Own lane: posts up to `ROLLOUT_TRANSITION_LENS_MAX_FINDINGS = 2` (must-fix included) outside the turn cap and PR finding budget, and never blocks or fails the turn if its session or dedup fails.
- **Why not a Deep perspective skill first?** Standard runs automatically on PRs; Deep is opt-in and uses a separate DB-synced skill system (`products/review_hog/skills/`). If the Standard lens passes the bars below, a matching canonical Deep perspective (`review-hog-perspective-rollout-transition`) is a clean follow-up.

### Plumbing, replay safety, and multi-lane interaction

- **Temporal replay safety (additive only — never rename):** keep `ReviewMeta.devex_lens`, `devex_lens_routed`, `SandboxStageInput.devex_lens`, `PRSnapshot.devex_lens_files`, and `lens_inputs["devex_lens"]` byte-identical for recorded histories and snapshots. Add `rollout_transition_lens`, `rollout_transition_lens_routed`, `rollout_transition_lens_files` with defaults, gate workflow branching behind a new `workflow.patched("flash-rollout-transition-lens-2026-12")` before the meta read, and append the new unit *after* `devex` in `lens_units`. Widen the pass-number filter (`activities.py:1934-1938`) and budget exclusion (`finding_budget.py:129`) to the set of optional-lane sources.
- **Two optional lanes on one PR cannot double-post:** e.g., a `products/review_hog/backend/temporal/` PR triggers both `devex` and `rollout_transition`. Optional-lane dedup must anchor the second lane against `[*main, *lens, *first_optional_lane]` (or run one ordered optional-lane dedup call), and run the restore-to-lane (`single_agent_review.py:914-940`) and same-lane priority raise (`:597`) per lane.
- **Coordinator, delta turns, follow-ups, and concurrency:**
  - Coordinated PRs (`reviewhog-standard-coordinator`) do not run optional lanes today; confirm with `team-devex` what share of PRs is coordinated before wiring a coordinator slot.
  - Delta turns skip optional lanes (`workflow.py:735`); on non-delta follow-ups, consider running the lens only when the trigger set changed since the last reviewed head to avoid repeating work on unrelated pushes.
  - `MAX_CONCURRENT_SANDBOXES = 10` (`constants.py:475`): `main + up to 8 lens parts + devex + rollout = 11`, so the rollout session queues last on very large PRs; track p95 latency and timeouts explicitly.

### Existing repo safeguards & what stays out of scope

- PostHog CI already runs `analyze_migration_risk --fail-on-blocked` (`.depot/workflows/ci-backend.yml`) covering hot-table ALTERs, non-concurrent indexes, `RunSQL`/`RunPython` review flags, and FK-drop lock ordering (`LockPhaseTransactionPolicy`). The lens prompt instructs the reviewer to skip generic static DDL lock lints already enforced by CI and focus on data semantics, dependency ordering, management-command backfills, legacy persisted state, mixed-version cutovers, and workflow replay safety.
- Deterministic checks worth proposing as CI rules separately from the LLM lens: editing an already-applied migration (`status == "modified"` on `migrations/*.py`, `#39299`), ClickHouse drop dependency ordering (`#134208`), and static `workflow.patched()` checks.

## Deterministic routing & bounded diff scope

Implemented in [`products/review_hog/backend/reviewer/rollout_routing.py`](../../../backend/reviewer/rollout_routing.py) and measured reproducibly by [`measure_router.py`](./measure_router.py):

- **Trigger (`rollout_transition_trigger_files`):** pure Python, zero network/LLM calls, repo-agnostic. Excludes docs, tests, snapshots, generated files (`services/mcp/schema/`, `posthog/schema.py`, `hogql/database/schema/`), frontend-only trees (`frontend/`, `products/*/frontend/`), `migration_analysis/`, and non-workflow `temporal/data_imports/sources/`. Matches migrations, management/backfill/cutover commands, persisted models & backend schemas, durable workflow/schedule modules, and whole-word quota/billing/auth/secret/encrypt/redis/cache modules.
- **Bounded scope (`rollout_transition_scope_files`):** starts from `trigger_files` and adds non-test changed files in the same directory or sharing a filename stem with a trigger, **capped at `FLASH_LENS_CHUNK_MAX_LINES` (600 lines)** (past the cap, `trigger_files` only). Because `SingleAgentPrompt` always includes the full PR file list and read-only git access, the lens can follow state from trigger files into sibling service files without paying a whole-PR diff prompt on 100-file PRs.
- **Prompt scope line override:** for this lens, override `prompt.jinja`'s `"Separate sessions review the other changed files"` with `"Start from these files; follow the persisted state, keys, or contracts they touch into any other changed file in the PR file list and report there."`
- **Reproducible measurement (`measure_router.py` on `master` as of 2026-11-22):**
  - Last 500 first-parent `master` commits: **68/500 (13.6%)** routed, median 1 trigger file, p95 = 5.
  - `holdout_unseen`: **17/29 (59%)** routed, 14/25 in the five rollout classes. This is the out-of-sample recall; the 12 unrouted cases count as lens misses. Misses include defects in `posthog/schema.py` (excluded as generated), Rust services, frontend-persisted state, and backend logic files whose names carry no trigger word. Do not tune the router on these cases.
  - Pilot `cases.json` defects (tune, holdout_recent, golden_set_overlap): **17/17 (100%)** *(note: in-sample on the initial mining batch; out-of-sample router recall must be verified on fresh unseen holdout PRs before flag-on).*

## Step 1: is there a gap at all (tune split only)

Run Standard as it is today (`main` + 2 shipped lenses) on the **`tune` split only**, twice. Never touch `holdout_recent` for this decision.

- Count **full, published mechanism matches** (same root cause on the same code path); report partials separately.
- **Stop rule:** if both `tune` runs reach ≥70% full published matches, stop the broad pilot and narrow to the specific sub-classes Standard misses. If the two runs straddle 70%, add more `tune` cases before deciding.

## Case set (`cases.json`) & leakage hygiene

Generated and verified programmatically against GitHub GraphQL (`headRefOid` + `baseRefOid`) by [`build_and_verify_cases.py`](./build_and_verify_cases.py). Every case is public `PostHog/posthog` data with an explicit `fix_pr` (for defects) or `why_safe` rationale (for clean controls).

- **Four explicit defect buckets (46 cases):**
  1. **`tune` (6 recent PRs):** used for Step 1 stop rule and prompt iteration (`#134867`, `#135345`, `#134208`, `#103937`, `#120180`, `#83059`).
  2. **`holdout_recent` (3 recent Sep–Nov 2026 PRs, pilot seed):** `#82610`, `#136756`, `#134689`. **Before any live flag decision**, freeze the prompt after `tune` iteration and expand `holdout_recent` to **≥25 fresh, unseen Sep–Nov 2026 fix/revert pairs** spanning all five rollout classes (including quota/cache and workflow-replay) that were never inspected while writing the prompt or router.
  3. **`holdout_unseen` (29 Jul–Nov 2026 PRs, mined 2026-11-23 with the prompt frozen at `4beee8a94`):** built from fix PR descriptions, not from reading the introducing PRs. Funnel: 11,872 fix/revert PRs merged since 2026-07-01 → 8,517 read by a small model (every Sep–Nov fix with a description; Jul–Aug keyword matches only) → 267 marked rollout-transition → 74 confirmed by a stronger model reading the fix beside up to five candidate introducing PRs (from the fix description and from blame of the fixed lines) → 46 with an introducing PR whose diff holds the faulty change → 37 after removing cases already in this file and PRs over 100 files → **29 after a hand check** of each claim against the fix description and the introducing diff. 25 fall in the five rollout classes; 4 are `deploy_interrupts_inflight_work` (a deploy restarts workers and in-flight work is lost), scored as their own group. The plan asked for Sep–Nov only; that window gave 22 cases, so 7 with a Jul–Aug introducing PR were added and are reported separately. Funnel recall on the 8 recent known cases: the keyword step kept 6, the small model kept 6 of 6.
  4. **`golden_set_overlap` (8 older public incident PRs, 2023–early 2026):** `#72913`, `#19938`, `#25129`, `#36834`, `#17466`, `#39299`, `#55662`, `#44198`. Scored as their own group because 2023–2025 PRs may sit in model training data and `#55662`/`#44198` were prior Deep hits.
- **Hard-negative clean controls (12 PRs):** diverse safe rollout changes exercising the same triggers — additive nullable column (`#124637`), dual-write columns (`#131161`), writer migration with fallback (`#125374`), flag-gated rollout (`#131165`), additive OAuth compat (`#127699`), properly sequenced PSAK backfill (`#109984`), additive model (`#124425`), bounded repin (`#126927`), safe workflow cleanup (`#128353`), new workflow (`#129897`), scoped operator command (`#127259`), bounded backfill ranking (`#127455`).
- **Memorization channel closed:** preflight verifies every `head_sha` and `base_sha` and runs every session in an isolated shallow checkout containing only `base_sha`..`head_sha` (no `origin/master`, no later commits). Later fix PRs sit strictly in the judge's evidence packet.

## Arms & scoring protocol

| Arm     | What runs                                                                 |
| ------- | ------------------------------------------------------------------------- |
| Control | Standard on the code-default arm set (`main` + 2 shipped lenses)          |
| Lens    | Same control sessions + `lens_rollout_transition.md` in its own lane      |

- Both arms share the same control sessions, production publish threshold, production dedup rules, and turn/lane caps.
- Two runs per arm; report both runs, paired PR-level deltas (wins / ties / losses by PR), and blind-judge disagreements. Local pilot uses `SingleAgentPrompt` + `codex exec` read-only on `SINGLE_AGENT_FLASH_ARM`; final validation runs through `manage.py run_review` with `team-devex`.

## Bars

Frozen before the first lens run (pilot validates mechanics on `tune` + seed `holdout_recent`; **live flag-on requires passing all bars on the expanded ≥25-case unseen holdout**):

| Bar                                                                          | Needed                                                                                                          |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Labeled issues found (full published mechanism match, paired by PR, 2 runs)  | Lens ≥20 points above control on the unseen holdout, with net positive paired PR wins on both runs              |
| Precision on **new posted root causes** from the lens (blind judge)          | ≥70% valid and minor or worse (report numerator/denominator; minimum 10 new posted root causes to pass)         |
| Clean controls (12 hard negatives)                                           | At most +0.5 extra posted findings/PR vs. control, AND report count of clean PRs receiving any invalid/overstated finding |
| Control findings displaced or priority-changed                               | 0 displaced and 0 priority-changed on shared findings (enforced structurally by own-lane; verified empirically) |
| Incremental token cost (same routed PRs, including dedup)                    | Median lens session tokens ≤ median shipped lens session; report median, p95, and total incremental cost        |
| Latency & timeouts                                                           | p95 lens session wall time ≤ 10 min (`FLASH_LENS_SESSION_TIMEOUT`), timeout rate ≤ 5%                           |
| Must-fix (P1) calibration (reviewer trust)                                   | ≥75% of lens `must_fix` findings judged major or worse by blind judge (report numerator/denominator; min n=4)   |

A lens that fails a bar does not turn its flag on.

## Open questions for `team-devex` before implementation PR

1. What share of Standard PRs currently runs on `reviewhog-standard-coordinator` (which skips optional lanes today)?
2. Should optional routed lanes be generalized across `activities.py`/`workflow.py`/`single_agent_review.py` now, or implemented alongside `DEVEX_LENS` and generalized once one ships?
3. Should ReviewHog pass the GitHub "Migration risk" check status/output (when completed at fetch time) into the lens prompt to prevent duplicate migration-lock comments?
4. Should deterministic checks (edited already-applied migrations, ClickHouse drop dependency order) be split into a dedicated CI gate rather than an LLM review pass?
