# Popup-corpus fixtures

Full-frame captures for the popup families discovered by issue #11, used to
test the dismissal rules in [`specs/popups.yaml`](../../specs/popups.yaml). Every
file is **1200×2000**, exactly the shape `AdbSnapshotter.capture()` returns:
the status-bar clock and the Android taskbar are retained, so the fixture is
the same frame a run sees. Account data is removed by masking the pixels, not by
cropping the frame.

The files are indexed PNGs (palette, no dither) to stay under the fixture
budget; dimensions are unchanged.

**Review rule:** a file here must contain no account nickname, coin balance,
coin-credit balance, order, cart, address or notification text. Operator search
text and the notification count/badge are masked. Every file was reviewed before
it was committed. Any new image added here must be reviewed the same way.

| file | family | what it is | redaction / review note |
|---|---|---|---|
| `launch_welcome_gifts.png` | `launch_welcome_gifts` | full cold-launch `Welcome gifts` modal over the home feed | search bar and notification badge masked; reused from the reviewed `states/launch_modal.png` frame |
| `promo_coupon_unlocked.png` | `promo_coupon_unlocked` | full `Extra Coupon unlocked` promo modal over the home feed | search bar and notification badge masked; reused from the reviewed `states/popup_over_page.png` frame |
| `promo_bundle_deals_badge.png` | `promo_bundle_deals_badge` | full home feed with the `Bundle deals` card and its dismissible ad badge | search bar and notification badge masked; no nickname, balance or cart shown |
| `coins_boost_sheet.png` | `coins_boost_sheet` | full `Coins boost` sheet over the coin page | header balance, credit balance and the coin-page balance masked; task rows remain |
| `push_permission_prompt.png` | `push_permission_prompt` | full-height `Receive timely notifications` variant | search bar and notification badge masked; the only visible close is the drawn ✕ |
| `push_permission_prompt_sheet.png` | `push_permission_prompt` | bottom-sheet `Receive timely notifications` variant | search bar and notification badge masked; `No thanks` is visible |
| `system_notification_permission.png` | `system_notification_permission` | Android `Allow AliExpress to send you notifications?` runtime dialog over the app prompt | search bar and notification badge masked; the dialog names the app, not the account |

`redeem_interstitial` (`YOU CAN NOW REDEEM!`) has no fixture: it is a one-shot
interstitial that only appears after a task pays coin credit, and the quiz had
already been claimed for the day when the corpus was captured. The row in
`specs/popups.yaml` records it as observed but not reproduced, and its close
strategy is an explicit loud failure.

The `assets/templates/` crops are taken from these files. `source_width: 1200`
in the YAML records the scale #15 must account for when matching at another
device width.
