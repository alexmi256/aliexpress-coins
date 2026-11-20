# 04 — Delivery plan

How the build is sequenced: five milestones, six areas, 37 issues. Every issue carries its own
acceptance criteria; the dependency edges below are also recorded natively on GitHub
(`blocked by #N`), so a "ready to start" filter is `is:issue is:open -label:blocked` plus the
dependency graph.

## Milestones

| milestone | issues | done when |
|---|---|---|
| **M0 Recon & decisions** | 7 | every open question in the brief has an answer or a blocking issue; the scaffold exists |
| **M1 Perception & control** | 11 | the screen can be read, tapped safely, waited on, and popups are closed after every navigation |
| **M2 Task engine** | 9 | every allowed task family has an executor; every refused task is refused by a test |
| **M3 Unattended operation** | 5 | three consecutive unattended nights, each with a report |
| **M4 Delivery** | 5 | container built by CI on every merge, image published, runbook written from a clean clone |

The scaffold (#33) sits in M0 rather than M4: it is a prerequisite for everything else, and
leaving it in "delivery" made every other issue look blocked by a late milestone.

## Areas, epics and their children

### #1 Epic: recon of the coins surface — M0

| issue | what it settles |
|---|---|
| #7 | shortcut and widget entry points: intent, reward trigger, clean-up |
| #8 | the canonical coins-page `AdcActivity` URL (one command instead of two taps) |
| #9 | whether the coin awards are replayable HTTP (updates ADR-0001) |
| #10 | the quiz, surprise-items and notification flows, captured |
| #11 | popup corpus and per-family dismissal rules |

### #2 Epic: perception layer — M1

| issue | what it settles |
|---|---|
| #12 | screenshot capture, `Snapshot`, artifact store |
| #13 | OCR adapter and fuzzy label matching |
| #14 | row/section inference — row-relative tap targets |
| #15 | template matching for glyph-only controls |
| #16 | screen-state classifier (loading / page / popup / error) |

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
| #22 | task discovery via "Earn more" |
| #23 | daily sign-in |
| #24 | browse-and-wait family (sponsored, recently viewed, check-in to win) |
| #25 | quiz |
| #26 | surprise items |
| #27 | launcher items (shortcut + widget) |
| #28 | notifications |

### #5 Epic: unattended operation — M3

| issue | what it settles |
|---|---|
| #29 | nightly scheduler and run window |
| #30 | daily state with crash recovery |
| #31 | run report artifacts and failure notification |
| #32 | failure taxonomy, retries, device hygiene |

### #6 Epic: delivery — M4

| issue | what it settles |
|---|---|
| #33 | project scaffold and quality gates *(pulled forward to M0)* |
| #34 | Docker image and compose service |
| #35 | CI workflows and image build on merge |
| #36 | simulated-device test harness |
| #37 | runbook and documentation |

## Critical path

```
#33 scaffold
   │
   ├──▶ #12 screenshot ──▶ #13 OCR ──▶ #14 rows ─┐
   │                         │                    │
   │                         ├──▶ #16 classifier ─┼──▶ #19 waits ──▶ #22 discovery ──▶ #24 browse-and-wait ──▶ #29 scheduler ──▶ #32 resilience
   │                         │                    │                        │
   │                         └──▶ #15 templates ─┴──▶ #18 popups ─────────┤
   │                                              │                        │
   └──▶ #17 taps ─────────────────────────────────┘                        │
                     │                                                      │
                     └──▶ #20 guardrails ◀── #21 policy ────────────────────┴──▶ #23 sign-in
                                                                              └──▶ #25/#26/#27/#28 other executors
#30 state ◀── #33, blocks #23 and #24
#34 image ◀── #33, blocks #29;  #35 CI ◀── #33 + #34
```

Read as: nothing can be verified until the screen can be read (#12–#14), nothing can be
*tapped* safely until the controller and the popup loop exist (#17, #18), and no task runs
until discovery (#22) and the policy (#21) are in place.

## Recommended working order

1. **#33** scaffold — it unblocks ten other issues and makes every later change reviewable.
2. **#7** and **#10** in parallel — both need the phone and both remove the largest unknowns
   (launcher entry points, and the three uncaptured task flows). Recon can run while the
   tooling is fresh.
3. **#12 → #13 → #14** — perception ladder; each has a recorded-frame test.
4. **#17, #18, #19, #20** — control layer, testable entirely offline once #12–#14 exist.
5. **#21 → #22 → #23/#24** — the first real earning path.
6. **#29, #30, #31, #32** — make it nightly.
7. **#34, #35, #36, #37** — packaging, automation, harness, docs (the container is pulled
   forward to #29 if the scheduler needs it sooner).

## Open questions blocking scope (from the brief)

| # | question | affects |
|---|---|---|
| Q1 | public or private repository? *(created private)* | docs, screenshots in issues |
| Q2 | is the device PIN/biometric-locked overnight? | #29, #32 |
| Q3 | is the shortcut/widget reward granted on creation or on launch-from-item? | #27 |
| Q4 | is the quiz rewarded for accuracy? | #25 |
| Q5 | where should failure notifications go (ntfy / Home Assistant / email)? | #31 |
| Q6 | run window and retry policy (03:30, one retry?) | #29, #32 |
| Q7 | is missing an occasional task acceptable to never take a risky action? | #21, #24 |
