# 05 — What the app actually does

Concrete knowledge about AliExpress `8.180.1` on the reference tablet, as observed. This is the
document the roadmap is built from: entry points, page variants, task surface, reward mechanics,
and what is still unknown.

Legend: ✅ observed · ⬜ inferred from an observation · ❓ unknown.

Measured 2026-11-19/20, updated 2026-11-22. The reviewed full-frame fixtures for the quiz,
surprise-items and notification flows live under `tests/data/flows/` (see its README); earlier
drafts referred to the research workspace, which is not what the tests replay.

---

## 1. Two coins exist, and they are not interchangeable

| thing | label in UI | where | what it does |
|---|---|---|---|
| coins | `Coins`, `worth C$0.0x`, `+6` | header, task rows | the balance; ~100 coins ≈ C$1 |
| coin credits | `Coins credits to redeem`, `C$0.00` | redeem card | a separate redeemable credit balance |

A quiz pays **+6 coins and + US$0.01 credit**. The check-in paid **+2 coins** (C$0.07 → C$0.09).
So a task's worth is two numbers, and the balance alone does not show credit progress.

## 2. Entry points

| route | how | settles on | notes |
|---|---|---|---|
| **shortcut deep link** ✅ | `adb shell am start -a android.intent.action.VIEW -d 'aliexpress://goto?url=https%3A%2F%2Fm.aliexpress.com%2Fp%2Fcoin-index%2Findex.html'` | `AdcActivity`, `adc_manifest=coinindex` | one command, no home feed, no account detour. **The route.** |
| **launcher shortcut** ✅ | the pinned deep shortcut `coinsPromotion` | same | its intent adds `from=shortcut&user_growth_type=shortcut&homescreen_src=coinD11&shortcutId=coinsPromotion`. Replaying that exact URI is indistinguishable in the resulting intent, and **works with the shortcut removed** — the item is a convenience, not a dependency |
| **widget** ✅ | `AECoin2x2NormalWidget`, tap "Check in now" | same, plus `from=widget&user_growth_scene=widget&aewidget_*` | the only visit that produced a **Collect** button and a task list |
| **Account page card** ⚠️ | Account tab → the promo card | varies | the card rotates: it was "Coins +10 / Collect" once, then "Daily rewards are ready! / Claim now" which opens a **coupon** page, not coins. Not implemented as a runner route. |
| advertised `/p/coin-index/` without the `aliexpress://goto?url=` wrapper ❌ | `am start -d https://www.aliexpress.com/p/coin-index/...` | blank web view | the wrapper is what routes into the app's container |

**Design consequence:** the shortcut deep link is the primary route, because it is the only one
that does not depend on a rotating promo slot. The Account-page card remains a fallback idea, not a
plan or an implemented route.

## 3. Page variants

The same `AdcActivity` renders different coin-page layouts depending on the entry:

* **plain / shortcut** — header (balance, "worth C$x"), redeem card, "Daily coins" streak row,
  mini-game tiles (Merge Boss / Play & Earn / Prize Land), product feed. **Task rows may be absent.**
* **widget** — same, plus an "Extra" streak column, a **Collect** button, and the task rows
  (quiz, sponsored `1/6`, surprise `0/2`).

Task rows are therefore **conditional**, not guaranteed. The executor must check, and treat
"absent" as a state (refresh / use the widget route / report) rather than an error.

## 4. The task surface

Tasks appear in two places: the **inline rows** on the coin page, and the **`Coins boost` page**
opened by `Earn more` in the *Daily coins* card. The boost page is the fuller list and is what the
brief meant by "click Earn more and wait for the popup".

**Correction to an earlier reading:** the boost page was twice observed in an *empty* state right
after a cold start — `Coins boost` over "There's nothing here yet, but come check back later!" — and
that was mis-read as "Earn more is not the task list". It **is** the task list; the empty page is a
load failure. With the app warm it renders the full list. Navigating to `Account` and back to the
coin page (or re-firing the shortcut) makes it populate.

Full list observed on one page (2026-11-20), in order:

| task | subtitle | reward | progress | executor family |
|---|---|---|---|---|
| Learn to save money with Coins Credits | Stack with coins, coupons and other discounts | +1 | — | browse-and-wait |
| Explore sponsored items | Browse, shop and save | +1 | `1/6` | browse-and-wait |
| Browse surprise items | Tap 3 items on this page to earn coins | +1 | `0/2` | surprise items |
| Place an order on this page | Each order grants you rewards | — | `0/3` | **refused** (order) |
| Add 5 items to your cart | — | — | — | **refused** (cart) |
| Check in to win Shopping credits | Don't forget to check in today! | — | — | browse-and-wait |
| Water to win a US $0.10 prize | Water your tree in Prize Land | — | — | **refused** (currency/game) |
| Share coins with friends | Win rewards together | — | `0/3` | **refused** (social) |
| Turn on notifications | Stay informed when rewards drop | +1 | — | system dialog |
| Add a coins shortcut | One tap away from your daily rewards | +1 | — | launcher item |
| Add & start using our widget | Add our widget to your homescreen to use | +1 | — | launcher item |
| Search for what you love | Earn by using keywords to explore items | +1 | `0/2` | **refused** (`unrecognised`; no executor yet, #24/#38) |
| Take the coins quiz | — | +3~6 coins, +0~US$0.01 | — | quiz |
| *coin page only:* Merge Boss / Play & Earn / Prize Land | — | +30 / +900 | — | **refused** (games) |

The quiz row appears **both** on the coin page's inline list and on the `Coins boost` sheet; the
game tiles in the row below it are coin-page only. The roster is **server-driven and changes
between loads**: at a second geometry `Browse surprise items` was replaced by `Search for what you
love` on the same account, minutes later. The catalogue must therefore match by pattern and treat
any missing task as normal.

Four of the twelve boost rows are refusals, all on the page the executor must open (the coin page
also renders the game tiles). That is the argument for enforcing the deny list at dispatch rather
than per-executor: the order, cart, water and share tasks are *rendered next to* the ones we want,
with identical `GO` buttons.

## 4b. The stuck state

The boost page has one failure mode that matters, and it is silent: the honest-looking empty page.

* **Detection** — `Coins boost` header **and** `There's nothing here yet` **and** zero task rows.
  The list is never legitimately empty: it always contains the declined tasks.
* **Cause** — firing the shortcut intent as the **cold start** of the app (force-stop → intent).
  4/4 trials empty. Starting the app normally first and *then* sending the intent: 2/2 populated.
  A warm app + intent: 3/3 populated.
* **Recovery** — never use the intent to launch a stopped app. Start `FirstActivity`, let it settle
  ~10 s, then fire the intent; `Earn more` then renders the full list.
* **Rule for the runner** — "no tasks" is a fault to reset, never a reason to end the run.

## 5. Reward mechanics

* **Daily check-in — there are two, and they are independent.** (measured 2026-11-20)
  1. the streak-row `Collect` on the coin page: balance `C$0.26` → `C$0.29`, a "You've got +2"
     toast, the control disappears, the streak advances;
  2. the boost-page row `Daily check-in` / "Finish today's check-in" (+1): still present *after*
     the streak claim, and tapping it paid +1.

  A run must claim both; neither is evidence that the other is done. The 2×2 widget's "Check in
  now" CTA only navigates to the coin page — it does not create collectability (`aewidget_status`
  is state, not a trigger).
* **Quiz** — **one question per visit**, and the answer set is not always two: True/False was
  observed on 2026-11-20, while the reviewed `states/quiz.png` frame is a four-option question
  ("Which statements about coins credits are correct?"). The result page prints
  **`Correct answer: X`** and `Your answer: Y`, then `You got +6`. A wrong answer still pays
  something. The quiz pays coins **and** coin credit, which is why its `Go back` can raise the
  `YOU CAN NOW REDEEM!` interstitial.
* **Browse tasks** — open a collection page and wait out a dwell requirement. The requirement is
  **observable**, so it does not need to be timed:

  a floating chip at the right edge (`x ≈ 0.97 · width`, mid-height) shows an orange clock with a
  **red `Ns` pill** while the timer runs, then a **green pill with a white check**. OCR reads the
  digits (`12s`, `1s`), so the executor polls for `\b\d+s\b` and proceeds when it disappears.
  Measured on one run: `12s` at t=1 s, `1s` at t=3 s, green check from t≈6 s. The timer starts
  during page load, so the first reading can be well under 15 s. The implementation waits for the
  chip itself (`find_timer_chip` + `wait_for_timer_chip`); 15 s is the catalogue bound for a chip
  that never finishes, not a claim that every task waits 15 s. A page with no chip returns `absent`
  immediately rather than sleeping.
  * **Explore sponsored items ✅** opens the **coin store** product list headed
    `Use coins for discounts`, with discounted price tiles (`Disc:C$…`, `Applied`) and the same
    right-edge chip. It is a browse destination, not an interstitial: the executor waits for its
    chip and then returns to the coin page (#83).
* **Launcher items** — the operator completed both tasks simply by **adding** the shortcut and the
  widget. Nothing suggests a launch from the item is required, and the widget/shortcut visits may
  be what credits a *different* (repeatable) task. ❓ one more observation would settle it.

## 5b. Flows walked end to end

### Browse surprise items ✅ (re-measured 2026-11-22)

Row: `Browse surprise items` / "Tap 3 items on this page to earn coins" / round badge `N/2` / `+1`.

The task is **two rounds of three distinct products per day**. The row badge counts rounds; the
page counter counts products in the current round.

1. `GO` opens a page headed `Browse surprise items` with the same subtitle, a segmented pill
   (`+1 | N /3 | three tick placeholders`) and a two-column product grid. Entry read `0/3` with
   three hollow placeholder icons.
2. Tapping a tile image opens a normal product page (`AIProductDetailActivity`, `Item 1/n` in the
   toolbar, `Add to cart` / `Buy now` at the bottom — **never tap those**).
3. **BACK returns to the grid and increments the counter** for that product. One visit is one
   product; a round needs three. The counter was already `1/3` in a frame taken ~1 s after BACK, so
   the update is immediate, not polled.
4. **The counter is per distinct product, not per tap.** Measured tap sequence on the round that
   started at `0/3` (see docs/01 §7c for the raw frames):

   | tap | product | counter after BACK |
   |---|---|---|
   | 1 | T9 Vintage Men's Electric Hair Clipper (left tile, row 1) | **1/3** |
   | 2 | 1pc Pressure Cooker Silicone Exhaust Valve (right tile, row 1) | **1/3** (no change) |
   | 3 | the same pressure-cooker tile, re-tapped with a 9 s dwell | **1/3** (still no change) |
   | 4 | Luxury Liquid Silicone Phone Case (fresh tile, row 2) | **2/3** |
   | 5 | Home 3-in-1 Portable Fan (fresh tile, row 2) | **3/3** |

   So a non-counting tile stays non-counting across repeat visits: the page opens the product but
   does not credit it. This is product-level distinctness, not "the first three taps count".
   ❓ *Whether the product set resets between rounds* is not settled: the task entered this session
   at `1/2` rounds, so finishing the round ended the task (2/2) and no round boundary with rounds
   remaining could be observed. The pressure-cooker tile's inertness is consistent with a product
   opened in the earlier round still being excluded, but its earlier-round history was not recorded.
   Until #63 settles it, the executor starts with the **per-round** interpretation (a product may
   count again in a later round) and falls back to whole-task exclusion after two fresh products in
   a row fail to move the counter; both paths are recorded in the round evidence.
5. At `3/3` all three tick icons fill, a **`Mission Completed`** overlay drops over the grid (three
   gold coins, no control), and the page stays on `3/3`. The overlay did **not** auto-dismiss within
   10 s; a tap anywhere on it dismissed it immediately. On a task with rounds remaining a
   **`Next round`** control appears at ~(597,1240) (⬜ from the 2026-11-21 session, not re-observed).
6. **Completion**: BACK returns to the boost list, the row is gone, and the balance moves +1 once
   (43 → 44 today). The final round paid +1; whether an intermediate round also pays was not
   observed this session. An earlier note recorded the balance rising during partial progress
   (13 → 15 on 2026-11-20), which suggests it may pay per round — ⬜, not settled.

The roster is server-driven and rotates immediately after this row completes: `Browse surprise
items` was replaced in place by `Search for what you love` in the same session.

### Learn to save money with Coins Credits ✅

`GO` opens an explainer page ("What are Coins Credits?", a worked checkout summary with
`Subtotal` / `Items discount` / `Saved`). It is a **dwell task**: the same timer chip appears at the
right edge and turns green when the requirement is met. Going back then paid **+1** (27 → 28) and
the row **disappeared from the roster**.

The task URL carries `taskId` / `taskInstanceId` query parameters, so per-task identity is
server-side and exposed in the intent — useful for logging, not required for driving.

### Search for what you love ⚠️

**Not dispatched in this build.** The row is not in `specs/tasks.yaml`: it has no timer chip and no
proven verification contract, so the default-deny policy reports it `unrecognised` until it gets
its own executor (#24/#38). The observations below are still the recon evidence that work needs.

`GO` opens a **search landing page** (a pre-filled query, `Search history`, "Discover more" and
category cards such as `Man Shoes` with "81k+ items"). Tapping a card opens search results. There is
**no timer chip** on either screen, and progress stayed `0/2` after one search, so the scoring rule
is not "wait on a page" — most likely one progress unit per search performed, which needs one more
session to confirm. ❓

**Navigation hazard:** from the results page the back stack is *results → landing → exit*. Two BACKs
take you out of the app to the home feed. Returning to the boost list after this task needs an
explicit re-entry (start the app, then the shortcut intent), not repeated BACK.

### Turn on notifications 🟡

Row: `Turn on notifications` / "Stay informed when rewards drop" / `+1`.

Tapping its `GO` credited **+1** (balance 15 → 16) with **no system permission dialog**, and
`adb shell cmd appops get com.alibaba.aliexpresshd POST_NOTIFICATION` still returned `ignore`
afterwards (2026-11-20).

So the reward is granted on the tap, not on the permission actually being held. A clean re-test
was attempted on 2026-11-22 but the row was already claimed for the app day (drawn check, inert),
and the task day is server-side. Two OS-side facts were measured while trying: with
`granted=false` and the `USER_SET` flag cleared, the app's own
`Settings → Notification settings → Push notifications → Allow phone notifications` toggle
**deep-links to the OS `AliExpress` notification page** instead of raising the runtime dialog, and
flipping the OS `All AliExpress notifications` switch there **grants the permission directly with
no dialog at all**. That grant was reverted immediately (`pm revoke` + `pm clear-permission-flags`)
and `Allow` was never tapped. The system dialog itself (`Allow AliExpress to send you
notifications?`, close `Don't allow`) is the #11 corpus observation.

## 6. Interstitials seen

| name | trigger | exit |
|---|---|---|
| `Welcome gifts` (cold launch) | sometimes on a cold start, not always | drawn ✕ at ≈(600, 1652), no OCR text |
| `YOU CAN NOW REDEEM!` | after a task pays credit (quiz → Go back) | no OCR-visible close; **BACK** exits it, a second BACK exits the app |
| `Coins boost` empty state | tapping "Earn more" | back arrow in the page header |
| `Mission Completed` | third distinct product in a surprise-items round | **tap anywhere on the overlay**; it did not auto-dismiss inside 10 s |

The post-task interstitial is the dangerous one: it looks like a page, not a modal, and BACK is
the only known exit — which is also the key that leaves the app.

## 7. A run, as the app expects it

```
open shortcut deep link
  → wait for the coin page (header "Coins" + "Daily coins")
  → if task rows are absent: refresh once, then decide (widget route / report)
  → claim the streak "Collect" if present            [state: done for today]
  → for each task row: policy → executor → verify progress/balance
  → dismiss the "YOU CAN NOW REDEEM!" interstitial if it appears
  → read balance + credits, report
```

## 8. Still unknown

| question | why it matters | how to settle it |
|---|---|---|
| Is a widget/shortcut *visit* needed for those two tasks, or is creation enough? | decides whether the executor must fake an entry point | wait for the task to reappear, then visit vs not |
| Why were task rows absent on some loads? | the executor's "nothing available" branch | observe across a day boundary; check whether rows return after the daily reset |
| The exact dwell per browse task | the executor waits for the chip, so a wrong fixed 15 s would be a hidden assumption | the chip is authoritative; time one sponsored-items task only to confirm the 15 s catalogue bound is generous |
| Does the surprise-items round counter reset its product set between rounds? | the round-2 rule in #26 | #63: one day starting at `0/2`, with the same product opened in both rounds |
| The quiz result and the notification denial frames | #25/#28 fixtures | one day when the quiz and the notification row are still unclaimed |
| Whether the quiz question set is finite | question bank size | capture over several days |
| **When does the daily reset actually happen — 00:00 or ~03:00 local?** | it decides whether a 03:30 schedule is after the roll or before it; a run before the roll finds every task already claimed and reports a successful, empty night | ❓ the operator recalls some rewards resetting around 03:00 EST, unconfirmed for coins. #63 settles it with two `--dry-run` passes at ~02:50 and ~03:10 local (no claims) plus the nightly run's `roster.jsonl` at 03:32. A pre-reset dry run is expected to exit non-zero (`surface_incomplete` / `surface_rows_incomplete`), not `planned`; the roster log is the measurement. |
