# 00 — Requirements brief

**Status: requirements of record.** What this project is for, what it must do, and where its edges
are, narrowed by measurements on the reference device ([`01-recon-findings.md`](01-recon-findings.md)).
Remaining app unknowns live in [`05-app-knowledge.md`](05-app-knowledge.md) §8.

## 1. Purpose

Collect AliExpress "coins" automatically, once a night, without supervision. Coins are worth little
(≈ C$0.01 each); the value is not spending twenty minutes a night tapping a phone, and doing it
reliably.

- **In scope:** daily sign-ins, browse-and-wait tasks, coins quiz (with a learning question bank),
  surprise items, notifications, popup dismissal, run reporting, and nightly scheduling. Launcher
  shortcut and widget tasks are catalogued in `specs/tasks.yaml` but descoped (#27 — skipped as
  `no executor`).
- **Out of scope (refused by policy):** mini-games (Play & Earn, Merge Boss, GoGo Match, Prize
  Land, Shelf Shuffle), currency conversion (water, diamonds, energy), purchases, order placement,
  cart building, coin redemption, reviews, and sharing/referrals.

## 2. Reference environment (verified 2026-11-19)

| fact | value |
|---|---|
| device | wireless adb (`AECOINS_DEVICE`, reference `192.168.8.65:5555`), model `T3_US` |
| Android | 15 (SDK 35), `userdebug`, `ro.adb.secure=0`; runtime uses plain `adb shell` (`uid=2000`) |
| screen | 2000×1200 physical, 240 dpi; portrait screenshots **1200×2000** (`c9b8f88670e295b5`) |
| app & locale | `com.alibaba.aliexpresshd` `8.180.1` (versionCode `80005998`), Canada store, `en` (`C$`) |

Portrait-only, single account, single device. `1200×2000` is the measured reference geometry; other
portrait sizes derive row layout and template scale from the frame and key last-resort popup
calibration by `doctor`'s profile id. An uncalibrated popup reports `popup_unclosable` rather than
tapping a guessed coordinate ([`06-runbook.md`](06-runbook.md) §14).

## 3. Functional requirements

### 3.1 Reaching the coins page & task engine

- **FR-1 / FR-2** Wake the device, dismiss the keyguard (stop with `device_locked` if a PIN or
  biometric lock remains), warm-start `FirstActivity`, and sweep popups before acting. An unclosable
  popup fails loudly as `popup_unclosable`, never by blind tapping.
- **FR-3 / FR-4** Reach the coins page via the shortcut deep link
  `aliexpress://goto?url=https%3A%2F%2Fm.aliexpress.com%2Fp%2Fcoin-index%2Findex.html` after the
  app is warm ([`05-app-knowledge.md`](05-app-knowledge.md) §2). `--dry-run` stops before the first
  in-app tap (`taps_issued: 0`) and writes a report.
- **FR-5 / FR-6** Claim both independent daily sign-ins (streak `Collect` and boost `Daily check-in`)
  once per local day, verify positively on screen, and record completion in atomic daily state.
- **FR-7** Enumerate tasks from `Coins boost` (`Earn more`), falling back to inline rows. An empty
  boost list ("There's nothing here yet") is a load fault that triggers warm re-entry, never an
  empty day.
- **FR-8 / FR-9** Classify every row against `specs/tasks.yaml` before dispatch. Unknown labels are
  skipped (`unrecognised`, default-deny). Refuse and log: any game (Play & Earn, Merge Boss, GoGo
  Match, Prize Land, Shelf Shuffle), currency conversion (water, diamonds, energy), buying, placing
  an order, adding to cart, leaving a review, or sharing/inviting.
- **FR-10** Enforce per-task daily limits, `AECOINS_MAX_TASKS_PER_RUN` (default 12), and
  `AECOINS_RUN_BUDGET_SECONDS` (default 2700 s).
- **FR-11** Dedicated executors: **11a browse-and-wait** (sponsored items, recently viewed,
  shopping-credits check-in, learn to save — watch the right-edge timer chip, 15 s catalogue bound);
  **11b quiz** (+ **FR-22** — OCR options, answer from `data/quiz_bank.json`, learn
  `Correct answer: X`, absorb `YOU CAN NOW REDEEM!`); **11c surprise items** (3 distinct products
  per round, BACK increments); **11d/11e shortcut & widget** (catalogued, automation descoped #27);
  **11f notifications** (immediate credit, app pre-prompt, Android runtime dialog, or stop at OS
  settings without toggling).
- **FR-12 / FR-13** Sweep popups after every navigation and verify completion positively (progress
  badge, completed control, balance delta, or row gone from a complete surface).

### 3.2 Unattended operation & packaging

- **FR-14 / FR-17** Run nightly from a container (`run --schedule`); refuse unreachable/locked
  devices and timezone mismatches (`AECOINS_TIMEZONE` ≠ `persist.sys.timezone`). Persist daily state
  atomically and serialise runs sharing a state directory with `state/run.lock`.
- **FR-15 / FR-18** Always write `runs/<UTC timestamp>/` (`summary.md`, `run.json`, `run.jsonl`,
  `roster.jsonl`, `shots/*.png` + OCR sidecars) and prune to `AECOINS_KEEP_RUNS` (default 30).
- **FR-16 / FR-23** Emit structured log levels and deliver optional notifications via Apprise
  (primary) or legacy webhook/ntfy; delivery failure never fails the run.
- **FR-19 / FR-20 / FR-21** Python 3.14, ruff + mypy strict (`src/` and `tests/`) + pytest coverage
  ratchet via `just check`; non-root `python:3.14-slim-trixie` container (`tini`, `adb`, RapidOCR);
  CI runs `just check` on every PR and publishes to GHCR with provenance and SBOM on `main`.

## 4. Task taxonomy

| task | policy | category / executor | notes |
|---|---|---|---|
| Daily coins (streak `Collect`) & `Daily check-in` | ✅ allowed | `sign_in` (`signin.py`) | two independent claims per day |
| Take the coins quiz | ✅ allowed | `quiz` (`quiz.py`) | 2 or 4 options; learns into `data/quiz_bank.json` |
| Explore sponsored items | ✅ allowed | `browse_wait` (`browse.py`) | timer chip (15 s bound), repeatable `6/day` |
| Learn to save money with Coins Credits | ✅ allowed | `browse_wait` (`browse.py`) | timer chip (15 s bound) |
| View Super discounts | ✅ allowed | `browse_wait` (`browse.py`) | timed view (~15 s), +1 coin, no purchase step; repeatability ❓ |
| Browse recently viewed items | ✅ allowed | `browse_wait` (`browse.py`) | timer chip (15 s bound), opportunistic |
| Check in to win Shopping credits | ✅ allowed | `browse_wait` (`browse.py`) | timer chip (15 s bound) |
| Browse surprise items | ✅ allowed | `item_tap` (`surprise.py`) | 3 distinct products per round, `2` rounds |
| Turn on notifications | ✅ allowed | `notification` (`notifications.py`) | handles system dialog / settings redirect |
| Add a coins shortcut | ⏭️ descoped | `shortcut` (descoped, #27) | skipped as `no executor` |
| Add & start using our widget | ⏭️ descoped | `widget` (descoped, #27) | skipped as `no executor` |
| Search for what you love | ❌ refused | — | default-deny (`unrecognised`) |
| Play games (Prize Land, Play & Earn, Merge Boss, GoGo Match, Shelf Shuffle) | ❌ refused | — | refused by policy |
| Convert water / diamonds / energy; Water to win | ❌ refused | — | refused by policy |
| Buy / checkout / purchase / place an order / add to cart | ❌ refused | — | refused by policy |
| Leave or write a review | ❌ refused | — | refused by policy |
| Share coins / invite friends | ❌ refused | — | refused by policy |

## 5. Constraints & assumptions

- `AdcActivity` and its modals expose no usable `uiautomator` nodes → screenshot + OCR perception
  ([`adr/0002`](adr/0002-screenshot-ocr-perception.md)). Bare `/p/coin-index/` URLs without
  `aliexpress://goto?url=` render a blank WebView.
- Device stays powered on LAN, signed in (`app_logged_out` stops at first detection), and unlocked
  (`device_locked` stops without tapping). One device, one account, no parallelism.
- UI-first ships first ([`adr/0001`](adr/0001-drive-the-ui-not-the-api.md)); HTTP API replayability
  remains an open spike (#9).

## 6. Decisions of record (answered 2026-11-20)

| # | topic | decision |
|---|---|---|
| Q1 | Repository visibility | **Private now.** Open-source only after the #41 release gate (§7). |
| Q2 | Device lock | **Assume no PIN/biometric lock.** Wake + dismiss keyguard; if still locked, report `device_locked` and stop without tapping. |
| Q3 | Shortcut/widget trigger | **Answered & descoped (#27, 2026-11-23):** adding earns the boost task reward and a widget visit rendered `Collect`/inline rows in session 2, but automation is descoped because the shortcut URI works without launcher items. |
| Q4 | Quiz accuracy | **Build `data/quiz_bank.json`:** learn `Correct answer: X` from the result page and answer from the bank next time. |
| Q5 | Notifications | **Apprise** owns downstream transports; the collector emits structured log levels and one report message. |
| Q6 | Run window & retries | **One retry per safe stage**; manual runs were built first and nightly scheduling (`run --schedule`) has since landed. |
| Q7 | Missed task vs risky action | **Miss the task.** Default-deny stays strict and every refusal is logged in the report. |

## 7. Acceptance criteria & open-source gate

**v1 baseline:** `just check` green from a clean clone; `doctor` reports `adb`, `device`, `geometry`,
`timezone`, and `ocr` all `ok`; a manual run collects sign-ins and tasks with a verified `summary.md`;
popups are dismissed and counted; zero refused tasks tapped; quiz uses and updates `data/quiz_bank.json`.

**Ready to open source (#41):**

| criterion | measure |
|---|---|
| onboarding | user with Docker + device reaches a first run in **≤5 minutes** from `git clone` |
| stability | **two consecutive weeks** of unattended runs in [`08-stability.md`](08-stability.md) with no unrecoverable failure |
| failure behaviour | every failure class has a documented, tested response and leaves the app clean |
| safety | deny list verified against a live roster; zero refused tasks tapped |
| portability | serial, screen geometry, app version and store region limits documented ([`06-runbook.md`](06-runbook.md) §14) |
