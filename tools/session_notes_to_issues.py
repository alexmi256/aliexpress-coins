#!/usr/bin/env python3
"""Post the session-2 findings as comments and close the issues they answer.

Kept in the repo so the reasoning behind a closed issue is reproducible, not just visible in
the GitHub UI.
"""

from __future__ import annotations

import subprocess
import tempfile

REPO = "alexmi256/aliexpress-coins"

COMMENTS: dict[int, str] = {
    8: """**Answered 2026-11-20 — the canonical route exists, and it is not the shape this issue assumed.**

The page is reached with the app's own `aliexpress://goto` wrapper:

```
adb shell am start -a android.intent.action.VIEW \\
  -d 'aliexpress://goto?url=https%3A%2F%2Fm.aliexpress.com%2Fp%2Fcoin-index%2Findex.html'
```

`DispatcherActivity` resolves it into `AdcActivity` with
`dat=https://m.aliexpress.com/p/coin-index/index.html?...&adc_manifest=coinindex&wh_ttid=adc` and
the native coin page renders (~34–37 OCR boxes: `Coins`, `Daily coins`, the streak row).

The bare `https://www.aliexpress.com/p/coin-index/…` URL — what this issue started from — is the
form that lands on the blank web view. The wrapper is the difference.

The launcher shortcut adds `from=shortcut&user_growth_type=shortcut&homescreen_src=coinD11&shortcutId=coinsPromotion`;
a plain wrapper without them reaches the same page, so the parameters identify the referrer rather
than select the page.

Full write-up: `docs/05-app-knowledge.md` §2. Closing as answered.
""",
    7: """**Answered 2026-11-20 — both items were installed by the operator, and both intents are now captured.**

Launcher DB (`launcher_6_by_5.db`):

* shortcut: `itemType=6` (DEEP_SHORTCUT), title `Coins`, `S.shortcut_id=coinsPromotion`
* widget: `itemType=4`, 2×2, provider `com.alibaba.aliexpresshd/com.aliexpress.module.widget.widget.AECoin2x2NormalWidget`

Shortcut intent (`/data/system_ce/0/shortcut_service/packages/com.alibaba.aliexpresshd.xml`):

```
aliexpress://goto?url=https%3A%2F%2Fm.aliexpress.com%2Fp%2Fcoin-index%2Findex.html
  %3F_immersiveMode%3Dtrue%26from%3Dshortcut%26user_growth_type%3Dshortcut
  %26homescreen_src%3DcoinD11%26shortcutId%3DcoinsPromotion
```

Widget click intent (tap "Check in now"):

```
dat=https://m.aliexpress.com/p/coin-index/index.html?_immersiveMode=true&adc_manifest=coinindex
   &disableNav=true&from=widget&aewidget_status=signedAndHasWaitReceiveCoins
   &bizId=coins&widgetId=widget_coin_2x2_normal&user_growth_scene=widget
   &aewidget_biz_version=1&aewidget_widgetId=widget_coin_2x2_normal&aewidget_bizId=coins
   &aewidget_login=true&aewidget_deviceLevel=1&_launchTID=<uuid>&wh_ttid=adc
```

On **Q3 (creation vs launch)**: the operator completed both tasks by *adding* the items, with no
launch from them — so creation appears sufficient. The widget visit does, however, produce a page
variant with a `Collect` control and the task rows, which a plain deep-link visit did not; whether
that credits a separate repeatable task is one more observation (tracked in #27).

Closing as answered. The executor work lives in #27; the route decision in #38.
""",
    22: """**Correction 2026-11-20: "Earn more" is not the task list.**

Tapped twice on the live app; both times it opened a `Coins boost` page reading
"There's nothing here yet, but come check back later!" — not a task list.

The tasks are the **inline rows on the coins page** (quiz, sponsored `1/6`, surprise `0/2`), and
those rows are **conditional**: some page loads render none at all, while a visit fired from the
widget rendered them plus a `Collect` control.

So this issue's scope narrows to "enumerate the inline rows and the boost page", and the
missing-rows handling moves to #38. Acceptance still holds for the rows-present case.
""",
    24: """**Recon note 2026-11-20 (partial).**

Opening the sponsored-items row leads to a product-collection page (`Bundle Deals`, "Extra 20% off",
a "Hot picks" grid). The 15 s countdown and the progress advance were not observed cleanly — a stray
BACK sent the app to the home feed before the row could be tapped deliberately, so the wait length
stays ⬜ (the brief's 15 s, unverified).

Worth designing for: these pages are ordinary shopping surfaces, which is exactly why the
"no purchase taps" guardrail has to be active *while* a browse task runs, not only on the coins page.
""",
    26: """**Still unobserved as of 2026-11-20.** The row (`Browse surprise items`, `0/2`) was visible, but the
session ended before the flow was walked. This issue remains blocked on a live session where the task
is available, and it is the most likely place for the multi-round popups the brief describes.
""",
    31: """**Narrowed 2026-11-20.** The operator runs an Apprise server and wants users to configure their own
targets, so the transport is out of scope here.

This issue now covers only the artifacts: `summary.md`, `run.json`, `shots/` + OCR sidecars,
`run.jsonl`, and retention. Notification delivery moved to #40.
""",
    5: """**Milestone note 2026-11-20.** The operator's decision (Q6) is **manual runs first, one retry per
stage**; scheduling is deferred until manual runs are reliable. #29 has moved to the new
**M5 Scheduled operation** milestone, and this epic's M3 portion now reads "reliable manual
operation" rather than "unattended".

Nothing else in this epic changes: state, reports, retries and the Apprise hook are all still needed
for a manual run to be trustworthy.
""",
    29: """**Deferred 2026-11-20** to M5 per the operator's decision: runs are manual for now, with one retry,
until the failure handling in #32 has proven itself. Keeping the issue open preserves the design work —
it just is not on the path to v1.
""",
}


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def comment(number: int, body: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(body)
        path = handle.name
    gh("issue", "comment", str(number), "-R", REPO, "--body-file", path)
    print(f"commented #{number}")


def main() -> None:
    for number, body in COMMENTS.items():
        comment(number, body)
    for number in (7, 8):
        gh("issue", "close", str(number), "-R", REPO, "--reason", "completed")
        print(f"closed #{number}")


if __name__ == "__main__":
    main()
