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
| `surprise_items_issue_164_scrolled_back.png` | `surprise_items` / product BACK return (#164) | the headerless scrolled grid BACK returned to in run `20261127T140626Z` (`frame-006`); no grid title or `N/3` pill is visible, only product cards and prices | status-bar clock retained; product titles and prices only; no account data |
| `search_results_issue_142.png` | `search_for_what_you_love` / results (#142) | the product-results surface after submitting `wireless phone`: query in the field, product cards, `200 sold 4.9`, `C$32.99` and `Disc:` prices | status-bar clock retained; no account nickname, balance, order, cart or address; the query is the benign test query from `SEARCH_QUERIES` |
| `recently_viewed_issue_116_entry.png` | `recently_viewed` / entry (#116) | the settled inline coin page carrying `Browse recently viewed items`, `GO`, `+1` and the coin-page markers | coin balance and coin-credit amounts masked; task rows retained |
| `recently_viewed_issue_116_destination.png` | `recently_viewed` / destination (#116) | `Recently viewed`, `Stack Coins, Save More!`, `History` / `Cart` tabs, product cards and the right-edge `4s` dwell chip | top balance and operator search text masked; product titles/prices retained; no account data |
| `recently_viewed_issue_116_destination_cleared.png` | `recently_viewed` / cleared (#116) | the same destination after the chip digits disappeared | top balance and operator search text masked; product titles/prices retained; no account data |
| `recently_viewed_issue_116_row_gone.png` | `recently_viewed` / verification (#116) | populated `Coins boost` list after the visit, without `Browse recently viewed items` | header/hero balance and coin-credit amount masked; other task rows retained |
| `boost_rotation_issue_149_first_read.png` | `boost_rotation` / first read (#149) | top of the sheet, captured 14:45:48Z from `open1.png`: `Explore sponsored items` `4/6`, `Browse surprise items` `1/2`, `Place an order on this page`, `Add 5 items to your cart` | band below the status bar masked (header balance/credit pills and the coin page behind the sheet); task rows retained; no account data |
| `boost_rotation_issue_149_open_36m.png` | `boost_rotation` / open 36m16s (#149) | same modal still open, captured 15:22:04Z from `open-late-20m.png`: same four top rows | same mask as `…_first_read.png`; no account data |
| `boost_rotation_issue_149_top.png` | `boost_rotation` / route reload (#149) | same sheet after a full shortcut reload, captured 15:23:02Z from `open-route-reload.png`: same four top rows | same mask; no account data |
| `boost_rotation_issue_149_scrolled.png` | `boost_rotation` / one scroll (#149) | same sheet after one viewport scroll, captured 15:23:08Z from `open-scroll1.png`: `Water to win a US $0.10 prize` and `Share coins with friends` exposed | same mask; no account data |
| `boost_rotation_issue_149_bottom.png` | `boost_rotation` / stable bottom (#149) | same sheet at a stable bottom, captured 15:26:41Z from `bottom-late.png`: `Browse recently viewed items`, `Add & start using our widget`, `Turn on notifications` | same mask; no account data |
| `learn_to_save_issue_159_sheet_top.png` | `learn_to_save` / sample top (#159) | top of the sheet captured 21:48:51Z from `s07-sheet-00.png`: the four refused rows and no learn-to-save row | band `0..450` masked (coin balance, coin-credit balance, coin page behind the sheet); task rows retained; no account data |
| `learn_to_save_issue_159_sheet_bottom.png` | `learn_to_save` / sample bottom (#159) | stable bottom of the same sheet captured 21:48:56Z from `s07-sheet-02.png`: `Add a coins shortcut`, `Add & start using our widget`, `Turn on notifications`, no learn-to-save row | same mask `0..450`; task rows retained; no account data |
| `recently_viewed_issue_171_claimed.png` | `recently_viewed` / claimed card (#171) | the already-claimed `Browse recently viewed items` card on a `Coins boost` sheet: title, the two-line subtitle `Coins deal on viewed, in-cart` / `items`, and its drawn check badge | band `0..450` masked (coin balance, coin-credit balance, coin page behind the sheet); task rows retained; no account data |

`search_results_issue_142.png` is the live results surface captured on 2026-11-25 while fixing the
search task. The measured `OPPO` category tap did not navigate or submit (its grey placeholder was
already in the field before the tap), so the fixture is the positive surface the executor must
recognise after a real query submission, not a category destination. The query is the benign test
query typed by the collector; no purchase control is present or tapped. The PNG is palette-quantized
from the full 1200×2000 RGB capture to stay under the 550 KB fixture budget; OCR still reads the
query, `N sold` and the prices used by `is_search_results`.

`surprise_items_issue_115.json` is sanitized report evidence from run `20261124T083006Z`, not a
screenshot. That run retained only the two balance frames: no surprise-grid PNG or OCR sidecar was
written, so the tile boxes and the OCR reading behind its zero-tap `0/3 -> 3/3` record cannot be
reconstructed truthfully. The JSON fixture preserves what the report does prove (`0/2 -> 0/2`, zero
product taps, one scroll, a falsely complete round) and its replay test covers the headerless-digit
failure mode.

`recently_viewed_issue_116.json` is sanitized evidence from the same run for issue #116. No post-tap
PNG or OCR sidecar was retained, so it preserves the serialized clock/date labels, the dispatched GO
point and the later inline row's action box. It records the limits explicitly: the requested point
matches the returned inline GO centre and the jittered point lies inside its box, while the older
boost-roster boxes had become stale after
reflow; the missing frame means neither launcher foreground ownership nor the cause of the transition
can be proven retrospectively. `states/launcher.png` is a separate recon capture used only to replay
recovery from a launcher that draws app icons.

The four `recently_viewed_issue_116_*.png` files are the real frames from the supervised observation
`20261125T221003Z`, not a runner run and not a synthetic screen. They were palette-quantized from the
full 1200×2000 captures to stay under the 550 KB budget; the masking rectangles are recorded in
`recently_viewed_issue_116_observation.json`, together with the source capture names, timestamps and
which claims are observed versus inferred. The destination and cleared frames retain the measured
`Recently viewed` header and the chip; the entry frame retains the row and its current `GO`; the
row-gone frame is the populated boost list after the visit. The old
`recently_viewed_issue_116.json` remains the evidence from run `20261124T083006Z` and deliberately
records that its launcher transition cannot be proven.

The five `boost_rotation_issue_149_*.png` files are live full-frame captures from 2026-11-26, taken
while issue #149 measured the sheet's rotation. Exact per-file provenance — capture timestamp,
source temp file, command, source and fixture SHA-256, and the claim each supports — is recorded in
[`boost_rotation_issue_149_observation.json`](boost_rotation_issue_149_observation.json). The
capture times are 14:45:48Z, 15:22:04Z, 15:23:02Z, 15:23:08Z and 15:26:41Z; the app hides the
status-bar clock while the sheet is open, so no on-frame clock is used. The first two captures are
36m16s apart (2176s); the background `sleep 1020` was started later and its launch time was not
logged, so the JSON records the exact interval from the capture timestamps and explains that the
sleep covers only the final 17 minutes. The PNG create/modify metadata
is the 16:05Z fixture-encoding pass, not the capture time; the JSON's command-log/file timestamps are
authoritative. A close/re-open capture at 15:22:10Z was byte-identical to the 15:22:04Z frame and is
deliberately not kept as a separate fixture; the JSON records the duplicate hash and command. The band below the status bar down to the
sheet header is masked with a solid rectangle, which removes the header balance/credit pills and the
coin page behind the sheet without cropping the system UI or task rows. The files are
palette-quantized for the fixture budget; no account nickname, balance, order, cart or notification
content remains.

The two `learn_to_save_issue_159_*.png` frames are live full-frame captures from the #159 sampling
window (2026-11-26 21:42:45Z–22:30:44Z), not a runner success. They show the top and stable bottom
of one fully scanned `Coins boost` sheet with no `Learn to save money with Coins Credits` row.
Exact source capture, timestamp, source/fixture SHA-256, masking rectangle and the claim each frame
supports are recorded in
[`learn_to_save_issue_159_observation.json`](learn_to_save_issue_159_observation.json). The band
`y = 0..450` is masked to remove the coin balance, coin-credit balance and the coin page behind the
sheet; no account nickname, order, cart, address or notification content remains. The same
observation JSON records all 15 sample timestamps and distinct rows, the live run
`20261126T223057Z`, and the ❓ contract claims. These frames prove absence in the measured window,
not that the family no longer exists.

`recently_viewed_issue_171_claimed.png` is the already-claimed
`Browse recently viewed items` card from the #159 sampling window
(`s01-sheet-01.png`, captured 2026-11-26 22:26:26Z). It is a full 1200×2000
frame with the band `y = 0..450` masked for the same account-data reason as the
two learn-to-save frames; the card, its two-line subtitle and its drawn check
are retained. The card's lower line ends in `in-cart items`; issue #171's test
replays this frame to prove that the card reader treats that line as the card's
subtitle rather than as the `cart` deny family.

`shortcut_visit_issue_130.json` is sanitized evidence for issue #130's below-the-fold
`Visit the page via coins shortcut` row. It records the row's two-line title, the Android
`Create shortcut` sheet opened by `GO`, the unchanged balance and still-present row after `Got it`,
after creating the shortcut and after the first shortcut URI, and the later `87 -> 88` credit that
followed launching the created `Coins` shortcut from the home screen. It deliberately does not claim
whether the launch or the later coin-page reload delivered the credit, and carries no screenshot or
account data.

`shortcut_visit_issue_158.json` is the supervised manual validation that followed #130. It records
the exact `Create shortcut` and `Add to home screen` controls, the `Shortcut has existed` toast, the
home-screen `Coins` item, the pinned shortcut's own launcher intent (with `from=shortcut` and
`shortcutId=coinsPromotion`), the later `129 -> 130` credit with the row gone, and the
`cmd shortcut clear-shortcuts` cleanup. It is explicitly not a collector run: the row was claimed by
the manual validation before the executor could record it, which is the limitation it states.

`shortcut_visit_issue_158_round2.json` records the first live run's failure (`20261127T140626Z`:
the row was tapped while the scrolled list was still moving, no shortcut sheet opened, the pinned
intent was never fired, and both balance reads were `None`) and the live collector run that closed
the acceptance (`20261127T145922Z`: `balance_delta`, `151 -> 152` for the task, the full run's
`147 -> 152` balance shots retained, the row removed from observation 5, the created pin cleared,
and `AdcActivity` resumed at the end). It states the named step failures that replaced the
unfalsifiable `None -> None`. The full-frame failure recordings remain in the runs volume, not in
git.

`surprise_items_issue_139.json` is sanitized OCR evidence from run `20261125T151001Z`'s failure
record `frame-004.jpg`. It keeps the grid header, counter, product-card titles and prices, and the
`Add To Cart` line painted inside the lower-left Hyundai card's photograph. The image itself is not
committed; the JSON carries only the text boxes and their measured coordinates, with no account
data. It is the regression fixture for telling a caption inside product imagery from page chrome.

`surprise_items_issue_115_loading_grid.png` and `surprise_items_issue_115_loaded_grid.png` are the
same `0/2` row reproduced live on 2026-11-24 after that run, while the tablet still offered the
task. They are not frames from `20261124T083006Z`; they are the supervised capture of its load-timing
gap: the first frame proves the header and `0/3` pill are readable while every product card is still
a skeleton, and the second proves the same grid paints its tiles moments later. The executor now
records each round's first grid frame and OCR sidecar under
`runs/<run-id>/shots/surprise-items-round-<n>-grid.*`.

`surprise_items_issue_164_scrolled_back.png` is a palette-quantized copy of `frame-006` from the
failure recorder's `20261127T140626Z` attempt. It proves the return shape for #164: BACK from a
product landed on the headerless scrolled grid, so the grid's title and `N/3` pill were absent
while its product cards remained. The frame has no nickname, balance, order, cart, address or
notification content; only product titles and prices are retained.

Three states in `specs/flows.yaml` have **no frame** and say so in the file, rather than being
backed by a screenshot that does not exist: `coins_quiz/result`, `coins_quiz/redeem_interstitial`
and `notifications/after_denial`. All three need a day when the quiz and the notification row are
still unclaimed; on 2026-11-22 both rows were already claimed for the app day, and the
`YOU CAN NOW REDEEM!` interstitial is one-shot per credit payout. `coins_quiz/result` is
documented by its labels in `docs/05-app-knowledge.md` §5, not by a frame.
