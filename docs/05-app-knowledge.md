# 05 — What the app actually does

Observed behaviour of AliExpress `8.180.1` on the reference tablet (`T3_US`, Android 15, Canada /
`en`). Measured 2026-11-19 through 2026-11-24; reviewed flow fixtures live under `tests/data/flows/`.

Legend: ✅ observed · ⬜ inferred from an observation · ❓ unknown.

## 1. Two balances exist ✅

| balance | UI labels | notes |
|---|---|---|
| coins | `Coins`, `worth C$0.0x`, `+6` | ~100 coins ≈ C$1; check-in paid +2 coins |
| coin credits | `Coins credits to redeem`, `C$0.00` | separate redeemable balance; quiz paid +6 coins and +US$0.01 credit |

## 2. Entry points & page variants ✅

| route | how | settles on | notes |
|---|---|---|---|
| **shortcut deep link** ✅ | `am start -a android.intent.action.VIEW -d 'aliexpress://goto?url=https%3A%2F%2Fm.aliexpress.com%2Fp%2Fcoin-index%2Findex.html'` | `AdcActivity` (`coinindex`) | **Primary route.** Works with `coinsPromotion` uninstalled; must target a warm app (§4b). |
| **widget** ✅ | `AECoin2x2NormalWidget` (`from=widget&aewidget_*`) | `AdcActivity` | Produced `Collect` + inline rows in session 2; descoped (#27). |
| **Account card** ⚠️ | Account tab → promo card | varies | Rotates between coins and coupon cards; not implemented. |
| bare `/p/coin-index/` ❌ | `am start -d https://www.aliexpress.com/p/coin-index/…` | blank WebView | Missing `aliexpress://goto?url=` wrapper. |

`AdcActivity` renders either the **plain/shortcut** layout (header, redeem card, `Daily coins`
streak, mini-game tiles, feed — inline task rows may be absent) or the **widget** layout (adds
`Collect` and inline rows). Because inline rows are conditional, the runner opens the fuller
`Coins boost` list (`Earn more`) and treats inline rows as a partial subset.

## 4. The task surface ✅

Tasks appear inline and on **`Coins boost`** (`Earn more`). The roster is server-driven and rotates
between loads (e.g., `Browse surprise items` rotated to `Search for what you love` after completion).

| task | subtitle | reward | progress | policy / executor |
|---|---|---|---|---|
| Daily check-in *(boost row)* | Finish today's check-in | +1 | — | allowed: `sign_in` (`signin.py`) |
| Take the coins quiz | — | +3~6 coins, +0~US$0.01 | — | allowed: `quiz` (`quiz.py`) |
| Learn to save money with Coins Credits | Stack with coins, coupons and other discounts | +1 | — | allowed: `browse_wait` (`browse.py`) |
| Explore sponsored items | Browse, shop and save | +1 | `1/6` | allowed: `browse_wait` (`browse.py`) |
| Browse recently viewed items | Coins deal on viewed, in-cart items | +1 | — | **refused**: `unrecognised_recently_viewed` (#116) |
| Check in to win Shopping credits | Don't forget to check in today! | — | — | allowed: `browse_wait` (`browse.py`) |
| Browse surprise items | Tap 3 items on this page to earn coins | +1 | `0/2` | allowed: `item_tap` (`surprise.py`) |
| Turn on notifications | Stay informed when rewards drop | +1 | — | allowed: `notification` (`notifications.py`) |
| Add a coins shortcut | One tap away from your daily rewards | +1 | — | catalogued `shortcut`; descoped (#27, skipped `no executor`) |
| **Visit the page via coins shortcut** | Unlock a daily surprise | +1 | — | allowed: `shortcut_visit` (`shortcut.py`); `GO` opens the OS shortcut sheet, so a launcher visit remains operator work (#130) |
| Add & start using our widget | Add our widget to your homescreen to use | +1 | — | catalogued `widget`; descoped (#27, skipped `no executor`) |
| Search for what you love | Earn by using keywords to explore items | +1 | `0/2` | allowed: `browse` (`search.py`) |
| View Super discounts | Check out this page for 15 seconds | +1 | — | allowed: `super_discounts` (`browse_wait`, `browse.py`) |
| Place an order on this page | Each order grants you rewards | — | `0/3` | **refused** (`spending_order`) |
| Add 5 items to your cart | — | — | — | **refused** (`spending_cart`) |
| Water to win a US $0.10 prize | Water your tree in Prize Land | — | — | **refused** (`currency_water`) |
| Share coins with friends | Win rewards together | — | `0/3` | **refused** (`social`) |
| *coin page only:* Merge Boss / Play & Earn / Prize Land | — | +30 / +900 | — | **refused** (games) |

Explicit deny rows sit beside allowed rows with identical `GO` buttons — enforcing the deny
list at dispatch is mandatory. `Search for what you love` is now an allowed browse row with its own
search-submission executor (#108/#142). `Browse recently viewed items` is also present with the same
`GO` shape, but #116 refuses it because its destination and completion contract were never captured.

### 4b. The stuck state (`empty_boost`) ✅

- **Detection:** `Coins boost` header + `"There's nothing here yet"` + zero task rows (never a
  legitimate empty day because refused tasks are always listed).
- **Cause:** firing the shortcut intent as a **cold start** (`force-stop` → intent: 4/4 empty;
  `FirstActivity` + ~10 s settle → intent: 2/2 populated; warm app + intent: 3/3 populated).
- **Runner rule:** warm-start first; on `empty_boost`, re-enter via the warm shortcut route.

### 4c. The boost list scrolls ✅

The `Coins boost` list is one scrollable surface; its first frame is only a screenful. On 2026-11-24 the
`Visit the page via coins shortcut` and `Share coins with friends` rows were both below the fold, which is
why the old first-frame roster never named them. Discovery now swipes down with the existing
viewport-relative gesture (no pixel constants), stops on a stable frame or on a frame that adds no new
title, merges frames by title, and restores the list to the top before live dispatch (#130). The bound is
`MAX_ROSTER_SCROLLS = 8`; a list still changing at that bound is reported, not silently accepted.
Leaving the boost surface during a swipe is also reported as an incomplete read, and absence only
proves a row is gone after a read that was proved complete.

The sanity check now compares every `GO` seen across the scrolled frames with the distinct enumerated
rows. A button that never becomes a row in any frame stays unassigned and appears as
`GO button(s) across N frame(s) but ... task action(s) were not enumerated`; one screenful is no longer
enough to call the list clean.

## 5. Reward mechanics ✅

- **Daily check-in (two independent claims) ✅:** streak-row `Collect` (+2 coins, disappears) and
  boost-page `Daily check-in` (+1 coin). Claimed and recorded independently; neither proves the other.
- **Quiz ✅:** one question/visit with 2 (`True`/`False`) or 4 OCR-enumerated options. Result page
  prints `Correct answer: X`, `Your answer: Y`, and `You got +6` (wrong answers still pay) and
  updates `data/quiz_bank.json`. `Go back` can raise `YOU CAN NOW REDEEM!`.
- **Browse-and-wait ✅:** opens a page with a right-edge timer chip (`x ≈ 0.97 · width`, red `Ns`
  pill → green check). `wait_for_timer_chip` polls until digits vanish (`15 s` catalogue bound in
  `specs/tasks.yaml`; timer starts during load, measured `12s → 1s → check` in ~6 s); a page with no
  chip returns `absent`. **`Explore sponsored items` ✅** opens the coin store (`Use coins for
  discounts`, `Disc:` / `Applied` tiles, right-edge chip) — a recognised browse destination.
- **`Browse recently viewed items` ❓ (run `20261124T083006Z`, issue #116):** the boost row reads
  `Browse recently viewed items`, subtitle `Coins deal on viewed, in-cart items`, `GO`, `+1`, and no
  readable badge ✅. It was absent from the initial roster, appeared mid-run at observation 3, and
  its `GO` moved again by observation 9 as the server rotated rows through the run; that reflow is
  why the older boost-roster boxes cannot judge the tap. The requested point `[1020,1640]` matches
  the centre of the `GO` box in the returned inline row and the jittered point `[1020,1644]` remains
  inside it ⬜. That favours a hit on the fresh inline control rather than either stale boost-roster
  box, but the pre-tap frame was not retained, so it is not proven. The only post-tap OCR retained is
  `["3:36","0","日","24"]` with `surface=unknown`; there is **no post-tap PNG or foreground-package
  record**, so the artifacts cannot prove the app left the foreground or distinguish launcher, crash,
  external navigation or an interstitial ❓. The `non_app` recogniser is therefore **unverified
  against a captured frame**: its test uses the retained OCR labels, not the missing image. One
  shortcut navigation returned to the coin page ✅, where the row remained uncompleted. The
  destination page, timer, badge/round count and completion contract are therefore unknown ❓. The
  family is explicitly refused as `unrecognised_recently_viewed` until a supervised capture
  establishes them; its `Browse` title is not a browse-wait contract. Sanitized evidence lives in
  `tests/data/flows/recently_viewed_issue_116.json`.
- **`Visit the page via coins shortcut` ✅ observed / ❓ completion source (2026-11-24 13:00 EST,
  issue #130):** the row sits below the first screenful, so its title OCRs on two lines:
  `Visit the page via coins` / `shortcut`, subtitle `Unlock a daily surprise`, reward `+1`, `GO`, no
  badge ✅. Tapping `GO` did **not** open the coin page and did not credit: it opened Android's
  `Create shortcut` sheet (`Once you've created a shortcut, find it on your home screen. Tap it to
  enter`, `Got it`, `Create shortcut`) while the balance stayed `87` and the row stayed visible ✅.
  `Got it` closed the sheet without a credit ✅. `Create shortcut` led to the launcher's
  `Add to home screen` step; after adding the `Coins` shortcut, the first shortcut URI fired from the
  app still left the balance at `87` and the row present ✅. Launching the created `Coins` shortcut
  from the home screen, then reloading the coin page, showed balance `87 → 88` and the row gone ✅.
  The exact instant of the credit (home launch versus the later reload) was not bracketed, so
  whether a launcher launch is strictly required rather than a delayed URI is ❓. The collector does
  **not** create or tap launcher items (#27); `shortcut.py` taps this row's `GO`, recognises the
  sheet, closes it and records `shortcut_sheet_no_credit` unless a readable +1 appears ✅. Sanitized
  evidence is in `tests/data/flows/shortcut_visit_issue_130.json`.
- **Launcher items ✅ (descoped #27):** adding earns the task row and a widget visit rendered
  `Collect`/inline rows in session 2; automation is descoped because the shortcut URI needs no
  launcher item. The `Visit the page via coins shortcut` row above is now a separate numbered
  family; its observed credit followed a launcher launch, not the URI alone (#130).

### 5b. Flows walked end to end

- **`Learn to save money with Coins Credits` ✅:** explainer page with the same timer chip; paid +1
  and removed the row.
- **`Browse surprise items` ✅ (2026-11-22):** two rounds of three distinct products (`N/2` row,
  `N/3` counter). Tapping a tile opens `AIProductDetailActivity` (`Item 1/n`, `Add to cart` /
  `Buy now` — **never tapped**); **BACK** increments the counter per **distinct product**, not per
  tap ([`01-recon-findings.md`](01-recon-findings.md) §7c). The counter is re-read until it stops
  changing within a bounded deadline, because a credit can arrive in more than one increment; a
  tile that leaves it unchanged is inert and the round fails `progress_unchanged` rather than being
  recorded. Scrolling for unseen products moves the header and pill off-screen; the
  executor recognises the same two-column card set it already read, not generic discount copy, and
  restores the top before reading the counter (#100). At `3/3`, `Mission Completed` stays >10 s
  until tapped; `Next round` appears on non-final rounds at ~(597, 1240) ⬜. Final round paid +1
  (43 → 44) and removed the row (intermediate-round payout ⬜). The header and `N/3` pill can paint
  while every product card is still a skeleton (measured 2026-11-24 in the `0/2` case), so a
  tile-less first read is settled before the grid is called empty; each round's first frame is
  retained under `shots/surprise-items-round-<n>-grid.*` (#115). The counter-proven round is recorded
  immediately; when the returned row badge (`N/2`) is readable it is re-read after a short dwell
  and a non-advancing badge fails `progress_unchanged`, but an absent row leaves the counter as the
  evidence. Until
  #63 settles cross-round reset ❓, `surprise.py` tries per-round distinctness first and falls back
  once to whole-task exclusion.
- **`Turn on notifications` 🟡:** paid +1 on 2026-11-20 with no dialog and `POST_NOTIFICATION: ignore`
  unchanged. App settings' toggle deep-links to OS settings (no runtime dialog). The executor
  handles all four outcomes: immediate credit, app pre-prompt, runtime dialog (`Allow` / `Don't allow`),
  and OS settings redirect (`routed_to_settings`, stops without toggling).
- **`Search for what you love` ✅ (2026-11-25, issue #142):** the row starts at `0/2`; its `GO`
  opens a landing with a search field, an account-specific `Search history` section and a
  server-rendered `Discover more` grid ✅. Landing alone does not advance the badge ✅.
- **A category card is not a submitted search ✅.** The search field's grey placeholder already read
  `OPPO` in the pre-tap frame (`frame-004` at 17:07:43.869; the tap followed at 17:07:44.417), and the
  landing was still on screen with the same field text at 17:07:46.071; run `20261125T170508Z` waited
  out its 3 s transition bound, recorded `transition_not_observed`, and the row stayed `0/2` ✅.
  Whether the card is inert or the tap was lost while the page was still loading images is ❓; this
  supersedes the 2026-11-24 recon note that a category tap advanced the row.
- **A real search does advance the row ✅.** The measured sequence is: tap the field (keyboard
  appears), type a benign query (`wireless phone`) through `input text`, then submit with the IME
  enter action. The field shows the query and suggestions; enter dismisses the keyboard and starts
  the search. The page was blank/skeleton for several seconds before product cards painted ✅.
  The results surface keeps the query in the field and paints product cards with `200 sold 4.9`,
  `C$32.99`, `Disc:` and `Applied` copy ✅. Returning to the coin page moved the row
  `0/2 → 1/2` and the balance `104 → 105` ✅.
- **The counter is the task, not one search ✅.** Live run `20261125T175208Z` resumed at `1/2`,
  typed `phone case`, submitted it, and returned to a populated boost list where the row was gone
  (the app removes it at `2/2`); the run recorded `progress_complete` from the row's disappearance ✅.
  The run's balance moved `105 → 107 (+2)` across the final search round and a shopping-credits
  check-in; the earlier manual search isolated the search task's `+1` (`104 → 105`) ✅, while the
  final round's own `+1` is inferred from the `+2` total and the `+1` row contract ⬜.
- **Verification is the results surface, never the landing ✅.** `search.py` counts a round only
  after `is_search_results` sees the submitted query plus a product price; a landing that remains on
  screen is `results_not_reached` and is not recorded. It tries the IME enter action first (observed
  live ✅) and one bounded retry on the field's own search control (implemented and unit-tested, not
  exercised live ⬜), then fails honestly ✅. No product, cart or buy control is tapped ✅.
- **The easiest return is the shortcut route ✅.** Two BACKs from the landing still exit to the home
  feed (docs/07 §1), so every round returns through the measured shortcut route.
- **Open questions:** whether repeating the same query counts as a second round ❓ (the executor
  uses two different benign queries), and whether a landing variant without `Search history` keeps
  the field in the same band ⬜ (the field is derived from the first heading and capped by a frame
  fraction). The reviewed results fixture is
  [`tests/data/flows/search_results_issue_142.png`](../tests/data/flows/search_results_issue_142.png).
- **`View Super discounts` ✅ (2026-11-24 00:35 EST, run `20261124T053441Z`):** a timed view task.
  The row reads `View Super discounts`, subtitle `Check out this page for 15 seconds`, `GO`, and
  `+1`; there is no purchase step. The `GO` opens a destination page; the live run read the
  right-edge chip as `14s` then `13s` before it vanished, and the coin balance moved
  `worth C$0.97 → C$0.98` (boost header `66 → 67`) — **the reward is +1 ✅**. The row was still
  visible on the verifier's first boost read moments after the chip cleared, then gone on the next
  load. The executor now re-enters the list once when the first verification read is still
  unchanged, and judges that settled read; the original run predates that guard, so it recorded
  `no_change` despite the coin having paid and is not quoted as a `row_gone` completion. The row
  sits directly above the mini-app shortcut strip
  (`Merge Boss`, `Play & Earn`, `Prize Land`), which is **not** its subtitle; the 2026-11-24
  roster refused it as `game_play_and_earn` only because the strip was absorbed upward. The exact
  destination page marker, the full countdown-to-check transition and whether the row repeats are
  ❓; do not infer them from the tiles below the row.

### 5c. Daily reset boundary ✅

The daily task reset is **00:00 device-local** ✅, bracketed by three `--dry-run` passes on the
same coin page on the night of 2026-11-23/24 (issue #111):

| local (EST) | `daily_checkin` | plan |
|---|---|---|
| 23:25 | allowed, then `task_skipped` `daily limit` | `steps: []` |
| 23:45 | allowed, then `task_skipped` `daily limit` | `steps: []` |
| 00:05 | allowed, **no skip** | `steps: ["Daily coins"]` |

Before the roll, the planner still recognises `daily_checkin` as allowed, but daily state marks it
at its limit and the plan is empty. At 00:05 the fresh day plans `Daily coins`. The 23:45 and 00:05
readings place the cut at device-local midnight, not 03:00 or 03:30.

This supersedes the recon-era "Pacific midnight?" hypothesis: a Pacific-midnight reset would land at
03:00 EST, outside the 23:45 → 00:05 bracket. The boundary follows the device's own local day.

Evidence: `/home/alex/aecoins-reset/20261123T234509-0500.log`, run `20261124T042511Z` (23:25 EST),
run `20261124T044513Z` (23:45 EST), and run `20261124T050459Z` (00:05 EST). The tablet showed `11:25`
at 23:25 EST and its timezone was EST (`UTC-05:00`, `America/Toronto`). The former 03:10 probe is
retired; this bracketing settles the reset hour.

## 6. Interstitials seen ✅

| interstitial | trigger | exit |
|---|---|---|
| `Welcome gifts` | sometimes on cold start | drawn ✕ at ~(600, 1652), no OCR text |
| `YOU CAN NOW REDEEM!` | after a task pays credit (quiz → `Go back`) | no OCR close; **BACK** exits (2nd BACK exits app) |
| `Coins boost` empty state | `Earn more` after cold-start intent | back arrow / warm re-entry (§4b) |
| `Mission Completed` | 3rd distinct product in surprise items | **tap the overlay** (>10 s without auto-dismiss) |

## 7. Run flow on the app

`FirstActivity` (~10 s warm-up) → shortcut deep link → popup sweep → `balance-before` → `Earn more`
(warm re-entry on `empty_boost`) → daily sign-ins → `DispatchGate` + executors + positive
verification → return to coins page → `balance-after` → report. See
[`07-navigation-map.md`](07-navigation-map.md) for screen markers and transitions.

## 8. Still unknown

| question | why it matters | how to settle it |
|---|---|---|
| Why were inline task rows absent on some loads? ❓ | informs the "nothing available" branch | observe across a daily reset boundary |
| Exact dwell per browse task ❓ | confirms `15 s` in `specs/tasks.yaml` stays generous (chip is authoritative) | time one sponsored-items visit |
| Does surprise-items distinctness reset between rounds? ❓ | resolves the fallback branch in `surprise.py` | #63: start at `0/2`, open same product in both rounds |
| Quiz result & notification denial frames ❓ | completes `tests/data/flows/` fixtures | capture before rows are claimed |
| Whether the quiz question set is finite ❓ | question-bank hit rate | observe `data/quiz_bank.json` growth |
| What does `Browse recently viewed items` open, and what proves completion? ❓ | the row is visible with `GO` / `+1` but no badge; #116 could not retain its destination or post-tap frame | one supervised run that retains the pre-tap row, destination/timer, and populated boost verification frames |
| Is a launcher launch strictly required to credit `Visit the page via coins shortcut`? ❓ | the observed +1 followed a home-screen shortcut launch, while a URI fired after creating the shortcut did not credit immediately | bracket the balance and row after each step on a fresh day, separating the home launch from the next coin-page reload (#130) |
