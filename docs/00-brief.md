# 00 — Requirements brief

What this project is for, what it must do, and where the edges are. Written from the
operator's brief and narrowed by what was measured on the device (see
[`01-recon-findings.md`](01-recon-findings.md)).

Status: **draft v1** — the open questions in §9 need answers before the task engine is
finished.

---

## 1. Purpose

Collect AliExpress "coins" automatically, once a night, without supervision. The app offers
coins for a handful of repeatable actions; this project performs the ones that pay and skips
the ones that cost money, cost minutes, or draw attention (reviews, referrals, purchases).

The coin balance itself is nearly worthless (≈ C$0.01 per coin). The value is in not
spending twenty minutes a night tapping a phone, and in doing it reliably.

**In scope:** the daily sign-in, the easy browse-and-wait tasks, the quiz, the surprise-item
task, the shortcut/widget/notification tasks, popup handling, run reporting.

**Out of scope:** mini-games, currency conversion (water/diamonds), purchases, reviews,
sharing, order placement, cart building. These are refused by policy, not merely unhandled.

---

## 2. Environment (verified on 2026-11-19)

| fact | value |
|---|---|
| device | `192.168.8.65:5555`, wireless adb, model `T3_US` |
| Android | 15 (SDK 35), `userdebug`, `ro.adb.secure=0` (no adb key authorisation) |
| adb | adbd already runs as **root** (no `su` binary on the image) |
| screen | 2000×1200 physical, 240 dpi, portrait screenshots are **1200×2000** |
| app | `com.alibaba.aliexpresshd` 8.180.1 (versionCode 80005998) |
| launcher | `com.android.launcher3`, single page, 6×5 grid |
| region/locale | en, CA — coin values render in `C$` |

Portrait-only, 1200×2000, single account, single device. Coordinate tables in code assume
this geometry; anything else is detected and refused rather than guessed (see FR-14).

---

## 3. Functional requirements

### 3.1 Reaching the coins page

- **FR-1** Bring the app to a usable state: wake the device, dismiss the keyguard, cold-start
  the app, wait for the screen to stop changing.
- **FR-2** Dismiss every popup before acting. The app shows a promotional modal on cold launch
  and interposes one after most navigations. A popup that cannot be dismissed fails the run
  loudly — never by tapping blind at coordinates in the hope it closes.
- **FR-3** Reach the coins page. Two routes are required:
  - **UI** (primary, verified): Account tab → coins card.
  - **Launcher entry point** (secondary, to be reversed): the shortcut and app widget the app
    itself can install. Both are *also* tasks the app rewards, so the project has to be able
    to add and remove them regardless.
- **FR-4** Stop before the first in-app tap when `--dry-run` is set, and still produce a report.

### 3.2 Daily sign-in

- **FR-5** Take the daily check-in once per local day, verified by the balance changing (or by
  the UI moving to "already collected"). Never re-press a claimed card: that is what raises
  "already collected" modals.
- **FR-6** Record the completion in persistent state so a restart does not repeat it.

### 3.3 Task discovery and selection

- **FR-7** Enumerate the available tasks. The coins page shows a subset; "Earn more" opens the
  full list. Open the full list, wait for it to settle, then enumerate.
- **FR-8** Classify each task by title against the spec catalogue
  (`src/aliexpress_coins/specs/tasks.yaml`). Unknown titles are **skipped** (default-deny) —
  a new easy task is a missed coin, an unknown "free trial" is a charge.
- **FR-9** Refuse, with the reason logged: any game (Play & Earn, Merge Boss, GoGo Match,
  Prize Land, Shelf Shuffle), any currency conversion (water, diamonds, energy), anything that
  buys/orders/adds to cart, any review, any sharing/invite, any order placement.
- **FR-10** Respect per-task and per-run limits: repeatable tasks have a daily maximum, and
  the run has a hard task cap (`AECOINS_MAX_TASKS_PER_RUN`, default 12) and a wall-clock budget.

### 3.4 Task execution

Each task family gets its own executor, because each fails differently:

- **FR-11a Browse-and-wait** (sponsored items, recently viewed, check-in to win): open, wait
  out the countdown (15 s measured), collect if a collect control appears, return to the coins
  page, verify the row's progress badge moved or the row disappeared.
- **FR-11b Quiz**: answer randomly. Correctness is not rewarded. Must survive the "you got it
  wrong" interstitial.
- **FR-11c Surprise items**: open the page, tap *n* distinct items (three, per the brief),
  returning between taps, absorbing any multi-round popups afterwards.
- **FR-11d Shortcut**: add the coins shortcut, then remove it. Whether the reward triggers on
  creation or on first launch **from** the shortcut is unknown (open question Q3).
- **FR-11e Widget**: same shape as FR-11d for the 2×2 coins widget.
- **FR-11f Notifications**: enable notifications if the task is offered; the permission prompt
  is a system dialog, not an app dialog, and needs its own handling.

- **FR-12** After **every** action, run a popup sweep before the next action. This is the
  single most repeated behaviour in the project and belongs in the runner, not in tasks.
- **FR-13** Verify completion against something observable (balance delta, progress badge,
  row disappearing). "The taps were sent" is not a completion.

### 3.5 Unattended operation

- **FR-14** Run nightly from a container on a schedule, and refuse to run when the device is
  unreachable, not rooted, or running an unexpected screen geometry.
- **FR-15** Write a run report every time, including on failure: `runs/<timestamp>/summary.md`,
  `run.json`, screenshots and their OCR sidecars, and a JSONL decision log.
- **FR-16** Notify on failure (optional webhook), because a silent week of no coins is the
  main failure mode of an unattended collector.
- **FR-17** Recover from a crash: state is written atomically, and a resumed run does not
  repeat completed tasks.
- **FR-18** Keep artifacts for a bounded number of runs (disk on the host is finite).

### 3.6 Packaging and delivery

- **FR-19** Python 3.14, typed (mypy strict on `src/` and `tests/`), ruff formatting/linting,
  pytest with a coverage ratchet, `just` as the single entry point.
- **FR-20** Docker image built from Debian trixie + distribution packages (adb, tesseract-free
  OCR via RapidOCR wheels), running unprivileged, `tini` as PID 1.
- **FR-21** CI on every push/PR: lint, format check, mypy, tests with coverage. A new image on
  every merge to `main`, pushed to GHCR with provenance and SBOM.

---

## 4. Task taxonomy

| task | do | executor | notes |
|---|---|---|---|
| Daily coins / check-in | ✅ | `daily` (runner) | once per day, verified by balance |
| Take the coins quiz | ✅ | `quiz` | random answers |
| Explore sponsored items | ✅ | `browse_wait` | 15 s countdown, repeatable |
| Browse surprise items | ✅ | `surprise_items` | 3 distinct item taps, rounds |
| Browse recently viewed items | ✅ | `browse_wait` | 15 s countdown |
| Check in to win shopping credits | ✅ | `browse_wait` | wait, then collect |
| Add a coins shortcut | ⚠️ | `shortcut` | remove after; reward trigger unknown |
| Add & start using our widget | ⚠️ | `widget` | remove after; reward trigger unknown |
| Turn on notifications | ✅ | `notifications` | system permission dialog |
| Play games (Prize Land, Play & Earn, Merge Boss, GoGo Match) | ❌ | — | refused |
| Convert water/diamonds/energy | ❌ | — | refused |
| Buy / place an order / add 5 items to cart | ❌ | — | refused |
| Leave a review | ❌ | — | refused |
| Share coins with friends | ❌ | — | refused |
| Water to win a US prize | ❌ | — | refused |

---

## 5. Non-functional requirements

- **Unattended:** a run must survive popups, a slow network, a half-loaded page, and a device
  that rebooted. Failures are reported, not retried forever.
- **Idempotent within a day:** re-running does not double-tap completed tasks or re-claim the
  sign-in.
- **Safe by default:** dry-run is one flag away; the deny list is enforced before any task
  executor sees a row; no action may spend money.
- **Observable:** every decision (allowed/skipped/why) is a log field and appears in the
  report. Debugging a silent night means reading `run.jsonl`, not re-playing the phone.
- **Small blast radius:** the device is shared with a human. Nothing may leave the app in a
  broken state; tasks return to the coins page.

---

## 6. Constraints

- The coins page is rendered by the app's hybrid container (`AdcActivity`): **no accessibility
  nodes**, so no resource ids and no `uiautomator` text. Perception is screenshot + OCR.
- Modals are drawn inside the same surface and are invisible to the accessibility tree.
- The app's deep link for coins (`https://www.aliexpress.com/p/coin-index/…`) resolves to a
  **web** page inside the container and renders blank on this build. Not a route.
- The 1200×2000 portrait geometry is assumed by stored coordinates and templates.
- One device, one account. No concurrency, no parallelism.

## 7. Assumptions

- The device stays powered and on the LAN overnight (charger attached).
- The account stays logged in; a logged-out app fails the run at the coins-page assertion.
- Coin values and task rewards can change at any time; the project asserts on UI state, not on
  reward amounts.

## 8. Out of scope (deliberate)

- Any server-side/API-only approach. Recorded as a spike (see the issue tracker): if the coin
  endpoints are plain HTTP and replayable, a later version could skip the UI — but a UI-only
  baseline is what ships first, because it cannot be broken by a signature or a token change.
- Multiple devices or accounts.
- Earning beyond the coin programme (coupons, affiliate, referrals).

## 9. Open questions

| # | question | why it matters |
|---|---|---|
| Q1 | Should the repository be public or private? (created **private**; automation of a commercial app's reward loop is not something to publish without a deliberate decision) | determines whether docs can quote account screenshots |
| Q2 | Does the phone's screen lock require a PIN/biometric overnight? | a locked device cannot be driven; needs `input text` of a PIN or lock disabled |
| Q3 | Is the shortcut/widget reward granted on creation, or only on a launch *from* the launcher item? | decides whether the project can fake the entry point or must create and then invoke it |
| Q4 | Should the quiz be answered randomly, or is there a reward for accuracy? | brief says random; worth one measurement |
| Q5 | Notification channel for failures (ntfy topic? Home Assistant? email?) | FR-16 |
| Q6 | Acceptable run window and frequency (03:30 nightly? retry once on failure?) | FR-14 |
| Q7 | Is a small number of missed tasks acceptable in exchange for never taking a risky action? (the policy assumes yes) | shapes how aggressive task matching should be |

## 10. Acceptance criteria for v1

1. `just check` green from a clean clone (lint, format, mypy, tests with coverage ≥ ratchet).
2. `docker run … doctor` reports the device, the app version and an OCR box count on the real phone.
3. A nightly run on the reference device collects the daily sign-in and at least two tasks,
   with a `summary.md` showing a positive balance delta.
4. Popups encountered during a run are all closed, and the count appears in the report.
5. No refused task type is ever tapped: the deny rules are covered by tests, and the run log
   shows each refusal with its reason.
