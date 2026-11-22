# 01 — Recon findings

What was measured on the reference device, how, and what each result changed in the design.
Everything below was observed on **2026-11-19** against AliExpress `8.180.1` on the T3_US
tablet (Android 15, userdebug).

Legend: ✅ verified by observation · ⬜ inferred, not verified · ❓ unknown

---

## 1. Device and access ✅

```
adb connect 192.168.8.65:5555     -> connected
adb shell id                      -> uid=2000(shell)          (default shell)
adb root; adb shell id            -> uid=0(root)              (verified 2026-11-22)
adb shell getprop ro.adb.secure   -> 0
adb shell getprop ro.debuggable   -> 1
adb shell which su                -> "su: inaccessible or not found"
adb shell wm size                 -> Physical size: 2000x1200   (density 240)
screencap                         -> 1200x2000 PNG (portrait)
```

**`ro.adb.secure=0`**: adb key authorisation is disabled on this `userdebug` build, so a fresh
container can connect without an authorised key. This removes the usual "works on my machine,
`unauthorized` in the container" failure — but it is a property of this build, not of the
project, and `doctor` reports it rather than assuming it.

**There is no `su` binary, but `adb root` works.** A plain `adb shell` is `uid=2000(shell)`; on this
`userdebug` build `adb root` restarts `adbd` as root (verified 2026-11-22: the launcher database is
readable and `am start` can launch a non-exported activity). Root-only steps are therefore plain
`adb shell` commands *after* `adb root`, not `su -c ...`. Do not write `su` anywhere.

The user's shell is shared with a human, so a run must be safe to point at a live session —
that is why `--dry-run` suppresses taps at the `Controller` level rather than at the CLI.

---

## 2. The app surface

```
pm list packages            -> com.alibaba.aliexpresshd (8.180.1, versionCode 80005998)
launcher activity           -> com.aliexpress.app.FirstActivity
current focus after launch  -> com.alibaba.aliexpresshd/.home.ui.MainActivity
coins page activity         -> com.aliexpress.adc.ui.impl.AdcActivity
```

`AdcActivity` is the app's **hybrid container**: it is handed a `dat=https://www.aliexpress.com/ssr/…`
URL and renders the page itself. A task list page observed in the wild:

```
Intent { dat=https://www.aliexpress.com/ssr/300000512/BundleDeals2?…&wh_pid=300000512/BundleDeals2
         &wh_ttid=adc&adc_strategy=snapshot cmp=com.alibaba.aliexpresshd/…AdcActivity }
```

So every coin page is identified by a `wh_pid`, and **the coins page is one of these**.

---

## 3. Perception: screenshots yes, accessibility tree no ✅

This is the finding that shapes the whole project.

| page | `uiautomator dump` | screenshot + OCR |
|---|---|---|
| Account page (native) | 214 nodes, every label present with bounds | works |
| Home feed (hybrid) | ~211 nodes, product titles present | works |
| **Coins page (hybrid)** | **6.4 KB, effectively empty** | **40 text boxes, all labels readable** |
| "Welcome gifts" modal | **absent entirely** | present, with a close control |

Consequences, all of them load-bearing:

1. **OCR is the primary perception channel**, not a fallback. RapidOCR (PP-OCRv6 small,
   onnxruntime, CPU) reads the coins page in **1.6 s** at ~0.99 confidence.
2. **Popups are invisible to the accessibility tree** and are drawn inside the app's own
   surface (only one window exists for the whole activity). A popup detector that consults
   `uiautomator` will never see them. This is why `PopupDismisser` works on screenshots.
3. Native pages still expose useful nodes (the bottom tab bar's `Account` label, for instance)
   so the accessibility tree is used opportunistically — never required.

### What OCR actually returned on the coins page ✅

```
  105,218  Coins                      (header)
  166,357  3                          (balance)
   36,451  worth C$0.04
   58,591  Coins credits to redeem >
  943,640  Redeem
  917,948  Earn more >
   52,938  Daily coins
  180,1269 Take the coins quiz         982,1273 GO
  180,1440 Explore sponsored items     981,1440 GO     1080,1432 1/6
  180,1608 Browse surprise items       980,1612 GO     1080,1601 0/2
```

Two design details fall out of this table:

* the action button (`GO`) is the **rightmost action label on the task's row** — tasks are
  matched by row, never by absolute coordinates, so a new task at the top does not shift
  every subsequent tap;
* the balance is the only bare number in the **top 25 %** of the screen, which is how
  `read_balance` avoids mistaking a `+3~6` reward badge for it.

---

## 4. Popups ✅

Cold launch produces a full-screen **"Welcome gifts"** modal over a dimmed home feed. Its
close control is a drawn circular ✕ at roughly **(600, 1652)** on this screen. OCR reports no
text for it, which is why dismissal is layered:

1. known dismiss labels (`X`, `×`, `Close`, `Skip`, `No thanks`, `Not now`, `Got it`) in a
   **small** box (a 400 px-wide "Cancel order" must never match);
2. template matching for the glyph, threshold 0.88 (`TM_CCOEFF_NORMED`, contrast-invariant so
   the same glyph over a dark scrim still matches);
3. a configured coordinate fallback, used **only** when a promo headline is present;
4. otherwise: report `popup_unclosable` and fail. Tapping blind is how a collector buys things.

Measured: one tap at the ✕ closed the modal and the feed became interactive ✅. Popups chain —
after the modal there is usually a second, in-page promo ("Bundle deals" with its own ×).

---

## 5. Routes to the coins page

### UI route — ✅ works

`Account` tab → `Coins` card (`Collect` / `Get more coins`). Two taps from the cold-start
home feed, plus popup handling on both sides. This is the default route (`flows/coins.py`).

### Deep link — ❌ does not reach the native page

The app advertises the link in its intent filters:

```
com.aliexpress.module.navigation.HttpDispatcherActivity
  Action: android.intent.action.VIEW, Schemes: http/https,
  Authority: .aliexpress.com (wildcard),
  Paths: /campaign/, /gcp/, /p/wallet, /ssr/, /p/coin-index/
```

Firing it:

```
adb shell am start -a android.intent.action.VIEW -d "https://www.aliexpress.com/p/coin-index/index.html"
-> focus: com.alibaba.aliexpresshd/.adc.ui.impl.AdcActivity
-> screenshot: blank white WebView showing "aliexpress.com/p/coin-index/index.html?..."
```

It lands in the container but as a **web fallback** that never renders — no coins, no error, no
retry.

**Correction (2026-11-20):** the bare `https://` URL is the wrong shape. The app's own route is
the `aliexpress://goto?url=<that URL, encoded>` wrapper, which `DispatcherActivity` resolves into
the native `AdcActivity` coin page. That wrapper is now the primary route — see §7 and
[`05-app-knowledge.md`](05-app-knowledge.md) §2. The bare-URL form stays documented because it is
the mistake to avoid repeating.

### Launcher shortcut and widget — ✅ reversed on 2026-11-20

At first inspection neither item existed (the workspace held one AliExpress app icon and two
unrelated widgets). The operator has since installed both by completing the app's own tasks, and
their intents are now captured — the shortcut is a pinned **deep shortcut** (`coinsPromotion`
firing `aliexpress://goto?url=…/p/coin-index/…`), and the widget is
`AECoin2x2NormalWidget` firing the same page with a `from=widget` parameter set. Both are
replayable with `am start`.

Full detail, including the exact URIs and what each visit renders, is in
[`05-app-knowledge.md`](05-app-knowledge.md) §2–§3. The app-side evidence that made this
possible:

```
split_widget.apk  -> res/layout/widget_coins_{init,login_signed,login_unsign,…}.xml
                  -> com.aliexpress.module.widget.widget.AECoin2x2NormalWidget (provider)
manifest          -> com.android.launcher.permission.INSTALL_SHORTCUT (granted)
```

So the widget is real, is 2×2, and renders the sign-in state (`signed` / `unsign`) itself.
Two paths remain open, both tracked as issues: reverse the widget/shortcut intent (what
`AdcActivity` URL it fires) and/or let the app install the widget and read the widget state.

The launcher database is readable via adb after `adb root` (the default shell is `uid=2000`),
which makes "is the widget installed" a cheap assertion:

```bash
adb shell cat /data/data/com.android.launcher3/databases/launcher_6_by_5.db
```

---

## 6. Navigation is hostile by design — a worked failure ✅

A blind three-tap sequence (`tap 600,1652` → `tap 1066,1855` → `tap 1030,1220`) intended as
"close popup, Account, Coins" landed on a **product page** (`AIProductDetailActivity`) with a
full referral-laden `dat=` URL, because the modal had already closed and the first tap hit a
product tile.

Nothing was bought, but the lesson is the design rule: **every tap is preceded by a
screenshot and a label assertion**, and coordinates only exist inside a matched element. The
project never issues a raw coordinate tap outside a popup fallback.

---

## 7. Session 2 — 2026-11-20

A second session with the launcher items installed. It answered the route question, captured the
check-in and the quiz end to end, and disproved one assumption in the original brief.

| finding | result |
|---|---|
| shortcut | pinned deep shortcut `coinsPromotion` → `aliexpress://goto?url=…/p/coin-index/index.html…` ✅ |
| widget | `AECoin2x2NormalWidget` → same page with `from=widget` + `aewidget_*` parameters ✅ |
| replay from adb | works: one `am start`, lands on `AdcActivity` with `adc_manifest=coinindex` ✅ |
| Account-page route | unreliable — the promo card rotates between a coins card and a **coupon** card ⚠️ |
| daily check-in | `Collect` in the streak row; balance +2 coins; no dialog ✅ |
| quiz | one True/False question; result page prints **`Correct answer: X`**; `+6` on a correct answer ✅ |
| "Earn more" | opens a **`Coins boost`** page, empty every time — it is *not* the full task list ❌ |
| task rows | **conditional**: absent on some page loads, present after a widget visit ⚠️ |
| post-task interstitial | `YOU CAN NOW REDEEM!` — no OCR-visible close; BACK exits it (a second BACK exits the app) ⚠️ |

The app model those findings produce — entry points, page variants, task surface, reward
mechanics — is written up in [`05-app-knowledge.md`](05-app-knowledge.md).

## 7b. Boost-page row geometry — 2026-11-21

Captured from a 1200×2000 `screencap` of the populated `Coins boost` list. The committed
fixture `tests/data/boost.png` is cropped to the header and task rows (1200×1520) and contains no
balance or account data. These are the numbers the layout tolerance is derived from ✅.

| measurement | value |
|---|---|
| median OCR box height (fixture) | 48 px |
| task action (`GO`) centres, y | 357, 688.5, 1046, 1404 |
| task row pitch | ~340 px (331.5–358 px between action centres) |
| largest within-row box-centre gap | 86.5 px |
| smallest gap between adjacent task rows | ~190 px |
| inline coins title → `GO` offset | ~31 px (see §3) |

The within-row spacing is less than half the gap to the next task, so a row tolerance derived
from the median box height keeps a row together without merging its neighbour. A cluster that
still contains more than one action label is split at the action boundary; a cluster without an
action is split when it spans more than one text line.

## 7c. Session 3 — 2026-11-22 (quiz, surprise items, notifications)

The tablet was re-addressed to `192.168.8.65:5555` after its battery died (the session of
2026-11-20 used the same address; `.220` appears in older notes and is stale). App `8.180.1`,
1200×2000 portrait.

**Access.** A plain `adb shell` is `uid=2000(shell)`, which is what the first attempt used; my
initial note here called that "no root shell" and that was wrong. `adb root` restarts `adbd` as
root and restores the root-only steps (verified: the launcher database lists, and
`am start -n com.alibaba.aliexpresshd/.push.PushSettingActivity` succeeds — the earlier
`SecurityException` was the un-rooted shell, not a missing capability). `pm clear-permission-flags`
and `cmd appops` work for either user.

**Task state at entry (00:12 EST).** The boost list was populated and carried, among others,
`Take the coins quiz`, `Check in to win Shopping credits`, `Add & start using our widget` and
`Turn on notifications` — **all four already marked with the drawn check control**, and
`Browse surprise items` at `1/2` rounds. The checkmark survived a `force-stop` + normal start +
warm shortcut intent, so it is server-side, not a cached page. Changing the device timezone to
`Pacific/Kiritimati` (a different local date) did not clear it either; the task day is decided
server-side. At 00:2x EST the app day was still the previous UTC-8 date, which is why the daily
tasks had not reset just after local midnight.

**Surprise items — counter rule.** Entered at `0/3` in the page counter (row badge `1/2`). Then,
deliberately, one visit per tap with BACK between each:

| # | product tapped | counter after BACK |
|---|---|---|
| 1 | T9 Vintage Men's Electric Hair Clipper (row 1, left) | `1/3` |
| 2 | 1pc Pressure Cooker Silicone Exhaust Valve (row 1, right) | `1/3` — no change |
| 3 | same pressure-cooker tile, re-tapped, 9 s dwell on the product page | `1/3` — still no change |
| 4 | Luxury Liquid Silicone Phone Case (row 2, left) | `2/3` |
| 5 | Home 3-in-1 Portable Fan (row 2, right) | `3/3` |

So the counter counts **distinct products**, not taps: a tile that does not credit the first time
never credits, however long the product page is left open. The third distinct product produced a
`Mission Completed` overlay (19:17:12 device frame); it stayed for >10 s and a tap dismissed it.
Because the task was on its **final** round (`1/2` at entry → `2/2` after this round), no
`Next round` control was offered; the `3/3` state is three ticks and the grid. BACK then returned
to the boost list, the row was **gone** (replaced in place by `Search for what you love`), and the
balance went `43 → 44` (+1). The cross-round half of the rule — whether a product opened in round
1 counts again in round 2 — could not be observed, because no round boundary with rounds remaining
existed this session.

**Quiz and notifications.** Both rows were already claimed for the app day at entry. Tapping the
quiz row (title or its completed control) is inert. The notification row's OS permission was still
`POST_NOTIFICATION: ignore` (`granted=false`), which confirms the row pays its +1 without the
permission being held. From the app's own `Settings → Notification settings → Push notifications`,
the `Allow phone notifications` toggle does **not** raise the runtime dialog: it deep-links to the
OS `AliExpress` notification page. Starting the app's `.push.PushSettingActivity` directly (as
root, after `adb root`) gives the same deep-link, which rules out "the row's activity would have
prompted". There, flipping the OS switch grants the permission directly,
with no `Allow AliExpress to send you notifications?` dialog at all — that path was reverted
(`pm revoke` + `pm clear-permission-flags … user-set`) within a minute and `Allow` was never
tapped. The runtime dialog and its `Don't allow` close are therefore recorded in
`specs/flows.yaml` from the #11 corpus, not re-observed here; the post-denial screen is the one
notification state with no frame.

The frames for the states observed above are committed under `tests/data/flows/` (see its README);
the three states that could not be captured on this app day are listed there with the reason, not
left implicit. The quiz result, the redeem interstitial and the post-denial screen need a session
that starts before the quiz/notification rows are claimed. The state machine itself is
`specs/flows.yaml`.

## 8. Open items

| item | state | next step |
|---|---|---|
| coins page canonical URL | ✅ answered | `aliexpress://goto?url=…/p/coin-index/index.html` (see §7 / doc 05) |
| shortcut intent | ✅ answered | deep shortcut `coinsPromotion`, intent captured |
| widget click intent | ✅ answered | `from=widget` + `aewidget_*` parameter set captured |
| quiz flow | 🟡 labels answered, frames partial | one question per visit, answer set 2 or 4; result frame still needed |
| why task rows are sometimes absent | ❓ | observe across a day boundary; check whether they return after the daily reset |
| surprise-items flow | 🟡 answered within a round (§7c) | counter is per distinct product; the cross-round reset is #63 |
| surprise-items round rule across rounds | ❓ #63 | start a day at `0/2` and open the same product in both rounds |
| notification task | 🟡 OS routes answered, denial frame missing | row pays +1 without the permission; capture `Don't allow` and the screen after it |
| `YOU CAN NOW REDEEM!` exit | ❓ | still one-shot: BACK is the only known exit; capture when a quiz pays credit |
| API feasibility | ❓ | mitmproxy capture of a task run: are the coin awards plain HTTP the UI merely reflects? |
