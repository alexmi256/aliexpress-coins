# Popup close-control templates

One crop per glyph-only close control discovered by issue #11. They are
referenced from [`specs/popups.yaml`](../../specs/popups.yaml); a family whose
control carries text uses a `label` instead and has no crop here.

Every crop was taken from a reviewed full frame in
[`tests/data/popups/`](../../tests/data/popups/README.md), tight to the control
with a few pixels of margin, at the reference device's **1200 px frame width**
(1200×2000, 240 dpi, sdk 35). The YAML records that width as `source_width` so
#15 can make matching scale-aware; the crops themselves are not rescaled.

| template | family | control | source frame |
|---|---|---|---|
| `launch_welcome_gifts.png` | `launch_welcome_gifts` | white circular ✕ | `launch_welcome_gifts.png` |
| `promo_coupon_unlocked.png` | `promo_coupon_unlocked` | pale ✕ over the promo | `promo_coupon_unlocked.png` |
| `promo_bundle_deals_badge.png` | `promo_bundle_deals_badge` | grey circular ✕ on the card badge | `promo_bundle_deals_badge.png` |
| `coins_boost_sheet.png` | `coins_boost_sheet` | white ✕ in the sheet header | `coins_boost_sheet.png` |
| `push_permission_prompt.png` | `push_permission_prompt` | white ✕ on the full-height variant | `push_permission_prompt.png` |

The templates are artwork only: no nickname, balance, order, cart, address or
notification text. A new crop must come from a reviewed frame and be added to a
YAML row; an orphan crop is a corpus error.
