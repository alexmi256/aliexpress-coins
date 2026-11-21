# Specs

`popups.yaml` is the popup corpus from issue #11: one row per family, the OCR
anchor that identifies it, the ordered close attempts, and the geometry the
crop was measured at. The policy order is declared in the file and read by the
loader; the committed corpus is **label → template → fraction**, because a text
label is scale-independent while a template crop is tied to one rendering. The
comments in the file are the policy; the loader that enforces the schema is
[`src/aliexpress_coins/control/popups.py`](../src/aliexpress_coins/control/popups.py)
and its tests are
[`tests/test_control_popups.py`](../tests/test_control_popups.py).

Run the corpus check with the normal gate:

```sh
just check
```

To add a family:

1. Capture the frame and mask account data into `tests/data/popups/`; add a row
   to that directory's README.
2. If the close control is glyph-only, crop it tight from the reviewed frame
   into `assets/templates/` and record its source frame width.
3. Add the YAML row. A row with no executable close strategy is rejected; an
   unclosable popup uses an explicit `fail` reason so the runner stops loudly.
4. Add a fixture-backed assertion only for facts the loader cannot know (for
   example, the two layouts of a family).
