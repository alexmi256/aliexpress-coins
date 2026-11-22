# 02 — Architecture

## The shape of the problem

The phone is a black-box GUI with no API contract, no accessibility tree on the pages that
matter, and interruptions that arrive without warning. So the project is a **perception →
decision → action → verify** loop, and everything else is plumbing around it.

```
                 ┌──────────────────────────────┐
  adb  ─────────▶│ screencap                    │
                 └──────────────┬───────────────┘
                                ▼
                 ┌──────────────────────────────┐
                 │ OCR (RapidOCR)  → TextBox[]  │        perception/
                 └──────────────┬───────────────┘
                                ▼
                 ┌──────────────────────────────┐
                 │ Snapshot: image + boxes      │
                 └──────────────┬───────────────┘
                                ▼
        ┌────────────────────────────────────────────┐
        │ flows: launch → popups → coins → tasks     │   flows/, runner/
        └───────────────┬────────────────────────────┘
                        ▼
        ┌────────────────────────────────────────────┐
        │ Controller: tap / wait / verify            │   control/
        └───────────────┬────────────────────────────┘
                        ▼
                 adb input tap x y   ──▶  popup sweep  ──▶  next step
```

## Layers, and why the seams are where they are

| layer | module | responsibility | depends on |
|---|---|---|---|
| transport | `adb/transport.py` | spawn `adb`, return bytes | nothing |
| device | `adb/client.py` | typed commands: tap, key, screencap, `am start` | transport |
| perception | `perception/*` | image → `TextBox[]`, matching, frame diff, templates | device (read-only) |
| control | `control/*` | taps with jitter and logging, waits, popup dismissal | perception, device |
| flows | `flows/*` | named journeys: launch, reach coins | control |
| tasks | `tasks/*` | one executor per task family + the policy | control, flows |
| runner | `runner/*` | the loop, state, reports | everything above |
| cli | `cli.py` | `doctor`, `snapshot`, `run` | runner |

Every arrow points one way. `tasks/` never imports `runner/`; a task receives a
`TaskContext` with exactly the capabilities it should have.

**The seams exist so tests can be honest.** `Runner` (transport) is a callable, so an
`AdbClient` can be driven by a scripted device; `OcrEngine` is a protocol, so a fake returns
hand-written boxes; `Snapshotter` is a protocol, so a test scripts a sequence of frames. That
means the popup logic, the row parser and the run loop are all tested without a phone — and
the two autouse guards in `tests/conftest.py` make an accidental real connection or process
spawn a failure instead of a false pass.

## Invariants

1. **A tap is never a raw coordinate** outside the popup fallback. It comes from a matched
   `TextBox` (or a configured fallback guarded by a promo headline).
2. **A popup sweep follows every navigation.** The modal is invisible to the accessibility
   tree, so nothing else in the stack can detect it.
3. **Completion is observable.** Balance delta, progress badge movement, or the row
   disappearing. The report says `failed` when the screen disagrees with the taps.
4. **The deny list is checked before a task is dispatched**, and the reason is logged.
5. **State is written whole and atomically**, so a killed container resumes rather than
   repeats.
6. **A run always leaves a report** — including the failure path, where the final-frame
   screenshot is itself best-effort.

## Data flow of one run

```
doctor → require_device → wake → force-stop → start FirstActivity
      → wait_until_stable → popup sweep          [01-launch.png]
      → Account tab → coins card → assert "Daily coins"
      → read balance                             [02-coins.png]
      → tap Collect/Claim if offered → sweep     [03-daily-claimed.png]
      → tap "Earn more" → wait → sweep → parse task rows
      → for each row: policy → spec → executor → verify → sweep
      → read balance → write run.json + summary.md → notify
```

Artifacts per run live in `runs/<UTC timestamp>/`:

```
summary.md            human summary (status, balance delta, task table)
run.json              the same, plus every event and OCR labels
shots/01-launch.png   frame
shots/01-launch.json  the OCR boxes for that frame
run.jsonl             the structured log of the whole process
```

## Geometry assumptions

Portrait **1200×2000** at 240 dpi. Stored coordinates (popup fallbacks) and templates are for
that geometry. `doctor` reports the screen size and `Controller` clamps every tap to it; a
different device is a supported future change only after re-measuring — see
[`adr/`](adr/).

## Geometry independence

The reference device is a tablet; users will run this on phones. Measured by driving the same device
at a simulated phone geometry (`wm size 1080x1920`, density 320):

| assumption | 1200×2000 | 1080×1920 | verdict |
|---|---|---|---|
| `Earn more` found by label | (1025,946) | (922,851) | label lookup ports |
| coin-page markers | present | present | text ports |
| title → `GO` offset | ≈100 px | ≈68–70 px | a fixed tolerance is wrong at both |
| modal close control as a fraction of height | 0.826 | 0.789 | even fractions drift with aspect ratio |
| scroll distance | absolute swipe | overshoots/shorter | not portable |
| close-button template crop | 1200-wide | wrong scale | not portable |

Rules the perception layer implements (#42):

1. **No stored pixels.** Targets come from the current frame's OCR boxes.
2. **Proportional tolerances.** Row pairing tolerance derives from the median text-box height, not a
   constant.
3. **Scrolling is a search.** Swipe a fraction of the content height, re-capture, look again; stop on
   found or on a stable frame. Never a fixed distance.
4. **Scale-aware templates**, or structural detection that needs no template (a circular control on
   the modal's centre axis below the panel).
5. **Per-device calibration** for the last-resort popup coordinates, keyed by a profile id that
   `doctor` prints. No calibration means `unclosable`, not a guess.

## What is deliberately not built yet

* Executors for quiz / surprise items / shortcut / widget / notifications (the policy and
  catalogue know them; only `browse_wait` is implemented end to end).
* The launcher entry points (shortcut, widget) as a navigation route.
* An API-first path. It may well be the right long-term answer; it needs a capture spike
  first, and the UI path is the fallback that survives a token change.
