# ADR 0002 — Screenshot + OCR is the primary perception channel

**Status:** accepted (2026-11-19) · **Revisit when:** AliExpress exposes the coins page through
a real accessibility tree, or the container starts publishing semantic nodes.

## Context

Android automation normally reads the accessibility tree (`uiautomator dump`): labels, ids,
bounds, all free and precise. On this app it is empty exactly where it matters.

Measured on the coins page: a 1200×2000 screenshot yields **40 OCR boxes** covering every
label and button; `uiautomator dump` of the same screen is **6.4 KB of empty scaffolding**.
The "Welcome gifts" modal does not appear in the tree at all, on any page.

## Decision

Perception is **screenshot → OCR → `TextBox[]`**, with the accessibility tree used only
opportunistically (native chrome such as the bottom tab bar). Every decision — finding a
button, reading the balance, detecting a popup — works from OCR geometry.

## Reasons

* It is the only channel that sees the coins page and the popups.
* It is testable: a saved screenshot plus its box list reproduces a decision without a device.
* It degrades legibly: a missing label is a logged `tap_miss`, not a silent no-op.

## Consequences

* Robustness comes from **relative geometry** (a task's button is the rightmost action label on
  its row) rather than absolute coordinates.
* Glyph-only controls (close buttons) need template matching; the project carries templates
  and a documented fallback coordinate per popup family.
* OCR cost: ~1.6 s per frame on CPU, so a run captures tens of frames, not hundreds. Waits poll
  the frame hash rather than re-OCRing in a tight loop.
* Localisation would change every label. The catalogue is pattern-based so a second locale is a
  spec file, not a redesign — but it has not been tested.
