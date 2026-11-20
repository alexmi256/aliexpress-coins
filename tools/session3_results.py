#!/usr/bin/env python3
"""Post the 2026-11-20(C) results: entry ordering, the surprise flow, the notification task.

The #38 comment here also retracts the "am start does not navigate a running app" claim from the
previous session, which a trial series disproved.
"""

from __future__ import annotations

import subprocess
import tempfile

REPO = "alexmi256/aliexpress-coins"

COMMENTS: dict[int, str] = {
    38: """**Entry ordering is now pinned by trial, and one earlier claim is retracted.**

| sequence | `Earn more` result | trials |
|---|---|---|
| `am force-stop` → fire the shortcut intent (intent cold-starts the app) | **empty** ("There's nothing here yet") | 4/4 |
| `am start -n …FirstActivity` → settle ~10 s → fire the intent | **populated** (full list) | 2/2 |
| app already warm → fire the intent | **populated** | 3/3 |

So the intent is a **navigation** command, not a **launch** command. Using it as the cold start is
what produced the stuck page — and it is exactly what a naive nightly runner would do, because it
looks like the cheapest way in.

**Retraction:** the earlier comment here said `am start` does not navigate a running app. That was
a single observation taken right after a BACK and it does not hold — the warm path navigated 5/5 in
the trials above.

Consequence for the runner:

1. start `FirstActivity`, wait for the screen to settle (~10 s on the reference device);
2. fire the shortcut intent (`aliexpress://goto?url=…`) to get the coin page;
3. tap `Earn more`;
4. assert the boost page is populated (`Coins boost` + ≥1 task title); if it is empty, that is a
   fault — re-enter via 1–3 once, then report.

The `Earn more` tap itself is fine; it is the entry that decides the outcome.
""",
    26: """**Walked end to end 2026-11-20 — the flow is now known.**

Row: `Browse surprise items` / "Tap 3 items on this page to earn coins" / progress `0/2` / `+1`.

1. `GO` opens a page headed `Browse surprise items` with the same subtitle, a **counter** (`0/3`),
   and a product grid.
2. Tapping a product tile opens the normal product detail page (`AIProductDetailActivity`).
3. **BACK to the grid increments the counter.** The tap alone does nothing; the return is the
   scoring event.
4. At three distinct items the grid is replaced by a **`Next round`** button (~597,1240).
5. `Next round` resets the counter to `0/3` on a fresh grid. Two rounds = the `0/2` badge.
6. **Duplicates are rejected.** Re-tapping round 1's tiles in round 2 left the counter at `0/3`;
   only previously unopened products moved it (`2/3`).

Balance moved 13 → 15 mid-flow, so partial progress pays — the executor does not need all-or-nothing
per round.

Design notes that follow:

* the executor must scroll the grid: the visible tiles are not enough for three distinct items plus
  a second round, and re-opening one silently costs a tap with no counter change;
* "did it count?" is observable (counter text), so the executor can verify each tap instead of
  assuming;
* the round counter is the completion signal — `0/2` → `1/2` after round one.
""",
    28: """**Observed 2026-11-20 — and it did not behave as designed.**

The row exists (`Turn on notifications` / "Stay informed when rewards drop" / `+1`). Tapping its
`GO` credited **+1** (balance 15 → 16) with **no system permission dialog**, and the app still does
not hold the permission afterwards:

```
adb shell cmd appops get com.alibaba.aliexpresshd POST_NOTIFICATION
Uid mode: POST_NOTIFICATION: ignore
```

Three possible readings, and they matter for the executor:

1. the reward is granted on the tap, and the permission is irrelevant (app bug / lax task) — then
   the executor needs no dialog handling at all;
2. the prompt was shown once earlier in the device's life and "remembered" — then a fresh install
   behaves differently and the executor must handle the dialog;
3. my tap landed on a different row's `GO` (the list re-renders and rows shift) — then the +1 came
   from something else entirely.

Worth one clean re-test on a fresh state before this executor is written: with the app's
notification permission explicitly revoked, tap the row and screenshot within ~1 s (the dialog, if
any, appears immediately). If no dialog appears, the executor is trivial; if one does, it needs a
system-dialog branch that the app's own close logic cannot see.
""",
}


def gh(*args: str) -> None:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    print(f"ok: gh {' '.join(args[:3])}")


def main() -> None:
    for number, body in COMMENTS.items():
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
            path = handle.name
        gh("issue", "comment", str(number), "-R", REPO, "--body-file", path)


if __name__ == "__main__":
    main()
