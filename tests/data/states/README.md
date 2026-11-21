# Screen-state fixtures

Ten recorded frames from the reference tablet, used by `tests/test_perception_state.py`
(issue #16). They are **full 1200×2000 frames**, exactly the shape `AdbSnapshotter.capture()`
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
| `launch_modal.png` | full cold-launch `Welcome gifts` modal over the home feed | search bar and notification count masked; system UI retained |
| `skeleton.png` | full loading frame with the container's repeated low-contrast blocks | no account data; clock and taskbar retained for the regression test |
| `blank_webview.png` | full blank frame captured before the container rendered | no account data; clock and taskbar retained for the regression test |
| `boost.png` | full `Coins boost` list after the check-in row was completed | header balance and currency amounts masked; task rows remain |
| `quiz.png` | full `Coins quiz` question and answer options | no account data |
| `surprise_grid.png` | full `Browse surprise items` page | product images and prices only; no account data |
| `popup_over_page.png` | full promo modal (`Extra Coupon unlocked`) over the home feed | search bar and notification count masked |
| `ambiguous_feed.png` | full home feed with no modelled coins/collection marker | search bar and notification count masked; the lower `Bundle deals` feed section is not a top product-collection header |

`error_404.png` was dropped as an extra fixture: the bare advertised coin URL is recorded in
`docs/01-recon-findings.md` as landing on a blank WebView, while the capture session saw a 404
page. Error-marker classification is covered by a synthetic test instead of committing a fixture
that contradicts the recon document.

The existing `tests/data/boost.png` remains the cropped layout fixture for issue #14. The state
suite uses `states/boost.png`, a full frame, so the state classifier is always tested on pipeline
input.

Every PNG is under the ~300 KB fixture budget. `.gitignore` has one negation for
`tests/data/states/*.png`; that exception exists only because this directory is reviewed as a
whole. It is not permission to drop arbitrary screenshots anywhere else in the tree.
