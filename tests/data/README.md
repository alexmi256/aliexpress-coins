# Test frames

`frame.png` is a synthetic 1200x2000 PNG drawn for the capture tests. It contains no device
screenshot, account nickname, order or cart data.

`boost.png` is a real 1200x1520 capture of the `Coins boost` task list, cropped to the header and
task rows. It contains no coin balance, account nickname, order or cart data. It is the measured
fixture for row pairing: four task rows at the device's ~340 px pitch, with the title-to-`GO`
offset visible in each row. The measurement is recorded in `docs/01-recon-findings.md` §7b.

These were the reviewed screenshot exceptions authorised by issue #14. They are not precedent for
dropping arbitrary screenshots into the tree: any other screenshot or OCR dump stays out of git
until it is cropped, reviewed the same way and given an explicit `.gitignore` exception.

`states/` holds the reviewed fixtures for the screen-state classifier (issue #16), e.g. a coin
page, the account page, the launch modal, loading/blank frames, a quiz, the surprise-items grid and
a popup over a page. They are full 1200×2000 frames with the status bar and taskbar retained, so
the classifier is tested on the same shape `capture()` returns. Account data, operator search text
and similar details are masked pixel-by-pixel rather than cropped away; see
[`states/README.md`](states/README.md) for exactly what was redacted in each frame. The single
`.gitignore` negation for `tests/data/states/*.png` is limited to that reviewed directory; it does
not re-enable PNGs elsewhere.

`popups/` holds the reviewed full-frame fixtures for the popup corpus (issue #11), one per family
with the status bar retained. They are masked the same way and described file-by-file in
[`popups/README.md`](popups/README.md). The single `.gitignore` negation for
`tests/data/popups/*.png` is limited to that directory; the glyph crops under `assets/templates/`
have their own negation because they are artwork only. A file in either directory is reviewed
before it is committed, and a new image is not evidence that arbitrary screenshots may enter git.

`flows/` holds the reviewed fixtures for the three state machines in
[`specs/flows.yaml`](../../specs/flows.yaml) (issue #10): one full frame per **captured** state of
the quiz, surprise-items and notification flows, with the same masking rule and a file-by-file inventory in
[`flows/README.md`](flows/README.md). The single `.gitignore` negation for `tests/data/flows/*.png`
is limited to that directory.
