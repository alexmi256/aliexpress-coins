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
| **launcher shortcut** ✅ | the pinned deep shortcut `coinsPromotion` | same | its intent adds `from=shortcut&user_growth_type=shortcut&homescreen_src=coinD11&shortcutId=coinsPromotion` |
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

Rows are `label + reward + progress + GO` on one horizontal band. Observed:

| task | reward | progress | executor family |
|---|---|---|---|
| Take the coins quiz | +3~6 coins, +0~US$0.01 | none | quiz |
| Explore sponsored items | +1 | `1/6` (repeatable) | browse-and-wait |
| Browse surprise items | +1 | `0/2` (repeatable) | surprise items |
| Browse recently viewed items | +1 (per brief; not observed) | — | browse-and-wait |
| Check in to win shopping credits | (per brief; not observed) | — | browse-and-wait |
| Add a coins shortcut | (completed by the operator) | — | launcher item |
| Add & start using our widget | (completed by the operator) | — | launcher item |
| Turn on notifications | (per brief; not observed) | — | system dialog |
| Merge Boss / Play & Earn / Prize Land | +30 / +900 | — | **refused** (games) |

**"Earn more" is not the full task list.** Both times it was tapped it opened a `Coins boost`
page reading "There's nothing here yet, but come check back later!". The brief assumed it reveals
all tasks; it does not. The coins page's inline rows are the task surface.

## 5. Reward mechanics

* **Daily check-in** — a `Collect` control in the streak row. Tapping it: balance moves, the
  control disappears, no dialog. Claimed once per day; the streak shows `N days streak`.
* **Quiz** — one True/False question per visit. The result page prints **`Correct answer: X`** and
  `Your answer: Y`, then `You got +6`. A wrong answer still pays something.
* **Browse tasks** — open a collection page, wait, and the progress badge advances (`1/6`).
  The countdown was not observed directly; 15 s is the brief's number and remains ⬜.
* **Launcher items** — the operator completed both tasks simply by **adding** the shortcut and the
  widget. Nothing suggests a launch from the item is required, and the widget/shortcut visits may
  be what credits a *different* (repeatable) task. ❓ one more observation would settle it.

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
