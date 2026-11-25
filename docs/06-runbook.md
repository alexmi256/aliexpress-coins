# 06 — Runbook

The operator's document: how to prove the collector can run, how to run one night, where the report
lands, how to read it, and what to check when a night fails. It is written to be followed from a
clean clone, not from a prepared working tree.

Companion documents: [`00-brief.md`](00-brief.md) for the requirements of record,
[`02-architecture.md`](02-architecture.md) for the module architecture,
[`05-app-knowledge.md`](05-app-knowledge.md) for the app's behaviour,
[`07-navigation-map.md`](07-navigation-map.md) for the screen graph,
[`08-stability.md`](08-stability.md) for the nightly outcomes ledger, and
[`adr/`](adr/README.md) for the decisions behind the architecture.

Legend: ✅ executed for this revision · ❓ still unknown · ⚠️ operator action required.

---

## 1. Before the first run

### 1.1 On the machine

| need | why |
|---|---|
| `git` | to clone the repository |
| [`just`](https://github.com/casey/just) | the one command entry point (`just setup`, `just check`) |
| Python **3.14** plus `venv` and `pip` | the local path; `just setup` creates `.venv` with plain `python3 -m venv` |
| `adb` on `PATH` | the local path talks to the tablet directly |
| Docker + Docker Compose | the unattended path; the image already carries `adb`, Python and the OCR runtime |

Do not use uv, poetry, hatch or pdm. The recipes assume none of them, and the image is built from
the pinned `requirements.txt`.

Keep the checkout path free of `{` and `}`: setuptools reads braces as format fields during an
editable install. The worktree supervisor creates brace-free paths.

### 1.2 On the tablet

The reference device is a T3_US tablet running Android 15 and AliExpress `8.180.1`, reached over
wireless adb at `192.168.8.65:5555`. The address is **configuration, not a constant**: both the IP
and the wireless-debugging port move when the tablet reconnects. Verify it every time:

```console
$ adb devices
List of devices attached
192.168.8.65:5555	device
```

If the serial is absent, enable wireless debugging on the tablet and connect once. A container runs
its own adb server, so it does not inherit the host's connection; `doctor` connects a `host:port`
serial itself.

The serial is **configuration**, not part of the artifact. This build is measured for one device
shape and one store region; read §14 before pointing it at a different phone.

The run also needs:

- the AliExpress app installed and **signed in**;
- the tablet **unlocked**, with no PIN or biometric lock (Q2 in
  [`00-brief.md`](00-brief.md) §6 — the collector cannot enter one);
- the charger attached, because a stuck run can hold the tablet for the 20-minute safety-net budget
  (the measured worst night took 6.4 minutes);
- the app allowed to stay foregrounded long enough to finish; a human using the tablet can interrupt
  any run.

### 1.3 The timezone fact ⚠️

`AECOINS_TIMEZONE` **must be the device's own timezone**, not the host's, not UTC by habit. Read it
from the tablet:

```console
$ adb -s 192.168.8.65:5555 shell getprop persist.sys.timezone
America/Toronto
```

Set the same value in the environment (local run) or in `.env` (Compose):

```sh
export AECOINS_TIMEZONE=America/Toronto
```

Why it matters: the nightly start (`AECOINS_SCHEDULE_TIME`, default `03:30`) and the daily state
store's day boundary both follow `AECOINS_TIMEZONE`. If the collector's timezone is ahead of the
tablet's, it fires **before the device's day has rolled**. Today's coins are already claimed, the
fresh-day path is never exercised, and the run reports success while earning nothing. The `.env`
example ships `AECOINS_TIMEZONE=UTC`; on this tablet that is the wrong value.

The collector checks this comparison at startup. `doctor` prints both values (see §3), and
`run --schedule` **refuses to schedule** (exit `2`, before waiting) when they disagree. The error
names the configured zone and the device zone:

```console
$ AECOINS_DEVICE=192.168.8.65:5555 AECOINS_TIMEZONE=UTC \
    .venv/bin/python -m aliexpress_coins run --schedule
error: configuration: refusing to schedule: configured timezone UTC; device timezone America/Toronto; the schedule would fire before the device day rolls. Set AECOINS_TIMEZONE to the device zone (`adb -s <serial> shell getprop persist.sys.timezone`).
```

If the device property cannot be read at startup (a transient adb outage, for example), the
scheduler **warns and keeps the schedule alive**; it checks again immediately before each night and
refuses that night, with the reason, if the zone is still unreadable or has since disagreed. A
transient reboot therefore costs no future night.

A manual `run --once` is supervised, so it **warns** in the log and on stderr with the same two
values and continues; use it to diagnose, not to hide a mismatch. The scheduler is the unattended
path, so it refuses at fire time.

The **daily reset is observed at 00:00 device-local** ✅, bracketed by three dry runs across the
2026-11-23/24 boundary ([`05-app-knowledge.md`](05-app-knowledge.md) §5c). The nightly default remains
03:30 plus up to three minutes of jitter, safely after that reset; §7 records why the later fire is
retained and its retry-window trade-off. See §9 for what to check when a night claims nothing.

---

## 2. Quickstart from a clean clone

Verified from a clean clone on 2026-11-23 with no `runs/`, `state/` or `.venv/` copied in (full
command table in §13; timed onboarding path in §2.1):

```console
$ git clone git@github.com:alexmi256/aliexpress-coins.git
Cloning into 'aliexpress-coins'...
$ cd aliexpress-coins
$ just setup
...
Successfully installed aliexpress-coins-0.1.0
$ just check
...
Required test coverage of 99.50% reached. Total coverage: 99.52%
======================= 1880 passed in 213.71s (0:03:33) =======================
All quality gates passed.
```

`just check` is the gate: ruff lint, ruff format check, mypy strict over `src/` and `tests/`, then
pytest with the coverage ratchet (current floor in `pyproject.toml`). A green test run without the
lint, format and type checks is not a green gate.

The clean clone has no device configuration yet. For a local run, export the two values that name
the device and its timezone:

```sh
export AECOINS_DEVICE=192.168.8.65:5555
export AECOINS_TIMEZONE=America/Toronto
```

For the Compose path, create `.env` from the example and edit the same two values:

```console
$ cp .env.example .env
```

Then set these lines in `.env` (the checked-in example ships `UTC`, which is the wrong zone for the
reference tablet):

```dotenv
AECOINS_DEVICE=192.168.8.65:5555
AECOINS_TIMEZONE=America/Toronto
```

`.env` is gitignored because it names a device on your network. Compose reads it automatically;
the local Python process does **not**, which is why the local path exports the variables instead.

### 2.1 Measured onboarding time

Measured on 2026-11-22/23 from a fresh clone on the build box, with
docker and the tablet reachable. The timed path was: clone → `just setup` → copy and edit `.env` →
local `doctor` → `docker compose build` → container `doctor` → container `run --once --dry-run`
(the safe first run; no live night was taken while the daemon was running). It used a separate
Compose project and image tag so the live daemon's volumes and image were untouched.

Box: Arch Linux, kernel 7.3.5, 16 vCPU, 27 GiB RAM, Docker 29.10.0, Compose 5.8.0, Python 3.14.10,
tablet on the same LAN at `192.168.8.65:5555`.

| stage | seconds |
|---|---:|
| `git clone` (local single branch) | 0.1 |
| `just setup` | 8.9 |
| configure `.env` | < 0.1 |
| local `doctor` | 2.0 |
| `docker compose build` (warm Docker layer cache) | 149.1 |
| container `doctor` | 7.1 |
| container `run --once --dry-run` | 25.3 |
| **total, warm image cache** | **192.4 s (3 min 12 s)** |

The image build dominates: **149 s of the 192 s**. With the Docker layer cache removed
(`docker build --no-cache`) the same image took **478 s (7 min 58 s)**, so a first-time machine
lands around **8 min 41 s** for the same sequence. The base image and this box's pip wheel cache
were already present; a truly empty machine adds their downloads. The apt layer (`adb`, `tini`,
`tzdata`, OpenCV's `libgl*`) and the `requirements.txt` layer (RapidOCR, onnxruntime,
opencv-headless, numpy) are the two heavy steps.

That is over the five-minute target on a cold machine. The shortest fix is a **prebuilt image**
published to GHCR and pulled by Compose instead of built locally; a smaller base or a thinner
requirements set would help less because the OCR runtime is the product. This measurement excludes
`just check` (about 3 min more); onboarding is the setup path, not the contributor gate.

---

## 3. `doctor` — prove the machine, device and OCR

Local path:

```console
$ AECOINS_DEVICE=192.168.8.65:5555 AECOINS_TIMEZONE=America/Toronto \
    .venv/bin/python -m aliexpress_coins doctor
[ok] adb: Android Debug Bridge version 1.0.41 (/usr/bin/adb)
[ok] device: 192.168.8.65:5555 (device)
[ok] geometry: 192.168.8.65:5555: 1200x2000 @ 240 dpi, sdk 35, portrait, profile c9b8f88670e295b5
[ok] timezone: configured timezone America/Toronto; device timezone America/Toronto (192.168.8.65:5555)
[ok] ocr: RapidOCR loaded and read 'COINS' from its probe image
```

Exit status is `0` when every check is `ok` or `skipped`, and `1` when any check is `fail`.
`doctor` is deliberately **read-only**: it may connect the network serial and start the container's
own adb server, but it never taps, launches, installs or changes the app.

What a healthy output means:

- **adb** — the executable exists and runs.
- **device** — the serial named by `AECOINS_DEVICE` answers `adb get-state`. With
  `AECOINS_DEVICE` unset this line is `[skipped]`, which is not a failure.
- **geometry** — the frame the run will actually see, plus the profile id used to key last-resort
  popup calibration. On the reference tablet this is `1200×2000 @ 240 dpi, sdk 35, portrait,
  profile c9b8f88670e295b5`. The profile is a hash of those four facts, not a stored model name.
- **timezone** — the configured `AECOINS_TIMEZONE` (or `TZ`, or the local zone) beside the device's
  `persist.sys.timezone`. Both values are always printed. They must match; a mismatch is a failed
  check, which is what stops a clean doctor from blessing the wrong zone. With `AECOINS_DEVICE`
  unset the device side is `skipped`, not failed.
- **ocr** — RapidOCR loads every bundled model and reads a generated probe image. No network and
  no device are used.

The container path is the same check inside the image ✅ (built from the same clean clone):

```console
$ docker compose build
...
Successfully tagged aliexpress-coins-collector:local
$ docker compose run --rm collector python -m aliexpress_coins doctor
[ok] adb: Android Debug Bridge version 1.0.41 (/usr/bin/adb)
[ok] device: 192.168.8.65:5555 (device)
[ok] geometry: 192.168.8.65:5555: 1200x2000 @ 240 dpi, sdk 35, portrait, profile c9b8f88670e295b5
[ok] timezone: configured timezone America/Toronto; device timezone America/Toronto (192.168.8.65:5555)
[ok] ocr: RapidOCR loaded and read 'COINS' from its probe image
```

If any check fails, stop here. A failure has a troubleshooting row in §10.

---

## 4. `snapshot` — look at the screen without touching it

This build has **no `aliexpress-coins snapshot` subcommand**; the CLI exposes `version`, `doctor`,
`idle` and `run`. Use the read-only adb capture for an operator snapshot ✅:

```console
$ adb -s 192.168.8.65:5555 exec-out screencap -p > /tmp/aecoins-screen.png
$ file /tmp/aecoins-screen.png
/tmp/aecoins-screen.png: PNG image data, 1200 x 2000, 8-bit/color RGBA, non-interlaced
```

The PNG is exactly the frame the collector will read. Its size is the geometry `doctor` reports
(1200×2000 portrait on the reference tablet), not the panel's natural `wm size` (2000×1200).

Screenshots contain the account nickname, cart and order information. Keep them out of git. If one
has to be shared in an issue or document, mask the account areas first; the repository's
`.gitignore` excludes `runs/` for this reason.

Fixture capture is a developer tool, not an operator step:

```console
$ .venv/bin/python -m aliexpress_coins.tools.capture_frames --list
states: 12 frames
  coins.png
  account.png
  ...
```

It writes under `tests/data/` only when explicitly asked to capture a set, and never taps the app.

---

## 5. `run --once --dry-run` — plan the night without tapping

A dry run starts the app normally, may fire the shortcut navigation once (an `am start`, not an
in-app tap), and applies the same deny list as a live run. When the page or its rows are still
rendering, it settles the frame with `wait_until_stable` and performs a bounded row re-read before
planning. It sends **no in-app taps** (`taps_issued: 0`) and still writes a report. The plan records
the page wait, frame-stability evidence (`stable_waits`), row re-read (`content_waits`) and the
row-set confirmation (`roster_waits`) so a partial render cannot be mistaken for an empty day.

```console
$ AECOINS_DEVICE=192.168.8.65:5555 AECOINS_TIMEZONE=America/Toronto \
    .venv/bin/python -m aliexpress_coins run --once --dry-run
2026-11-22 20:54:16,491 INFO {"category":"sign_in","decision":"allow","event":"policy_decision","key":"daily_checkin","label":"Daily coins","reason":null,"rule":"daily_checkin","subtitle":"Earn more > Today Day 3 Day 5 Collect Worth C$0.01 Daily check-in","surface":"rows_present"}
2026-11-22 20:54:16,491 WARNING {"code":"unrecognised","event":"policy_refusal","label":"Search for what you love","matched_field":null,"matched_pattern":null,"reason":"unrecognised","rule":null,"subtitle":"Q","surface":"rows_present"}
2026-11-22 20:54:16,491 WARNING {"category":null,"detail":"unrecognised: unrecognised","event":"task_skipped","key":null,"reason":"policy","title":"Search for what you love"}
2026-11-22 20:54:16,492 WARNING {"code":"spending_order","event":"policy_refusal","label":"Place an order on this page","matched_field":"title","matched_pattern":"Place an order on this page","reason":"spending: placing an order or buying anything is refused","rule":"spending_order","subtitle":null,"surface":"rows_present"}
2026-11-22 20:54:16,492 WARNING {"category":null,"detail":"spending_order: spending: placing an order or buying anything is refused","event":"task_skipped","key":null,"reason":"policy","title":"Place an order on this page"}
run 20261123T015402Z: planned
detail: dry run: 1 task(s) planned, 2 refusal(s); no tap was issued
plan: dry run: 1 task(s) planned, 2 refusal(s); no tap was issued
  1. Daily coins [daily_checkin] category=sign_in rule=daily_checkin
  refused: Search for what you love code=unrecognised rule=None reason=unrecognised
  refused: Place an order on this page code=spending_order rule=spending_order reason=spending: placing an order or buying anything is refused
  taps issued: 0
summary: runs/20261123T015402Z/summary.md
run.json: runs/20261123T015402Z/run.json
```

Read it like this:

- **`surface`** is the screen the planner understood: `rows_present`, `rows_absent`, `boost_page`,
  `redeem_interstitial`, `empty_boost`, `coin_store`, `surprise_grid`, `search_landing`, `product_detail`,
  `non_app`, or `unknown`. `unknown`, `non_app`, `empty_boost`, a redeem interstitial, or a
  `rows_absent` with no readable task row is an unreadable report, not a plan. `non_app` is the
  measured status-clock plus date-widget frame with no app content; a launcher
  with icons stays `unknown` and is identified by the foreground-package probe during browse recovery.
- **steps** are the rows that would be dispatched, in order, with the catalogue key and executor
  category. `Daily coins` is the streak sign-in; it is allowed but not a boost row. When task rows are
  legible on an `unknown` surface, the run fails as `surface_unverified`; `steps` is then evidence of
  what was read, not a forecast that the day is workable.
- **refusals** are the deny-list decisions. `unrecognised` is default-deny: no catalogue rule matched.
  `unrecognised_recently_viewed` is an explicit refusal for the unmeasured family (#116).
  `spending_order` is another explicit refusal. A refusal is a safety feature, not a failure.
- **roster warnings** name visible `GO` buttons that did not become task rows, or known task titles
  that could not be split from another row's copy. A recovered split is still reported; an
  incomplete read keeps the night open instead of letting a swallowed row prove the day is done.
- **`taps_issued: 0`** is the proof that the dry run did not touch the app.

A dry run exits `0` only when it positively read a complete roster. Any unreadable surface — still
loading, no known coin page, a popup or redeem interstitial, the empty `Coins boost` load fault, or
coin-page markers without their task rows — ends `failed` with `navigation_failed` and a `surface_*`
detail (or `aborted` with `unexpected_screen` when the same unreadable frame repeats three times) and
exits non-zero. When task rows were legible but the coin-page marker was missing, the failure is
`surface_unverified` and the rows remain in `steps` as evidence, not as a successful plan. Treat any
non-zero dry run as "retry or inspect", never as "the day is done"; a clean empty day cannot be proven
from the inline coin page alone. The `Coins boost` empty page is a load fault
([`05-app-knowledge.md`](05-app-knowledge.md) §4b), not evidence about the day.

A failed dry run is a **probe failure**, not a reset-hour or empty-roster datapoint. `run.json` carries
`plan.probe_failed: true`, the CLI prints `dry-run probe failed`, and the summary's plan line names it.
A probe wrapper must record that result as "probe failed" and must not append its zero rows to a
reset measurement. Only a `planned` dry run, or a roster read by a real run, is evidence about what
the app offered.

---

## 6. `run --once` — one real night

Once `doctor` and a dry run agree, run one night:

```console
$ AECOINS_DEVICE=192.168.8.65:5555 AECOINS_TIMEZONE=America/Toronto \
    .venv/bin/python -m aliexpress_coins run --once
```

The command exits when the night ends:

| exit | meaning |
|---|---|
| `0` | the run completed, ended on its budget, or produced a dry-run plan |
| `1` | the run failed or was aborted (the report still exists) |
| `2` | configuration error before the device was touched |
| `3` | another run holds this state directory's run lock; nothing was dispatched |

A real run claims the daily sign-in, walks the allowed rows, and writes the same report shape as a
dry run plus screenshots and the roster log.

Every run holds an exclusive lock, `state/run.lock` (under `AECOINS_STATE_DIR`), from before it
touches adb until its report, cleanup and notification are finished. `run --once` takes it without
waiting: if the nightly daemon is mid-night, the manual command exits `3`, names the holder's pid,
host and start time, and sends no tap. The daemon behaves differently when the fire time arrives
while a manual run holds the lock: it waits up to one run budget plus five minutes, then reads the
day again. A manual run that walked the roster and left nothing worth retrying has marked the night
complete, so the daemon skips it; a manual run stopped by its budget, or one whose remaining tasks
failed with retryable reasons, leaves the night open, so the daemon takes it. If the wait expires,
the daemon skips that fire with a `run lock held by another run` event and tries again at the next
fire time. A killed holder cannot lock the collector out: its file descriptor
closes with the process, the kernel releases the lock, and the next run logs a `run_lock_takeover`
warning naming the stale owner before replacing it.

The lock coordinates runs that share a state directory. The container and a host `.venv` run do not
share one by default (the named `state` volume versus the checkout's `state/`), so take the manual
night through the container to share the daemon's lock:

```console
$ docker compose run --rm collector python -m aliexpress_coins run --once
```

Do **not** run a real `--once` just to test the installation. It may claim coins and it may be
impossible to undo. Use `--dry-run` unless you are deliberately taking the night.

### Where the run directory lands

Local runs write under the current directory:

```text
runs/<UTC timestamp>/          # e.g. runs/20261123T015402Z/
├── summary.md                 # the human report
├── run.json                   # the machine-readable twin (schema 6)
├── run.jsonl                  # structured process log, one JSON object per line
├── roster.jsonl               # every roster observation and its diff
├── shots/
│   ├── balance-before.png     # the frame the starting balance was read from
│   ├── balance-before.json    # its OCR boxes and geometry sidecar
│   ├── balance-after.png
│   ├── balance-after.json
│   ├── browse-<key>-foreground-lost.png  # when a browse tap leaves the app
│   ├── browse-<key>-foreground-lost.json # its OCR evidence, before recovery
│   └── failure.png            # best-effort, failed runs only
└── failures/
    └── <key>-attempt-<n>-<UTC timestamp>/
        ├── attempt.json       # failure/abort code, verification, taps/keys/swipes
        └── frame-000.jpg ...  # pinned pre-action frame, last 30 s, three aftermath frames
```

A successful or skipped task writes nothing under `failures/`: the rolling buffer is discarded. A
failed attempt keeps its own directory, so two failures of the same task in one run leave two
artifacts instead of overwriting one name. Frames are JPEG-encoded because encoding happens on the
capture path; reviewed 1200x2000 fixtures measured about 3 ms as JPEG versus about 48-61 ms as PNG
on the development machine. The buffer keeps the last 30 s, capped at 120 frames and 4 MiB per
attempt, with the pre-attempt and last pre-action frames pinned so a long attempt does not lose the
tap. A failure adds three aftermath frames at about 2.5 fps. The worst case is about 4 MiB per
attempt plus the pinned frames, roughly 48 MiB for a 12-attempt run and about 1.4 GiB across 30
retained runs. A run-level abort or operator stop writes the buffer already collected, with the
abort/stop code, and does not spend aftermath frames. The normal run retention
(`AECOINS_KEEP_RUNS`, default 30) removes those directories with the rest of the run.

`AECOINS_RUNS_DIR` overrides the root. In the container it is `/app/runs`, backed by the named
volume `aliexpress-coins_runs`; `AECOINS_STATE_DIR` is `/app/state`, backed by
`aliexpress-coins_state`. Read a report from a finished container run without starting another:

```console
$ docker compose run --rm collector cat /app/runs/20261122T131125Z/summary.md
# Run 20261122T131125Z
...
```

The timestamp is **UTC**, even though the schedule and the daily state use the device's local
timezone. That is deliberate: run ids sort correctly across DST changes.

A browse task checks the foreground package before its `GO` and before accepting an unknown
destination. A measured clock/date `non_app` frame, or a package probe naming another app, stops the
arrival wait: the executor does not wait for a timer or tap `Collect`, saves
`browse-<key>-foreground-lost.*` (a repeated loss for the same key gets `-2`, `-3`, … so an earlier
frame is never overwritten), warm-starts AliExpress if needed, fires the shortcut route, and only
then reopens the populated boost list for positive verification. `arrival.left_app` and the saved
frame's labels are kept in `run.json`. If fresh proof is absent the task remains `task_unverified`;
it is never called complete from the launcher frame. If foreground loss is detected before `GO`, the
executor recovers but records `foreground_lost_pre_action` without action-based verification, because
a missing row after roster rotation is not proof that this task completed. If the package probe is
unreadable on an unknown destination, that page is still accepted as the destination, but the
executor warm-starts before the return route so an icon launcher cannot cold-link into the empty-boost
state. If initial route discovery also returns only `non_app`, the run fails as `navigation_failed`
instead of declaring an empty night.

---

## 7. `run --schedule` — the nightly daemon

Container (the normal unattended path):

```console
$ docker compose up -d
$ docker compose ps
$ docker compose logs -f collector
```

Local foreground equivalent:

```sh
AECOINS_DEVICE=192.168.8.65:5555 AECOINS_TIMEZONE=America/Toronto \
  .venv/bin/python -m aliexpress_coins run --schedule
```

The daemon waits for `AECOINS_SCHEDULE_TIME` (default `03:30` local) plus jitter from
`AECOINS_SCHEDULE_JITTER_SECONDS` (default 180 seconds), runs one night synchronously, then asks
the trigger for the next fire time. A night already marked complete in the daily state is skipped;
a night whose walk left a retryable task failure is not marked complete, so the day stays open for a
later run. A run-level failure is likewise not silently treated as complete, so the next day's fire
tries again.

**Cron and fire-time rationale.** `AECOINS_SCHEDULE_TIME=03:30` is cron `30 3 * * *` in
`AECOINS_TIMEZONE`; up to 180 seconds of jitter makes the actual fire 03:30–03:33 local (03:32 in the
example below). The reset is observed at **00:00 device-local**, so this late slot is safely after the
roll. It is retained because it is less likely to compete with a human using the tablet in the
evening. The cost is recovery time: a failure at 03:32 leaves roughly 20 hours in which tasks are
still claimable, and the daemon has no second scheduled fire that day. The planned in-run retry loop
(#123) will re-enumerate a retryable failure within the same night instead of forfeiting it once it
lands, which narrows that cost; it will not restore the extra hours an earlier fire would leave for a
supervised retry if the whole run aborts. Firing shortly after the reset (for example 00:20) would
leave room for that supervised retry, at the risk of driving a tablet while someone is still using it.
The 03:32 fire is
therefore an operational choice, not a reset-time estimate; change it only with a supervised first
run or an operator retry plan. The former 03:10 reset probe is retired now that the boundary is
measured.

**What "complete" means.** A finished roster walk marks the day complete only when the roster was
read completely and nothing dispatched is worth retrying: every row either completed, was
skipped/refused (policy, daily limit, or the app reporting it unavailable), or failed with a failure
class that spends no retry. Any `failed` row whose taxonomy entry is retryable (`task_unverified`,
including a timeout or a recovered unknown screen), or a visible action that could not be attached
to a task row, keeps `completed_night` false even if another row completed, because unclaimed work
may still be hidden by the splitter. This is a cross-run retry, not the in-run rule that
`acted=True` blocks a repeated tap. A task that acted without proof is recorded in
`acted_unverified`; its next dispatch first re-reads the fresh task surface. A row that still shows
its unclaimed `GO` may proceed, a row that is gone/checked on a complete surface is reported
`already_done` with no tap, and an unreadable or partial surface stays retryable rather than
guessing. The marker remains until the row is proven done — a recorded completion or a terminal
executor outcome — and every dispatch while it remains re-checks instead of tapping from a stale
roster. The decision is recorded as `last_night` in the daily state and printed by the report as
one `Night:` line.

**The in-night loop (#123).** A night is no longer one roster sweep. After every real attempt —
completed, failed, or a row that vanished between enumeration and dispatch — the engine
re-enumerates the surface and continues while something is still dispatchable. An unreadable or
unknown surface restarts from the shortcut route instead of ending the night. A full pass that
finds no allowed, unblacklisted work may complete the day. Three bounds keep this finite:
`AECOINS_MAX_PASSES` (default 4 full sweeps), `AECOINS_MAX_TASK_ATTEMPTS` (default 2 failed
attempts of one task) and the existing wall-clock/task budgets. A verified success resets that
task's failure streak, so repeatable rows continue until their daily `max_per_run`. A task that
keeps failing is blacklisted for the remainder of that local day with
`reason: blacklisted after N attempts`; the rest of the roster continues and the report records the
attempts and the blacklist. The blacklist
lives in the day-keyed state file, so a new local day starts empty, and a real completion clears the
entry. A policy refusal is permanent by design and is never counted as a blacklist attempt. Hitting
the pass bound ends the run as `budget`, never as complete.

A real schedule wait looks like this ✅:

```console
$ docker compose logs --tail 5 collector
collector-1  | 2026-11-23 00:54:29,746 INFO {"event":"schedule_wait","next_fire":"2026-11-23T03:32:06.380161-05:00","seconds":27456.634}
```

`03:32:06-05:00` is 03:30 Toronto plus about two minutes of jitter. `seconds` is the remaining wait,
not a second delay.

Stopping: `docker compose stop` sends SIGTERM; the image's `tini` forwards it, the run raises
`RunInterrupted` at its next seam, the report is written atomically, and Compose waits up to
`stop_grace_period: 5s`. `restart: on-failure:5` retries a failed daemon a bounded number of times
rather than looping forever on a configuration error. One daemon is still the intended deployment,
but two schedulers pointed at the same state directory no longer race: the second waits for the run
lock, then re-reads the day and skips a night the first one completed. Two runs against **different**
state directories are not coordinated and would still drive the same tablet.

Before this command waits, it checks the configured zone against the device and refuses on a
mismatch (§1.3). An unreadable property at startup only warns; the check runs again immediately
before each night and refuses that night if it is still unreadable or now mismatched. Record every
finished night in [`08-stability.md`](08-stability.md); two consecutive weeks of rows is the release
gate in #41.

---

## 8. Reading `summary.md` and `run.json`

`summary.md` is the morning read. The header is one bullet per fact:

| line | what it tells you |
|---|---|
| **Status** | `completed`, `budget`, `stopped`, `planned`, `aborted` or `failed` |
| **Budget** | seconds used of the wall-clock budget |
| **Passes** | full re-enumerated sweeps used, of the configured `AECOINS_MAX_PASSES` |
| **Attempts per task** | real dispatches per catalogue key, with the configured per-task limit |
| **Blacklist** | tasks skipped after their attempt budget, with the attempt count |
| **Device** | serial, adb state, whether the device was asleep and whether stay-awake was set |
| **Geometry** | frame size, density, SDK, orientation and calibration profile id |
| **Surface** | the screen family the roster was read from (`rows_present`, `boost_page`, ...) |
| **Route** | the route used and the routes tried, including a warm re-entry |
| **Roster** | row count, allowed count and refused count, with the read status and any swallowed-row warning |
| **Observations** | how many roster diffs were logged in `roster.jsonl` |
| **Balance** | before and after, with the delta when both frames were readable |
| **Popups closed** | verified dismissals by family, or `none` |
| **Back on coins page** | whether the cleanup route proved the coin page |
| **Detail / Failure / Abort / Run error** | the terminal sentence and the named failure, when present |
| **Failure recordings** | one row per recorded attempt, naming its artifact directory and frame count |

### A good night

A good night looks like:

- `Status: completed`;
- `Roster: available; N row(s), M allowed, K refused` with `N` greater than zero;
- at least one task row with `completed` and an evidence value that names what proved it (a balance
  move, a progress badge, a row gone from a complete surface, or a recorded state transition);
- `Balance: <before> -> <after> (<delta>)` with a non-negative delta, or `unknown` when a frame was
  unreadable — never a guessed number;
- `Back on coins page: yes`;
- no `Failure`, `Abort` or `Run error`;
- `Notification: delivered via apprise` when notifications are configured (delivery failure is
  recorded but does not change the run's status).

The task table in `summary.md` has one row per dispatched task:

```text
| # | task | key | code | outcome | evidence | seconds | detail |
```

`key` is the catalogue key, `code` is the failure code for a task that did not verify, and
`evidence` is the executor's own proof. In `run.json`, a task's `state` is `in_flight` while its
executor is running (a report left by a killed run can show this), `aborted` when the run stopped
mid-task, and otherwise the executor's own outcome. A task that was running when the run aborted
keeps its row with `outcome=aborted`; its `code` and `evidence` name the abort and the `detail`
names the screen. `Skipped` lists rows that were never dispatched and groups
them by `reason`: `policy` (deny list), `no executor`, `no action`, `daily limit`, `run budget`,
`blacklisted after N attempts`, `stopped` or `not selected`.

When a task fails, a `## Failure recordings` table follows the task table with one row per recorded
attempt: the task, attempt number, failure code, frame count and the artifact directory under
`failures/`. A run-level abort or operator stop also flushes the frames already collected. A run
with no recorded failure has no such section and no recording paths.

### A budget-exhausted night

A budget stop is a **clean end**, not an error:

- `Status: budget`;
- `Failure: budget_exhausted — The run used its whole time or task budget; the remaining rows wait
  for tomorrow.`;
- `Detail` names either `wall-clock budget of ...s exhausted`, `task budget of N dispatch(es)
  exhausted`, or `pass budget of N exhausted` when the in-night loop reached its sweep bound;
- remaining rows appear under `Skipped` with reason `run budget`;
- the process exits `0`, and the nightly daemon treats the night as finished for scheduling
  purposes only if the walk reached `completed` **and** left nothing worth retrying. A manual budget
  stop does **not** mark the day complete, so it does not swallow the nightly window.

The `AECOINS_RUN_BUDGET_SECONDS` default of **1200 s (20 minutes)** is a safety net, not an expected
duration. The worst night observed (run `20261124T083006Z`) took **386.7 s — 6.4 minutes — with ten
dispatches**, and a night with nothing dispatchable exits in under a minute. The retry loop and the
daily `max_per_run` limits allow about twelve dispatches at roughly 25-40 s each over at most four
passes, so a legitimate worst case is about 8-12 minutes. Twenty minutes covers that with headroom
while still ending a stuck executor loop within 20 minutes. The setting stays operator-overridable:
raise it for a slower device or a deliberately larger `AECOINS_MAX_PASSES`, not to paper over a slow
network or a stuck screen.

The budget is checked between dispatches and, while a task is running, at that task's own clock,
sleep and capture seams (`engine._task_budget`), so a stuck executor cannot poll past the deadline.
A task cut off this way leaves its in-flight row as `aborted` with `budget_exhausted`, while the
report still lists every row that was allowed at the stop. The budget does not interrupt an adb
command already in flight, but each command has its own subprocess timeout (30 s for a screenshot,
20 s for a route/hygiene command, 10 s for an input tap or swipe), so the worst overrun from an adb
command is that command plus report and cleanup time. A native OCR/PIL decode has no in-process
timeout, so a native call that never returns has no in-process or host-level bound; in the container
deployment only the stop grace and SIGKILL remain.

If the budget is routinely exhausted, look at the wall-clock budget
(`AECOINS_RUN_BUDGET_SECONDS`, default 1200 s), the task cap
(`AECOINS_MAX_TASKS_PER_RUN`, default 12), the pass bound (`AECOINS_MAX_PASSES`, default 4), the
per-task attempt bound (`AECOINS_MAX_TASK_ATTEMPTS`, default 2) and the per-task time in the table.
Raise a limit only deliberately; a larger budget is not a fix for a slow network or a stuck screen.

### `run.json`

`run.json` is the same report plus every field a script may need. Schema `6`. The important keys:

| key | meaning |
|---|---|
| `schema` | report schema; `6` is this build |
| `run_id`, `status`, `detail` | identity and terminal sentence |
| `started_at`, `finished_at`, `budget` | wall-clock evidence |
| `hygiene`, `geometry` | what the device said before the first capture |
| `surface`, `roster`, `route_error`, `observations` | what the roster looked like |
| `balance_before`, `balance_after`, `balance_delta`, `balance_note` | the two recorded frames, or why one is unknown |
| `popups_closed`, `shots`, `artifact_errors` | artifacts and best-effort write failures |
| `failure_recordings` | one entry per recorded attempt: task/key/attempt, failure or abort code, artifact directory, frame and sidecar paths |
| `notification` | attempted, sink, ok and a class-plus-status error |
| `plan`, `plan_note` | the dry-run plan, when there was one; `plan.probe_failed` distinguishes an unreadable probe from evidence about the day, and `stable_waits` / `content_waits` / `roster_waits` record settling, row re-reads and row-set confirmation |
| `abort` | safety code, reason and the screen that triggered it |
| `night` | the completion decision: `outcome`, dispatched/completed counts and retryable failures |
| `passes`, `max_passes`, `max_task_attempts`, `attempts`, `blacklist` | the in-night loop's sweeps, per-task dispatch attempts and tasks skipped at their attempt bound (#123) |
| `failure` | code, stage, retry scope, retries, `abort_run`, sentence and detail |
| `error` | the raw exception for a failed run, in addition to the named failure |
| `tasks[]`, `skipped[]` | the task and skip tables in structured form; each task carries `state` (`in_flight`, `aborted`, or the executor outcome) |

**Balance provenance.** The engine writes the `balance-before` frame, then **reloads the PNG and
its OCR sidecar from `shots/`** and reads the balance from that copy. It does the same for
`balance-after`. The summary's delta is therefore recomputed from the shipped frames, not from a
number that only existed in memory. If a frame cannot be written or read, `artifact_errors` records
it and the balance is left `unknown` rather than guessed. To audit a night, open the two PNGs and
their `.json` sidecars; the balance is the bare number in the top quarter of the coin page.

### `run.jsonl` and `roster.jsonl`

`run.jsonl` is the process log. Every line is a JSON object; the useful `event` values are
`run_started`, `run_hygiene`, `policy_refusal`, `task_skipped`, `task_outcome`, `failure_retry`,
`roster_observe_failed`, `run_aborted`, `run_failed`, `notification_sent` / `notification_failed`,
`runs_pruned` and `run_finished`. The level is on every line, so a pipeline can filter
`level=ERROR` without parsing prose.

`roster.jsonl` is the observed task list over time: each observation, what appeared, what
disappeared and the route used. It is the record that answers "was the row ever offered?" without
replaying the phone.

---

## 9. When a night claims nothing

The daily reset is **✅ observed at 00:00 device-local**, bracketed by dry runs at 23:25, 23:45
and 00:05 EST ([`05-app-knowledge.md`](05-app-knowledge.md) §5c). The task day is decided server-side;
changing the tablet's timezone did not clear completed task rows in the 2026-11-22 session. Treat a
run before the roll as a successful, empty night: it can report `completed` with a zero delta.

The schedule is **03:30 plus up to 180 s of jitter** (03:30–03:33 local), safely after the observed
reset. §7 records why that late fire is retained and the trade-off with an after-reset fire; 03:30 is
not a reset estimate. The former 03:10 probe is no longer needed. A dry run that cannot settle and
read its surface is still a **probe failure** (`plan.probe_failed: true`), not an empty-roster
datapoint; retry or inspect it rather than counting its zero rows as evidence about the day.

If a night claims nothing, check in this order:

1. **Status and night.** `budget` means the walk ran out of time or tasks; `completed` with zero
   completed tasks means the roster was read but nothing was dispatchable, unless the report's
   `Night:` line says `open`, which means a retryable task failure kept the day available for a
   later run; `aborted` or `failed` means read the code in §10 first.
2. **Balance delta.** If it is `unknown`, the balance frames were not readable; inspect
   `balance_note` and `artifact_errors`. If it is `0`, the app was already claimed or no allowed
   row was available. If it is negative, the run should have aborted with `balance_decreased`.
3. **Roster.** `Roster: nothing available` or a `Surface: unknown` with zero rows is a load/route
   problem, not an empty day. Re-read §4b of `05-app-knowledge.md` and try a warm start: start the
   app normally, wait for the home feed, then send the shortcut intent.
4. **Skipped reasons.** `policy` rows are refusals and are expected; `no executor` means the row is
   in the catalogue but this build has no executor for its category; `daily limit` means the state
   file already counts the task today; `run budget` means the cap stopped the walk.
5. **Timezone.** Confirm `AECOINS_TIMEZONE` equals
   `adb shell getprop persist.sys.timezone`; `doctor` prints both lines, and the scheduler now
   refuses a mismatch instead of starting. A mismatch is the classic "successful, empty night".
6. **The device's day.** Open the app and look at the task rows. If they carry the drawn check or
   are absent because the server has already credited them, the day is claimed. Wait for the next
   reset; do not re-press a claimed card.
7. **State.** Read `state/daily-<YYYY-MM-DD>.json`. `completed_night` is the scheduler's skip flag,
   `last_night` records the decision behind it (`exhausted` or `retryable_failures`, with the
   dispatch and failure counts), and `acted_unverified` names tasks that acted without proof and
   still need a fresh-row re-check. A file that fails validation is moved aside as
   `.corrupt-<timestamp>` and the day starts empty with a warning; that is deliberate, but it means
   a resumed run may repeat a task.

A night that finds the signed-out screen stops at the first detection with `app_logged_out`; it
does not report every remaining task as separately failed.

---

## 10. Troubleshooting by failure code

Every class in `src/aliexpress_coins/runner/failures.py` has one row below. The **Retry** column is
the taxonomy's own value (`stage ×1`, `task ×1` or `none`); `tests/test_docs.py` fails `just check`
if a code is missing or the retry column disagrees with the table. A **stage** retry repeats a read
or navigation; a **task** retry repeats a row dispatch only when the executor recorded no tap, key,
swipe or page press. A result that may already have acted is reported, never repeated.

| code | what it means | what the operator should check | Retry |
|---|---|---|---|
| `device_offline` | The serial stopped answering adb, even after one re-read. | `adb devices`; `adb connect <host:port>`; same LAN; the wireless port may have moved. In Docker the container has its own adb server: `docker compose exec collector adb devices`. | stage ×1 |
| `device_sleeping` | The screen stayed dark after `KEYCODE_WAKEUP`. | Charger and battery; wake the tablet by hand; `adb shell dumpsys power` for `mWakefulness`; check `settings get global stay_on_while_plugged_in`. | stage ×1 |
| `device_locked` | The keyguard still showed after wake and swipe. A PIN or biometric lock cannot be driven. | Unlock the tablet; remove the lock or leave it unlocked (Q2); `adb shell dumpsys window` and look for a keyguard. | none |
| `app_not_installed` | `pm path com.alibaba.aliexpresshd` returned no package. | `adb shell pm path com.alibaba.aliexpresshd`; install or update AliExpress; confirm the right Android user/profile. | none |
| `app_launch_failed` | The package exists but `FirstActivity` did not start. | `adb shell am start -n com.alibaba.aliexpresshd/com.aliexpress.app.FirstActivity`; `adb shell am force-stop com.alibaba.aliexpresshd` then retry; check for an app update or a disabled activity. | stage ×1 |
| `unsupported_geometry` | The frame reports no measured profile or calibration. This is a taxonomy refusal, not the capture path's response to a different portrait geometry; see §14. | `doctor`; `adb shell wm size`, `wm density`, `getprop ro.build.version.sdk`; compare [`02-architecture.md`](02-architecture.md), §14 and #42; add calibration rather than forcing a different size. | none |
| `frame_undecodable` | A screencap arrived but could not be decoded. | `adb -s <serial> exec-out screencap -p > /tmp/frame.png`; `file /tmp/frame.png`; check free disk, the USB/network path and the screen. A truncated frame is not retried because the device did answer. | none |
| `app_logged_out` | The app shows a sign-in prompt instead of the account surface. | Sign in on the tablet; confirm the Account tab loads; `adb shell dumpsys window` for the focused app; run again. The run stops at the first detection. | none |
| `navigation_failed` | The route did not reach the screen it promised; a live run records a fallback and may continue from the inline surface, while a dry run has no fallback and exits non-zero with a `surface_*` detail. When rows were legible but the surface was unrecognised, the detail is `surface_unverified` and the rows stay in `steps` as evidence. | Read `Route fallback` and `route_error`; for a dry run, read the `surface_*` detail and `plan.probe_failed`, then retry once the page settles; start the app normally, settle ~10 s, then send the shortcut intent; check for the empty `Coins boost` page and an app update that changed labels. | none |
| `popup_unclosable` | A popup had no safe dismissal: no known label, no template match and no calibration for this profile. | Open the failure frame in `shots/`; note the app version and the `doctor` profile id; update the popup corpus (#11) or the profile calibration (#42). Never tap a guessed coordinate. | none |
| `task_unverified` | The task did not prove completion on screen. | Read the task row's `code`, `outcome`, `verification` and `detail`; open the attempt directory named in `failure_recordings` (or the summary's `Failure recordings` row) and watch the frames around the tap; inspect `run.jsonl`; check `tasks[i].result.acted`. A task that acted is not retried, so verify by hand before the next night. | task ×1 |
| `unexpected_screen` | The same unrecognised or dead screen was seen three times in a row. Inside a task that fails the task and the run tries three shortcut entries back to the coins page; the run aborts only when that recovery fails. | Read `tasks[i].result.payload.screen` for a task-scoped failure, or `abort.screen` labels and fingerprint when the run aborted; check for an app update, a new popup or a hybrid page that did not render; capture a fixture for #11. | none |
| `dangerous_control` | A short buy/checkout/cart/redeem action caption (for example `Buy now`, `Add to cart` or `Place order`) appeared while a task was running and was treated as a live control. A caption painted inside a product photograph on the surprise grid is not such a control: that card is skipped with a `tile_skipped_dangerous` event and the run continues. | Read `abort.screen`; do not tap it; confirm no purchase or redemption happened; check whether the app changed a row or layout; file an issue with the redacted frame and the label that triggered it. | none |
| `balance_decreased` | The coin balance is lower than the baseline recorded before the first task. | Compare `shots/balance-before.*` and `shots/balance-after.*`; check account activity and orders; something acted outside the policy or a human used the tablet. Do not rerun until the cause is known. | none |
| `budget_exhausted` | The wall-clock or task budget ended the walk; the remaining rows wait for tomorrow. | Read `budget.seconds` / `used_seconds`, the number of tasks, and the per-task seconds; check network and OCR speed; raise `AECOINS_RUN_BUDGET_SECONDS` or `AECOINS_MAX_TASKS_PER_RUN` only deliberately. | none |
| `internal_error` | An unexpected exception escaped; the report was still written. | Read `error` and the last events in `run.jsonl`; reproduce with `--dry-run`; file an issue with the run id, the traceback and the report. Do not edit the run directory to hide it. | none |

### Run-level statuses that are not failures

| status | meaning | operator action |
|---|---|---|
| `planned` | a dry run read a complete roster and wrote its plan | read §5; an unreadable surface is `failed` or `aborted`, not `planned` |
| `completed` | the walk ended normally | read §8; verify the delta and the task evidence |
| `budget` | the budget stopped the walk cleanly | read §8; the remaining rows are in `Skipped` |
| `stopped` | SIGTERM/SIGINT ended the run between tasks | the report is valid; restart the daemon if it was not an operator stop |

---

## 11. Notifications

Notifications are optional and go through an Apprise API server. Set `AECOINS_APPRISE_URL` (and
optionally `AECOINS_APPRISE_KEY` and `AECOINS_APPRISE_TAG`) in `.env` or the environment. With
Apprise configured, every run sends one message; without it, only a failed run or a run that
claimed nothing (when `AECOINS_NOTIFY_ON_EMPTY=true`) is sent through the legacy webhook/ntfy
transports.

The message contains the run status, the failure code and sentence when there is one, counts by
task outcome, the skip counts and the report path. It never contains the account name, an absolute
balance or order details. `AECOINS_NOTIFY_BALANCE_DELTA=true` adds the balance *delta* only.
Delivery failure is logged with the exception class and an HTTP status, recorded in `run.json`
under `notification`, and **never changes the run's exit status**.

Configure one transport. If `AECOINS_APPRISE_URL` is set, Apprise is used; a webhook and an ntfy
topic are legacy alternatives, and setting all of them sends more than one message.

---

## 12. Retention and cleanup

`AECOINS_KEEP_RUNS` (default 30) keeps the newest run directories **including the current one**.
Retention only deletes directories whose names match `YYYYMMDDTHHMMSSZ`; files, symlinks and
operator directories such as `runs/manual` are left alone and logged as `runs_not_pruned`.
Pruning runs after the report is final and after the device settings are restored, so a slow
notifier cannot leave rotation pinned.

State files are one per local day: `state/daily-<YYYY-MM-DD>.json`. Each file carries the day's
completion counts, `completed_night` (the scheduler's skip flag), `last_night` (the outcome of the
last finished walk: `exhausted` or `retryable_failures`, with counts), `acted_unverified` (tasks
that acted without proof and must be re-read before another tap) and `blacklist` (tasks skipped for
the rest of that local day, with the attempts spent; the next day's file starts empty). The
directory also holds
`state/run.lock`, the exclusive run lock described in §6; leave it in place while a run is active.
Keep at least the days you want to audit; the store does not prune them itself. Do not hand-edit a
state file while a run is using it. A corrupt file is quarantined, not silently reused.

---

## 13. What was executed for this revision

Executed on **2026-11-23** against device `192.168.8.65:5555`, app `8.180.1`, timezone
`America/Toronto`, from a clean clone (with an isolated Compose project name so the running nightly
daemon container and its volumes stayed untouched):

| command | result |
|---|---|
| `git clone git@github.com:alexmi256/aliexpress-coins.git` | fresh clone, no `runs/`, `state/` or `.venv/` |
| `cp .env.example .env` + set `AECOINS_DEVICE=192.168.8.65:5555` | `.env` created from example |
| `docker compose build` | image built and tagged `aliexpress-coins-collector:local` |
| `docker compose run --rm collector python -m aliexpress_coins doctor` *(with default `UTC`)* | `[fail] timezone: configured timezone UTC; device timezone America/Toronto ...`, exit `1` |
| `docker compose run --rm collector python -m aliexpress_coins doctor` *(with `America/Toronto`)* | all five checks `[ok]` (`adb`, `device`, `geometry` `c9b8f88670e295b5`, `timezone`, `ocr`), exit `0` |
| `docker compose run --rm collector python -m aliexpress_coins run --once --dry-run` | `run 20261123T170916Z: planned`, `surface: rows_present`, 2 planned / 1 refused, `taps issued: 0`, exit `0` |
| `docker compose run --rm collector cat /app/runs/20261123T170916Z/summary.md` | report read cleanly from the `runs` volume |
| `docker logs --tail 5 aliexpress-coins-collector-1` *(live daemon)* | `schedule_wait` for `2026-11-24T03:31:31.431469-05:00` |
| `just setup && just check` | ruff, format check, mypy strict, **1880 passed**, coverage **99.52%** (floor **99.50%**) |
| `.venv/bin/python -m aliexpress_coins.tools.capture_frames --list` | `states: 12 frames`, `flows: 14 frames`, `popups: 7 frames` |

Deliberately **not** executed here:

- `run --once` without `--dry-run`: a real run taps and may claim coins on the shared tablet; the
  dry-run transcript is the safe proof.
- `docker compose up -d` / `stop` / `down`: a live nightly daemon container was already running on
  the build box and must not be stopped or recreated; the schedule log above was read from it.

---

## 14. Portability limits

This is a measured collector, not a universal AliExpress client. Everything below is a limit of
the artifact, not a preference; a new phone or store region needs its own measurement before it
gets an unattended night.

| limit | measured value | what breaks if it differs |
|---|---|---|
| device serial | `AECOINS_DEVICE`, e.g. `192.168.8.65:5555` | The IP and wireless-debugging port move when the tablet reconnects. `doctor` reports the serial it can reach; a container needs its own `adb connect` (it does this itself). The serial is never compiled in. |
| screen geometry | `c9b8f88670e295b5` = 1200×2000 @ 240 dpi, sdk 35, portrait | This is the only profile with measured row positions and popup calibration. A different portrait geometry is **not** refused at capture; only a popup whose calibration is absent for that profile is refused, as `popup_unclosable` rather than a guessed tap. `doctor` prints the new profile and `--dry-run` shows what the planner can read. Forcing a different `wm size` is not a substitute for measuring the new profile (#42). |
| Android / app | Android 15 (sdk 35), AliExpress `8.180.1` | An update can move a row, change a label or add a popup. The deny list still refuses unknown rows, but the allowed catalogue may stop matching; capture a fixture and update the catalogue rather than tapping a guess. |
| store region / locale | AliExpress Canada store, `en`, coin values in `C$` | The catalogue and patterns were built from this region. Another region or locale changes labels, row ordering and currency rendering; treat it as unmeasured and re-run recon. |
| account and device use | one signed-in account, one device, no concurrency | The collector assumes the tablet is unlocked with no PIN/biometric lock, the charger is attached, and no human is using it. Runs that share a state directory serialise on `state/run.lock`; runs with different state directories are not coordinated and would each drive the device. |
| daily reset hour | ✅ observed at 00:00 device-local, bracketed by 23:45 and 00:05 EST dry runs; schedule is 03:30 local plus up to 180 s jitter | A run before the device's day rolls finds every task already claimed and reports a successful, empty night. Keep the schedule after midnight; weigh human tablet use against same-night retry room before moving it (§7, §9). |
| balance and verification | OCR on the shipped frame | Account data is never in git; screenshots contain the nickname, cart and orders. A frame that cannot be read leaves the balance `unknown`, never guessed. |

### Before the first unattended night

A user on their own phone must do these in order, with the daemon **not** running yet:

1. `doctor` — all five lines `ok`, including the timezone line with both zones equal. The geometry
   line gives the profile; on another phone it will not be `c9b8f88670e295b5`, and that is not a
   capture refusal. It is an unmeasured profile whose popup calibration is absent.
2. `run --once --dry-run` — read the plan. `taps_issued: 0` is required. The plan shows what the
   new frame can read before a real night; a popup with no calibration for the new profile will
   report `popup_unclosable` instead of tapping. An unreadable surface (`surface: unknown` or
   `surface_incomplete`) exits non-zero rather than `planned`; treat it as a load/timing fault and
   retry or inspect the screen, not as an empty day.
3. One supervised `run --once` — take one real night by hand, well away from the scheduled fire
   time, and read `summary.md`, the task evidence and the balance delta. Do not start the
   scheduler until this night is understood.

Only then start `run --schedule` (Compose: `docker compose up -d`), and append its first row to
[`08-stability.md`](08-stability.md). A manual `--once` beside the daemon is safe when both use the
same state directory: the manual run exits `3` while the daemon holds the day, and the daemon waits
for a manual run and then re-checks `completed_night` (§6). A host run and the container daemon use
different state directories by default, so use the container command when you mean to share the
daemon's day.
