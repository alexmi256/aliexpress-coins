#!/usr/bin/env python3
"""File the roster-observation issue and record the timer/sign-in findings on the existing ones."""

from __future__ import annotations

import subprocess
import tempfile

REPO = "alexmi256/aliexpress-coins"

BODY = """## Context

Measured 2026-11-20: **the task roster changes while a run is in progress.** Two observations, hours
apart, on the same account:

* `Browse surprise items` was replaced by `Search for what you love` between page loads.
* After completing the boost row `Daily check-in` (+1), **`Take the coins quiz` appeared at the top
  of the boost list** — it had not been there on the previous capture, and the balance moved 20 → 21.

So enumerating the list once per run misses tasks that only appear after another one completes, and
it gives no record of what the app actually offers over time.

## Scope

- **Re-enumerate after every task**, not once per run: capture the roster after each completion or
  collection and diff it against the previous capture.
- **Log every roster snapshot** to `runs/<ts>/roster.jsonl`: timestamp, task titles, subtitles,
  rewards, progress badges, and what changed since the previous snapshot (`added` / `removed` /
  `progress_moved`).
- **Feed the log into the catalogue review**: a task title seen in the log but unknown to
  `specs/tasks.yaml` is a candidate — the point is to discover families the project cannot yet do,
  not only to skip them safely.
- **Cap the churn**: bound re-enumeration to once per completed task (plus one final capture) so a
  flapping list cannot spin the run.

## Acceptance criteria

- [ ] Each completed task is followed by a roster capture and a logged diff.
- [ ] `roster.jsonl` accumulates across runs and is the source for "what tasks exist" documents.
- [ ] A task that appears mid-run is attempted in the same run if it is allowed by policy.
- [ ] An unknown task seen in any snapshot is reported once per run, with its screenshot.
- [ ] The run report includes the roster timeline (added/removed/progress) rather than a single list.

## Dependencies

Depends on: #22 (discovery), #38 (task surface). Feeds: #21 (catalogue growth).
"""

COMMENT_24 = """**The countdown is observable — do not hard-coded-sleep it.**

Measured 2026-11-20 on a browse task: a floating control sits at the **right edge** of the page
(`x ≈ 0.97 · width`, mid-height). While the dwell requirement runs it shows an orange clock icon with
a **red pill containing `Ns`**; when the requirement is met the pill turns **green with a white
checkmark**.

It is readable by OCR, which makes the wait a poll instead of a guess:

| t after tapping GO | OCR in the chip area |
|---|---|
| 1 s | `12s` |
| 3 s | `1s` |
| 6 s | *(no digits — green check)* |
| 10 s / 18 s | *(no digits)* |

So the executor should: tap `GO` → poll for a `\\b\\d+s\\b` label near the right edge → when it
disappears (or a green badge appears), the page is ready → collect/return. That removes the
"15 s" constant entirely and generalises across devices, because it keys on text, not position.

Note the timer is already running when the page finishes loading, so the first OCR read can be well
below 15 s (12 s in this run) — another reason not to measure dwell time from the tap.
"""

COMMENT_28 = """**Sign-in mechanics measured 2026-11-20 — and there are two of them.**

1. **Streak `Collect`** on the coin page (the `Daily coins` row). Tapping it: balance `C$0.26` →
   `C$0.29`, a "You've got +2" toast, the control disappears, the streak advances.
2. **`Daily check-in` row on the boost page** ("Finish today's check-in", +1). Still present *after*
   the streak claim, and tapping it paid +1 (20 → 21).

They are **independent**, so the executor must claim both and must not treat one as proof of the
other.

On the widget question: the 2×2 widget's "Check in now" CTA only **navigates** to the coin page —
it does not create collectability. The same `Collect` was present on the shortcut route and the
widget route in the same minute, and the widget's intent carries `aewidget_status` (`unSigned`) as
state, not as a trigger. Treat the widget as one more entry point.
"""


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def main() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(BODY)
        path = handle.name
    url = gh(
        "issue", "create", "-R", REPO,
        "--title", "Roster observation log: re-enumerate and diff after every completion",
        "--body-file", path,
        "--label", "area:tasks", "--label", "type:feature", "--label", "priority:p1",
        "--milestone", "M2 Task engine",
    )
    number = url.rstrip("/").split("/")[-1]
    print(f"created #{number}")
    gh("issue", "edit", str(number), "-R", REPO, "--parent", "4")
    print("linked under #4")

    for target, body in ((24, COMMENT_24), (28, COMMENT_28)):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
            cpath = handle.name
        gh("issue", "comment", str(target), "-R", REPO, "--body-file", cpath)
        print(f"commented #{target}")

    gh("issue", "comment", str(number), "-R", REPO, "--body",
       "Observed again during the same session: `Take the coins quiz` appeared in the boost list "
       "immediately after the `Daily check-in` row was completed, and the balance moved 20 → 21. "
       "That is the case this issue exists for.")
    print("noted the fresh observation")


if __name__ == "__main__":
    main()
