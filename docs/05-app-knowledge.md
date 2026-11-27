# 05 — What the app actually does

Observed behaviour of AliExpress `8.180.1` on the reference tablet (`T3_US`, Android 15, Canada /
`en`). Measured 2026-11-19 through 2026-11-26; reviewed flow fixtures live under `tests/data/flows/`.

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
| Learn to save money with Coins Credits | Stack with coins, coupons and other discounts | ❓ | — | allowed: `browse_wait` (`browse.py`); subtitle ⬜ from old recon; **not offered in any #159 sample**, contract ❓ (§4e) |
| Explore sponsored items | Browse, shop and save | +1 | `1/6` | allowed: `browse_wait` (`browse.py`) |
| Browse recently viewed items | Coins deal on viewed, in-cart items | +1 | — | allowed: `recently_viewed` (`browse_wait`, `browse.py`); measured #116 |
| Check in to win Shopping credits | Don't forget to check in today! | — | — | allowed: `browse_wait` (`browse.py`) |
| Browse surprise items | Tap 3 items on this page to earn coins | +1 | `0/2` | allowed: `item_tap` (`surprise.py`) |
| Turn on notifications | Stay informed when rewards drop | +1 | — | allowed: `notification` (`notifications.py`) |
| Add a coins shortcut | One tap away from your daily rewards | +1 | — | catalogued `shortcut`; descoped (#27, skipped `no executor`) |
| **Visit the page via coins shortcut** | Unlock a daily surprise | +1 | — | allowed: `shortcut_visit` (`shortcut.py`); accepts the shortcut sheet, uses the launcher item, verifies +1 and removes the pin (#130, #158) |
| Add & start using our widget | Add our widget to your homescreen to use | +1 | — | catalogued `widget`; descoped (#27, skipped `no executor`) |
| Search for what you love | Earn by using keywords to explore items | +1 | `0/2` | allowed: `browse` (`search.py`) |
| View Super discounts | Check out this page for 15 seconds | +1 | — | allowed: `super_discounts` (`browse_wait`, `browse.py`) |
| Place an order on this page | Each order grants you rewards | — | `0/3` | **refused** (`spending_order`) |
| Add 5 items to your cart | — | — | — | **refused** (`spending_cart`) |
| Water to win a US $0.10 prize | Water your tree in Prize Land | — | — | **refused** (`currency_water`) |
| Share coins with friends | Win rewards together | — | `0/3` | **refused** (`social`) |
| *coin page only:* Merge Boss / Play & Earn / Prize Land | — | +30 / +900 | — | **refused** (games) |

Explicit deny rows sit beside allowed rows with identical `GO` buttons — enforcing the deny
list at dispatch is mandatory. `Search for what you love` has its own search-submission executor
(#108/#142). `Browse recently viewed items` is a measured `browse_wait` row: its timer chip and
land-then-return completion contract are recorded in §5.

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

### 4d. The boost sheet is sampled, not a fixed list ✅/⬜/❓ (#149)

The sheet's row set is not a fixed list. Measured on 2026-11-26 on the same account and modal;
sanitized full frames are
[`boost_rotation_issue_149_first_read.png`](../tests/data/flows/boost_rotation_issue_149_first_read.png),
[`…_open_36m.png`](../tests/data/flows/boost_rotation_issue_149_open_36m.png),
[`…_top.png`](../tests/data/flows/boost_rotation_issue_149_top.png),
[`…_scrolled.png`](../tests/data/flows/boost_rotation_issue_149_scrolled.png) and
[`…_bottom.png`](../tests/data/flows/boost_rotation_issue_149_bottom.png). Exact capture times,
source files, commands and hashes are in
[`boost_rotation_issue_149_observation.json`](../tests/data/flows/boost_rotation_issue_149_observation.json).

- **The top set did not change while the sheet stayed open ❓.** The first read at 14:45:48Z read
  `Explore sponsored items` (`4/6`), `Browse surprise items` (`1/2`), `Place an order on this page`,
  `Add 5 items to your cart`; the same open modal read the same four rows at 15:22:04Z
  (`…_first_read.png`, `…_open_36m.png`). The app hides the status-bar clock, so the interval comes
  from the command log and file timestamps, not an on-frame clock; the elapsed-time test is
  therefore **❓**, not a timer measurement.
- **A simple close/re-open produced the same top set ✅.** Closing with the header arrow and tapping
  `Earn more` again at 15:22:10Z produced a frame byte-identical to the 15:22:04Z capture, so it is
  not kept as a second fixture; the provenance JSON records the duplicate hash and command.
  Reloading the whole page through the shortcut route produced the same top set at 15:23:02Z
  (`…_top.png`).
- **Scroll position changes what one read sees ✅.** One viewport scroll exposed
  `Water to win a US $0.10 prize` and `Share coins with friends` (`…_scrolled.png`, 15:23:08 UTC);
  further scrolls exposed `Browse recently viewed items`, `Add & start using our widget` and
  `Turn on notifications` (`…_bottom.png`), then stopped on a stable frame ✅. The read before the
  scroll and the read after it therefore differ, but the measured cause here is the scroll, not a
  timer. Holding the bottom for three minutes and then closing/re-opening and scrolling back to it
  produced the same bottom set at 15:26:41Z ✅.
- **`View Super discounts` was read and completed before this measurement ✅.** Run
  `20261126T100143Z` (a different worktree using the pre-#149 code) recorded
  `policy_decision key=recently_viewed allow` at 10:06:35Z; its post-tap frame already listed
  `View Super discounts` / `Check out this page for 15` / `GO` / `seconds` on the same
  `boost_page`. It then recorded `policy_decision key=super_discounts allow` at 10:08:48Z, tapped at
  `(1007,1830)` at 10:08:49Z, and recorded
  `task_outcome key=super_discounts outcome=completed verification=row_gone seconds=53.427
  recorded=true` at 10:09:40Z. The row is therefore absent from the 14:45–15:27Z sheet because it
  was **already claimed for the day**, not because the app never offered it; a second live
  completion in the same day cannot be produced. That run proves the row exists on the device; it
  is **not** evidence for the new rotation rule, because it predates the change.
- **The later read is consistent with that claim ✅.** Run `20261126T152715Z` read the sheet twice
  (`rotation.samples: 2`, `agreeing_samples: 2`) after the 10:08Z claim, found no Super-discounts
  row, reported `super_discounts` under `not seen in any read`, and left the night open
  (`incomplete_roster`) rather than claiming a complete day. That run used an isolated temporary
  state that did not carry the 10:08Z completion; with the real day state the carve-out now reports
  it as already claimed instead of unseen. The earlier in-place swaps in
  `20261125T175208Z`'s roster log (`Check in to win Shopping credits` → `Water …` between
  observations 2 and 5) are still best explained by a server refresh on load/completion ⬜; an
  in-place swap while the same frame is left open was not reproduced today ❓.

The runner therefore treats every readable read as one rotation sample, merges samples by
catalogue key (normalised title for refused rows), and requires two freshly opened reads to agree
before it may report `no dispatchable row remains`; a changed sample spends another pass and its
rows stay in the union. A catalogue family no read saw is **not** silently equated with "the app had
nothing": while the absence is still ambiguous it is `unread` and makes the roster incomplete and
the night `INCOMPLETE_ROSTER`. The first carve-out is a family already completed to its daily limit
in the day state — its absence is expected because it was claimed earlier today. Those families are
still named in the report as already claimed, not as unseen. `super_discounts` is the motivating
case: run `20261126T100143Z` claimed it at 10:08Z, so its absence from every later sheet is expected,
while an unseen and unclaimed family keeps the day open (#149). The second carve-out is dormancy
(#161): after `AECOINS_FAMILY_DORMANCY_DAYS` (default 7, floor 3) consecutive complete nights with
no sighting, the family is reported as `not_offered`, stops blocking completion, remains named with
its `missed_days`/`samples`, and is re-probed every `AECOINS_FAMILY_RETRY_DAYS` (default 7). A
sighting clears the record, so a returning row resumes normal dispatch immediately.

### 4e. `learn_to_save` was not offered in the #159 sample window ❓

The catalogue keeps `learn_to_save`; absence from a rotating sheet is not proof that the family was
removed. The coin page and a freshly re-opened, fully scanned `Coins boost` sheet were sampled 15
times on 2026-11-26, in three batches around the same settled 130-coin day:

| batch | first sample (UTC) | last sample (UTC) | samples |
|---|---|---|---|
| 1 | `21:42:45.242` | `21:48:59.346` | 7 |
| 2 | `22:07:11.813` | `22:08:19.992` | 2 |
| 3 | `22:26:23.873` | `22:30:44.184` | 6 |

Each sample fired the shortcut route, tapped `Earn more`, and scanned the sheet from the top until a
stable bottom. Every sample read the same ten rows: `Place an order on this page`,
`Add 5 items to your cart`, `Water to win a US $0.10 prize`, `Share coins with friends`,
`Explore sponsored items` (`6/6`), `Browse surprise items` (`2/2`), `Browse recently viewed items`,
`Add a coins shortcut`, `Add & start using our widget` and `Turn on notifications`.
`Learn to save money with Coins Credits` never appeared on the coin page or in any sheet frame. The
observation JSON also records the inline coin-page titles read in each sample; they were
`Place an order on this page`, `Add 5 items to your cart` and `Water to win a US $0.10 prize`, never
the learn-to-save row. The complete per-sample timestamps and distinct rows, source captures, hashes
and masking rectangles are
in [`learn_to_save_issue_159_observation.json`](../tests/data/flows/learn_to_save_issue_159_observation.json);
the reviewed top and bottom frames are
[`learn_to_save_issue_159_sheet_top.png`](../tests/data/flows/learn_to_save_issue_159_sheet_top.png)
and [`learn_to_save_issue_159_sheet_bottom.png`](../tests/data/flows/learn_to_save_issue_159_sheet_bottom.png).

| claim | status | evidence |
|---|---|---|
| the row is offered today | ❓ | absent from all 15 full reads; the catalogue still regards it as claimable |
| destination page | ❓ | never opened |
| what counts as progress | ❓ | never observed |
| badge / `N/M` counter | ❓ | never observed |
| return route | ❓ | never observed |
| timer / dwell timing | ❓ | no timer chip observed |
| reward amount | ❓ | no balance movement tied to this family |
| catalogue retention | ✅ | still an allowed `browse_wait` row; after the configured complete-night threshold it is reported `not_offered`/dormant rather than blocking every night (#161) |
| historical “paid +1” note | ⬜ | 2026-11-20 operator prose with no retained frame; not a measured contract |

A live run in this work (`20261126T223057Z`) read the sheet with only `learn_to_save` still
claimable, did not find the row, moved the balance `130 -> 130`, and named `learn_to_save` under
`unread_families` with `confirmed: false`. It stopped `budget` after two consecutive no-progress
passes; `surface_incomplete` was raised because the already-claimed `Browse recently viewed items`
row and the `Add & start using our widget` row (skipped `no executor`) had no readable card
structure, and the sheet did not produce two agreeing reads. It is an honest exclusion observation,
not a completed night. The catalogue row is deliberately **not** removed to make a night complete.

**Bounded dormancy (#161).** One absent night is still `unread`, but a family absent from every
complete read for `AECOINS_FAMILY_DORMANCY_DAYS` consecutive local days (default 7; startup floor 3)
is reported as **`not_offered`** and becomes dormant. Its evidence — `unseen_since`, the number of
consecutive unsighted days, the readable samples in that streak and zero sightings — is written to
`run.json`, `summary.md` and the day state. It stops keeping the night open, and a weekly re-probe
(`AECOINS_FAMILY_RETRY_DAYS`, default 7) revalidates the verdict. A sighting anywhere clears
dormancy immediately and makes the row dispatchable again. A pass that lost the boost surface or
had unreadable rows records no absence: that is the #149 regression guard, and it still makes the
roster incomplete. The label strings are **not** known to be stale: the #159 frames contain no row
resembling either the title or the subtitle, so there is nothing for OCR to match. The title and
subtitle prefixes are kept as additional patterns so a changed suffix still matches if the row
returns; the full copy remains unmeasured (§4e table: row offer ❓).

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
- **`Browse recently viewed items` ✅ observed / ⬜ inferred old cause / ❓ old launcher (supervised
  capture, 2026-11-25 ~17:10 EST, issue #116):** on a settled inline coin page (committed frame
  `tests/data/flows/recently_viewed_issue_116_entry.png`) the row's own `GO` centre was
  `(1018.5, 1470)`; tapping that current box opened the app's `Recently viewed` list
  (`Recently viewed`, `Stack Coins, Save More!`, `History` / `Cart` tabs, two-column product cards)
  ✅. The same right-edge dwell chip appeared at `x ≈ 0.97 · width` and counted
  `14s → 13s → 10s → 4s → 3s → 2s → 1s` before vanishing about 15.6 s after the tap ✅
  (`recently_viewed_issue_116_destination.png`, `…_destination_cleared.png`). There is **no `N/M`
  badge or round counter** on the row or destination ✅. After the chip cleared, the shortcut route
  returned to the coin page; the balance moved `107 → 108` and the row was gone from both the inline
  coin page and the populated `Coins boost` list ✅ (`recently_viewed_issue_116_row_gone.png`). The
  server then rotated `View Super discounts` into that slot ✅. Completion is land-and-dwell-then-
  return; the executor's decisive signal is the row disappearing from a complete populated list
  after a cleared chip. The manual observation also saw the `+1`, but the executor does not read the
  balance, so rotation remains a residual risk ❓. A foreground loss after `GO` is explicitly a
  failed attempt, never `row_gone` completion (#116 recovery guard).
  The earlier `[1020,1644]` dispatch is **best explained** by a stale roster box read before the
  reflow ⬜; the old run retained no post-tap PNG or foreground-package record, and its tap point
  matched the returned inline `GO`, so whether that tap hit the row and the app left anyway remains
  ❓. Today's capture proves what a settled tap does; it does not retroactively prove the old
  transition. The destination page is still `unknown` to the surface classifier and is accepted only
  after the chip appears within the arrival bound, not from the destination header alone. The
  catalogue poll bound is `20 s` (above the measured 15.6 s clear), never a fixed sleep. Provenance
  for the committed frames is in `tests/data/flows/recently_viewed_issue_116_observation.json`.
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
  whether a launcher launch is strictly required rather than a delayed URI is ❓. The executor now
  owns the measured path (#158): it accepts the sheet's explicit `Create shortcut` control, confirms
  the launcher's `Add to home screen`, reuses an item when the `Shortcut has existed` toast says one
  is already pinned, fires that item's own persisted intent
  (`SHORTCUT_LAUNCH_DEEP_LINK`, with `from=shortcut` / `shortcutId=coinsPromotion`), reloads the
  coin page once, and records completion only on a readable `+1` while the app is foreground. A
  launcher frame after the dispatch is a foreground loss, never completion. Cleanup runs
  `adb shell cmd shortcut clear-shortcuts com.alibaba.aliexpresshd`. Android exposes no per-pin adb
  delete, so this is package-wide: it also drops the app's ordinary long-press shortcuts (Search,
  Cart, Orders, Account), which the app republishes on its next launch. The operator-facing detail
  says so; it is a deliberate platform limitation, not a targeted delete. Sanitized #130 evidence is
  in `tests/data/flows/shortcut_visit_issue_130.json`.
- **Launcher items ✅ (descoped #27):** adding earns the task row and a widget visit rendered
  `Collect`/inline rows in session 2. The `Visit the page via coins shortcut` row is a separate
  numbered family: its observed credit followed a launcher launch, so #158 now creates, uses and
  removes that one item instead of leaving the row for the operator (#130). The generic shortcut and
  widget catalogue families remain without executors.

### 5b. Flows walked end to end

- **`Learn to save money with Coins Credits` ❓ (issue #159):** not offered in any of the coin-page
  or `Coins boost` samples taken on 2026-11-26 (§4e). The historical 2026-11-20 note that an
  explainer page with the same timer chip paid +1 retained no frame, so the destination, dwell
  chip, reward, badge/counter, return route and timing are all ❓. The catalogue row and
  `browse_wait` mapping remain; after the configured complete-night threshold it is reported
  `not_offered`/dormant instead of keeping every night open, and a sighting resumes it (#161). It
  is not removed to make a night complete.
- **`Browse surprise items` ✅ (2026-11-22):** two rounds of three distinct products (`N/2` row,
  `N/3` counter). Tapping a tile opens `AIProductDetailActivity` (`Item 1/n`, `Add to cart` /
  `Buy now` — **never tapped**); **BACK** increments the counter per **distinct product**, not per
  tap ([`01-recon-findings.md`](01-recon-findings.md) §7c). The counter is re-read until it stops
  changing within a bounded deadline, because a credit can arrive in more than one increment; a
  tile that leaves it unchanged is inert and the round fails `progress_unchanged` rather than being
  recorded. Scrolling for unseen products moves the header and pill off-screen; the
  executor recognises the same two-column card set it already read, not generic discount copy, and
  restores the top before reading the counter (#100). Product photographs are store-listing
  screenshots, so OCR can read a small `Add To Cart` inside a merchant's image (measured frame on
  2026-11-25). That caption is not page chrome or a card action: the guard pairs it with the card's
  image band, the executor skips that card and records `tile_skipped_dangerous`, and a caption
  outside every card's photo still aborts as `dangerous_control` (#139). At `3/3`, `Mission
  Completed` stays >10 s until tapped; `Next round` appears on non-final rounds at ~(597, 1240) ⬜.
  Final round paid +1
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
| Which step credits `Visit the page via coins shortcut`? ❓ | the observed +1 followed a home-screen shortcut launch, while a plain URI fired after creating the shortcut did not credit immediately; #158 now fires the shortcut's own persisted launcher intent and then reloads | bracket the balance and row after the launcher intent and again after the reload on a fresh day (#130, #158) |
| When is `Learn to save money with Coins Credits` offered? ❓ | it was absent from 15 full reads on 2026-11-26 while the catalogue still called it claimable; the destination, timer and reward are unverified (§4e) | sample another day/window and check whether the account completed it before the daily-state era; do not remove the catalogue row just to close a night (#159) |
