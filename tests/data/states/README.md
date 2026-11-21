# Screen-state fixtures

Ten recorded frames (plus one extra error frame) from the reference tablet, used by
`tests/test_perception_state.py` (issue #16). Every file here was reviewed before it was
committed, then cropped and/or downsampled for layout and luminance, not for fine OCR.

**Review rule:** a file in this directory must contain no account nickname, coin balance, order,
cart, address or notification content. The screenshots that carried those details were cropped
below the header; the coin-page currency amounts were also painted out after cropping. Any new
image added here must be reviewed the same way before it is committed.

| file | what it is | review notes |
|---|---|---|
| `coins.png` | widget-route coin page, below the balance header | `Daily coins` and `Coins credits to redeem`; balance header cropped, currency amounts redacted |
| `account.png` | Account page, below the nickname/header | `My orders`, `History`, `Wishlist`, `Coupons`; nickname and notification count cropped out |
| `launch_modal.png` | cold-launch `Welcome gifts` modal over the home feed | no account identity, order, cart or address content |
| `skeleton.png` | loading frame: the hybrid container's repeated low-contrast blocks | no text or account data |
| `blank_webview.png` | blank frame captured before the container rendered | no text or account data |
| `quiz.png` | `Coins quiz` question and answer options | no account data |
| `surprise_grid.png` | `Browse surprise items` page, header and top of the grid | product images and prices only; no account data |
| `popup_over_page.png` | promo modal (`Extra Coupon unlocked`) over the home feed | no account identity, order, cart or address content |
| `ambiguous_feed.png` | home-feed/product recommendations with no modelled coin marker | deliberately ambiguous; header and account chrome cropped out |
| `error_404.png` | `404 Page` from the bare advertised coin URL | extra error fixture; no account data |

The boost-list frame is the existing reviewed fixture `tests/data/boost.png`, shared with the
layout tests (issue #14). It is not duplicated here.

Every PNG is under the ~300 KB fixture budget. `.gitignore` has one negation for
`tests/data/states/*.png`; that exception exists only because this directory is reviewed as a
whole. It is not permission to drop arbitrary screenshots anywhere else in the tree.
