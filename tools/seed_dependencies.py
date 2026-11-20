#!/usr/bin/env python3
"""Add native "blocked by" edges between issues.

GitHub renders these as `blocked by #N` on the issue and in the project. The dependency
list mirrors the `## Dependencies` section of each body; this script is the machine-side
source of truth, so re-running it is how the graph is corrected.
"""

from __future__ import annotations

import json
import subprocess
import sys

REPO = "alexmi256/aliexpress-coins"

# issue number -> issues that must land first
EDGES: dict[int, list[int]] = {
    # perception chain
    13: [12],
    14: [13],
    15: [12],
    16: [13],
    # control chain
    17: [12],
    18: [17, 13, 15],
    19: [12, 16],
    20: [17, 21],
    # tasks
    21: [33],
    22: [14, 18, 19, 21],
    23: [22, 19, 30],
    24: [22, 18, 19, 11],
    25: [10, 22, 18],
    26: [10, 24, 18],
    27: [7, 22],
    28: [10, 22],
    # operations
    29: [34, 24],
    30: [33],
    31: [12],
    32: [20, 29],
    # delivery
    34: [33],
    35: [33, 34],
    36: [13, 17],
    37: [33],
    # new in session 2
    38: [14, 18, 22],
    39: [25],
    40: [31],
    41: [37, 32],
}


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"FAILED gh {' '.join(args)}: {result.stderr.strip()[:200]}", file=sys.stderr)
        raise SystemExit(1)
    return result.stdout


def main() -> None:
    added = skipped = 0
    for blocked, blockers in EDGES.items():
        current = json.loads(gh("api", f"repos/{REPO}/issues/{blocked}/dependencies/blocked_by"))
        have = {item["number"] for item in current}
        for blocker in blockers:
            if blocker in have:
                skipped += 1
                continue
            blocker_id = json.loads(gh("api", f"repos/{REPO}/issues/{blocker}", "--jq", "{\"id\": .id}"))["id"]
            gh(
                "api", "-X", "POST",
                f"repos/{REPO}/issues/{blocked}/dependencies/blocked_by",
                "-F", f"issue_id={blocker_id}",
            )
            added += 1
            print(f"#{blocked} blocked by #{blocker}")
    print(f"\nadded {added} edges, {skipped} already present")


if __name__ == "__main__":
    main()
