#!/usr/bin/env python3
"""Post the 2026-11-20(B) corrections: the boost page, the stuck state, the full task list.

The first recon session concluded "Earn more is not the task list" from two empty renders. That was
wrong, and the correction has to land where the wrong claim did — on #22 — not only in the docs.
"""

from __future__ import annotations

import subprocess
import tempfile

REPO = "alexmi256/aliexpress-coins"

COMMENTS: dict[int, str] = {
    22: """**Correction — my earlier comment on this issue was wrong, and this one supersedes it.**

`Earn more` **is** the full task list. It opens the `Coins boost` page, which on one page load
listed eleven tasks (sponsored, surprise, quiz-family, check-in-to-win, notifications, shortcut,
widget — plus the four we refuse). The two renders I based the earlier comment on were the page's
**empty state**, not a different surface:

```
Coins boost
There's nothing here yet, but come check back later!
```

That state reproduces deterministically: `am force-stop` → fire the shortcut intent → tap
`Earn more` → empty, twice in a row. The same tap on a warm page (the app has already rendered the
boost page once) shows the full list. Recovery is exactly what the operator described: back out to
the coin page, re-enter (shortcut, widget, or Account → coin page), tap `Earn more` again.

So the scope of this issue is unchanged from the original: open the boost page, wait for it to
settle, enumerate the rows. The empty case is handled by #38.

Full observed list, with subtitles and progress badges: `docs/05-app-knowledge.md` §4.
""",
    38: """**Measured 2026-11-20 — this issue now also owns the stuck state.**

### The failure mode

The `Coins boost` page renders, with its header, but no tasks:

```
Coins boost
There's nothing here yet, but come check back later!
```

Reproduced twice: `am force-stop` → shortcut deep link → `Earn more` → empty. Warm `Earn more`
(same process, page already loaded once) → the full eleven-row list. So it is a first-load failure,
not an empty day: the list always contains the declined tasks (order, cart, water, share), which is
why "no tasks" can be treated as a fault.

### Detection (what the runner should assert)

* header `Coins boost` **and** `There's nothing here yet` → **stuck**, regardless of styling
  (the red background the operator saw is a style, not a marker);
* header `Coins boost` **and** at least one known task title → loaded;
* no coin/boost marker at all after a navigation → wrong screen entirely.

### Reset (the operator's rule, now verified)

1. BACK to the coin page.
2. Re-enter: fire the shortcut intent **after a force-stop**, or tap the widget.
3. Tap `Earn more` again.

### A second finding: the intent does not work on a running app

`am start -a VIEW -d 'aliexpress://goto?url=…'` fired while the app was already running on the home
feed **did not navigate** — the capture stayed on the feed (frame diff 0.007–0.010, i.e. no change).
From a force-stopped state the same command lands on the coin page reliably. So every reset path must
stop the app first; a bare `am start` is not a reset.

### Change detection

The operator's suggestion is now the design: capture → act → capture, and compare 36×60 grayscale
thumbnails. A page swap measured 0.19–0.40; a stale-coordinate tap on the feed measured 0.007–0.010.
Threshold ≈ 0.02 separates them cleanly, and it makes "did anything happen?" a logged number rather
than an assumption.

Screen markers and the full graph: `docs/07-navigation-map.md`.
""",
    21: """**Full task list observed 2026-11-20**, which lets the deny rules be written against reality rather
than the brief. Eleven rows on the `Coins boost` page:

| task | subtitle | refuse? |
|---|---|---|
| Learn to save money with Coins Credits | Stack with coins, coupons and other discounts | no |
| Explore sponsored items `1/6` | Browse, shop and save | no |
| Browse surprise items `0/2` | Tap 3 items on this page to earn coins | no |
| Check in to win Shopping credits | Don't forget to check in today! | no |
| Turn on notifications | Stay informed when rewards drop | no |
| Add a coins shortcut | One tap away from your daily rewards | no |
| Add & start using our widget | Add our widget to your homescreen to use | no |
| **Place an order on this page** `0/3` | Each order grants you rewards | **yes — order** |
| **Add 5 items to your cart** | — | **yes — cart** |
| **Water to win a US $0.10 prize** | Water your tree in Prize Land | **yes — currency + game** |
| **Share coins with friends** `0/3` | Win rewards together | **yes — social** |

Two implications for the policy:

1. **Four refused rows sit on the same page as the wanted ones**, with identical `GO` buttons and
   the same row geometry. Enforcing the deny list at dispatch (before an executor sees a row) is not
   belt-and-braces here — it is the only thing standing between "tap the third GO" and placing an
   order.
2. The catalogue's `label_patterns` should be seeded from the **subtitle** as well as the title:
   "Each order grants you rewards" and "Water your tree in Prize Land" are clearer deny signals than
   the short titles.
""",
    16: """**Markers measured for the classifier (2026-11-20).** `docs/07-navigation-map.md` §3 has the full
table; the ones this issue needs:

| screen | assert on | trap |
|---|---|---|
| coin page | `Daily coins` **and** `Coins credits to redeem` | the account page shows `Coins` without either |
| coins boost (loaded) | `Coins boost` + ≥1 task title | — |
| coins boost (empty/stuck) | `Coins boost` + `There's nothing here yet` | same header as loaded |
| quiz | `Coins quiz` + `True`/`False` | — |
| quiz result | `Correct answer:` | — |
| redeem interstitial | `YOU CAN NOW REDEEM!` | looks like a page, not a modal |
| product collection | `Bundle Deals` + a price | any product page |

The "empty vs loaded" distinction is the one that matters most: identical header, opposite meaning.
""",
    27: """**Observation 2026-11-20, relevant to the reward trigger (Q3).**

The operator completed both tasks by *adding* the items — no launch from them — which is the
strongest evidence yet that creation is sufficient.

Separately: tapping the widget produced a coin page variant that a plain deep-link visit did not —
it carried a `Collect` control and the task rows, with `aewidget_status=signedAndHasWaitReceiveCoins`
in the intent. So a widget visit may credit something repeatable even after the install task is
done. Worth one deliberate A/B when a task row is available: visit via widget vs via shortcut, and
compare the progress badges.
""",
    26: """**Measured 2026-11-20** — the row is `Browse surprise items` / subtitle **"Tap 3 items on this page to
earn coins"** / progress `0/2` / `+1`.

"On this page" is the ambiguity to settle: the boost list has no product tiles below the tasks
(scrolled to the bottom — the last rows are notifications/shortcut/widget), so the task's `GO`
almost certainly opens the product page rather than asking for taps on the list itself. The `0/2`
progress against "3 items" also suggests either two rounds of three taps, or a miscounted target.

Capture plan when it is available: tap `GO`, screenshot the destination, tap three visually distinct
tiles, and screenshot after each to see whether progress moves per tap or per round.
""",
}


def gh(*args: str) -> None:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr}")
    print(f"ok: gh {' '.join(args[:3])}")


def main() -> None:
    for number, body in COMMENTS.items():
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
            path = handle.name
        gh("issue", "comment", str(number), "-R", REPO, "--body-file", path)


if __name__ == "__main__":
    main()
