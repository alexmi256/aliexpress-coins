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
| Check in to win Shopping credits | Don't forget to check in today! | — | — | allowed: `browse_wait` (`browse.py`) |
| Browse surprise items | Tap 3 items on this page to earn coins | +1 | `0/2` | allowed: `item_tap` (`surprise.py`) |
| Turn on notifications | Stay informed when rewards drop | +1 | — | allowed: `notification` (`notifications.py`) |
| Add a coins shortcut | One tap away from your daily rewards | +1 | — | catalogued `shortcut`; descoped (#27, skipped `no executor`) |
| Add & start using our widget | Add our widget to your homescreen to use | +1 | — | catalogued `widget`; descoped (#27, skipped `no executor`) |
| Search for what you love | Earn by using keywords to explore items | +1 | `0/2` | allowed: `browse` (`search.py`) |
| View Super discounts | Check out this page for 15 seconds | +1 | — | allowed: `super_discounts` (`browse_wait`, `browse.py`) |
| Place an order on this page | Each order grants you rewards | — | `0/3` | **refused** (`spending_order`) |
| Add 5 items to your cart | — | — | — | **refused** (`spending_cart`) |
| Water to win a US $0.10 prize | Water your tree in Prize Land | — | — | **refused** (`currency_water`) |
| Share coins with friends | Win rewards together | — | `0/3` | **refused** (`social`) |
| *coin page only:* Merge Boss / Play & Earn / Prize Land | — | +30 / +900 | — | **refused** (games) |

Four explicit deny rows sit beside allowed rows with identical `GO` buttons — enforcing the deny
list at dispatch is mandatory. `Search for what you love` is now an allowed browse row with its own
category-tap executor (#108).

### 4b. The stuck state (`empty_boost`) ✅

- **Detection:** `Coins boost` header + `"There's nothing here yet"` + zero task rows (never a
  legitimate empty day because refused tasks are always listed).
- **Cause:** firing the shortcut intent as a **cold start** (`force-stop` → intent: 4/4 empty;
  `FirstActivity` + ~10 s settle → intent: 2/2 populated; warm app + intent: 3/3 populated).
- **Runner rule:** warm-start first; on `empty_boost`, re-enter via the warm shortcut route.

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
- **Launcher items ✅ (descoped #27):** adding earns the task row and a widget visit rendered
  `Collect`/inline rows in session 2; automation is descoped because the shortcut URI needs no
  launcher item.

### 5b. Flows walked end to end

- **`Learn to save money with Coins Credits` ✅:** explainer page with the same timer chip; paid +1
  and removed the row.
- **`Browse surprise items` ✅ (2026-11-22):** two rounds of three distinct products (`N/2` row,
  `N/3` counter). Tapping a tile opens `AIProductDetailActivity` (`Item 1/n`, `Add to cart` /
  `Buy now` — **never tapped**); **BACK** increments the counter per **distinct product**, not per
  tap ([`01-recon-findings.md`](01-recon-findings.md) §7c). The counter is re-read after a short
  dwell; a tile that leaves it unchanged is inert and the round fails `progress_unchanged` rather
  than being recorded. Scrolling for unseen products moves the header and pill off-screen; the
  executor recognises the same two-column card set it already read, not generic discount copy, and
  restores the top before reading the counter (#100). At `3/3`, `Mission Completed` stays >10 s
  until tapped; `Next round` appears on non-final rounds at ~(597, 1240) ⬜. Final round paid +1
  (43 → 44) and removed the row (intermediate-round payout ⬜). The counter-proven round is recorded
  immediately; when the returned row badge (`N/2`) is readable it is re-read after a short dwell
  and a non-advancing badge fails `progress_unchanged`, but an absent row leaves the counter as the
  evidence. Until
  #63 settles cross-round reset ❓, `surprise.py` tries per-round distinctness first and falls back
  once to whole-task exclusion.
- **`Turn on notifications` 🟡:** paid +1 on 2026-11-20 with no dialog and `POST_NOTIFICATION: ignore`
  unchanged. App settings' toggle deep-links to OS settings (no runtime dialog). The executor
  handles all four outcomes: immediate credit, app pre-prompt, runtime dialog (`Allow` / `Don't allow`),
  and OS settings redirect (`routed_to_settings`, stops without toggling).
- **`Search for what you love` ✅ (2026-11-24 01:48–01:53 EST, recon):** two rounds, each paid +1
  (balance `67 → 68 → 69`). `GO` opens a search landing with a search field, an account-specific
  `Search history` section and a server-rendered `Discover more` grid of category cards. Landing on
  the page alone did **not** advance the badge; tapping a history entry (`Smartphone`) only filled
  the search field and also did not advance it. Tapping a category card under `Discover more`
  (`Woman Clothing`, then `Sneakers`) advanced the row `0/2 → 1/2`; after the second category the
  row was gone from the populated boost list (the app removes it at `2/2`). No timer chip and no
  product visit are involved; the executor taps one untried category per round and verifies the
  badge after returning. The landing is now classified as `PAGE` / `search_landing`; two BACKs
  still exit to the home feed (docs/07 §1), so the measured shortcut route is used for every return.
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
| **When does the daily reset happen — 00:00 or ~03:00 local?** ❓ | decides whether `03:30` local is after the server reset; running before the roll yields an empty night | #63: two `--dry-run` passes at ~02:50 and ~03:10 local plus nightly `roster.jsonl` at 03:32 (a pre-reset dry run exits non-zero; `roster.jsonl` is the measurement) |
