# 02 — Architecture

## The shape of the problem

The device is a black-box GUI with no API contract, no accessibility tree on the coins surface, and
popups that arrive without warning. The collector is a **perception → decision → action → verify**
loop wrapped in safety guards, daily state, and structured reporting.

```text
  adb screencap ─▶ RapidOCR ─▶ Snapshot (image + TextBox[] + geometry)     perception/
                                         │
                                         ▼
              SafetyGuard ─▶ route / discovery ─▶ DispatchGate (policy)     runner/, tasks/
                                         │
                                         ▼
              Controller (clamp, jitter, dry-run) ─▶ PopupDismisser         control/
                                         │
                                         ▼
              executors (signin, browse, quiz, surprise, notifications)     tasks/
                                         │
                                         ▼
              DailyStateStore + RunReport + Apprise / webhook / ntfy        runner/
```

## Modules (`src/aliexpress_coins/`)

| layer | modules | responsibility |
|---|---|---|
| transport | `transport.py` | shared `AdbRunner` protocol and `SubprocessAdbRunner` |
| perception | `perception/{capture,ocr,snapshot,geometry,layout,matching,templates,state,waits,scroll,artifacts,errors}.py` | screencap, RapidOCR, frame geometry profiles, row grouping, fuzzy matching, scaled templates, screen classification, waits, search-scrolling, artifact sidecars |
| control | `control/{device,controller,popups,errors}.py` | typed adb input (`AdbInputDevice`), clamped/jittered/dry-run `Controller`, layered popup dismissal (`PopupDismisser`, `specs/popups.yaml`) |
| tasks | `tasks/{catalogue,dispatch,surface,discovery,signin,browse,quiz,quiz_bank,surprise,notifications,errors}.py` | Pydantic task policy (`specs/tasks.yaml`), sealed `DispatchGate`, surface routing & roster observation, and the five task executors + `data/quiz_bank.json` |
| runner | `runner/{config,device,engine,safety,failures,state,lock,report,notify,levels,schedule,timezone,errors}.py` | env config, device hygiene & routes (`AdbDevice`), nightly walk (`engine`), `SafetyGuard`, failure taxonomy, atomic daily state, exclusive `run.lock`, reports, notifications, scheduler, timezone check |
| entry & tools | `cli.py`, `doctor.py`, `tools/capture_frames.py` | `version`, `doctor`, `idle`, `run` (`--once` / `--schedule`, `--dry-run`); developer fixture capture |

Runtime specs and data live beside the package: `specs/` (`tasks.yaml`, `popups.yaml`, `flows.yaml`),
`assets/templates/` (popup close glyphs), and `data/quiz_bank.json` (learning quiz bank).

Dependencies point one way (`tasks/` never imports `runner/`). Tests substitute two seams —
`AdbRunner` (`tests/support/adb.py`) and `Snapshotter` (`tests/support/frames.py`) — beneath two
autouse guards in `tests/conftest.py` that forbid real sockets and process spawns.

## Invariants

1. **No raw coordinate taps** outside a profile-calibrated popup fallback guarded by an OCR anchor.
2. **Popup sweep after every navigation**, because modals are invisible to `uiautomator`.
3. **Single dispatch gate:** `DispatchGate` checks the deny list before any executor sees a row and
   mints a sealed `TaskAuthorization` token. OCR provenance narrows what may *allow* a row: a
   subtitle span also present in a neighbouring element is contaminated and ignored. It never
   narrows denial: an unattested field is still displayed text, and the deny list runs on it.
4. **Structural abort guard:** `SafetyGuard` inspects every frame for `dangerous_control`,
   `balance_decreased`, and three identical `unexpected_screen` sightings; `RunAborted` propagates
   through executors and cleanup, and the guard disarms after the first abort.
5. **Positive verification:** a task completes only when the screen proves it (balance delta,
   progress badge, completed control, or row gone from a complete surface).
6. **Atomic state, exclusive lock, always-written report:** `state/daily-<date>.json` writes
   atomically, `state/run.lock` serialises runs sharing a state dir, and `runs/<UTC timestamp>/`
   (`summary.md`, `run.json`, `run.jsonl`, `roster.jsonl`, `shots/`) is written on every path.

## Data flow of one night

```text
acquire run.lock → timezone check → hygiene (wake, keyguard check, portrait lock, stayon)
  → warm start FirstActivity (~10 s settle) → shortcut deep link → popup sweep
  → read balance-before [shots/balance-before.{png,json}]
  → open Coins boost ("Earn more"; warm re-entry if empty_boost) → enumerate roster
  → claim daily sign-ins (streak Collect + boost Daily check-in)
  → for each row: DispatchGate → budget/daily-limit check → executor → verify → roster diff
  → return to coins page → read balance-after → restore device settings
  → write run.json + summary.md → prune old runs → notify → release run.lock
```

## Geometry independence

The reference profile is `c9b8f88670e295b5` (`1200×2000 @ 240 dpi, sdk 35, portrait`; raw
measurements in [`01-recon-findings.md`](01-recon-findings.md) §7b, portability limits in
[`06-runbook.md`](06-runbook.md) §14). The perception layer derives layout from the frame:

1. **No stored pixels:** enforced by `tests/test_geometry_constants.py`.
2. **Proportional tolerances:** row pairing uses `2.25 × median(box height)`, not a fixed pixel gap.
3. **Search-based scrolling:** swipe a fraction of content height, re-capture, stop on match or
   stability.
4. **Scale-aware templates:** close-button glyphs scale from frame width (with vertical fallback).
5. **Profile-keyed calibration:** last-resort popup fallbacks are keyed by `doctor`'s profile id;
   missing calibration reports `popup_unclosable` instead of guessing.

Architecture decisions are recorded in [`adr/`](adr/README.md).

## Not built / descoped

- `shortcut` and `widget` task executors and widget-route fallback (catalogued in `specs/tasks.yaml`,
  skipped as `no executor`; descoped in #27 because the shortcut URI needs no launcher item).
- An executor for `Search for what you love` (refused as `unrecognised` by default-deny).
- An HTTP/API-first spike (#9; see [`adr/0001`](adr/0001-drive-the-ui-not-the-api.md)).
