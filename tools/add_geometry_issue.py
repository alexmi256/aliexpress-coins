#!/usr/bin/env python3
"""Create the geometry-independence issue and link it into the perception epic."""

from __future__ import annotations

import subprocess
import tempfile

REPO = "alexmi256/aliexpress-coins"

BODY = """## Context

The reference device is a tablet (1200×2000 screenshots at 240 dpi). Other users will run this on
phones with different sizes, densities and aspect ratios, so **any stored pixel is a bug**. That was
tested directly: the same device was driven at a simulated phone geometry (`wm size 1080x1920`,
`wm density 320`) and the assumptions were measured.

What survived, and what did not:

| assumption | at 1200×2000 | at 1080×1920 | verdict |
|---|---|---|---|
| find `Earn more` by label | (1025,946) | (922,851) | ✅ label lookup ports |
| coin-page markers (`Daily coins`, `Coins credits to redeem`) | present | present | ✅ text ports |
| pair a task title with its `GO` | dy ≈ 100 px | dy ≈ 68 px | ⚠️ fixed 50 px tolerance failed at **both**; 120 happened to cover both |
| popup close control, as a fraction of height | 0.826 | 0.789 | ❌ even fractions shift with aspect ratio |
| scroll distances | absolute swipes (600→1700) | would overshoot/short-scroll on a shorter screen | ❌ |
| close-button template crop | 1200-wide crop | wrong scale | ❌ |

## Scope

- **Device profile**: `doctor` records width, height, density, orientation and SDK, and derives a
  profile id. Calibration data (popup fallback points, template scale) is keyed by that id.
- **Proportional geometry**: every distance is expressed relative to something measured in the
  frame — `GO` row tolerance from the median text-box height (e.g. `3 × box height`), popup offsets
  as fractions of the *modal*, not of the screen.
- **Scrolling as a search, not a jump**: swipe a fraction of the content height (≈0.5–0.6), then
  re-capture and look for the target; stop on target-found or on a stable frame (screenshot diff),
  with a bounded number of attempts. No fixed pixel distance anywhere.
- **Scale-aware templates**: match a stored crop at a range of scales (0.5–2.0×, derived from
  `screen_width / template_reference_width`), or prefer structural detection (e.g. a small circular
  control on the modal's centre axis below the panel) so that no template is needed at all.
- **Popup fallbacks become per-device calibration**: a global coordinate constant is removed; a
  popup with no calibration is reported `unclosable` rather than tapped.
- **Tests**: replay one recorded frame set at two geometries and assert the same rows/targets are
  derived.

## Acceptance criteria

- [ ] No module contains an absolute pixel constant that is not derived from the current frame or a device profile.
- [ ] A recorded coins-page frame scaled to a second geometry still yields the same task rows.
- [ ] The launch modal is dismissed at both geometries without a hard-coded coordinate.
- [ ] Scrolling to reach a task below the fold works with the same code at both geometries.
- [ ] `doctor` prints the profile id and the geometry it derived.

## Dependencies

Depends on: #12, #13, #14. Constrains: #18 (popup dismissal), #24/#26 (scrolling to tasks),
#36 (frame replay at multiple geometries).

## Notes

The measurement that matters most: at 1080×1920 the `GO` for a row sat **68–70 px** below the title
and the task roster itself had changed (`Search for what you love` replaced `Browse surprise items`).
Both facts point the same way — derive targets from the frame in front of you, never from a table
compiled on an earlier device.
"""


def gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, input=stdin, check=False)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def main() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(BODY)
        path = handle.name
    url = gh(
        "issue", "create", "-R", REPO,
        "--title", "Geometry independence: device profiles, proportional tolerances, scaled templates",
        "--body-file", path,
        "--label", "area:perception", "--label", "type:feature", "--label", "priority:p0",
        "--milestone", "M1 Perception & control",
    )
    number = url.rstrip("/").split("/")[-1]
    print(f"created #{number}")

    gh("issue", "edit", str(number), "-R", REPO, "--parent", "2")
    print("linked as a sub-issue of #2")
    gh("issue", "comment", "36", "-R", REPO, "--body",
       f"Related: #{number} requires recorded frames to be replayed at more than one geometry "
       "(the reference tablet plus a phone-sized profile). Worth building into the harness from "
       "the start rather than retrofitting.")
    print("cross-referenced from #36")


if __name__ == "__main__":
    main()
