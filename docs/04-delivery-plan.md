# 04 — Delivery plan

How the build is sequenced: six milestones, six areas, 41 issues (two already answered and
closed). Every issue carries its own acceptance criteria; the dependency edges below are also
recorded natively on GitHub (`blocked by #N`), so the "ready to start" filter is
`is:issue is:open -label:blocked` plus the graph.

## What changed after the second recon session

The 2026-11-20 session with the launcher items installed moved three things:

- **The route question is settled** (#8 closed): a single `am start` of the app's own
  `aliexpress://goto?url=…` wrapper reaches the native coin page. The Account-page card is a
  fallback, not the plan — it rotates between a coins card and a coupon card.
- **`Earn more` is the task list, and it has a silent failure mode** (#38): it opens the `Coins
  boost` page, which on its first load after a cold start renders **empty** ("There's nothing here
  yet"). An empty list is a fault to reset, not an empty day — the page always lists the declined
  tasks too. The trigger is now pinned: firing the shortcut intent as the app's **cold start**
  produces the empty page (4/4); starting the app normally first, then sending the intent, is
  5/5 good. The intent is a navigation command, not a launch command.
- **The quiz leaks the answer** (new issue #39): the result page prints `Correct answer: X`, so a
  question bank is buildable for free.

The screens and the transitions between them are drawn in
[`06-navigation-map.md`](06-navigation-map.md) — entry points, the marker each screen is recognised
by, and the stuck states with their recovery. The perception (#16) and task (#38) issues reference
it directly.

## The project board

There is a GitHub Project (v2) at `/users/alexmi256/projects/3` mirroring these issues, with `Area`
and `Priority` fields copied from the labels and a `Status` field for flow. It is a **view**, not a
second source of truth: labels, sub-issues and `blocked by` edges in the issues remain authoritative,
and `tools/setup_project_board.py` re-syncs the board from them.

The repository is the project's **default**, and **auto-add is enabled** (set by the operator), so a
new issue lands on the board without a manual step. Auto-add copies the issue, not the field
mapping: `Area` and `Priority` stay empty until the sync runs. The flow is therefore
*create the issue with its labels → run `tools/setup_project_board.py`*.

Deliberately **no CI workflow does this sync**: it needs a token with the `project` scope, and a
long-lived one in a repository secret is a worse trade than running one command when issues are
filed. Whoever files issues runs the sync; if that ever becomes a bottleneck, revisit — not before.

Use it for one question — *what can be started now*: filter `Status = Todo`, `Area = Perception` and
look at the `blocked by` list on each item. The roadmap view is deliberately absent until the
milestones carry real dates; a roadmap of guesses is worse than none.

## Milestones

| milestone | issues | done when |
|---|---|---|
| **M0 Recon & decisions** | 5 (2 closed) | the remaining unknowns are captured |
| **M0.5 Foundations** | 3 | `just check` green from a clean clone, CI enforcing it on every PR, image published on every merge |
| **M1 Perception & control** | 11 | the screen can be read, tapped safely, waited on, and popups are closed after every navigation |
| **M2 Task engine** | 11 | every allowed task family has an executor; every refused task is refused by a test; the quiz learns; a stuck page resets itself |
| **M3 Reliability & manual operation** | 5 | a manual run is trustworthy: state, report, retries, Apprise, documented failure classes |
| **M4 Delivery** | 6 | container built by CI on every merge, harness, runbook, and the gate for going public |
| **M5 Scheduled operation** | 1 | nightly runs, once the manual ones are boring |

**M0.5 Foundations** exists because the toolchain, the image and CI are not "delivery" — they are what
every later milestone stands on, and they come first:

| issue | why it is here |
|---|---|
| #33 scaffold | `just check` is the gate every later PR is judged by; it cannot be late |
| #34 Docker image | the collector's runtime lives in the image, not on a host |
| #35 CI + publish | #33 promises a gate equal to CI — so CI must exist before the first feature PR |

The edges are `#33 → #34 → #35`. Previously all three carried the M4 label, which made every feature
issue look blocked by a milestone that would not start for weeks; the labels were the bug, not the
issues.

Scheduling (#29) sits in M5 by decision (Q6): runs are manual with one retry per stage until the
failure handling in #32 has proven itself.

## Areas, epics and their children

### #1 Epic: recon of the coins surface — M0 *(#7, #8 answered and closed)*

| issue | what it settles |
|---|---|
| #7 ✅ | shortcut and widget entry points — **answered**, intents captured |
| #8 ✅ | the canonical coins-page intent — **answered**, `aliexpress://goto?url=…` |
| #9 | whether the coin awards are replayable HTTP (updates ADR-0001) |
| #10 | the surprise-items and notification flows, captured (quiz now captured) |
| #11 | popup corpus and per-family dismissal rules |

### #2 Epic: perception layer — M1

| issue | what it settles |
|---|---|
| #12 | screenshot capture, `Snapshot`, artifact store |
| #13 | OCR adapter and fuzzy label matching |
| #14 | row/section inference — row-relative tap targets |
| #15 | template matching for glyph-only controls |
| #16 | screen-state classifier (loading / page / popup / error) |
| #42 | **geometry independence**: device profiles, proportional tolerances, scaled templates |

### #3 Epic: control and safety — M1

| issue | what it settles |
|---|---|
| #17 | the tap controller (clamp, jitter, log, dry-run) |
| #18 | popup dismissal loop, invoked by the run loop |
| #19 | wait primitives (text, stability, countdown) |
| #20 | safety guardrails and abort conditions |

### #4 Epic: task engine — M2

| issue | what it settles |
|---|---|
| #21 | task catalogue and default-deny policy |
| #22 | task discovery on the coins page (scope corrected) |
| #23 | daily check-in (the streak `Collect`) |
| #24 | browse-and-wait family (sponsored, recently viewed, check-in to win) |
| #25 | quiz |
| #26 | surprise items |
| #27 | launcher items (shortcut + widget) |
| #28 | notifications |
| #38 | **task surface & stuck-state handling**: page variants, the empty boost page, reset rule, route choice |
| #39 | **quiz question bank**: record, learn, answer from the bank |
| #43 | **roster observation log**: re-enumerate and diff after every completion |

### #5 Epic: reliability & manual operation — M3

| issue | what it settles |
|---|---|
| #30 | daily state with crash recovery |
| #31 | run report artifacts (notification delivery narrowed out) |
| #32 | failure taxonomy, retries, device hygiene |
| #40 | **Apprise notifications** with standard log levels |

### #6 Epic: delivery — M4

| issue | what it settles |
|---|---|
| #33 | project scaffold and quality gates *(pulled forward to M0)* |
| #34 | Docker image and compose service |
| #35 | CI workflows and image build on merge |
| #36 | simulated-device test harness |
| #37 | runbook and documentation |
| #41 | **release readiness gate**: 5-minute onboarding, two-week stability |

### Scheduled operation — M5

| issue | what it settles |
|---|---|
| #29 | nightly scheduler and run window |

## Critical path

```
#33 scaffold
   │
   ├──▶ #12 screenshot ──▶ #13 OCR ──▶ #14 rows ──┐
   │                         │                    │
   │                         ├──▶ #16 classifier ─┼──▶ #19 waits ──▶ #22 discovery ──▶ #38 task surface ──▶ #24 browse-and-wait
   │                         │                    │                                        │
   │                         └──▶ #15 templates ──┴──▶ #18 popups ────────────────────────┤
   │                                              │                                        │
   └──▶ #17 taps ─────────────────────────────────┘                                        │
                     │                                                                     │
                     └──▶ #20 guardrails ◀── #21 policy ───────────────────────────────────┴──▶ #23 check-in
                                                                                            ├──▶ #25 quiz ──▶ #39 question bank
                                                                                            └──▶ #26/#27/#28
#30 state ◀── #33, blocks #23 and #24
#31 report ◀── #12;  #40 Apprise ◀── #31
#34 image ◀── #33, blocks #29;  #35 CI ◀── #33 + #34
#41 release gate ◀── #37 + #32
```

Read as: nothing can be verified until the screen can be read (#12–#14), nothing can be *tapped*
safely until the controller and the popup loop exist (#17, #18), no task runs until discovery (#22)
and the policy (#21) are in place, and the task surface is variable enough to be its own issue
(#38).

## Recommended working order

1. **#33** scaffold — unblocks nine other issues and makes every later change reviewable.
2. **#10 + #11** — one more device session for the surprise-items and notification flows and the
   popup corpus. Everything else can proceed without it, but those executors need it.
3. **#12 → #13 → #14**, with **#42** as a constraint on all three — no stored pixels, tolerances
   derived from the frame, scrolling as a search. The ladder is where portability is won or lost.
4. **#16, #15** — classifier and glyph matching, in parallel.
5. **#17, #18, #19, #20** — the control layer; testable offline once the perception ladder exists.
6. **#21 → #22 → #38 → #43 → #23/#24** — the first real earning path, with conditional rows
   and a roster log that records what the app offers and what changes as tasks complete.
7. **#25 → #39** — the quiz and its question bank.
8. **#30, #31, #32, #40** — make a manual run trustworthy.
9. **#34, #35, #36, #37, #41** — packaging, automation, harness, docs, release gate.
10. **#29** — scheduling, last.

## Decisions taken (was: open questions)

| # | decision |
|---|---|
| Q1 | Private now; open-source at the #41 gate (5-minute onboarding + two stable weeks) |
| Q2 | No PIN/biometric lock — assume the device is unlocked |
| Q3 | Adding the launcher item appears sufficient; one widget-visit observation outstanding (#27) |
| Q4 | Accuracy pays; the quiz prints the correct answer, so the bank learns it (#39) |
| Q5 | Apprise for delivery; standard log levels from the project (#40) |
| Q6 | Manual runs, one retry per stage; scheduling deferred (#29 → M5) |
| Q7 | Miss the task rather than take a risky action; every refusal is logged (#21) |

## Remaining unknowns (they do not block the build)

| unknown | issue | how to settle |
|---|---|---|
| whether the empty boost page is only a cold-cache effect, or also rate limiting | #38 | repeat a cold-start trial series |
| surprise-items flow and its rounds | #26 | one session with the task available |
| notification task's system dialog | #28 | one session with the task available |
| how to exit `YOU CAN NOW REDEEM!` other than BACK | #11 | capture the interstitial's full extent |
| whether the coin awards are replayable HTTP | #9 | mitmproxy capture |
