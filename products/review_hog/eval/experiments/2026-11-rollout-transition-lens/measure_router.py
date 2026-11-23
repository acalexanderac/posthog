"""Reproducible measurement of rollout_routing.py on PostHog master commits and cases.json."""

from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))

# Direct module load to avoid Django app initialization when running standalone.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "rollout_routing",
    REPO_ROOT / "products/review_hog/backend/reviewer/rollout_routing.py",
)
assert _spec and _spec.loader
# Inject minimal stub for constants & PRFile only if Django/pydantic isn't in standalone python env.
try:
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    rollout_transition_trigger_files = _mod.rollout_transition_trigger_files
except Exception:
    import types
    from dataclasses import dataclass

    @dataclass
    class _PRFile:
        filename: str
        additions: int = 0
        deletions: int = 0

    for pkg in [
        "products",
        "products.review_hog",
        "products.review_hog.backend",
        "products.review_hog.backend.reviewer",
        "products.review_hog.backend.reviewer.models",
    ]:
        sys.modules.setdefault(pkg, types.ModuleType(pkg))
    c_mod = types.ModuleType("products.review_hog.backend.reviewer.constants")
    c_mod.FLASH_LENS_CHUNK_MAX_LINES = 600  # type: ignore[attr-defined]
    sys.modules["products.review_hog.backend.reviewer.constants"] = c_mod
    gm_mod = types.ModuleType("products.review_hog.backend.reviewer.models.github_meta")
    gm_mod.PRFile = _PRFile  # type: ignore[attr-defined]
    sys.modules["products.review_hog.backend.reviewer.models.github_meta"] = gm_mod

    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    rollout_transition_trigger_files = _mod.rollout_transition_trigger_files


def fetch_pr_files(pr_numbers: list[int]) -> dict[int, list[str]]:
    fields = "\n".join(
        f"pr{n}: pullRequest(number: {n}) {{ files(first: 100) {{ nodes {{ path }} }} }}" for n in pr_numbers
    )
    query = f'query {{ repository(owner: "PostHog", name: "posthog") {{ {fields} }} }}'
    out = subprocess.check_output(["gh", "api", "graphql", "-f", f"query={query}"], text=True, encoding="utf-8")
    raw = json.loads(out)["data"]["repository"]
    return {n: [f["path"] for f in raw[f"pr{n}"]["files"]["nodes"]] for n in pr_numbers}


def main() -> None:
    cases = json.loads((Path(__file__).resolve().parent / "cases.json").read_text(encoding="utf-8"))
    defect_nums = [c["pr"] for c in cases["defects"]]
    clean_nums = [c["pr"] for c in cases["clean"]]
    pr_files = fetch_pr_files(defect_nums + clean_nums)

    print("=== ROUTER ON DEFECT CASES ===")  # noqa: T201
    by_split: dict[str, list[bool]] = {}
    for c in cases["defects"]:
        hits = rollout_transition_trigger_files(pr_files[c["pr"]])
        by_split.setdefault(c["split"], []).append(bool(hits))
        print(  # noqa: T201
            f"  {'HIT ' if hits else 'MISS'} [{c['split']:18s}] #{c['pr']:6d} ({c['category']}): {len(hits)} triggers -> {hits[:2]}"
        )
    for split, vals in by_split.items():
        print(f"  Summary [{split}]: {sum(vals)}/{len(vals)} ({100 * sum(vals) / len(vals):.1f}%)")  # noqa: T201

    print("\n=== ROUTER ON CLEAN CONTROLS ===")  # noqa: T201
    clean_hits = 0
    for c in cases["clean"]:
        hits = rollout_transition_trigger_files(pr_files[c["pr"]])
        if hits:
            clean_hits += 1
        print(f"  {'HIT ' if hits else 'MISS'} #{c['pr']:6d}: {len(hits)} triggers -> {hits[:2]}")  # noqa: T201
    print(f"  Clean controls routed: {clean_hits}/{len(cases['clean'])}")  # noqa: T201

    print("\n=== ROUTER ON LAST 500 FIRST-PARENT MASTER COMMITS ===")  # noqa: T201
    out = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "log", "--first-parent", "-n", "500", "--name-only", "--format=COMMIT:%H"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    commits: list[list[str]] = []
    cur: list[str] | None = None
    for line in out.splitlines():
        if line.startswith("COMMIT:"):
            if cur is not None:
                commits.append(cur)
            cur = []
        elif line.strip() and cur is not None:
            cur.append(line.strip())
    if cur is not None:
        commits.append(cur)

    routed = 0
    trigger_counts: list[int] = []
    for files in commits:
        hits = rollout_transition_trigger_files(files)
        if hits:
            routed += 1
            trigger_counts.append(len(hits))
    trigger_counts.sort()
    med = trigger_counts[len(trigger_counts) // 2] if trigger_counts else 0
    p95 = trigger_counts[int(len(trigger_counts) * 0.95)] if trigger_counts else 0
    print(f"  Routed: {routed}/{len(commits)} ({100 * routed / len(commits):.1f}%), median triggers={med}, p95={p95}")  # noqa: T201


if __name__ == "__main__":
    main()
