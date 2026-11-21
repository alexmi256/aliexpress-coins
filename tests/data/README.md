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
a popup over a page. Each one was cropped and reviewed for account data; see
[`states/README.md`](states/README.md) for what each frame is. The single `.gitignore` negation for
`tests/data/states/*.png` is limited to that reviewed directory; it does not re-enable PNGs
elsewhere.
