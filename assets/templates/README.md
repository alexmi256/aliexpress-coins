# Popup close-control templates

One crop per glyph-only close control discovered by issue #11. They are
referenced from [`specs/popups.yaml`](../../specs/popups.yaml); a family whose
control carries text uses a `label` instead and has no crop here.

Every crop was taken from a reviewed full frame in
[`tests/data/popups/`](../../tests/data/popups/README.md), tight to the control
with a few pixels of margin, at the reference device's **1200 px frame width**
(1200×2000, 240 dpi, sdk 35). The YAML records that width as `source_width` so
[`perception/templates.py`](../../src/aliexpress_coins/perception/templates.py)
can find the same control on a frame of another width. The crop itself is not
rescaled or resampled on disk.

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

## How matching works

`match_template(frame, template)` in
[`templates.py`](../../src/aliexpress_coins/perception/templates.py) runs
`cv2.TM_CCOEFF_NORMED` over grayscale pixels. A score at or above **0.88** is a
match. The committed crops match their own fixture at 0.999–1.000; the closest
wrong glyph, the coupon ✕ against the launch modal, peaks at 0.791, so 0.88
keeps the real control and rejects the nearest impostor. The threshold is the
one recorded in [`docs/01-recon-findings.md`](../../docs/01-recon-findings.md)
§4.

The nominal scale is `frame_width / source_width`. Nine scales from 0.8× to
1.2× of that nominal are searched, so a crop cut at 1200 px is still found on a
1080 px frame. The result carries the winning `scale`, its `location`, `box`,
`center` and the scales that were tried. The search first runs at half size and
then verifies/refines around the coarse hit at full resolution; the returned
coordinates are always full-resolution. Five templates over the 1200×2000 launch
fixture take about **0.45 s** on the development machine, next to ~1.6 s of OCR
for the same frame.

NCC is invariant to the brightness and contrast of the scrim behind a glyph, so
the same white ✕ matches over a light sheet and a dark scrim. It is **not**
invariant to a polarity reversal: an inverted glyph is a different template.
The corpus stores each crop in the polarity the app renders, rather than
searching inverted copies that would make unrelated shapes look alike.

A flat crop has no variance, so NCC is undefined (OpenCV reports a perfect 1.0
for a uniform template at every location). The matcher returns `None` for an
all-white or all-black crop, and it masks candidate frame windows whose variance
is zero. Blanking the control region of a real frame therefore removes the match
instead of producing a spurious one.

## Cutting a new template

Add the reviewed frame under `tests/data/popups/` first, then cut the control
with the tool. The box is `(left, top, right, bottom)` with `right`/`bottom`
exclusive, exactly like `PIL.Image.crop()`:

```sh
.venv/bin/python tools/cut_template.py \
    tests/data/popups/launch_welcome_gifts.png launch_welcome_gifts \
    --box 532 1600 666 1733 --force
```

The tool writes `assets/templates/<family>.png`, refuses a uniform crop, and
prints the rule row:

```yaml
close:
  template: assets/templates/launch_welcome_gifts.png
  source_width: 1200
```

`source_width` is the width of the **frame the crop came from**, not the width
of the crop. If the reviewed frame is a different geometry, the tool records
that frame's width; do not copy the old row's number. Add the row under the
family in `specs/popups.yaml`, then run `just check`.

No module change is needed: `load_templates(corpus)` discovers every
`CloseKind.TEMPLATE` row and pairs it with that row's `source_width`. The tests
exercise a crop and row dropped into a temporary corpus, so an added family is
data, not code.

## What #18 should call

```python
corpus = load_popup_corpus('specs/popups.yaml')
templates = load_templates(corpus)
match = templates.match(frame, family.family)
if match is not None:
    tap(match.center)  # the box is the hit area; clamp inside it
```

A family with no template is a `KeyError` from `by_family`; only call `match`
for a family whose close attempts include a template.
