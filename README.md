# aliexpress-coins

Planning repository for an unattended collector of the AliExpress in-app **coins** programme:
daily sign-in, the easy browse-and-wait tasks, the quiz, the surprise-items task — and a
refusal of everything that costs money, costs minutes, or leaves a review.

**Status: design only.** This repository holds the brief, the device recon that constrains the
design, and the issue plan the build is broken into. No implementation exists yet; the issues
are the work items.

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
| [`docs/00-brief.md`](docs/00-brief.md) | requirements, task taxonomy (do / don't), open questions |
| [`docs/01-recon-findings.md`](docs/01-recon-findings.md) | what was measured on the device and what it changed |
| [`docs/02-architecture.md`](docs/02-architecture.md) | layers, invariants, perception and control strategy |
| [`docs/03-conventions.md`](docs/03-conventions.md) | engineering conventions the build must follow |
| [`docs/04-delivery-plan.md`](docs/04-delivery-plan.md) | milestones, issue map, dependency order |
| [`docs/adr/`](docs/adr/) | decisions with their trade-offs (UI-first, OCR-first) |

## How the work is organised

Six milestones, from "decide what is possible" to "runs nightly in a container":

| milestone | goal |
|---|---|
| **M0 Recon & decisions** | answer the questions that change the architecture (launcher entry points, API feasibility, task catalogue, popup corpus) |
| **M1 Perception & control** | see the screen reliably, tap it safely, close what interrupts |
| **M2 Task engine** | one executor per task family, plus the policy that refuses the bad ones |
| **M3 Unattended operation** | schedule, state, reports, failure handling |
| **M4 Delivery** | container, CI, image per merge, test harness |

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
