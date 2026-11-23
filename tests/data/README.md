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

## Regenerating a set

The suite replays these files through `tests/support/frames.py`; it never captures from the
device. When a flow needs a new fixture, capture it once with
[`tools/capture_frames.py`](../../tools/capture_frames.py). The tool uses the same
`SubprocessAdbRunner` transport as the collector, calls only `adb exec-out screencap -p` (no
tapping, ordering, redeeming, sharing or games), and never downscales: the frame must match the
geometry `wm size`/`wm density` report, or it exits non-zero, because #42 keys calibration by that
geometry.

```sh
# Capture the `states` set. The tool prompts before each frame so you can put
# the device on the right screen, then prints the manifest.
.venv/bin/python tools/capture_frames.py --serial 192.168.8.65:5555 --set states

# Print the manifest for the frames already in the directory, without a device.
.venv/bin/python tools/capture_frames.py --set states --manifest-only

# List the sets and the files each one expects.
.venv/bin/python tools/capture_frames.py --list
```

`--set` is one of `states`, `flows` or `popups`; `--out` overrides
`tests/data/<set>/`, and `--manifest-out` also writes the manifest as JSON.  The
`--manifest-only` manifest for `states` (one `frames` entry per file; the rest are omitted for
brevity) is:

```json
{
  "version": 1,
  "set": "states",
  "source": "existing",
  "serial": null,
  "captured_at": null,
  "width": 1200,
  "height": 2000,
  "density": 240,
  "sdk": 35,
  "frames": [
    {
      "name": "coins",
      "file": "coins.png",
      "width": 1200,
      "height": 2000,
      "bytes": 142830,
      "sha256": "d06c7f69f048e06df73c4b66d89ed6482d855bcdf53df47a5e625a350963a515"
    }
  ]
}
```

`ScriptedSnapshotter.from_directory()` reads a `manifest.json` in the directory when one is
present and uses its density and SDK for the replay key; the committed sets do not carry one, so
they replay at the measured 240 dpi / SDK 35 defaults.

A captured frame is a *candidate*, not a fixture: mask account data, review it against the
review rule above, and add a `.gitignore` exception only for the specific reviewed directory.
