# aliexpress-coins

Planning repository for an unattended collector of the AliExpress in-app **coins** programme:
daily sign-in, the easy browse-and-wait tasks, the quiz, the surprise-items task — and a
refusal of everything that costs money, costs minutes, or leaves a review.

**Status: design only, scaffold in place.** This repository holds the brief, the device recon
that constrains the design, the issue plan the build is broken into, and the toolchain every
later issue is built on. No product behaviour exists yet; the issues are the work items.

## Quick start

The toolchain is Python 3.14 with plain `venv` + pip, plus [`just`](https://github.com/casey/just)
for the recipes. There is no uv, poetry, hatch or pdm. Install `just` first; `just setup` does not
install it.

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
| `just cov` | pytest plus HTML and XML coverage reports |
| `just clean` | remove `.venv`, caches, coverage output and build artefacts |

### Quality gate

Configuration lives in `pyproject.toml`, not in scattered `setup.cfg`/`pytest.ini`/`mypy.ini`
files. Ruff uses a 120-character line length and single quotes; mypy is strict on both `src/`
and `tests/`. Ruff checks those two trees only: `tools/` is excluded because its pre-scaffold
issue-filing scripts contain intentional prose punctuation and are not package code.

Coverage is a **ratchet** with a starting floor of **85%**. The scaffold is fully covered, but a
one-statement package makes that 100% an artefact of the stub, so the floor starts at 85% rather
than pinning it at the maximum. As real modules land, raise the floor toward the measured number;
never lower it to make a change pass. The gate fails the build when coverage drops below the floor.

The plan and the conventions the build must follow are in [`docs/`](docs/) — start with
[`docs/03-conventions.md`](docs/03-conventions.md) and
[`docs/04-delivery-plan.md`](docs/04-delivery-plan.md).

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
| [`docs/06-navigation-map.md`](docs/06-navigation-map.md) | the screen graph: entry points, markers per screen, stuck states and recovery |
| [`docs/adr/`](docs/adr/) | decisions with their trade-offs (UI-first, OCR-first) |

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
