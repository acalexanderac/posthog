# FINAL REPORT: rollout-transition lens pilot

**Question:** on a PR that changes files meeting live production state, does Standard with a rollout-transition lens find deploy-time and existing-state failures that Standard alone misses, without adding noise or cost beyond a shipped lens?

**Short answer:** no, not on the model this pilot ran on. The lens finds real rollout bugs on PRs it has never seen, but Standard alone found every one of them too.
The lens is not wired into the review workflow and has no flag. This PR adds the prompt, the router, an unseen test set and this report.

- On 29 unseen PRs that each shipped a rollout bug, the router sends 17 to the lens. The lens found the bug on 8 and 7 of them in two runs.
- Standard alone (the main session and the two shipped lenses) found the bug on all 8 of the PRs where the lens found it. The lens added no catch of its own.
- On a second model, Grok 4.7, the lens found one bug that Standard missed on 8 PRs, and Standard found one that the lens missed.
- The router is the larger loss. It never shows the lens 12 of the 29 bugs.
- On 12 safe PRs the lens wrote 3 findings in each run, which is 0.25 per PR against a bar of 0.5.
- A lens session costs less than a shipped lens session: 149k against 183k median tokens on the same 8 PRs.

This is a local pilot on a different model than the product uses. See [Limits](#limits).

## Setup

- **Sessions.** [`run_pilot.py`](./run_pilot.py) rendered the main, shipped-lens and rollout-lens prompts for each PR with the product's prompt files.
  Each session ran read-only through the Claude CLI on `claude-fable-5-2` at medium effort, in a shallow checkout that holds only the PR's base and head commits.
  The fix for each bug is therefore not reachable from the session.
- **Control** is the main session and the two shipped lenses. **Treatment** adds the rollout-transition lens session, which runs only when the router matches a changed file.
- **Prompt.** Frozen at commit `4beee8a94` before the unseen set was mined. Nothing in this report was used to change the prompt or the router.
- **Judging.** Each finding was matched by hand to the case's labeled bug with the rubric of [`2026-09-incident-golden-set/JUDGE_RUBRIC.md`](../2026-09-incident-golden-set/JUDGE_RUBRIC.md): hit, partial or miss.
  Opus 5.5 then judged the same findings without tools, pooled across arms in random order and without arm names. It gave the same number of hits in every arm and differed on four partial calls.
  This report counts hits only.
- **Not simulated:** the product's dedup, publish threshold and finding cap. A finding counts when a session wrote it.

## The unseen set

29 PRs merged July to November 2026, each paired with the later PR that fixed the bug it introduced. [`PLAN.md`](./PLAN.md) has the funnel.
In short: a small model read 8,517 fix PR descriptions, a stronger model checked the 267 it marked against the PRs that last touched the fixed lines, and each surviving case was read by hand against the fix and the introducing diff.

| Group                                | Cases | Sent to the lens by the router |
| ------------------------------------ | ----- | ------------------------------ |
| The five rollout classes of the plan | 25    | 14                             |
| A deploy restarts workers mid-task   | 4     | 3                              |
| All                                  | 29    | 17                             |

The second group is outside what the prompt describes. It is scored on its own.

The 12 PRs the router does not match carry the bug in `posthog/schema.py` (excluded as generated, 2 cases), in Rust services (2), in frontend code (2), in a source connector under `temporal/data_imports/sources/` (1), and in backend files whose names hold no trigger word (5).
On the 17 pilot cases the router was built from, it matched 17.

## What the lens found

Hits on the 17 routed PRs, `claude-fable-5-2`, two runs:

| Group                              | Routed | Run 1 | Run 2 |
| ---------------------------------- | ------ | ----- | ----- |
| The five rollout classes           | 14     | 8     | 7     |
| A deploy restarts workers mid-task | 3      | 0     | 0     |
| All 29 cases, unrouted as missed   | 29     | 8     | 7     |

- Seven PRs were hits in both runs. The eighth, `#86493`, was a hit in run 1 only.
- The hits: `#86493`, `#94017`, `#95132`, `#98358`, `#99183`, `#104566`, `#116696`, `#127448`.
- Routed and missed in both runs: `#83784`, `#99917`, `#106933`, `#113788`, `#117313`, `#131167` (partial), and the three worker-restart cases `#122005`, `#122437`, `#133413`.
- On the five PRs used to write the prompt, the lens hit in 8 of 10 sessions. On unseen routed PRs in the same classes it hit in 15 of 28.

## Against Standard alone

Control ran once, on the 8 PRs where the lens had a hit. A PR the lens missed cannot be a lens-only catch, so control was not run on the other 21.

| Model              | Lens hits | Standard hits        | Hit by the lens only |
| ------------------ | --------- | -------------------- | -------------------- |
| `claude-fable-5-2` | 8 of 8    | 8 of 8               | 0                    |
| Grok 4.7, medium   | 5 of 8    | 5 of 8, main session | 1 (`#95132`)         |

- **Claude.** 54 control sessions. The main session found six of the eight. `#95132` and `#127448` were each found by one shipped-lens session, the second at the lowest priority.
- **Grok.** Run through the Cursor agent CLI with the same prompts and checkouts. The table compares the lens with the main session on the 8 PRs. On `#95132`, where only the lens hit, the six shipped-lens sessions were also run and all missed. The main session hit `#86493`, which the lens missed. Neither hit `#127448`.

These 8 PRs were chosen because the Claude lens hit on them, so they favor the lens.

## Safe PRs

12 PRs with a stated reason why the rollout is safe. The router matches 10.
The lens wrote 3 findings in each run, on 3 and 2 of the PRs, none of them must-fix.
Whether those findings are valid was not judged. Two of the three PRs drew a finding in both runs.

## Cost

Median per session on `claude-fable-5-2`:

| Session                            | Sessions | Tokens | Seconds |
| ---------------------------------- | -------- | ------ | ------- |
| Rollout lens, the 8 PRs, two runs  | 16       | 149k   | 46      |
| Rollout lens, all 17 PRs, two runs | 34       | 172k   | 61      |
| A shipped lens session, the 8 PRs  | 46       | 183k   | 51      |
| Main, the 8 PRs                    | 8        | 384k   | 91      |

- On the 8 PRs the lens session adds 8% to the tokens of the review.
- The 95th percentile lens session took 128 seconds, and no session timed out.
- The prompt asks for at most four batched steps and small lookups. On the five tuning PRs that cut the median from 302k to 179k tokens with the same hits.
- Grok did not follow those instructions: its lens sessions had a median of 1,188k tokens and 375 seconds.

Token counts include cache reads. [`2026-07-prompt-caching`](../2026-07-prompt-caching/INVESTIGATION.md) shows why they overstate cost.

## Against the bars in the plan

| Bar                                                         | Result                                                                    |              |
| ----------------------------------------------------------- | ------------------------------------------------------------------------- | ------------ |
| Labeled issues found: lens at least 20 points above control | 0 points on Claude; 1 lens-only hit on Grok among 8 PRs                   | **Fails**    |
| New root causes valid and minor or worse: at least 70%      | Not judged                                                                | Not measured |
| Safe PRs: at most 0.5 more findings per PR                  | 0.25, before dedup                                                        | Passes       |
| Control findings displaced or priority-changed: 0           | The lens ran as a separate session; dedup was not simulated               | Not measured |
| Lens tokens: at most a shipped lens session                 | 149k against 183k on the same 8 PRs                                       | Passes       |
| 95th percentile at most 10 minutes, timeouts at most 5%     | 128 seconds, 0 of 54 lens sessions                                        | Passes       |
| Must-fix findings judged major or worse: at least 75%       | No severity judge. 6 of the 7 must-fix findings are hits on a shipped bug | Not measured |

The plan's first step, a baseline of Standard on the tuning set with a stop rule at 70%, was not run before the lens was built.
The control result on the unseen PRs is what that step was meant to detect.

## Limits

- **Not the production model or path.** Sessions ran through the Claude CLI and, for the second model, the Cursor agent CLI. The product's Standard arm uses a different model, and nothing ran through `manage.py run_review`. The golden-set report that motivated this lens measured the product's model.
- **Control covers 8 of the 29 PRs**, once. The share of all 29 bugs that Standard finds is not known.
- **The Grok control is partial.** The shipped lenses ran on one PR. On the other seven the comparison is with the main session alone.
- **The labels come from fix PR descriptions.** A bug whose fix did not explain the cause is not in the set. Each case was read by one person.
- **July and August were searched less.** Only fix PRs matching a keyword list were read there; September to November was read in full.
- **The judge is one person and one model of the same family as the reviewer.**
- **No dedup, validity or severity judging**, so three bars are not measured.
- **Small sets.** 17 routed PRs and two runs. Read a difference of one PR as noise.
- **The safe PRs are safe by argument**, not by a later absence of incidents.

## What this supports

- A strong reviewer already reasons about rollout transitions on these PRs. On that reviewer the lens repeats Standard and adds cost.
- The lens may add a little on a weaker reviewer. One PR in eight on one run does not show it.
- A path-based router reaches 59% of rollout bugs. Five of the twelve it misses sit in ordinary backend files that no file-name rule would pick out.
- The unseen set is reusable. It is 29 public PRs merged after June 2026 with a labeled deploy-time bug each, and the mining steps are in the plan.

Before any wiring, run both arms through `run_review` on the unseen set. If Standard finds most of these bugs there as well, drop the lens and keep the set as a regression check for the main prompt.
