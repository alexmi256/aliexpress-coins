# Screen-state fixtures

Sixteen recorded frames from the reference tablet, used by the screen-state classifier tests
(`tests/test_perception_state.py`) and the #84 discovery/sign-in tests. They are **full 1200×2000
frames**, exactly the shape `AdbSnapshotter.capture()`
returns: the status-bar clock and the Android taskbar glyphs are retained. The classifier
ignores those system-UI bands with frame-relative fractions; a cropped 1200×1700 fixture would
test a frame no run ever sees.

Account data is removed by masking the specific pixels, not by cropping the frame. Every file was
reviewed before it was committed.

**Review rule:** a file here must contain no account nickname, coin balance, order, cart, address
or notification content. Operator search text is masked out. Any new image added here must be
reviewed the same way.

| file | what it is | redaction / review note |
|---|---|---|
| `coins.png` | full widget-route coin page | balance digits and every currency amount masked; `Daily coins` and `Coins credits to redeem` remain |
| `account.png` | full Account page | nickname and notification count masked; `My orders`, `History`, `Wishlist`, `Coupons` remain |
| `launch_modal.png` | full cold-launch `Welcome gifts` modal over the home feed | search bar and notification badge/bell masked; system UI retained |
| `skeleton.png` | full loading frame with the container's repeated low-contrast blocks | no account data; clock and taskbar retained for the regression test |
| `blank_webview.png` | full blank frame captured before the container rendered | no account data; clock and taskbar retained for the regression test |
| `boost.png` | full `Coins boost` list after the check-in row was completed | header balance and currency amounts masked; task rows remain |
| `quiz.png` | full `Coins quiz` question and answer options | no account data |
| `surprise_grid.png` | full `Browse surprise items` page | product images and prices only; no account data |
| `popup_over_page.png` | full promo modal (`Extra Coupon unlocked`) over the home feed | search bar and notification badge/bell masked |
| `ambiguous_feed.png` | full home feed with no modelled coins/collection marker | search bar and notification badge/bell masked; the lower `Bundle deals` feed section is not a top product-collection header |
| `boost_daily_checkin.png` | full `Coins boost` page from the failed 2026-11-23 nightly run, first row `Daily check-in` (#84) | balance chip and hero, coin-credit chip, notification badge and status-bar clock masked; `Coins boost` and all task rows remain |
| `coins_merged_card.png` | full coin page from the same run, where the daily card and the first task row share one OCR band (#84) | coin balance, worth, coin-credit and credits-to-use amounts, notification badge and status-bar clock masked; `Daily coins`, `Earn more`, `Collect` and `Daily check-in` remain |
| `coins_search_row.png` | full coin page from the issue #107 occurrence on 2026-11-24 (run `20261124T042511Z`): the daily card absorbs `Search for what you love` and its `0/2` badge, and the mini-app strip abuts `View Super discounts` | coin balance, worth, coin-credit and credits-to-use amounts and status-bar clock masked; `Daily coins`, the streak chips, `Search for what you love`, `Place an order on this page`, `View Super discounts` and the `Merge Boss` / `Play & Earn` / `Prize Land` strip remain |
| `search_landing.png` | full search landing opened by `Search for what you love` on 2026-11-24 01:53 EST: `Search history`, `Discover more` and its category cards (#108) | the current search-field text and both `Search history` entries masked pixel-by-pixel; the two section headings, category titles and item counts remain, with no account nickname, balance, order, cart or address |
| `coin_store.png` | full `Use coins for discounts` sponsored product list opened by `Explore sponsored items`, with its right-edge `13s` dwell chip, `Disc:` prices and `Applied` labels (#83) | no account data; product titles, prices, the system clock and taskbar are kept, and the header/`Disc:`/`Applied` labels remain for the classifier regression |
| `launcher.png` | full Android launcher captured during recon on 2026-11-21, with app icons and the `2:22` / `Sat, Nov 21` widget (#116) | no account, notification, order or cart data; app labels and wallpaper only. This is **not** the missing post-tap frame from run `20261124T083006Z`; it drives the package-probe recovery replay for a launcher with icons |

`error_404.png` was dropped as an extra fixture: the bare advertised coin URL is recorded in
`docs/01-recon-findings.md` as landing on a blank WebView, while the capture session saw a 404
page. Error-marker classification is covered by a synthetic test instead of committing a fixture
that contradicts the recon document.

The existing `tests/data/boost.png` remains the cropped layout fixture for issue #14. The state
suite uses `states/boost.png`, a full frame, so the state classifier is always tested on pipeline
input.

Every PNG is under the **550 KB** fixture budget, raised from ~300 KB on 2026-11-23. Fixtures
must stay full 1200×2000 captures — downscaling would test a frame no run ever sees (#42/#36) —
and the dense gradient/photo frames added for #84 and #107 are each under 515 KB after a lossless
`optimize=True, compress_level=9` pass (pixels unchanged, so OCR is unchanged). 550 KB is
the ceiling; anything larger needs a recorded reason. `.gitignore` has one negation for
`tests/data/states/*.png`; that exception exists only because this directory is reviewed as a
whole. It is not permission to drop arbitrary screenshots anywhere else in the tree.
