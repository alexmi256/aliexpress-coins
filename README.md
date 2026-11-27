# aliexpress-coins

**Operator overview:** [What a claim run does](#what-a-claim-run-does) — the route, current loop,
bounds, refusals and evidence.

## What it is

An unattended collector for the AliExpress in-app **coins** programme. It drives the Android app
over wireless `adb` using screenshots and OCR — claiming daily sign-ins, browse-and-wait tasks,
the coins quiz, surprise items and notification tasks — and it never spends money, adds to cart,
places orders or redeems coins.

<img src="tests/data/states/coins.png" width="320" alt="AliExpress Coins page (balance redacted)">

## Why it exists

AliExpress coins are worth little individually (≈ C$0.01 per coin). The value is not spending
twenty minutes every night tapping through promos and popups, and having a clear report when
something breaks.

## Requirements

- **Docker + Docker Compose** (the image bundles `adb`, `tini`, Python 3.14 and RapidOCR — no
  host Android SDK or Python needed).
- **An Android device on the same LAN with wireless `adb` enabled**, signed in to AliExpress,
  plugged into a charger, and **unlocked** (no PIN or biometric lock; the collector cannot enter
  one and stops with `device_locked` if the keyguard stays up).
- **Measured baseline:** AliExpress `com.alibaba.aliexpresshd` **`8.180.1`** on Android 15 (SDK
  35), `1200×2000` portrait @ 240 dpi (profile `c9b8f88670e295b5`), Canada / `en` store (`C$`).
  Another portrait phone is not refused at capture, but any popup lacking calibration for its
  profile fails safely as `popup_unclosable` instead of tapping a guess; another app version or
  store locale may change labels. See [`docs/06-runbook.md`](docs/06-runbook.md) §14 for the full
  portability limits.
- **Timezone rule:** `AECOINS_TIMEZONE` **must equal the device's `persist.sys.timezone`**. Both
  the nightly schedule and the daily state boundary follow it; if it disagrees with the device,
  the schedule can fire before the device's day rolls and report a successful, empty night.
  `doctor` prints both zones and fails on mismatch; `run --schedule` refuses to start (exit `2`).

## Quick start

### 1. Clone and configure `.env`

```sh
git clone git@github.com:alexmi256/aliexpress-coins.git
cd aliexpress-coins
cp .env.example .env
```

Edit `.env` and set your device serial (`host:port`) and its IANA timezone (if you have `adb` on
the host, read it with `adb -s <host:port> shell getprop persist.sys.timezone`; otherwise leave
`AECOINS_TIMEZONE=UTC` for step 2 and `doctor` will print the device's timezone for you):

```dotenv
AECOINS_DEVICE=192.168.8.65:5555
AECOINS_TIMEZONE=America/Toronto
```

### 2. Build and run `doctor`

```sh
docker compose build
docker compose run --rm collector python -m aliexpress_coins doctor
```

Expected output (all five checks `ok`, exit `0`):

```text
[ok] adb: Android Debug Bridge version 1.0.41 (/usr/bin/adb)
[ok] device: 192.168.8.65:5555 (device)
[ok] geometry: 192.168.8.65:5555: 1200x2000 @ 240 dpi, sdk 35, portrait, profile c9b8f88670e295b5
[ok] timezone: configured timezone America/Toronto; device timezone America/Toronto (192.168.8.65:5555)
[ok] ocr: RapidOCR loaded and read 'COINS' from its probe image
```

`doctor` is read-only: it connects the container's own `adb` server to `AECOINS_DEVICE`, checks
geometry, compares timezones, and probes OCR without tapping or launching the app. If a check
fails:

- **`device`** — confirm wireless debugging is enabled and `AECOINS_DEVICE` matches the current
  `host:port` (the wireless port rotates on reconnect).
- **`timezone`** — `doctor` prints `configured timezone ...; device timezone ...`; copy the
  device timezone into `AECOINS_TIMEZONE` in `.env` and re-run `doctor`.
- **`geometry`** — a profile id other than `c9b8f88670e295b5` is normal on another phone (still
  `[ok]`), meaning popup fallbacks are uncalibrated for that device.

### 3. Dry run (zero in-app taps)

```sh
docker compose run --rm collector python -m aliexpress_coins run --once --dry-run
```

Expected output (`taps issued: 0`, exit `0`):

```text
run 20261123T170916Z: planned
detail: dry run: 2 task(s) planned, 1 refusal(s); no tap was issued
plan: dry run: 2 task(s) planned, 1 refusal(s); no tap was issued
  1. Daily coins [daily_checkin] category=sign_in rule=daily_checkin
  2. Explore sponsored items [sponsored_items] category=browse_wait rule=sponsored_items
  refused: Browse surprise items code=game_play_and_earn rule=game_play_and_earn reason=game: Play & Earn is a mini-game and is never played
  taps issued: 0
summary: /app/runs/20261123T170916Z/summary.md
run.json: /app/runs/20261123T170916Z/run.json
```

A refusal is expected safety behaviour, not an error. A non-zero dry run means the surface did not
settle cleanly (`surface_*` / `navigation_failed`) — retry or inspect before going live.

### 4. Take one supervised night

Run one real night manually (well outside the scheduled window) and read its report:

```sh
docker compose run --rm collector python -m aliexpress_coins run --once
docker compose run --rm collector cat /app/runs/<run-id>/summary.md
```

Confirm `Status: completed`, positive task evidence, and `Back on coins page: yes`.

### 5. Start the nightly daemon

```sh
docker compose up -d
docker compose logs --tail 5 collector
```

The default is cron `30 3 * * *` (`03:30` local via `AECOINS_SCHEDULE_TIME`), plus up to 180 s
jitter via `AECOINS_SCHEDULE_JITTER_SECONDS`, so the actual fire lands between 03:30 and 03:33. The
reset is observed at **00:00 device-local**. The late slot reduces competition with evening tablet
use, but the daemon has no second scheduled fire despite roughly 20 hours of claimable time; the
planned in-run retry loop (#123) will re-enumerate a retryable failure within the same night once it
lands. An after-reset fire such as 00:20 would leave more room for a supervised retry, at the risk of
driving a tablet still in use. See [`docs/06-runbook.md`](docs/06-runbook.md) §7 for the full rationale.

Expected log line:

```text
2026-11-23 16:31:14,439 INFO {"event":"schedule_wait","next_fire":"2026-11-24T03:31:31.431469-05:00","seconds":57616.992}
```

Stop cleanly with `docker compose stop` (`tini` forwards `SIGTERM` and writes the report within
the 5 s grace period). Runs sharing the `state` volume coordinate on `state/run.lock`: a manual
`run --once` exits `3` while the daemon is mid-night, and the daemon waits for a manual run before
re-checking whether the day is already done.

## What a claim run does

The executable contract is [`run_once()`](src/aliexpress_coins/runner/engine.py); it coordinates the
route, roster, budgets, daily state and report. Task-specific behaviour — sign-ins, browse-and-wait,
quiz, surprise items, search and notifications — lives in the executors under
[`src/aliexpress_coins/tasks/`](src/aliexpress_coins/tasks/), selected by
[`_dispatch_entry()`](src/aliexpress_coins/runner/engine.py).

### Route and the current loop

1. **Reach the tasks.** After device hygiene, the engine fires the coins shortcut URI
   ([`SHORTCUT_DEEP_LINK`](src/aliexpress_coins/tasks/surface.py)) to reach the coins page.
   [`discover_roster()`](src/aliexpress_coins/tasks/discovery.py) taps `Earn more` and enumerates the
   `Coins boost` task rows, falling back to the coin page's inline rows if that list cannot be opened.
2. **Dispatch one allowed row.** [`_next_entry()`](src/aliexpress_coins/runner/engine.py) selects a row
   with an executor, a dispatch target and daily allowance left. Selection uses an explicit cost
   order (cheap sign-ins before long dwells, catalogue key as the tie-break), never the reading order
   OCR happened to return. The reading order is still recorded in `roster.jsonl` for frame diagnostics.
   [`DispatchGate`](src/aliexpress_coins/tasks/dispatch.py) re-checks the row against
   [`specs/tasks.yaml`](specs/tasks.yaml) before an executor can act.
3. **Re-enumerate and continue.** After every real attempt,
   [`_observe()`](src/aliexpress_coins/runner/engine.py) re-opens the task surface and logs a fresh
   roster to `roster.jsonl`; the loop then selects the next row. A completed repeatable row is
   offered again on the next pass while its daily allowance remains, a retryable failure stays
   pending for a fresh pass, and the rest of the roster continues. An unknown or unreadable surface
   fires the shortcut again rather than dispatching from the stale roster. When a full pass finds
   nothing allowed and unblacklisted, the run exits.

### Bounds and unfinished nights

- **Shipped bounds:** [`RunConfig`](src/aliexpress_coins/runner/config.py) defaults to a 1200-second
  (20-minute) wall-clock safety net, 12 dispatched rows per run, a 12-sweep absolute ceiling and 2
  attempts per task (`AECOINS_RUN_BUDGET_SECONDS`, `AECOINS_MAX_TASKS_PER_RUN`,
  `AECOINS_MAX_PASSES` and `AECOINS_MAX_TASK_ATTEMPTS`). The engine checks both budgets before each
  dispatch and reapplies the wall-clock deadline at the running task's clock, sleep and capture
  seams, so a stuck executor cannot poll past it silently; each task also has its own `max_per_run`
  daily limit in `specs/tasks.yaml`.
- **Shipped retry bounds:** the outer pass loop retries `task_unverified` after a fresh enumeration,
  and only when the attempt took no action. A row that acted without proof is not repeated within
  the run. A task that fails `AECOINS_MAX_TASK_ATTEMPTS` times is blacklisted for the rest of that
  local day with its failure count in `summary.md`; a verified success resets that failure streak,
  so a repeatable row is bounded by its daily `max_per_run`. The blacklist is stored in the day-keyed
  state file and clears on the next day or a real completion. The exact stage/task retry policy is in
  [`src/aliexpress_coins/runner/failures.py`](src/aliexpress_coins/runner/failures.py).
- **Honest completion:** when a roster walk finishes,
  [`_night_record()`](src/aliexpress_coins/runner/engine.py) leaves `completed_night: false` if a task
  failure is still retryable or the roster was incomplete. Only an `exhausted` decision records
  `completed_night: true` through [`DailyStateStore.record_night()`](src/aliexpress_coins/runner/state.py),
  so the scheduler does not mistake an unfinished night for success. A catalogue family that no
  complete read has shown stays `unread` until `AECOINS_FAMILY_DORMANCY_DAYS` consecutive days
  (default 7, floor 3) prove it is not being offered; then it is reported as `not_offered`, named in
  the day state, and the night may finish. The everyday read still catches a return, and a dormant
  family also gets one explicit extra sample every `AECOINS_FAMILY_RETRY_DAYS` (default 7); a
  sighting clears that dormancy immediately (#161).
- **Pass and attempt bounds (#123, #141):** a pass is a full re-enumerated sweep. The loop normally
  ends by progress: it keeps sweeping while a pass dispatched a row or revealed new dispatchable
  rows, and stops after two consecutive sweeps that did neither. `AECOINS_MAX_PASSES` is the
  absolute ceiling and ends the run as `budget`, never as complete; so does the progress rule, and
  its detail names any still-dispatchable row. A ceiling below the retry-aware minimum
  `max_per_run * max_task_attempts` logs `pass_budget_below_daily_capacity` at startup, because a
  success resets the failure streak and a retry can spend a sweep before every round. The shipped
  worst case is 12 (six sponsored rounds times two attempts), which is also the default. A
  blacklisted row is skipped for the rest of the local day, while the rest of the roster continues;
  the run stays open for a later day. An unreadable, unknown or capped observation never dispatches
  from or completes on the stale roster.

### Permanent refusals

The catalogue permanently refuses placing an order or buying (`spending_order`), adding to cart
(`spending_cart`), playing a game (`game_play_and_earn` and the other `game_*` rules), sharing or
inviting (`social`), leaving a review (`review`), and converting currency (`currency_*`). These are
not retryable failures: [`DispatchGate`](src/aliexpress_coins/tasks/dispatch.py) emits a
`policy_refusal` event to `run.jsonl`, the row appears under `Skipped` in `summary.md`, and no
executor receives it. A policy refusal is permanent by design — it is never retried and must not be
counted as a failed attempt or blacklisted. Unknown labels are refused as `unrecognised`
(default-deny).

There is no redeem executor. Separately, an unexpected redeem control encountered during a task trips
[`SafetyGuard`](src/aliexpress_coins/runner/safety.py) and aborts the run as `dangerous_control`; that
is a runtime guard, not a catalogue refusal code.

### What is recorded

Every run writes `runs/<UTC timestamp>/summary.md`, `run.json` and `run.jsonl`. A live run also
attempts `roster.jsonl` and retained `shots/*.png` with OCR `.json` sidecars (balance frames,
surprise-item grids, and a best-effort final frame when the run status is `failed`). Artifact write
errors are reported rather than ending the run. [`RunReport`](src/aliexpress_coins/runner/report.py)
and [`ArtifactStore`](src/aliexpress_coins/perception/artifacts.py) define those artifacts; runs are
pruned to `AECOINS_KEEP_RUNS` (default 30). A successful dry run writes its plan and reports but no
roster log or screenshots; a failed dry run may also retain the best-effort failure frame described
above. The atomic daily file `state/daily-<YYYY-MM-DD>.json` records completion counts,
`acted_unverified`, the last balance, the per-day `blacklist` and `last_night`/`completed_night` through
[`DailyStateStore`](src/aliexpress_coins/runner/state.py). Learned quiz answers persist separately in
[`data/quiz_bank.json`](data/README.md), not in the run directory.

Per-attempt video or dense frame sequences for failed/unverified tasks are **planned, not shipped**
([#124](https://github.com/alexmi256/aliexpress-coins/issues/124)). Today a failed task is not guaranteed
a screenshot of its tap and aftermath; diagnose it from the task outcome and failure code in the
report, plus whichever frames that executor retained.

### Notifications (optional)

Set `AECOINS_APPRISE_URL` (plus optional `AECOINS_APPRISE_KEY` and `AECOINS_APPRISE_TAG`) in
`.env` for one Apprise message per run, or use the legacy `AECOINS_WEBHOOK_URL` /
`AECOINS_NTFY_TOPIC` settings; configure only one transport. Messages never contain the account
name, an absolute balance, or order details (`AECOINS_NOTIFY_BALANCE_DELTA=true` opts in to the
delta only), and a failed notification never fails a run. Delivery is handled in
[`src/aliexpress_coins/runner/notify.py`](src/aliexpress_coins/runner/notify.py).

## Safety posture

The collector drives a device that a human may also use. Four rules are enforced in code:

- **Default-deny before dispatch:** the deny list is enforced by `DispatchGate` **before** any
  task executor sees a row (sealed authorization token), and unknown task labels are skipped as
  `unrecognised`.
- **Refused by policy (never tapped):** mini-games (`Play & Earn`, `Merge Boss`, `GoGo Match`,
  `Prize Land`, `Shelf Shuffle`), currency conversion (`water`, `diamonds`, `energy`), purchases,
  checkout, order placement, adding to cart, leaving reviews, and sharing/referrals.
- **No blind coordinate taps:** every tap comes from a matched OCR box on a fresh screenshot,
  except per-profile calibrated popup fallbacks guarded by an OCR anchor.
- **Runtime abort guard & dry-run:** `SafetyGuard` aborts immediately on a live `dangerous_control`
  (buy/order/cart/redeem), a `balance_decreased` reading, or three identical unrecognised screens
  that fail recovery; `--dry-run` suppresses in-app taps entirely (`taps_issued: 0`).

## How it works

The AliExpress coins container (`AdcActivity`) and its popups expose no usable accessibility tree,
so perception is **screenshot → RapidOCR → `TextBox[]`**. Row pairing uses frame-proportional
tolerances (`2.25 × median` box height), scrolling is search-based, popup close glyphs scale from
frame width, and last-resort popup fallbacks are keyed by `doctor`'s geometry profile id. Every
dispatched row crosses `DispatchGate`, concurrent runs coordinate on `state/run.lock`, and every
error maps to one of 16 typed failure codes with an explicit retry policy (`stage ×1`, `task ×1`
only when `acted=False`, or `none`).

| document | contents |
|---|---|
| [`docs/06-runbook.md`](docs/06-runbook.md) | operator reference: local setup, reports, failure codes, portability limits |
| [`docs/02-architecture.md`](docs/02-architecture.md) | package layout (`src/aliexpress_coins/`), data flow, invariants, ADRs |
| [`docs/05-app-knowledge.md`](docs/05-app-knowledge.md) | measured AliExpress behaviour, task surface, navigation map, open unknowns |
| [`docs/03-conventions.md`](docs/03-conventions.md) | enforced engineering rules, testing seams, requirements & decisions of record |

## Troubleshooting

See [`docs/06-runbook.md`](docs/06-runbook.md) §9 (*When a night claims nothing*) and §10
(*Troubleshooting by failure code*), which lists the meaning, operator check, and retry rule for
every failure code and is verified against the code by `tests/test_docs.py`.

## Licence and contributors

**Licence:** Private project, no licence granted.

**Contributing:** local development uses Python 3.14, plain `venv` + `pip`, and
[`just`](https://github.com/casey/just) (no uv, poetry, hatch or pdm; keep the checkout path free
of `{` and `}`). Run `just setup`, use `just check-fast` while iterating, and run `just check`
(ruff lint, ruff format check, mypy strict over `src/` and `tests/`, and pytest with the coverage
ratchet — current floor **99.50%** in `pyproject.toml`) before opening a PR. Read
[`AGENTS.md`](AGENTS.md) and [`docs/03-conventions.md`](docs/03-conventions.md) first; recorded
test fixtures are documented in [`tests/data/README.md`](tests/data/README.md).
