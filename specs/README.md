# Specs

`popups.yaml` is the popup corpus from issue #11: one row per family, the OCR
anchor that identifies it, the ordered close attempts, and the geometry the
crop was measured at. The policy order is declared in the file and read by the
loader; the committed corpus is **label → template → calibrated modal offset**,
because a text label is scale-independent while a template crop is tied to one
rendering and a fallback is only safe on the profile it was measured on. The
`calibration` section is keyed by the profile id `doctor` prints; a device with
no row there has no fallback and the popup is reported `popup_unclosable`. The
comments in the file are the policy; the loader that enforces the schema is
[`src/aliexpress_coins/control/popups.py`](../src/aliexpress_coins/control/popups.py)
and its tests are
[`tests/test_control_popups.py`](../tests/test_control_popups.py).

`tasks.yaml` is the task catalogue and default-deny policy from issue #21:
`allowed` rows name a family, its OCR patterns, category, wait and per-run
limit; `denied` rows name a refusal and carry the human-readable reason that
ends up in the run report. The loader is
[`src/aliexpress_coins/tasks/catalogue.py`](../src/aliexpress_coins/tasks/catalogue.py)
and its tests are
[`tests/test_tasks_catalogue.py`](../tests/test_tasks_catalogue.py). The file is
validated with Pydantic at load: an unknown category, an empty pattern list, a
negative wait, a duplicate key, or an allowed row that the deny list refuses
stops the run at startup. Matching is case-insensitive whole-word matching;
deny is checked before allow, and an unknown label is refused as
`unrecognised`. A one-word allow pattern is only matched against the title, so
a generic word in a subtitle cannot pull an unknown row into an executor. A
small generic deny-word set (`cart`, `order`, `game`, `review`, `buy`, `share`,
`invite`) is also title-only, so an allowed row is not refused for its
description; action phrases and precise action verbs (`Add to cart`,
`Checkout`) may match either field. The streak `Collect` control is deliberately
not an allow pattern: #23 owns it outside row dispatch.

`flows.yaml` is the flow state machines from issue #10 for the three task
families the executors need: `coins_quiz` (#25), `surprise_items` (#26) and
`notifications` (#28). Each flow names its entry (surface, row label, control),
its states with the OCR labels that identify them and the one control to act on,
its transitions with the verification signal for each, the popup families that
can appear, the exit back to the coins page, and the measured timings. A state
whose frame could not be captured on the recording day carries
`fixture: null` and `fixture_absent_reason` instead of a guessed screenshot; the
verification test requires a reviewed full frame for every captured state. Its
tests are [`tests/test_specs_flows.py`](../tests/test_specs_flows.py).

Run all spec checks with the normal gate:

```sh
just check
```

To add a task family:

1. Add an `allowed` row with a unique snake_case key, one or more patterns taken
   from the measured title or subtitle, a known category, and a wait from the
   timer chip (0 when the task is not a dwell task). Add a deny row instead if
   the family must never be touched, and give it a reason a report can print.
2. Run `just check`. The loader proves the new row is not shadowed by another
   allowed row and is not refused by the deny list.

To add a popup family:

1. Capture the frame and mask account data into `tests/data/popups/`; add a row
   to that directory's README.
2. If the close control is glyph-only, cut it from the reviewed frame with
   [`tools/cut_template.py`](../tools/cut_template.py) into `assets/templates/`;
   the tool records the source frame width. See that directory's README.
3. Add the YAML row. A row with no executable close strategy is rejected; an
   unclosable popup uses an explicit `fail` reason so the runner stops loudly.
   A fallback is not a global coordinate: measure the close control inside the
   dialog's own bounds and add it under `calibration.<profile-id>` using the
   offset fraction. A scrim-backed full-screen modal uses the content bounds
   (system bars excluded, header/footer included); a panel without a scrim uses
   the measured panel. Do not add a fallback for a frame whose dialog bounds
   cannot be measured.
4. Add a fixture-backed assertion only for facts the loader cannot know (for
   example, the two layouts of a family, or a template match at a second
   geometry).
