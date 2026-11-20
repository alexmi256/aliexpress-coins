#!/usr/bin/env python3
"""Comments for the 2026-11-20 (F) session: two more task families characterised."""

from __future__ import annotations

import subprocess
import tempfile

REPO = "alexmi256/aliexpress-coins"

C24 = """**The timer chip is a general dwell indicator — confirmed on a second family.**

`Learn to save money with Coins Credits` opens an explainer page ("What are Coins Credits?") that is
not a shop page at all, yet carries the same right-edge chip. Waiting for it to turn green and going
back paid **+1** (27 → 28) and the row disappeared from the roster.

So the executor's rule is uniform for the browse family: tap `GO` → poll for `\\b\\d+s\\b` near the
right edge → on disappearance, collect/return → verify the row's progress or removal. Nothing about
the destination page needs to be understood.
"""

C21 = """**Two more rows characterised 2026-11-20, and one new family that is not a dwell task.**

| task | destination | paid | roster after |
|---|---|---|---|
| `Learn to save money with Coins Credits` | explainer page ("What are Coins Credits?") | +1, timer chip | row removed |
| `Take the coins quiz` | quiz (one True/False) | +6 on a correct answer | row removed, `Learn to save money` returned to the top |

**`Search for what you love`** (+1, `0/2`, "Earn by using keywords to explore items") is different:
`GO` opens a search landing page (pre-filled query, `Search history`, category cards with item
counts), a card opens results, and **there is no timer chip** on either screen. Progress stayed
`0/2` after one search, so it is almost certainly "perform N searches", not "wait on a page".

Catalogue implications:

* the browse family's spec needs no page-specific knowledge — the chip is the contract;
* the search family needs its own executor and its own verification (perform a search, check the
  badge), because there is nothing to wait for;
* every completion above changed the roster, which is exactly what #43 exists to record.
"""


def gh(*args: str) -> None:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    print(f"ok: gh {' '.join(args[:3])}")


def main() -> None:
    for number, body in ((24, C24), (21, C21)):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
            path = handle.name
        gh("issue", "comment", str(number), "-R", REPO, "--body-file", path)


if __name__ == "__main__":
    main()
