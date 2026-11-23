# aliexpress-coins

Planning repository for an unattended collector of the AliExpress in-app **coins** programme:
daily sign-in, the easy browse-and-wait tasks, the quiz, the surprise-items task — and a
refusal of everything that costs money, costs minutes, or leaves a review.

**Status: scaffold, perception, task executors, the nightly runner and the operator runbook in
place.** This repository holds the brief, the device recon that constrains the design, the issue
plan the build is broken into, the toolchain every issue is built on, the `run --once` /
`run --schedule` engine, and the runbook an operator follows from a clean clone.

## Quick start

The toolchain is Python 3.14 with plain `venv` + pip, plus [`just`](https://github.com/casey/just)
for the recipes. There is no uv, poetry, hatch or pdm. Install `just` first; `just setup` does not
install it. Keep the checkout path free of braces: setuptools reads `{...}` as a format field during
install. The worktree supervisor already creates brace-free paths.

```sh
git clone git@github.com:alexmi256/aliexpress-coins.git
cd aliexpress-coins
just setup   # creates .venv and installs the pinned requirements
just check   # the gate: lint, format check, mypy, tests with coverage
```

`just check` is the gate CI (#35) will run, so a local green is the green CI should report. Until
that workflow lands, a local green is the only automated verdict. Other recipes:

| recipe | what it does |
|---|---|
| `just setup` | create `.venv` and install `requirements-dev.txt` plus the package |
| `just lint` | ruff lint |
| `just fmt` | format in place with ruff |
| `just fmt-check` | fail if formatting would change anything |
| `just typecheck` | mypy strict over `src/` and `tests/` |
| `just test` | pytest, with the coverage ratchet enforced |
| `just test-fast` | pytest excluding the `slow` marker; no coverage ratchet |
| `just check-fast` | the fast lane: lint, format check, mypy and `test-fast` |
| `just cov` | pytest plus HTML and XML coverage reports |
| `just clean` | remove `.venv`, caches, coverage output and build artefacts |

### Container

The collector image carries `adb`, `tini` and the RapidOCR runtime, so the host needs
neither Android tooling nor Python. Copy `.env.example` to `.env`, set
`AECOINS_DEVICE=<host>:<port>`, then:

```sh
docker compose build
docker compose run --rm collector python -m aliexpress_coins doctor
```

A container runs its own adb server, so its connection to a wireless device does not
inherit the host's; `doctor` connects a `host:port` serial itself. `doctor` is
read-only: it checks adb, the device named by `AECOINS_DEVICE` (skipped when unset)
and the OCR models, and never taps or launches anything.

`docker compose up -d` starts the nightly daemon (`run --schedule`). Its local start
time, timezone, jitter and wall-clock budget come from `AECOINS_SCHEDULE_TIME`,
`AECOINS_TIMEZONE`, `AECOINS_SCHEDULE_JITTER_SECONDS` and
`AECOINS_RUN_BUDGET_SECONDS` (see `.env.example`). **Set `AECOINS_TIMEZONE` to the device's own
timezone** (`adb shell getprop persist.sys.timezone`): the schedule and the daily store's day boundary
both follow it, so a timezone that disagrees with the device fires a run before the device's day has
rolled — the coins are already claimed, the fresh-day path is never exercised, and the run reports
success while earning nothing. `doctor` prints both zones; `run --schedule` refuses to start when
they differ or the device value cannot be read, while a supervised `run --once` warns and continues.
A night that finished its roster
walk marks the day complete and is skipped on a retry, while a manual run stopped by
its budget leaves the nightly window open. Compose retries a failed daemon a bounded
number of times rather than looping forever on a configuration error. Run one night
manually with:

```sh
docker compose run --rm collector python -m aliexpress_coins run --once
```

Each night writes `runs/<UTC timestamp>/` with `summary.md`, `run.json`,
`run.jsonl`, `roster.jsonl` and the balance frames under `shots/` (each PNG with
its OCR sidecar). Retention keeps the newest `AECOINS_KEEP_RUNS` directories
(default 30). Notifications are optional and go through an Apprise API server:
set `AECOINS_APPRISE_URL` (and optionally `AECOINS_APPRISE_KEY` and
`AECOINS_APPRISE_TAG`) to receive one message per run. The message contains the
run status, the failure code when it failed, counts by task outcome and the
report path; it never contains the account name, an absolute balance or order
details. Set `AECOINS_NOTIFY_BALANCE_DELTA=true` to add the balance delta. A
broken Apprise endpoint is logged with its exception class and HTTP status only,
and never changes the run's exit status. The older `AECOINS_WEBHOOK_URL` and
`AECOINS_NTFY_TOPIC` settings still work with the failure-only selection and
`AECOINS_NOTIFY_ON_EMPTY`; use only one transport so a run sends one message. A
stop signal ends the daemon within the compose grace period and still leaves the
report written atomically.

### Quality gate

Configuration lives in `pyproject.toml`, not in scattered `setup.cfg`/`pytest.ini`/`mypy.ini`
files. Ruff uses a 120-character line length and single quotes; mypy is strict on both `src/`
and `tests/`. Ruff checks those two trees only: `tools/` is excluded because its pre-scaffold
issue-filing scripts contain intentional prose punctuation and are not package code.

Coverage is a **ratchet** with a starting floor of **85%**, raised to **98%** when the perception
capture module landed (measured 98.45%), to **98.8%** when the daily state store landed
(measured 98.87%), to **99.0%** when the daily sign-in executor landed (measured 99.01%), to
**99.1%** when the browse-and-wait executor landed (measured 99.11%), to **99.15%** when the
notifications executor landed (measured 99.20%), to **99.2%** when the roster observation log
landed (measured 99.25%), to **99.25%** when the nightly runner landed (measured 99.31%), to
**99.3%** when the failure taxonomy, retries and device hygiene landed (measured 99.31%), to
**99.31%** when Apprise notifications and log levels landed (measured 99.32%), to **99.32%**
when geometry independence landed (measured 99.33%), to **99.33%** when the dispatch gate and
abort conditions landed (measured 99.33%), to **99.35%** when abort propagation through executor
and cleanup handlers landed (measured 99.36%), and to **99.37%** when the inside-task countdown
band was narrowed (measured 99.38%), then to **99.39%** when the wait primitives landed (measured
99.40%), then to **99.42%** when the timezone refusal and stability ledger landed (measured
99.43%).
The scaffold is fully covered, but a one-statement package makes that 100% an artefact of the
stub, so the floor starts below the maximum. As real modules land, raise the floor toward the
measured number; never lower it to make a change pass. The gate fails the build when coverage
drops below the floor.

The plan and the conventions the build must follow are in [`docs/`](docs/) — start with
[`docs/03-conventions.md`](docs/03-conventions.md) and
[`docs/04-delivery-plan.md`](docs/04-delivery-plan.md).

### Simulated-device test harness

The suite never needs the tablet, and a forgotten mock cannot reach it. Two autouse fixtures in
`tests/conftest.py` refuse socket connections and process spawns, naming the convention in the
failure; `tests/test_package.py` proves the guards bite, including for `Popen`, `os.system`,
`os.posix_spawn` and `fork`.

Above those guards sit the two seams from `docs/03-conventions.md`:

- **transport** — `aliexpress_coins.transport` is the one `AdbRunner` contract. The real client
  (`AdbSnapshotter`, `AdbInputDevice`, `AdbDevice`, `doctor`, the notification probe) is always
  constructed around it; tests pass `tests/support/adb.py:ScriptedAdbRunner`, which replays a
  queue, answers by argv, or replays a transcript recorded with `RecordingAdbRunner`. An
  unscripted call raises `AssertionError` naming the argv instead of falling back to a device.
  Recording defaults to the gitignored `tests/transcripts/`; binary output such as a screencap is
  omitted (only its byte count is recorded), never base64-encoded into the transcript.
- **snapshot** — `tests/support/frames.py:ScriptedSnapshotter` implements the `Snapshotter`
  protocol and replays a directory of recorded PNGs. `repeat=2` supplies the same frame twice for
  stability waits, and `override_call()` substitutes one frame without rewriting the sequence.

Recorded frames live under `tests/data/` and are gitignored by set; the regeneration recipe is in
[`tests/data/README.md`](tests/data/README.md).

`just check` remains the gate and runs every test with the coverage ratchet. `just check-fast` is
the diagnostic loop: it excludes tests marked `slow` (full-frame OCR, template scans and the long
engine retry walks) and does not enforce coverage. Measured on 2026-11-23: the full suite is
**1610 passed in 181 s** (203 tests marked slow, coverage 99.43%); `just check-fast` is
the same fast lane, dominated by the remaining sub-200 ms frame and parser tests plus collection,
not by any single fixture.

## What was learned before writing any requirements

The device was inspected (read-only) on 2026-11-19, and three findings reshaped the design:

1. **The coins page exposes no accessibility nodes.** `uiautomator dump` of that page is
   ~6.4 KB of empty scaffolding while a screenshot yields 40 OCR labels. Perception has to be
   screenshot + OCR, not the accessibility tree.
2. **Popups are invisible to the accessibility tree too** — they are drawn inside the app's
   own surface. A collector that only reads nodes will happily tap a dimmed scrim all night.
3. **The advertised coins deep link does not reach the coins page.** It resolves to a blank
   web view inside the app's hybrid container.

Full evidence, with the commands that produced it:
[`docs/01-recon-findings.md`](docs/01-recon-findings.md).

## The plan

| document | what it settles |
|---|---|
| [`docs/00-brief.md`](docs/00-brief.md) | requirements, task taxonomy (do / don't), the answered decisions |
| [`docs/01-recon-findings.md`](docs/01-recon-findings.md) | what was measured on the device and what it changed |
| [`docs/02-architecture.md`](docs/02-architecture.md) | layers, invariants, perception and control strategy |
| [`docs/03-conventions.md`](docs/03-conventions.md) | engineering conventions the build must follow |
| [`docs/04-delivery-plan.md`](docs/04-delivery-plan.md) | milestones, issue map, dependency order |
| [`docs/05-app-knowledge.md`](docs/05-app-knowledge.md) | how the app actually works: entry points, pages, tasks, rewards |
| [`docs/06-runbook.md`](docs/06-runbook.md) | the operator's quickstart, reports, failure codes and daily-reset caveat |
| [`docs/07-navigation-map.md`](docs/07-navigation-map.md) | the screen graph: entry points, markers per screen, stuck states and recovery |
| [`docs/08-stability.md`](docs/08-stability.md) | the nightly outcomes ledger and its append rule, for the #41 stability gate |
| [`docs/adr/README.md`](docs/adr/README.md) | the ADR index: every decision with a one-line summary |

## How the work is organised

Six milestones, from "decide what is possible" to "runs nightly in a container":

| milestone | goal |
|---|---|
| **M0 Recon & decisions** | answer the questions that change the architecture (launcher entry points, API feasibility, task catalogue, popup corpus) — two of six answered |
| **M1 Perception & control** | see the screen reliably, tap it safely, close what interrupts |
| **M2 Task engine** | one executor per task family, plus the policy that refuses the bad ones |
| **M3 Reliability & manual operation** | state, reports, retries, Apprise — a manual run has to be trustworthy before anything runs alone |
| **M4 Delivery & release gate** | container, CI, test harness, runbook, and the criteria for going public |
| **M5 Scheduled operation** | nightly runs, once the manual ones are boring |

Issue labels: `area:*` (recon, perception, control, tasks, ops, delivery), `type:*` (spike,
feature, test, infra, docs), `priority:p0|p1|p2`, `needs-device`, `blocked`. Dependencies are
stated in each issue body (`Depends on #N`), and the epics carry their children as GitHub
sub-issues.

## Safety posture

The collector may tap a phone that a human is also using. Three rules are fixed before any code
exists:

- the deny list (games, purchases, reviews, sharing, currency conversion) is enforced **before**
  a task executor ever sees a row;
- no raw coordinate taps outside a documented popup fallback — every tap follows a label
  assertion on a fresh screenshot;
- `--dry-run` suppresses in-app taps entirely.

## Out of scope

Mini-games, currency conversion (water/diamonds), purchases, order placement, cart building,
reviews, referrals. These are refused by policy rather than merely unhandled.

## Licence

Private project, no licence granted.
