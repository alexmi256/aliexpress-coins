# Test frames

`frame.png` is a synthetic 1200x2000 PNG drawn for the capture tests. It contains no device
screenshot, account nickname, order or cart data.

`boost.png` is a real 1200x1520 capture of the `Coins boost` task list, cropped to the header and
task rows. It contains no coin balance, account nickname, order or cart data. It is the measured
fixture for row pairing: four task rows at the device's ~340 px pitch, with the title-to-`GO`
offset visible in each row. The measurement is recorded in `docs/01-recon-findings.md` §7b.

This is the **one reviewed screenshot exception**, authorised by issue #14. It is not precedent:
any other screenshot or OCR dump stays out of git until it is cropped, reviewed the same way and
given an explicit `.gitignore` exception. The `.gitignore` exceptions for these two files exist
only so that the fixtures can be committed; there is deliberately no directory-wide exception.
