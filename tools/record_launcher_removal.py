#!/usr/bin/env python3
"""Record the launcher-item removal and the intent-independence result on #27."""

from __future__ import annotations

import subprocess
import tempfile

REPO = "alexmi256/aliexpress-coins"

BODY = """**2026-11-20: the shortcut and widget were removed from the device, and the route still works.**

## The intent does not depend on the launcher item

Firing the shortcut's exact URI with **no shortcut installed**:

```
adb shell am start -a android.intent.action.VIEW -d \\
  'aliexpress://goto?url=https%3A%2F%2Fm.aliexpress.com%2Fp%2Fcoin-index%2Findex.html
   %3F_immersiveMode%3Dtrue%26from%3Dshortcut%26user_growth_type%3Dshortcut
   %26homescreen_src%3DcoinD11%26shortcutId%3DcoinsPromotion'

-> AdcActivity, dat=https://m.aliexpress.com/p/coin-index/index.html?...&adc_manifest=coinindex
```

The resulting intent carries the same `from=shortcut&user_growth_type=shortcut&homescreen_src=coinD11&shortcutId=coinsPromotion`
parameters a real tap produces, plus a fresh `_launchTID` (which the app generates per launch anyway).
So the executor can reproduce "opened from the shortcut" without ever creating the item — the
launcher entry is a convenience, not a dependency.

That also means the `Add a coins shortcut` reward, once earned, does not need the item to survive:
create it, and remove it again.

## Removal, and how to automate it

Both items were removed with the launcher's drag-to-remove gesture, which is automatable:

```
adb shell input motionevent DOWN <x> <y>      # on the icon or widget
sleep 1.2                                      # long enough for drag mode
adb shell input motionevent MOVE <x> <y-200>
adb shell input motionevent MOVE <x> <y-900>
adb shell input motionevent MOVE 620 250       # the "× Remove" target
adb shell input motionevent UP 620 250
```

`input swipe` and a single long press both failed here — the launcher either opened the app drawer or
showed the app menu (App info / Split screen / Widgets, with no Remove). The motionevent sequence with
a held DOWN is what works, and it is what the cleanup step in the executor should use.

Verified after removal:

* launcher DB (`launcher_6_by_5.db`) no longer contains the `itemType=6` shortcut row or the
  `AECoin2x2NormalWidget` row — only the AliExpress app icon remains;
* no bound widget instance for the app in `dumpsys appwidget` (only the provider registration, which
  exists whenever the app is installed);
* the home screen shows neither item.
"""


def gh(*args: str) -> None:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    print(f"ok: gh {' '.join(args[:3])}")


def main() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(BODY)
        path = handle.name
    gh("issue", "comment", "27", "-R", REPO, "--body-file", path)


if __name__ == "__main__":
    main()
