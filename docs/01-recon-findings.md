# 01 — Recon findings

Verified device evidence from the reference tablet (`T3_US`, Android 15, AliExpress `8.180.1`,
`1200×2000` portrait @ 240 dpi), measured **2026-11-19** through **2026-11-24**, and the design
constraints each measurement imposed.

Legend: ✅ verified by observation · ⬜ inferred, not verified · ❓ unknown.

## 1. Device and access ✅

```text
adb connect 192.168.8.65:5555     -> connected
adb shell id                      -> uid=2000(shell)          (default shell; runtime uses this)
adb root; adb shell id            -> uid=0(root)              (verified 2026-11-22; recon only)
adb shell getprop ro.adb.secure   -> 0                        (userdebug: no adb key prompt)
adb shell which su                -> "su: inaccessible or not found"
adb shell wm size                 -> Physical size: 2000x1200 (density 240)
screencap                         -> 1200x2000 PNG (portrait)
```

The collector runtime uses plain `adb shell` (`uid=2000`) and never invokes `su` or `adb root`
(root was only used during recon to read `/data/data/com.android.launcher3/databases/launcher_6_by_5.db`
and probe `PushSettingActivity`). `ro.adb.secure=0` is a property of this `userdebug` build;
`doctor` probes reachability rather than assuming it.

## 2. App surface ✅

```text
pm list packages            -> com.alibaba.aliexpresshd (8.180.1, versionCode 80005998)
launcher activity           -> com.aliexpress.app.FirstActivity
home focus                  -> com.alibaba.aliexpresshd/.home.ui.MainActivity
coins page activity         -> com.aliexpress.adc.ui.impl.AdcActivity (hybrid container)
```

`AdcActivity` renders pages from `https://www.aliexpress.com/ssr/…` (`wh_pid=…`).

## 3. Perception: screenshots yes, accessibility tree no ✅

| page | `uiautomator dump` | screenshot + OCR |
|---|---|---|
| Account page (native) / Home feed | 214 / ~211 nodes | works |
| **Coins page (hybrid)** | **6.4 KB, effectively empty** | **40 text boxes, all labels readable** |
| `"Welcome gifts"` modal | **absent entirely** | present, with drawn close control |

OCR is therefore the primary perception channel ([`adr/0002`](adr/0002-screenshot-ocr-perception.md)):
RapidOCR (CPU, onnxruntime) reads the coins page in **~1.6 s** (~0.99 confidence), and
`PopupDismisser` operates on screenshots because modals are invisible to `uiautomator`.

Sample OCR on the coins page ✅ (proving row-relative `GO` lookup and top-25% bare-number balance):

```text
  105,218  Coins                      (header)
  166,357  3                          (balance — bare number in top 25%)
   36,451  worth C$0.04
   58,591  Coins credits to redeem >
  943,640  Redeem          917,948  Earn more >
   52,938  Daily coins
  180,1269 Take the coins quiz         982,1273 GO
  180,1440 Explore sponsored items     981,1440 GO     1080,1432 1/6
  180,1608 Browse surprise items       980,1612 GO     1080,1601 0/2
```

## 4. Popups and the worked navigation failure ✅

Cold launch intermittently shows a `"Welcome gifts"` modal with an OCR-invisible circular ✕ at
~(600, 1652). Dismissal is layered: (1) small known dismiss labels (`X`, `×`, `Close`, `Skip`,
`No thanks`, `Not now`, `Got it`); (2) scale-aware template matching (`TM_CCOEFF_NORMED`,
threshold `0.88`); (3) per-profile calibrated modal offset guarded by a promo headline; (4) fail
loudly as `popup_unclosable`.

**Worked failure:** a blind three-tap sequence (`600,1652` → `1066,1855` → `1030,1220`) landed on
`AIProductDetailActivity` because the modal had already closed and the first tap hit a product
tile. Rule: every tap follows a fresh screenshot and a label assertion.

## 5. Routes to the coins page ✅

- **Shortcut deep link (`aliexpress://goto?url=…/p/coin-index/index.html`) ✅:** captured from
  `coinsPromotion`, works with the shortcut uninstalled, lands on `AdcActivity` (`coinindex`). Must
  target a **warm** app (`FirstActivity` settled ~10 s): cold-start intent produced an empty
  `Coins boost` page in 4/4 trials vs 5/5 populated when warm ([`05-app-knowledge.md`](05-app-knowledge.md) §4b).
- **Bare `https://www.aliexpress.com/p/coin-index/index.html` ❌:** renders a blank WebView.
- **Account tab promo card ⚠️:** rotates between coins and coupons; not implemented.
- **2×2 widget (`AECoin2x2NormalWidget`) ✅:** captured (`from=widget&aewidget_*`); automation descoped (#27).

## 7b. Boost-page row geometry and 1080×1920 replay ✅

Measured from `tests/data/boost.png` (cropped `1200×1520` from a `1200×2000` capture, no account
data) and replayed at simulated phone geometry (`wm size 1080x1920`, density 320):

| measurement | 1200×2000 | 1080×1920 |
|---|---|---|
| profile id (`doctor`) | `c9b8f88670e295b5` (240 dpi, sdk 35) | distinct hash (uncalibrated fallback) |
| median OCR box height | 48 px (boost) / 45 px (coins) | 43 px (scaled replay) |
| title → `GO` vertical offset | ≈100 px (boost) / ≈31 px (coins) | 68–70 px measured |
| derived row tolerance (`2.25 × median`) | 108 px (boost) / 101.25 px (coins) | ≈96.75 px |
| smallest adjacent-task / coin-card gap | ~190 px (pitch ~340 px) / 106 px | scales with frame |
| modal close control (fraction of height) | 0.826 | 0.789 (drifts with aspect ratio) |

`2.25×` covers every within-row gap (max 86.5 px) without bridging the 106 px card gap or ~190 px
task pitch (`3×` merged the coin card into row 1). Templates scale from frame width (with vertical
fallback up to 20:9).

## 7c. Session 3 findings (2026-11-22) ✅

- **Server-side task day:** at 00:12 EST, completed checkmarks survived `force-stop` + warm
  re-entry and changing device timezone to `Pacific/Kiritimati`.
- **Surprise items counter:** counts **distinct products**, not taps. Measured at `1/2` rounds:
  tile 1 → `1/3`; tile 2 → `1/3` (unchanged after a 9 s repeat dwell); tile 3 → `2/3`; tile 4 →
  `3/3` + `Mission Completed` overlay (>10 s until tapped); BACK returned with row gone and balance
  `43 → 44` (+1).
- **Notifications:** row credited `+1` while `POST_NOTIFICATION` stayed `ignore` (`granted=false`).
  App settings' `Allow phone notifications` deep-linked to the OS notification page (no runtime
  dialog); the runtime dialog (`Allow` / `Don't allow`) is in the #11 popup corpus.

## 7d. Session 4 findings — search landing (2026-11-24) ✅

- **The search row is a two-round category task.** At 01:48 EST the `Coins boost` row read
  `Search for what you love`, `+1`, `0/2`, `GO`. The landing page carried a search field, an
  account-specific `Search history` section and a server-rendered `Discover more` category grid.
- **Landing alone did not advance the badge.** A 6 s dwell and a return to the boost list left the
  row at `0/2`; the legacy note “progress stayed `0/2` after one search” was this case.
- **A history tap did not advance it either.** Tapping the `Search history` entry `Smartphone`
  filled the search field, opened no results and left the row at `0/2`.
- **A `Discover more` category tap did.** `Woman Clothing` advanced `0/2 → 1/2` and paid +1
  (balance `67 → 68`). A second category (`Sneakers`) completed the task; the row was gone from the
  populated boost list on the next read and the balance moved `68 → 69`. The app removes the row at
  `2/2`, so the final badge was not visible on a returned frame.
- **The landing has no timer chip and no item visit is required.** The measured page marker is
  `Discover more`; search-history text is account data and is not a marker. The reviewed frame is
  [`tests/data/states/search_landing.png`](../tests/data/states/search_landing.png).

Reviewed flow fixtures live under `tests/data/flows/` and `specs/flows.yaml`; open app questions
are tracked in [`05-app-knowledge.md`](05-app-knowledge.md) §8.
