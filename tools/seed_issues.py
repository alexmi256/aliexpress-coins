#!/usr/bin/env python3
"""Create the label set, milestones, epics and issues for aliexpress-coins.

Idempotent: an issue whose title already exists is reused rather than duplicated.
Run:  python3 /opt/data/projects/aliexpress-coins/tools/seed_issues.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "alexmi256/aliexpress-coins"

LABELS = {
    "area:recon": ("5319e7", "Investigating the app, device or flows to remove unknowns"),
    "area:perception": ("1d76db", "Screenshots, OCR, matching, screen classification"),
    "area:control": ("0e8a16", "Tapping, waiting, popup dismissal, safety"),
    "area:tasks": ("d93f0b", "Task catalogue, policy and executors"),
    "area:ops": ("fbca04", "Scheduling, state, reporting, failure handling"),
    "area:delivery": ("6f42c1", "Scaffold, container, CI, tests, docs"),
    "type:spike": ("c5def5", "Time-boxed investigation that produces a written answer"),
    "type:feature": ("a2eeef", "New behaviour"),
    "type:test": ("bfd4f2", "Test harness or coverage work"),
    "type:infra": ("f9d0c4", "Build, container, automation"),
    "type:docs": ("0075ca", "Documentation"),
    "priority:p0": ("b60205", "Blocks the project"),
    "priority:p1": ("d93f0b", "Needed for a usable v1"),
    "priority:p2": ("fef2c0", "Nice to have / later"),
    "needs-device": ("0052cc", "Requires the live phone at 192.168.8.65:5555"),
    "blocked": ("000000", "Waiting on another issue"),
}

MILESTONES = {
    "M0 Recon & decisions": "Answer the questions that change the architecture. Nothing here ships code that runs unsupervised.",
    "M1 Perception & control": "See the screen reliably, tap it safely, close what interrupts.",
    "M2 Task engine": "One executor per task family, plus the policy that refuses the bad ones.",
    "M3 Unattended operation": "Schedule, state, reports, failure handling. This is what makes it nightly.",
    "M4 Delivery": "Container, CI, image per merge, test harness, docs.",
}


def b(context: str, scope: list[str], acceptance: list[str], deps: str, notes: str = "") -> str:
    parts = ["## Context", context, "", "## Scope"]
    parts += [f"- [ ] {item}" for item in scope]
    parts += ["", "## Acceptance criteria"]
    parts += [f"- [ ] {item}" for item in acceptance]
    parts += ["", "## Dependencies", deps]
    if notes:
        parts += ["", "## Notes", notes]
    return "\n".join(parts)


ISSUES: list[dict] = [
    # ------------------------------------------------------------------ epics
    {
        "key": "E1",
        "epic": True,
        "title": "Epic: recon of the coins surface",
        "labels": ["area:recon", "type:spike", "priority:p0"],
        "milestone": "M0 Recon & decisions",
        "body": b(
            "Five unknowns decide the architecture: whether the launcher shortcut/widget is a viable route, "
            "what the canonical coins-page intent is, whether the coin awards are replayable HTTP, what the quiz / "
            "surprise / notification flows actually do, and what the popup families look like. All five need the "
            "device and produce a written answer, not code.",
            [
                "Run each spike with a time box and record the result in `docs/01-recon-findings.md`",
                "Attach raw artifacts (screenshots, intents, captures) locally — never in git",
                "Close a spike with a decision, even if the decision is 'not viable'",
            ],
            [
                "Every ❓ row in the recon findings has an answer or an explicit 'unknown, blocking issue #N'",
                "Findings are marked ✅ verified / ⬜ inferred with the command that produced them",
            ],
            "Nothing depends on this epic; #E4 cannot be finished without #R4 and #R5.",
        ),
    },
    {
        "key": "E2",
        "epic": True,
        "title": "Epic: perception layer",
        "labels": ["area:perception", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "The coins page exposes no accessibility nodes (measured: a 6.4 KB empty dump against 40 OCR boxes from "
            "the same screenshot), so perception is screenshot → OCR → typed text boxes. Everything downstream — "
            "task rows, popup controls, the balance — is derived from that one structure.",
            [
                "Capture, OCR and match, behind protocols that tests can substitute",
                "Row-relative targeting: a task's button is the rightmost action label on its row",
                "Glyph controls that OCR cannot read need template matching",
            ],
            [
                "A recorded coins-page frame reproduces the same task rows every run",
                "No module in `perception/` taps anything",
            ],
            "Blocks #E3 and #E4. Internally ordered: #P1 → #P2 → #P3, with #P4 and #P5 in parallel.",
        ),
    },
    {
        "key": "E3",
        "epic": True,
        "title": "Epic: control and safety",
        "labels": ["area:control", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "Between perception and the task engine sits the layer that actually touches the phone: taps with "
            "assertions, bounded waits, popup dismissal after every navigation, and the guardrails that make it safe "
            "to point at a live device.",
            [
                "One controller owns every tap (clamping, jitter, logging, dry-run suppression)",
                "Popup sweep is an invariant of the run loop, not a task concern",
                "Abort conditions: unexpected screen, denied control visible, balance going backwards",
            ],
            [
                "A tap cannot be issued without a `why` and a label or an explicit popup fallback",
                "`--dry-run` produces a full plan and zero taps",
            ],
            "Depends on #E2. Blocks every executor in #E4.",
        ),
    },
    {
        "key": "E4",
        "epic": True,
        "title": "Epic: task engine",
        "labels": ["area:tasks", "type:feature", "priority:p0"],
        "milestone": "M2 Task engine",
        "body": b(
            "One executor per task family plus the policy that decides which rows may be attempted at all. The "
            "policy is default-deny: a task whose title no spec recognises is skipped, because a new easy task costs "
            "one coin while an unrecognised 'free trial' costs money.",
            [
                "Catalogue of specs (`specs/tasks.yaml`) validated at startup",
                "Executors: daily sign-in, browse-and-wait, quiz, surprise items, launcher items, notifications",
                "Every completion verified against balance, progress badge or row disappearance",
            ],
            [
                "Each of the brief's 11 refused task types is refused by a test, before any executor sees it",
                "A run that earns nothing reports `failed` with the reason, not a list of successes",
            ],
            "Depends on #E3 for the controller and on #R4/#R5 for the flows and popups nobody has captured yet.",
        ),
    },
    {
        "key": "E5",
        "epic": True,
        "title": "Epic: unattended operation",
        "labels": ["area:ops", "type:feature", "priority:p1"],
        "milestone": "M3 Unattended operation",
        "body": b(
            "The difference between a script that works when watched and one that runs at 03:30. Schedule, per-day "
            "state so a restart does not double-claim, a report for every run including failures, and a notification "
            "so a silent week is visible.",
            [
                "Nightly scheduler in the container, with a run window and a hard budget",
                "Atomic daily state; resume without repeating",
                "Run artifacts + failure notification",
            ],
            [
                "Three consecutive unattended nights with a report for each",
                "A killed container mid-run resumes on the next start without re-claiming the sign-in",
            ],
            "Depends on #E4 and #I2.",
        ),
    },
    {
        "key": "E6",
        "epic": True,
        "title": "Epic: delivery",
        "labels": ["area:delivery", "type:infra", "priority:p1"],
        "milestone": "M4 Delivery",
        "body": b(
            "The scaffolding that makes the project maintainable: one task runner, real type checking, tests that "
            "cannot reach the phone, a container, and an image built on every merge.",
            [
                "Python 3.14, `src/` layout, just + ruff + mypy + pytest",
                "Container from Debian trixie packages, non-root, tini",
                "CI on every PR, image to GHCR on every merge to main",
            ],
            [
                "`just check` green from a clean clone",
                "Merging to main publishes `:latest` and `:sha-<short>`",
            ],
            "Runs in parallel with #E1..#E5; #I1 unblocks everything else in this epic.",
        ),
    },
    # ------------------------------------------------------------------ recon
    {
        "key": "R1",
        "parent": "E1",
        "title": "Recon: reverse the coins shortcut and widget entry points",
        "labels": ["area:recon", "type:spike", "priority:p1", "needs-device"],
        "milestone": "M0 Recon & decisions",
        "body": b(
            "The operator reported a coins shortcut and widget on the home screen as a faster route to the coins "
            "page. Neither is present now (the launcher DB holds one AliExpress app icon and two unrelated widgets), "
            "but the app can create both: `split_widget.apk` ships `widget_coins_*` layouts and the "
            "`AECoin2x2NormalWidget` provider, and the manifest holds `INSTALL_SHORTCUT`.",
            [
                "Have the app install the shortcut and the widget through its own 'add' tasks",
                "Diff `/data/data/com.android.launcher3/databases/launcher_6_by_5.db` before/after; record `itemType`, `appWidgetProvider`, `intent`",
                "Tap the widget and the shortcut, capture the resulting `AdcActivity` intent (`dumpsys activity activities`)",
                "Establish whether the reward is granted on creation or only on a launch *from* the launcher item",
                "Test whether a plain `am start` of the same intent satisfies the task (and whether removing the item afterwards matters)",
            ],
            [
                "`docs/01-recon-findings.md` answers: what intent, what reward trigger, does removal undo it",
                "The task spec for 'Add a coins shortcut' / 'Add & start using our widget' can be written from the finding",
            ],
            "Depends on: none. Blocks #T7.",
            "Useful commands: `adb shell cat /data/data/com.android.launcher3/databases/launcher_6_by_5.db` "
            "(adbd is root; there is no `su`), `adb shell dumpsys shortcut`.",
        ),
    },
    {
        "key": "R2",
        "parent": "E1",
        "title": "Recon: capture the canonical coins-page intent (wh_pid / AdcActivity URL)",
        "labels": ["area:recon", "type:spike", "priority:p1", "needs-device"],
        "milestone": "M0 Recon & decisions",
        "body": b(
            "Every hybrid page is an `AdcActivity` launched with a `dat=https://www.aliexpress.com/ssr/<wh_pid>?…` "
            "URL. The coins page is one of these, and knowing its exact URL is the difference between a two-tap "
            "navigation and a one-command `am start` that also skips the home-feed popups.",
            [
                "Navigate Account → Coins, then read the inciting intent from `dumpsys activity activities`",
                "Try `am start -a android.intent.action.VIEW -d <that URL>` from a cold app state",
                "Compare: number of popups, time to a settled page, and whether the page is the native one or the web fallback",
                "Record the URL's stable part vs the volatile query parameters",
            ],
            [
                "The exact intent and its stability across two cold starts are recorded",
                "A recommendation: use it as the primary route, or keep the UI route and say why",
            ],
            "Depends on: none. Informs #E3 navigation and #O1 timing.",
            "The advertised `/p/coin-index/` deep link was already tested and lands on a blank web view — "
            "this spike is about the *container* URL, which is a different thing.",
        ),
    },
    {
        "key": "R3",
        "parent": "E1",
        "title": "Recon: quantify the API path (capture a task run)",
        "labels": ["area:recon", "type:spike", "priority:p2", "needs-device"],
        "milestone": "M0 Recon & decisions",
        "body": b(
            "If the coin awards are plain HTTP the collector could skip the UI entirely. ADR-0001 chose the UI path "
            "first, and says to revisit only if a capture proves the calls are replayable.",
            [
                "Capture one full run (sign-in + one task) through mitmproxy or an equivalent",
                "Identify the award endpoints, their parameters, and any signature/nonce/attestation",
                "Assess: replayable with a captured token? per-request signing? certificate pinning in the way?",
                "Record the answer as an update to ADR-0001 (accept / reject / defer)",
            ],
            [
                "ADR-0001 carries a dated verdict backed by a capture",
                "If replayable, a follow-up issue describes the API path; if not, the reason is specific",
            ],
            "Depends on: none. Does not block any milestone.",
        ),
    },
    {
        "key": "R4",
        "parent": "E1",
        "title": "Recon: capture the quiz, surprise-items and notification flows",
        "labels": ["area:recon", "type:spike", "priority:p1", "needs-device"],
        "milestone": "M0 Recon & decisions",
        "body": b(
            "Three executor families are described in the brief but have never been observed: the quiz (random "
            "answers, wrong-answer interstitial), surprise items (three distinct item taps, possible multi-round "
            "popups) and the notification task (a system permission dialog, not an app dialog).",
            [
                "For each: capture the entry, the intermediate states, and the return path to the coins page",
                "Note every popup encountered, with its close control (feeds #R5)",
                "For the notification task: capture the system dialog and what the screen looks like after granting",
                "Record the number of taps and the wait points a executor needs",
            ],
            [
                "Each flow has a state list in `docs/01-recon-findings.md` (entry → steps → exit)",
                "The executors in #T5/#T6/#T8 can be written without further device work",
            ],
            "Depends on: none. Blocks #T5, #T6, #T8.",
        ),
    },
    {
        "key": "R5",
        "parent": "E1",
        "title": "Recon: build a popup corpus and per-family dismissal rules",
        "labels": ["area:recon", "type:spike", "priority:p1", "needs-device"],
        "milestone": "M0 Recon & decisions",
        "body": b(
            "Only one popup family has been measured: the cold-launch 'Welcome gifts' modal, whose close control is "
            "a drawn glyph with no OCR text at ≈(600, 1652). Every other family — reward interstitials, 'already "
            "collected', promo overlays, the system permission dialog — is unknown, and each one that is missed "
            "swallows the rest of the run.",
            [
                "Provoke and capture every popup family reachable from the coins flows",
                "For each: a crop of the close control (template), any dismiss label, and a fallback coordinate",
                "Note which ones chain (close one, another appears)",
                "Decide the escalation rule: template → label → fallback → fail the run",
            ],
            [
                "`assets/templates/` has one crop per glyph-only control, named by family",
                "Every family has a rule row, and an unclosable popup is a loud failure rather than a guess",
            ],
            "Depends on: none. Blocks #C2's rule table and #T4.",
        ),
    },
    # ------------------------------------------------------------ perception
    {
        "key": "P1",
        "parent": "E2",
        "title": "Screenshot capture and run artifact store",
        "labels": ["area:perception", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "Every decision starts from one frame. Capture must be binary-safe, validated (a failed `screencap` "
            "returns text, not a PNG), and persisted with the OCR output that was derived from it — the run report "
            "is unreadable without the pair.",
            [
                "`AdbSnapshotter.capture()`: `exec-out screencap -p`, PNG magic check, decode to PIL",
                "`Snapshot` value type: image, OCR boxes, timestamp, `find`/`has`/`describe` helpers",
                "Artifact writer: `shots/<name>.png` plus `<name>.json` sidecar of the boxes",
                "Region crop helper (read one card without re-OCRing the page)",
            ],
            [
                "A non-PNG response raises rather than producing a corrupt snapshot",
                "PNG + sidecar round-trip drops no box, and the sidecar is valid JSON",
                "Measured: one capture + OCR of the coins page under 3 s on CPU",
            ],
            "Depends on: #I1. Blocks #P2, #P4, #O3.",
        ),
    },
    {
        "key": "P2",
        "parent": "E2",
        "title": "OCR text index with fuzzy label matching",
        "labels": ["area:perception", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "The UI has no ids, so labels are the API. OCR noise means matching must tolerate `Cs0.04` for "
            "`C$0.04` while refusing to tap the wrong row; a 0.6 match against a list of reward rows is how a tap "
            "lands on the wrong task.",
            [
                "`OcrEngine` protocol + RapidOCR adapter (PP-OCRv6 small, onnxruntime, CPU)",
                "`normalize`, `similarity`, `find_text`, `find_all` with an explicit `min_score`",
                "A separate OCR-confidence floor, distinct from the text-match threshold",
                "Keep the engine import lazy so the CLI starts without loading models",
            ],
            [
                "The measured coins-page labels ('Earn more >', 'Take the coins quiz', 'GO') resolve on a real frame",
                "`C$0.04` vs `Cs0.04` scores ≥ 0.8; 'Coins' vs 'Vouchers' scores below the default threshold",
                "A symbol-only label (the close glyph) does not normalise to the empty string",
            ],
            "Depends on: #P1. Blocks #P3, #P5, #C2.",
        ),
    },
    {
        "key": "P3",
        "parent": "E2",
        "title": "Row and section layout inference (row-relative targets)",
        "labels": ["area:perception", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "Coordinates drift: a new task at the top of the list moves every row. The measured coins page puts a "
            "task label at x≈180 and its `GO` button at x≈980, ~31 px lower — so rows must be recovered by grouping, "
            "not by remembering pixels.",
            [
                "`group_rows` / `row_of` with a tolerance justified by the measurement (31 px offset, ~340 px row pitch)",
                "`find_action_in_row`: the rightmost action label on a row is that row's button",
                "Progress badge parsing (`1/6`), attached to the row it shares",
                "Section grouping for diagnostics (what a human sees, in reading order)",
            ],
            [
                "On the measured geometry each task label resolves to its own `GO`",
                "Inserting a row above the list changes no target's identity",
                "Two adjacent rows are never merged into one",
            ],
            "Depends on: #P2. Blocks #T2, #T4.",
        ),
    },
    {
        "key": "P4",
        "parent": "E2",
        "title": "Template matching for glyph-only controls",
        "labels": ["area:perception", "type:feature", "priority:p1"],
        "milestone": "M1 Perception & control",
        "body": b(
            "The launch modal's close control is a drawn ✕ with no OCR text. Template matching is the only reliable "
            "way to find it — and the same mechanism covers every other icon-only control the popup corpus turns up.",
            [
                "`TM_CCOEFF_NORMED` match with a documented threshold (contrast-invariant across dark/light scrims)",
                "Template store under `assets/templates/`, named by popup family",
                "A capture recipe so a new template can be cut from a live frame",
                "Guard against zero-variance regions (uniform crops make NCC undefined)",
            ],
            [
                "The launch-modal ✕ is found on a real frame",
                "A uniform crop does not produce a spurious match",
                "Adding a template is a file drop plus one rule row, no code change",
            ],
            "Depends on: #P1. Blocks #C2.",
        ),
    },
    {
        "key": "P5",
        "parent": "E2",
        "title": "Screen-state classifier: loading / page / popup / error",
        "labels": ["area:perception", "type:feature", "priority:p1"],
        "milestone": "M1 Perception & control",
        "body": b(
            "The hybrid container renders in stages (skeleton → layout → images) and fails into a blank white page "
            "with no error text. A run needs to tell 'still loading' from 'arrived' from 'this page is dead' before "
            "it asserts anything.",
            [
                "Classify a frame: `LOADING`, `PAGE`, `POPUP`, `ERROR`, `UNKNOWN`",
                "Signals: page markers ('Daily coins', 'Coins credits to redeem'), dimmed scrim, spinner/skeleton, blank-frame heuristics",
                "Never guess: unresolvable frames are `UNKNOWN` and the caller decides",
                "Feed the classification into waits (a popup during 'loading' is not the same as one after 'arrived')",
            ],
            [
                "Ten recorded frames classify correctly (coins page, account page, launch modal, skeleton, blank web view)",
                "A blank frame never classifies as `PAGE`",
            ],
            "Depends on: #P2. Blocks #C3's wait policies.",
        ),
    },
    # --------------------------------------------------------------- control
    {
        "key": "C1",
        "parent": "E3",
        "title": "Tap controller: clamping, jitter, logging, dry-run suppression",
        "labels": ["area:control", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "One place owns every tap. It clamps to the screen, jitters a few pixels (identical pixel-perfect taps "
            "are a bot signature), logs the decision with its reason, and is the only thing that knows about dry-run.",
            [
                "`tap(point, why=, label=)`, `tap_box`, `tap_text` against an already-captured frame",
                "Coordinate clamping to the reported screen size",
                "`dry_run` suppresses the tap but logs the plan",
                "Tap counter for the run report",
            ],
            [
                "A tap cannot be issued without a reason; the log carries label + coordinates",
                "Dry run: zero `input tap` calls, a full log of what would have happened",
                "Test proves clamping at the screen edges",
            ],
            "Depends on: #P1. Blocks #C2, #C4, every executor.",
        ),
    },
    {
        "key": "C2",
        "parent": "E3",
        "title": "Popup dismissal loop invoked after every navigation",
        "labels": ["area:control", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "The app interposes a modal after cold launch and after most navigations, and the modal is invisible to "
            "the accessibility tree. A collector that does not close it keeps issuing confident taps at a scrim. This "
            "is the single most repeated behaviour in the project.",
            [
                "Detection order: dismiss label in a *small* box → glyph template → headline + configured coordinate",
                "A wide 'Cancel order'-style button must never match as a close control",
                "Bounded rounds, repeated-target guard, and an explicit `popup_unclosable` failure",
                "Invoked by the run loop after every navigation and after every task, not by tasks themselves",
            ],
            [
                "The measured launch modal is closed in one tap on a real frame",
                "A popup that does not close ends the sweep with a warning instead of tapping forever",
                "`--dry-run` detects and reports popups without tapping",
            ],
            "Depends on: #C1, #P2, #P4 (and #R5 for the per-family table). Blocks all executors.",
        ),
    },
    {
        "key": "C3",
        "parent": "E3",
        "title": "Wait primitives: text appearance, screen stability, countdowns",
        "labels": ["area:control", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "Blind sleeps are why UI automation is flaky. Waits must poll something observable: a label appearing, "
            "the frame stopping changing, or a countdown finishing. The hybrid container makes 'stopped changing' the "
            "most useful of the three.",
            [
                "`wait_for_text` / `wait_for_text_gone` with poll interval and deadline",
                "`wait_until_stable` via a small downscaled frame signature (mean absolute difference below a threshold for K seconds)",
                "Countdown wait for the 15 s browse tasks, driven by the frame or by a timer with verification",
                "Injectable clock so tests do not take wall-clock time",
            ],
            [
                "A skeleton → page transition is waited out without a fixed sleep",
                "`wait_until_stable` returns in milliseconds under a fake clock",
                "No wait can loop unbounded",
            ],
            "Depends on: #P1, #P5. Blocks #T2 and every executor.",
        ),
    },
    {
        "key": "C4",
        "parent": "E3",
        "title": "Safety guardrails: deny enforcement points and abort conditions",
        "labels": ["area:control", "type:feature", "priority:p0"],
        "milestone": "M1 Perception & control",
        "body": b(
            "The project's value is as much in what it refuses as in what it does. The refusal has to be structural — "
            "enforced where a task is dispatched — and the run needs abort conditions for screens it does not "
            "understand.",
            [
                "A single dispatch point where the policy decision is made and logged before an executor runs",
                "Abort conditions: the same unexpected screen three times, a screen containing buy/order/cart controls while a task is running, balance decreasing",
                "A `--dry-run` path that plans without acting",
                "An audit line per refusal: label, rule, reason",
            ],
            [
                "No executor can be reached without a policy decision",
                "Each of the brief's 11 refused task types is covered by a test at this layer",
                "An aborted run reports the screen and the reason, and leaves the app on the coins page",
            ],
            "Depends on: #C1, #T1. Blocks #E4.",
        ),
    },
    # ----------------------------------------------------------------- tasks
    {
        "key": "T1",
        "parent": "E4",
        "title": "Task spec catalogue and default-deny policy",
        "labels": ["area:tasks", "type:feature", "priority:p0"],
        "milestone": "M2 Task engine",
        "body": b(
            "The catalogue is how the project knows a task family exists, how long it waits, how often it may run, "
            "and — through the policy — whether it may be touched at all.",
            [
                "`specs/tasks.yaml` with key, label patterns, category, wait seconds, repeatable, max per run",
                "Pydantic validation at load: an unknown category or an empty pattern list fails the run at startup",
                "Deny rules: games, currency conversion, spending/orders/cart, reviews, social",
                "Default-deny for unrecognised labels",
            ],
            [
                "The 11 refused task types from the brief are refused, each with a reason",
                "The eight allowed families are accepted",
                "A spec that its own deny list refuses fails CI, not the 03:30 run",
            ],
            "Depends on: #I1. Blocks #C4, #T2.",
        ),
    },
    {
        "key": "T2",
        "parent": "E4",
        "title": "Task discovery: open 'Earn more' and enumerate the full list",
        "labels": ["area:tasks", "type:feature", "priority:p0"],
        "milestone": "M2 Task engine",
        "body": b(
            "The coins page shows a subset of tasks; 'Earn more' opens the full list behind a wait. Discovery has to "
            "open it, wait for the popup-shaped page to settle, close whatever appears, and enumerate rows with their "
            "progress.",
            [
                "Tap 'Earn more', wait for stability, sweep popups, capture the list",
                "Parse rows (title, action, progress) via #P3",
                "Deduplicate rows that appear both inline and in the list",
                "Record the catalogue snapshot in the run report",
            ],
            [
                "On a live page, every visible task appears exactly once with its progress",
                "A missing 'Earn more' degrades to the inline subset instead of failing",
            ],
            "Depends on: #P3, #C2, #C3, #T1. Blocks #T3..#T8.",
        ),
    },
    {
        "key": "T3",
        "parent": "E4",
        "title": "Daily sign-in executor",
        "labels": ["area:tasks", "type:feature", "priority:p0"],
        "milestone": "M2 Task engine",
        "body": b(
            "The one task that must work: the daily check-in. It is also the one that punishes repetition — pressing "
            "the card after it is claimed raises 'already collected' modals for the rest of the run.",
            [
                "Detect the claim control (Collect / Claim / Sign in) on the coins page",
                "Tap once, sweep, verify by balance delta or by the card flipping to claimed",
                "Record the completion in daily state and never re-press in the same day",
                "Handle the reward interstitial that follows",
            ],
            [
                "A run on a fresh day claims the sign-in and reports the balance delta",
                "A second run the same day skips it with `already done today`",
                "If the control is absent, the result is `unavailable`, not `completed`",
            ],
            "Depends on: #T2, #C3, #O2.",
        ),
    },
    {
        "key": "T4",
        "parent": "E4",
        "title": "Browse-and-wait executor (sponsored, recently viewed, check-in to win)",
        "labels": ["area:tasks", "type:feature", "priority:p0"],
        "milestone": "M2 Task engine",
        "body": b(
            "Most paying tasks are the same shape: open a page, wait out a countdown (15 s measured), collect if "
            "offered, come back, verify the progress badge moved. One executor serves all of them; the spec carries "
            "the wait.",
            [
                "Tap the row's action, wait, sweep, collect if offered, back to the coins page (max 3 presses, re-checking)",
                "Verify: progress advanced, or the row is gone, or the outcome is `failed`",
                "Per-spec repeat limits with a daily counter",
                "Leave the app on the coins page even when the task fails",
            ],
            [
                "A completed task shows a changed progress badge in the report",
                "A task whose taps did nothing reports `failed`, never `completed`",
                "The run is always back on the coins page afterwards",
            ],
            "Depends on: #T2, #C2, #C3, #R5. Blocks #O1.",
        ),
    },
    {
        "key": "T5",
        "parent": "E4",
        "title": "Coins quiz executor",
        "labels": ["area:tasks", "type:feature", "priority:p1", "needs-device"],
        "milestone": "M2 Task engine",
        "body": b(
            "The quiz rewards participation, not accuracy (per the brief: answer randomly). It still needs its own "
            "executor because it has a question page, an answer set, and a wrong-answer interstitial.",
            [
                "Open the quiz, wait, sweep",
                "Pick an answer option at random; handle multi-question rounds if they exist",
                "Absorb the result interstitial (right or wrong) and return",
                "Verify the task row advanced or disappeared",
            ],
            [
                "A run completes the quiz and returns to the coins page",
                "A wrong answer does not abort the task",
            ],
            "Depends on: #R4 (flow capture), #T2, #C2.",
        ),
    },
    {
        "key": "T6",
        "parent": "E4",
        "title": "Surprise items executor (three distinct item taps)",
        "labels": ["area:tasks", "type:feature", "priority:p1", "needs-device"],
        "milestone": "M2 Task engine",
        "body": b(
            "Opening the page is not enough: three *different* items must be opened, coming back between them, and "
            "finishing may trigger round popups before the reward is granted.",
            [
                "Open the page and identify item tiles from the frame",
                "Tap a tile, back out, tap a different tile — three distinct items, tracked by label or position",
                "Absorb the round popups and collect the reward",
                "Verify progress or row disappearance",
            ],
            [
                "Three distinct items are opened (proved by the tap log, not by hope)",
                "The same tile is never counted twice",
                "Round popups are handled without stranding the run",
            ],
            "Depends on: #R4, #T4 (shares the browse skeleton), #C2.",
        ),
    },
    {
        "key": "T7",
        "parent": "E4",
        "title": "Launcher items: add/remove the coins shortcut and widget",
        "labels": ["area:tasks", "type:feature", "priority:p2", "needs-device"],
        "milestone": "M2 Task engine",
        "body": b(
            "Two tasks pay for installing a launcher item: 'Add a coins shortcut' and 'Add & start using our widget'. "
            "Whether the reward needs the item to persist, or a launch *from* it, is exactly what #R1 must answer. "
            "The executor must also clean up, because a widget left behind is a permanent change to someone's home "
            "screen.",
            [
                "Trigger the app's own add flow (or `requestPinShortcut` if the app supports it)",
                "Verify installation by reading the launcher DB / widget provider state",
                "If the reward needs a launch from the item, synthesise the intent the item fires (see #R1)",
                "Remove the item afterwards and verify removal",
            ],
            [
                "Adding and removing is verified against launcher state, not assumed from the tap",
                "The home screen is unchanged after the run (unless the operator asks otherwise)",
                "If the reward cannot be obtained without a persistent item, the spec is disabled and documented",
            ],
            "Depends on: #R1, #T2.",
        ),
    },
    {
        "key": "T8",
        "parent": "E4",
        "title": "Notifications executor (system permission dialog)",
        "labels": ["area:tasks", "type:feature", "priority:p2", "needs-device"],
        "milestone": "M2 Task engine",
        "body": b(
            "'Turn on notifications' is cheap, but the confirmation is a *system* dialog — a different window, "
            "different styling, and it cannot be dismissed by the app's own close-button logic.",
            [
                "Detect the task and open it",
                "Handle the system permission dialog (allow/deny) from a screenshot",
                "Verify the app's notification setting afterwards (`cmd notification` / settings)",
                "Restore the previous setting if the operator does not want notifications on",
            ],
            [
                "The task completes with the setting actually enabled",
                "A denied/skipped dialog is reported, not retried forever",
            ],
            "Depends on: #R4, #T2.",
        ),
    },
    # ------------------------------------------------------------------- ops
    {
        "key": "O1",
        "parent": "E5",
        "title": "Nightly scheduler and run window",
        "labels": ["area:ops", "type:feature", "priority:p1"],
        "milestone": "M3 Unattended operation",
        "body": b(
            "The point of the project is that nobody is awake. The container should run itself once a night, inside a "
            "window, with a hard budget and a jittered start so it does not look like a cron.",
            [
                "In-container schedule (APScheduler cron) with a configurable time and timezone",
                "`run --once` for manual runs and `run --schedule` for the daemon",
                "A wall-clock budget that ends the run cleanly and reports what it skipped",
                "A jitter of a few minutes on the start time",
            ],
            [
                "`docker compose up -d` produces one run per night",
                "A run that hits the budget still writes a report",
                "Manual `run --once` works with the container idle",
            ],
            "Depends on: #I2, #T4. Blocks #E5's acceptance.",
        ),
    },
    {
        "key": "O2",
        "parent": "E5",
        "title": "Daily state store with crash recovery",
        "labels": ["area:ops", "type:feature", "priority:p0"],
        "milestone": "M3 Unattended operation",
        "body": b(
            "State answers two questions: what has been done today, and how many times. Without it a restart "
            "re-claims the sign-in and re-runs repeatable tasks until the app pushes back.",
            [
                "Per-day JSON state: completion counters, last balance, run count, last run timestamp",
                "Atomic writes (temp file + rename) so a kill leaves old-or-new, never a truncated file",
                "A corrupt file resets rather than blocking the night",
                "Automatic reset when the local date changes",
            ],
            [
                "A killed container resumes without repeating a completed task",
                "A corrupt state file does not stop a run",
                "Counters survive a restart within the same day",
            ],
            "Depends on: #I1. Blocks #T3, #T4.",
        ),
    },
    {
        "key": "O3",
        "parent": "E5",
        "title": "Run report artifacts and failure notification",
        "labels": ["area:ops", "type:feature", "priority:p1"],
        "milestone": "M3 Unattended operation",
        "body": b(
            "An unattended collector that silently stops earning is the main failure mode. Every run leaves a report "
            "a human can read in ten seconds, and a failure pushes a notification.",
            [
                "`runs/<UTC timestamp>/`: `summary.md`, `run.json`, `shots/*.png` + OCR sidecars, `run.jsonl`",
                "Summary: status, balance before/after and delta, popups closed, per-task outcome table",
                "A notification sink (ntfy/webhook) for failures; a broken webhook must not fail the run",
                "Retention: keep the last N runs, prune the rest",
            ],
            [
                "A failed run still produces a readable summary naming the error",
                "The balance delta in the summary matches the two screenshots in `shots/`",
                "A webhook failure is logged and the run still exits successfully",
            ],
            "Depends on: #P1. Blocks #E5's acceptance.",
        ),
    },
    {
        "key": "O4",
        "parent": "E5",
        "title": "Failure taxonomy, retries and device hygiene",
        "labels": ["area:ops", "type:feature", "priority:p1"],
        "milestone": "M3 Unattended operation",
        "body": b(
            "Overnight, the device will be asleep, the app will be logged out once, a page will fail to load. Each "
            "failure deserves a different response — some retry once, some abort the run, some should simply be "
            "reported tomorrow.",
            [
                "Named failure classes: device offline, app not installed, logged out, navigation failed, popup unclosable, task unverified",
                "Retry policy per class (one retry per stage, exponential pause), and a stop condition",
                "Device hygiene: wake, dismiss keyguard, keep-awake during the run, connectivity check before starting",
                "Document the unlock question (PIN/biometric) in the brief's open questions",
            ],
            [
                "Pulling the network mid-run produces a reported failure, not a hang",
                "A run against a sleeping device wakes it and completes",
                "Every failure class has a test with a simulated device",
            ],
            "Depends on: #C4, #O1.",
        ),
    },
    # -------------------------------------------------------------- delivery
    {
        "key": "I1",
        "parent": "E6",
        "title": "Project scaffold and quality gates",
        "labels": ["area:delivery", "type:infra", "priority:p0"],
        "milestone": "M4 Delivery",
        "body": b(
            "The tooling that keeps the project reviewable: one task runner, a formatter, a linter, a type checker "
            "that is actually green, tests with a coverage ratchet, and the documents a newcomer reads first.",
            [
                "`src/` layout, `pyproject.toml` as the single config home, requirements split runtime/dev",
                "`just setup|check|test|cov|clean`, with `check` equal to CI",
                "ruff (line length 120, single quotes), mypy strict on `src/` and `tests/`",
                "pytest + coverage ratchet; README + AGENTS.md",
            ],
            [
                "`just check` green from a clean clone",
                "mypy reports 'no issues found', not 'configured'",
                "The coverage gate passes and its number is documented as a ratchet",
            ],
            "Depends on: none. Blocks #P1, #O2, #I2, #I3.",
        ),
    },
    {
        "key": "I2",
        "parent": "E6",
        "title": "Docker image and compose service",
        "labels": ["area:delivery", "type:infra", "priority:p1"],
        "milestone": "M4 Delivery",
        "body": b(
            "The collector runs in a container on a host with no Android tooling: adb, the OCR runtime and the "
            "scheduler all live in the image. Distribution packages only — nothing compiled from source.",
            [
                "Base `python:3.14-slim-trixie`; `adb`, `tzdata`, and opencv's runtime libs from apt",
                "RapidOCR models come with the wheel (no network at runtime)",
                "Non-root user, `tini` as PID 1, SIGTERM handled by the scheduler",
                "Compose service with `runs/` and `state/` volumes and the device serial from `.env`",
            ],
            [
                "`docker compose run --rm collector python -m aliexpress_coins doctor` works against the device",
                "The container runs as a non-root user and stops within a few seconds of SIGTERM",
                "Image build does not download models at runtime",
            ],
            "Depends on: #I1. Blocks #O1, #I3.",
        ),
    },
    {
        "key": "I3",
        "parent": "E6",
        "title": "CI workflows and image build on merge",
        "labels": ["area:delivery", "type:infra", "priority:p1"],
        "milestone": "M4 Delivery",
        "body": b(
            "Every pull request proves lint, types and tests; every merge to main publishes an image. Latest action "
            "versions, pinned by major tag so Dependabot can keep them current.",
            [
                "`ci.yml`: checkout → setup-python 3.14 → setup-just → install → lint/format/mypy/tests+cov → artifact upload",
                "`docker.yml`: build on every PR (no push), push `:latest` + `:sha-<short>` on main, provenance + SBOM, smoke test the image",
                "`codeql.yml` for Python; `dependabot.yml` for pip, actions and docker",
                "Dependency review on PRs",
            ],
            [
                "A PR shows lint/type/test and dependency-review checks",
                "Merging to main publishes an image whose `version` command runs",
                "No workflow uses a deprecated action major",
            ],
            "Depends on: #I1, #I2.",
        ),
    },
    {
        "key": "I4",
        "parent": "E6",
        "title": "Simulated-device test harness with recorded frames",
        "labels": ["area:delivery", "type:test", "priority:p1"],
        "milestone": "M4 Delivery",
        "body": b(
            "The suite must never need the phone, and must never accidentally use it. Two seams plus two guards make "
            "that structural rather than a matter of discipline.",
            [
                "Transport seam (scripted `adb` runner → real client) and snapshot seam (scripted snapshotter → real controller)",
                "Injectable clock so waits are instant",
                "Autouse guards: no socket connect, no process spawn",
                "A small set of recorded frames (gitignored) with a recipe to regenerate them; row-parse fixtures use measured geometry",
            ],
            [
                "Suite runs offline in under a minute",
                "A test that tries to spawn adb fails loudly instead of driving the device",
                "A recorded coins-page frame reproduces the same task rows",
            ],
            "Depends on: #P2, #C1.",
        ),
    },
    {
        "key": "I5",
        "parent": "E6",
        "title": "Runbook and documentation set",
        "labels": ["area:delivery", "type:docs", "priority:p2"],
        "milestone": "M4 Delivery",
        "body": b(
            "A project that runs at 03:30 needs a document that explains how to run it, how to read its output, and "
            "what to do when a night fails — written from a clean clone, not from the author's working tree.",
            [
                "Runbook: doctor, snapshot, dry run, one-shot run, schedule, reading `summary.md`",
                "Troubleshooting table: each failure class → what the operator should check",
                "ADR index and the recon findings kept current",
                "Every command in the docs executed on a clean clone before merge",
            ],
            [
                "The quickstart works from `git clone` with no local state",
                "Every failure class in #O4 has a troubleshooting row",
            ],
            "Depends on: #I1; evolves with #O4.",
        ),
    },
]


def gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, input=stdin, check=False)
    if result.returncode != 0:
        print(f"FAILED: gh {' '.join(args)}\n{result.stderr}", file=sys.stderr)
        raise SystemExit(1)
    return result.stdout.strip()


def main() -> None:
    # labels
    for name, (colour, description) in LABELS.items():
        gh("label", "create", name, "--color", colour, "--description", description, "--force", "-R", REPO)
    print(f"labels: {len(LABELS)}")

    # milestones
    existing = {m["title"] for m in json.loads(gh("api", f"repos/{REPO}/milestones?state=all"))}
    for title, description in MILESTONES.items():
        if title in existing:
            continue
        gh(
            "api", "-X", "POST", f"repos/{REPO}/milestones",
            "-f", f"title={title}", "-f", f"description={description}", "-f", "state=open",
        )
    print(f"milestones: {len(MILESTONES)}")

    # indexes
    open_issues = json.loads(gh("api", f"repos/{REPO}/issues?state=all&per_page=100"))
    by_title = {issue["title"]: issue for issue in open_issues}

    numbers: dict[str, int] = {}
    created = 0

    for spec in ISSUES:  # order matters: epics first, then children
        title = spec["title"]
        if title in by_title:
            issue = by_title[title]
            numbers[spec["key"]] = issue["number"]
            print(f"skip  #{issue['number']} {title}")
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(spec["body"])
            path = handle.name
        args = ["issue", "create", "-R", REPO, "--title", title, "--body-file", path, "--milestone", spec["milestone"]]
        for label in spec["labels"]:
            args += ["--label", label]
        url = gh(*args)
        number = int(url.rstrip("/").split("/")[-1])
        numbers[spec["key"]] = number
        created += 1
        print(f"new   #{number} {title}")

    # sub-issue links for epics
    linked = 0
    for spec in ISSUES:
        parent_key = spec.get("parent")
        if not parent_key:
            continue
        parent_no, child_no = numbers[parent_key], numbers[spec["key"]]
        parent_id = gh("api", f"repos/{REPO}/issues/{parent_no}", "--jq", ".node_id")
        child_id = gh("api", f"repos/{REPO}/issues/{child_no}", "--jq", ".node_id")
        query = (
            "mutation($p: ID!, $c: ID!) { addSubIssue(input: {issueId: $p, subIssueId: $c}) "
            "{ issue { number } subIssue { number } } }"
        )
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"p={parent_id}", "-f", f"c={child_id}"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            linked += 1
        else:
            print(f"  sub-issue link failed for #{child_no} -> #{parent_no}: {result.stderr.strip()[:160]}")

    Path("/tmp/issue-numbers.json").write_text(json.dumps(numbers, indent=2))
    print(f"\ncreated {created} issues, linked {linked} sub-issues, total tracked {len(numbers)}")


if __name__ == "__main__":
    main()
