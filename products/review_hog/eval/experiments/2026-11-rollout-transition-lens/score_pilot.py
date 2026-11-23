"""Summarize raw pilot results (findings, token usage, latency) for a run."""

from __future__ import annotations

import json
import argparse
import statistics
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent


def session_group(name: str) -> str:
    if name.startswith("main_"):
        return "main"
    if name.startswith("rollout_"):
        return "rollout"
    return "shipped_lens"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--run-id", default="run1")
    parser.add_argument("--split", default="tune")
    args = parser.parse_args()

    cases = json.loads((EXP_DIR / "cases.json").read_text(encoding="utf-8"))
    selected = cases["clean"] if args.split == "clean" else [c for c in cases["defects"] if c["split"] == args.split]
    run_root = Path(args.work_dir) / "runs" / args.run_id

    tokens_by_group: dict[str, list[int]] = {"main": [], "shipped_lens": [], "rollout": []}
    time_by_group: dict[str, list[float]] = {"main": [], "shipped_lens": [], "rollout": []}

    for c in selected:
        pr = c["pr"]
        pr_dir = run_root / f"pr_{pr}"
        print(f"\n================ PR #{pr} ({c.get('category', 'clean')}) ================")  # noqa: T201
        if "issues" in c:
            for iss in c["issues"]:
                print(f"  TARGET [{iss['id']}]: {iss['claim']}")  # noqa: T201
        if not pr_dir.exists():
            print("  (no results yet)")  # noqa: T201
            continue
        for rfile in sorted(pr_dir.glob("*.result.json")):
            r = json.loads(rfile.read_text(encoding="utf-8"))
            sname = r["session"]
            grp = session_group(sname)
            u = r["usage"]
            total_tok = u["input_tokens"] + u["output_tokens"]
            tokens_by_group[grp].append(total_tok)
            time_by_group[grp].append(r["elapsed_s"])
            findings = (r.get("parsed") or {}).get("findings") or [] if isinstance(r.get("parsed"), dict) else []
            print(  # noqa: T201
                f"  [{sname:16s}] rc={r['exit_code']} t={r['elapsed_s']:5.1f}s tok={total_tok:7d} (in={u['input_tokens']} cached={u['cached_input_tokens']} out={u['output_tokens']}) findings={len(findings)}"
            )
            for f in findings:
                print(f"      - [{f.get('priority')}] {f.get('file')}:{f.get('line_start')} | {f.get('title')}")  # noqa: T201
                print(f"        {f.get('body', '')}")  # noqa: T201

    print("\n================ COST & LATENCY SUMMARY ================")  # noqa: T201
    for grp in ("main", "shipped_lens", "rollout"):
        toks = tokens_by_group[grp]
        times = time_by_group[grp]
        if not toks:
            continue
        med_tok = int(statistics.median(toks))
        p95_tok = sorted(toks)[int(len(toks) * 0.95)] if len(toks) > 1 else toks[0]
        med_t = statistics.median(times)
        max_t = max(times)
        print(  # noqa: T201
            f"  {grp:14s}: n={len(toks):2d} | median_tok={med_tok:7d} | p95_tok={p95_tok:7d} | median_s={med_t:5.1f}s | max_s={max_t:5.1f}s"
        )


if __name__ == "__main__":
    main()
