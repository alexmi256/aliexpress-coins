# 01 — Recon findings

What was measured on the reference device, how, and what each result changed in the design.
Everything below was observed on **2026-11-19** against AliExpress `8.180.1` on the T3_US
tablet (Android 15, userdebug).

Legend: ✅ verified by observation · ⬜ inferred, not verified · ❓ unknown

---

## 1. Device and access ✅

```
adb connect 192.168.8.65:5555     -> connected
adb shell id                      -> uid=0(root) gid=0(root) groups=... context=u:r:su:s0
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

**There is no `su` binary**; `adbd` itself runs as root, so root-only steps (launcher
database, widget state) are plain `adb shell` commands. Do not write `su -c ...` anywhere: it
fails on this device.

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
retry. Kept in the code as a fallback route, disabled in practice by the `is_coins_page`
assertion. Do not build on it.

### Launcher shortcut and widget — ❓ not present, not yet reversed

The brief said a shortcut and a widget were placed on the home screen. They are **not there
now**:

* `uiautomator dump` of the workspace shows 13 app icons, the Google search widget and the
  clock widget — no AliExpress shortcut, no coins widget;
* the launcher database agrees (`/data/data/com.android.launcher3/databases/launcher_6_by_5.db`):
  one `AliExpress` entry (`com.aliexpress.app.FirstActivity`) and two `itemType=4` widgets
  (Google search, Digital clock).

What *does* exist is the app's ability to create them:

```
split_widget.apk  -> res/layout/widget_coins_{init,login_signed,login_unsign,…}.xml
                  -> com.aliexpress.module.widget.widget.AECoin2x2NormalWidget (provider)
manifest          -> com.android.launcher.permission.INSTALL_SHORTCUT (granted)
```

So the widget is real, is 2×2, and renders the sign-in state (`signed` / `unsign`) itself.
Two paths remain open, both tracked as issues: reverse the widget/shortcut intent (what
`AdcActivity` URL it fires) and/or let the app install the widget and read the widget state.

The launcher database is readable **as the collector's own user** via adb (adbd is root), which
makes "is the widget installed" a cheap assertion:

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

## 7. Open items

| item | state | next step |
|---|---|---|
| coins page `wh_pid` / canonical URL | ❓ | read `dumpsys activity` right after the UI route reaches it, or capture the navigation intent |
| shortcut intent | ❓ | let the app create the shortcut, then diff the launcher DB and `dumpsys shortcut` |
| widget click intent | ❓ | add the widget, tap it, read the resulting `AdcActivity` intent |
| quiz flow | ❓ | open the quiz, capture the question/answer layout and the wrong-answer interstitial |
| surprise-items flow | ❓ | capture the page, the item tiles, and the round popups |
| notification task | ❓ | capture the system permission dialog and the post-grant state |
| API feasibility | ❓ | mitmproxy capture of a task run: are the coin awards plain HTTP the UI merely reflects? |
