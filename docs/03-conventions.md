# 03 — Engineering conventions

The conventions the build must follow, decided before the first line of code so they are not
negotiated later. Each one exists because of something measured on the device or a failure mode
this kind of project hits.

Nothing here describes code that exists yet — this repo is the plan.

---

## Non-negotiables

| rule | why |
|---|---|
| The deny list is enforced **before** a task executor sees a row | a task that "almost" touches an order is an order |
| No raw coordinate taps outside a documented popup fallback | a blind three-tap sequence during recon landed on a product page |
| A task counts as completed only if the screen agrees (balance, progress badge, row gone) | "the taps were sent" is how a run reports ten successes and earns nothing |
| Sweep popups after **every** navigation | the modal is invisible to the accessibility tree; the next tap silently hits the scrim |
| Never `su -c` on this device | there is no `su` binary; use `adb root` (a plain shell is `uid=2000`) |
| Screenshots and OCR dumps never enter git | they contain the account nickname, orders and cart |
| Tests never spawn a process or open a socket | a test that reaches the real phone "passes" while testing nothing |
| Unknown task labels are skipped, not attempted | default-deny is the only safe default |

---

## Tooling baseline

| concern | choice | note |
|---|---|---|
| language | Python 3.14 | `requires-python = ">=3.14"`; 3.13 support is not attempted |
| layout | `src/aliexpress_coins/` | flat package import, no implicit namespace tricks |
| environment | `.venv` created by the `just` recipe | never a bare `python`/`python3` in a recipe |
| dependencies | `requirements.txt` (runtime) + `requirements-dev.txt` (`-r requirements.txt` on top) | every line pinned, with a comment saying what it is for |
| lint/format | ruff, line length 120, single quotes | the formatter owns wrapping, so `E501` is off |
| types | mypy, `disallow_untyped_defs`, `warn_unused_ignores` | green on `src/` **and** `tests/`; a stale `# type: ignore` fails the build |
| tests | pytest + pytest-cov | coverage is a ratchet: 85% starting floor on the stub, raised to just below the measured number as modules land, never lowered to pass |
| task runner | `just` | `just check` must equal what CI runs |
| container | `python:3.14-slim-trixie` + distribution packages (`adb`, `tzdata`, opencv's `libgl*`) | nothing compiled from source |
| CI | GitHub Actions, latest majors | lint → format check → mypy → tests; image built and pushed to GHCR on every merge to `main` |
| image hygiene | non-root user, `tini` as PID 1, provenance + SBOM | the collector runs unattended, so a clean SIGTERM matters |

## Testing strategy

The device is simulated at one of two seams, never mocked ad hoc:

- **transport seam** — a scripted `adb` runner feeds a real client, so argv construction,
  output parsing and error paths are genuinely exercised;
- **snapshot seam** — a scripted snapshotter feeds a real controller, so waits, taps and popup
  logic run against a frame sequence.

Two autouse fixtures must remain: one refuses any socket connect, one refuses any process
spawn. Without them a forgotten mock can *succeed* against the live phone.

Time is injected (`clock` + `sleep`), because `wait_until_stable` and countdowns otherwise make
the suite take minutes and flake under load.

Row-parsing fixtures use the **measured** coins-page geometry (label at x≈180, its `GO` button
at x≈980 and ~31 px lower). If a fixture is invented rather than measured, a real layout change
looks like a test bug.

## Verification: absence is not evidence

A task is `completed` only when a **positive** signal says so: the progress badge advanced, the row's action
flipped to a completed state, or the row disappeared from a surface that is *known to be complete*.

Two surfaces are complete and can be trusted for a "gone" verdict: the populated boost list, and a page the
`Surface` classifier positively identifies. The **inline coin page is a partial subset** — rows come and go
with the variant and the entry route — so a missing row there proves nothing on its own.

Corollary, learned three times over (#23 streak scoping, #24 row-gone, the #23 interstitial): **a false
`completed` is worse than `unverified`.** `unverified` costs one night and retries tomorrow; a false
`completed` is recorded in daily state and silently skips the task for the rest of the day.

### No effect observed is not the same as effect unknown

The rule above forbids repeating an action whose effect is unknown. It does **not** forbid repeating an action
whose effect the screen proves did not happen: a tap followed by an unchanged, settled frame is evidence that
nothing occurred, which is the `unverified_pre_action` case, not `unverified_post_action`.

So a swallowed tap may be retried — bounded (two attempts), with the frame difference recorded as the evidence,
and a log line naming why the retry was safe. Without that distinction, a device that intermittently ignores the
first tap fails a task that a single re-tap would have completed, and the retry rule gets blamed for it.

## Retries: never repeat an unknown action

A retry may re-read the screen, re-navigate, or re-open a page. It must **never** repeat an action whose effect
is unknown but possibly already applied. A tap that has been sent is not re-sent, because the app's reaction to a
second press — "already collected", a duplicate claim, a second order — is a *new* side effect rather than a
second attempt at the first.

So a failure is retryable only when its stage can be re-entered without repeating a side effect. Where an
executor cannot prove what an action did, it reports `unverified` and the run moves on: a missed claim costs one
night, a double claim costs the day.

## Logging and reporting

- Structured events, not sentences: `task_skipped` with `reason` is groupable; a prose line is not.
- One run = one directory: `runs/<UTC timestamp>/` with `summary.md`, `run.json`, `shots/*.png`
  and each frame's OCR sidecar, plus `run.jsonl` for the process log.
- A run writes its report on **every** path, including failure. On the failure path the final
  screenshot is best-effort: it must never mask the original error.
- A run that silently stops earning is the main unattended failure mode; the notification hook
  exists for that, and a failed notification must never fail the run.

## Documentation rules

- Every claim about the device is marked ✅ verified / ⬜ inferred / ❓ unknown, with the command
  that produced it. An inference presented as a measurement is worse than no document.
- Numbers that came from a device session (geometry, timings, label positions) are recorded in
  the recon findings, not scattered through the code.
- A superseded decision is replaced, not appended to; ADRs record why, not the history of drafts.
- No document names a file that does not exist. The plan describes the target layout explicitly,
  so paths are marked as *planned* until the corresponding issue lands.
