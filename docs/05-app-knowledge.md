# 05 — What the app actually does

Concrete knowledge about AliExpress `8.180.1` on the reference tablet, as observed. This is the
document the roadmap is built from: entry points, page variants, task surface, reward mechanics,
and what is still unknown.

Legend: ✅ observed · ⬜ inferred from an observation · ❓ unknown.

Measured 2026-11-19/20. Frames and OCR sidecars live in the research workspace
(`aliexpress-coins-spike/tools/notes/`), not in this repository.

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
| **Account page card** ⚠️ | Account tab → the promo card | varies | the card rotates: it was "Coins +10 / Collect" once, then "Daily rewards are ready! / Claim now" which opens a **coupon** page, not coins |
| advertised `/p/coin-index/` without the `aliexpress://goto?url=` wrapper ❌ | `am start -d https://www.aliexpress.com/p/coin-index/...` | blank web view | the wrapper is what routes into the app's container |

**Design consequence:** the shortcut deep link is the primary route, because it is the only one
that does not depend on a rotating promo slot. The Account-page card is a fallback, not the plan.

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
| Search for what you love | Earn by using keywords to explore items | +1 | `0/2` | browse-and-wait (search page) |
| *coin page only:* Take the coins quiz | — | +3~6 coins, +0~US$0.01 | — | quiz |
| *coin page only:* Merge Boss / Play & Earn / Prize Land | — | +30 / +900 | — | **refused** (games) |

The roster is **server-driven and changes between loads**: at a second geometry `Browse surprise
items` was replaced by `Search for what you love` on the same account, minutes later. The catalogue
must therefore match by pattern and treat any missing task as normal.

Five of the twelve rows are refusals, all on the page the executor must open. That is the argument
for enforcing the deny list at dispatch rather than per-executor: the order, cart, water and share
tasks are *rendered next to* the ones we want, with identical `GO` buttons.

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
* **Quiz** — one True/False question per visit. The result page prints **`Correct answer: X`** and
  `Your answer: Y`, then `You got +6`. A wrong answer still pays something.
* **Browse tasks** — open a collection page and wait out a dwell requirement. The requirement is
  **observable**, so it does not need to be timed:

  a floating chip at the right edge (`x ≈ 0.97 · width`, mid-height) shows an orange clock with a
  **red `Ns` pill** while the timer runs, then a **green pill with a white check**. OCR reads the
  digits (`12s`, `1s`), so the executor polls for `\b\d+s\b` and proceeds when it disappears.
  Measured on one run: `12s` at t=1 s, `1s` at t=3 s, green check from t≈6 s. The timer starts
  during page load, so the first reading can be well under 15 s.
* **Launcher items** — the operator completed both tasks simply by **adding** the shortcut and the
  widget. Nothing suggests a launch from the item is required, and the widget/shortcut visits may
  be what credits a *different* (repeatable) task. ❓ one more observation would settle it.

## 5b. Flows walked end to end

### Browse surprise items ✅

Row: `Browse surprise items` / "Tap 3 items on this page to earn coins" / progress `0/2` / `+1`.

1. `GO` opens a page headed `Browse surprise items` with the same subtitle, a counter (`0/3`) and a
   product grid.
2. Tapping a tile opens a normal product page (`AIProductDetailActivity`).
3. **BACK returns to the grid and increments the counter** — one tap does not complete the task.
4. After three distinct items the grid is replaced by a **`Next round`** button (~597,1240).
5. `Next round` resets the counter to `0/3` and shows a fresh grid. Two rounds match the `0/2`
   progress badge.
6. **Re-opening an already-opened item does not count.** In round 2, re-tapping the round-1 tiles
   left the counter at `0/3`; new items moved it to `2/3`. Distinctness is tracked per product, not
   per tile position.

Balance moved 13 → 15 during the exercise (partial completion), so the reward is not all-or-nothing
per round.

### Learn to save money with Coins Credits ✅

`GO` opens an explainer page ("What are Coins Credits?", a worked checkout summary with
`Subtotal` / `Items discount` / `Saved`). It is a **dwell task**: the same timer chip appears at the
right edge and turns green when the requirement is met. Going back then paid **+1** (27 → 28) and
the row **disappeared from the roster**.

The task URL carries `taskId` / `taskInstanceId` query parameters, so per-task identity is
server-side and exposed in the intent — useful for logging, not required for driving.

### Search for what you love ⚠️

`GO` opens a **search landing page** (a pre-filled query, `Search history`, "Discover more" and
category cards such as `Man Shoes` with "81k+ items"). Tapping a card opens search results. There is
**no timer chip** on either screen, and progress stayed `0/2` after one search, so the scoring rule
is not "wait on a page" — most likely one progress unit per search performed, which needs one more
session to confirm.

**Navigation hazard:** from the results page the back stack is *results → landing → exit*. Two BACKs
take you out of the app to the home feed. Returning to the boost list after this task needs an
explicit re-entry (start the app, then the shortcut intent), not repeated BACK.

### Turn on notifications 🟡

Row: `Turn on notifications` / "Stay informed when rewards drop" / `+1`.

Tapping its `GO` credited **+1** (balance 15 → 16) with **no system permission dialog**, and
`adb shell cmd appops get com.alibaba.aliexpresshd POST_NOTIFICATION` still returned `ignore`
afterwards.

So the reward appears to be granted on the tap, not on the permission actually being held. That is
either an app bug or the prompt is shown once and remembered. Worth one clean re-test (fresh state,
notifications disabled) before the executor relies on it — tracked in #28.

## 6. Interstitials seen

| name | trigger | exit |
|---|---|---|
| `Welcome gifts` (cold launch) | sometimes on a cold start, not always | drawn ✕ at ≈(600, 1652), no OCR text |
| `YOU CAN NOW REDEEM!` | after a task pays credit (quiz → Go back) | no OCR-visible close; **BACK** exits it, a second BACK exits the app |
| `Coins boost` empty state | tapping "Earn more" | back arrow in the page header |

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
| The 15 s countdown on browse tasks | per-task wait time | time one sponsored-items task from GO to progress change |
| Surprise-items flow (3 taps, rounds) | executor design | one session with the task available |
| Notifications task (system dialog) | executor design | one session with the task available |
| Whether the quiz question set is finite | question bank size | capture over several days |
