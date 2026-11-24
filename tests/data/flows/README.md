# Flow fixtures

One full 1200×2000 frame per **captured** state of the three flows from issue #10 —
`specs/flows.yaml` is the state machine, these are the frames a reviewer can check it against.
Three states were not captured and have no file (listed at the end of this README).
Every file is exactly the shape `AdbSnapshotter.capture()` returns: the status-bar clock and the
Android taskbar are retained, so a state is tested on the frame a run actually sees. On the
boost-sheet frames the clock sits behind the sheet and is dimmed by its scrim exactly as it is on
the live screen; the mask starts below the status bar, so the system UI is not painted over.

Account data is removed by masking the specific pixels, never by cropping. Masked here: the coin
balance and coin-credit balance in both header pills and in the coin page behind the boost sheet.
No frame contains an account nickname, order, cart, address or notification text. Product prices
and product titles are kept: they are the content of the surprise-items page, and the reviewed
`states/surprise_grid.png` keeps them for the same reason.

The files are indexed PNGs (64-colour palette, no dither) to stay under the ~300 KB fixture
budget; dimensions are unchanged.

**Review rule:** a file here must contain no account nickname, coin balance, coin-credit balance,
order, cart, address or notification content. Any new image added here must be reviewed the same
way before it is committed. `.gitignore` has one negation for `tests/data/flows/*.png`; that
exception exists only because this directory is reviewed as a whole, and is not permission to drop
arbitrary screenshots anywhere else in the tree.

| file | flow / state | what it is | redaction note |
|---|---|---|---|
| `surprise_items_entry_row.png` | `surprise_items` / entry | `Coins boost` sheet with the `Browse surprise items` row, badge `1/2`, `GO`, and the rows around it | header balance/credit pills and the coin page's balance behind the sheet masked |
| `surprise_items_round_grid.png` | `surprise_items` / `round_grid` | `Browse surprise items` page, counter `0/3`, three hollow tick placeholders, two-column product grid | no account data; prices and titles kept |
| `surprise_items_item_detail.png` | `surprise_items` / `item_detail` | product page opened from a tile (`Item 1/6`, `Add to cart`, `Buy now`) | no account data |
| `surprise_items_progress_1of3.png` | `surprise_items` / `round_grid` | grid after one credited product: `1/3`, first tick filled | no account data |
| `surprise_items_progress_2of3.png` | `surprise_items` / `round_grid` | grid after two credited products: `2/3`, two ticks filled | no account data |
| `surprise_items_round_complete.png` | `surprise_items` / `round_complete` | grid at `3/3`, three ticks, no `Next round` (task's second and final round) | no account data |
| `surprise_items_mission_completed.png` | `surprise_items` / `mission_completed` | the `Mission Completed` coin overlay over the dimmed grid | no account data |
| `surprise_items_row_gone.png` | `surprise_items` / `row_gone` | same boost sheet after BACK: the row is gone and `Search for what you love` has taken its slot | header balance/credit pills and the coin page's balance behind the sheet masked |
| `notifications_row_completed.png` | `notifications` / `entry` (completed variant) | `Turn on notifications` row with the drawn check control, after its reward was already paid with the OS permission still `ignore` | header balance/credit pills and the coin page's balance behind the sheet masked |
| `coins_quiz_entry_row.png` | `coins_quiz` / entry (completed variant) | `Take the coins quiz` row with the drawn check control where `GO` sits; labels and reward line unchanged | header balance/credit pills and the coin page's balance behind the sheet masked |
| `coins_quiz_question.png` | `coins_quiz` / question | `Coins quiz` page with a four-option question (`Q : Which statements about coins credits are correct?`) | reused from the reviewed `states/quiz.png`; no account data |
| `coins_quiz_exit_coin_page.png` | `coins_quiz` / coin page | the coin page the flow returns to (`Daily coins`, `Coins credits to redeem`) | reused from the reviewed `states/coins.png`; balances masked there |
| `notifications_os_settings.png` | `notifications` / `os_settings` | OS page the app's `Allow phone notifications` toggle lands on: `All AliExpress notifications` off, "You haven't allowed notifications from this app" | no account data; app name only |
| `notifications_after_grant.png` | `notifications` / `after_grant` | same OS page after the permission is granted: switch on, `Categories` list, `Allow notification dot` | no account data |
| `surprise_items_issue_115_loading_grid.png` | `surprise_items` / opening load (#115) | `Browse surprise items` header and `0/3` pill over empty skeleton cards; `find_surprise_tiles` returns none | status-bar clock retained; no account data |
| `surprise_items_issue_115_loaded_grid.png` | `surprise_items` / settled grid (#115) | the same `0/3` grid a moment later, with product cards, titles and prices painted | status-bar clock retained; product titles and prices only; no account data |

`surprise_items_issue_115.json` is sanitized report evidence from run `20261124T083006Z`, not a
screenshot. That run retained only the two balance frames: no surprise-grid PNG or OCR sidecar was
written, so the tile boxes and the OCR reading behind its zero-tap `0/3 -> 3/3` record cannot be
reconstructed truthfully. The JSON fixture preserves what the report does prove (`0/2 -> 0/2`, zero
product taps, one scroll, a falsely complete round) and its replay test covers the headerless-digit
failure mode.

`surprise_items_issue_115_loading_grid.png` and `surprise_items_issue_115_loaded_grid.png` are the
same `0/2` row reproduced live on 2026-11-24 after that run, while the tablet still offered the
task. They are not frames from `20261124T083006Z`; they are the supervised capture of its load-timing
gap: the first frame proves the header and `0/3` pill are readable while every product card is still
a skeleton, and the second proves the same grid paints its tiles moments later. The executor now
records each round's first grid frame and OCR sidecar under
`runs/<run-id>/shots/surprise-items-round-<n>-grid.*`.

Three states in `specs/flows.yaml` have **no frame** and say so in the file, rather than being
backed by a screenshot that does not exist: `coins_quiz/result`, `coins_quiz/redeem_interstitial`
and `notifications/after_denial`. All three need a day when the quiz and the notification row are
still unclaimed; on 2026-11-22 both rows were already claimed for the app day, and the
`YOU CAN NOW REDEEM!` interstitial is one-shot per credit payout. `coins_quiz/result` is
documented by its labels in `docs/05-app-knowledge.md` §5, not by a frame.
