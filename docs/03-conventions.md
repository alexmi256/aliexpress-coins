# 03 — Engineering conventions

Enforced engineering rules for this repository. Every rule below has an automated check in
`just check` or `.gitignore`; anything unverified is not a rule.

---

## Enforced invariants

| rule | why | enforced by |
|---|---|---|
| Deny list enforced **before** any executor sees a row (sealed `TaskAuthorization` token) | a task that "almost" touches an order is an order | `tests/test_tasks_dispatch.py` (`test_a_raw_row_cannot_reach_any_executor`, `test_every_denied_family_is_refused_with_its_code_and_audit_line`) |
| Unknown task labels are skipped (`unrecognised`, default-deny) | a missed coin is cheap; an unknown trial or purchase is not | `tests/test_tasks_catalogue.py`, `tests/test_tasks_dispatch.py` (`test_an_unrecognised_label_is_refused_by_default`) |
| No stored device pixels; targets come from OCR boxes or profile-calibrated popup fallbacks | blind coordinates hit product tiles ([`01-recon-findings.md`](01-recon-findings.md) §4) | `tests/test_geometry_constants.py`, `tests/test_geometry_independence.py`, `tests/test_control_controller.py`, `tests/test_control_popups.py` |
| Sweep popups after every navigation | modals are invisible to `uiautomator` | `tests/test_control_dismiss.py`, `tests/test_tasks_discovery.py` |
| One abort event per aborted run (`SafetyGuard.disarm()`) and aborts propagate through cleanup | cleanup must never overwrite or swallow a safety abort | `tests/test_runner_safety.py` (`test_disarm_stops_all_inspection_after_an_abort`), `tests/test_safety_propagation.py`, `tests/test_runner_engine.py` |
| Positive completion verification only (absence on the partial inline page is not evidence) | a false `completed` poisons daily state for the rest of the day | `tests/test_tasks_browse.py` (`test_run_browse_task_does_not_trust_row_absence_on_the_inline_coin_page`), `tests/test_tasks_signin.py` (`test_claim_daily_signins_fails_named_on_the_empty_boost_page`) |
| `completed_night` means "nothing dispatched is worth retrying", not "the roster walk ended": a retryable task failure leaves the day open even when another row completed | an all-failed night must not consume the day's retry or a manual verification run | `tests/test_runner_engine.py` (`test_all_retryable_failures_leave_the_night_open_for_a_later_run`, `test_a_mixed_night_with_one_retryable_failure_stays_open`), `tests/test_runner_state.py` (`test_a_retryable_failure_leaves_the_day_open_and_records_the_decision`) |
| Never repeat an action whose effect is unknown (`acted=True` blocks retry) | a duplicate press can double-claim or trigger modals | `tests/test_runner_engine.py` (`test_an_unverified_sign_in_is_dispatched_once_and_taps_once`, `test_acted_evidence_includes_keys_and_taps`), `tests/test_runner_failures.py` |
| Bounded retry (2 attempts) allowed only for a proven swallowed tap (unchanged frame diff logged) | unchanged settled frame is `unverified_pre_action`, not unknown side effect | `tests/test_tasks_discovery.py` (`test_open_boost_list_retaps_once_when_the_first_tap_had_no_effect`) |
| Tests never spawn a process or open a socket; time is injected (`clock` + `sleep`) | a forgotten mock must fail, not reach the real tablet | `tests/conftest.py`, `tests/test_package.py` |
| Atomic daily state and exclusive `state/run.lock` | crashes resume cleanly; manual runs cannot race the daemon | `tests/test_runner_state.py`, `tests/test_runner_lock.py` |
| Report written on every path; failed notification never fails the run | failure frames and logs must survive errors and broken webhooks | `tests/test_runner_report.py`, `tests/test_runner_notify.py`, `tests/test_runner_engine.py` |
| Screenshots, OCR dumps, `runs/`, `state/` and transcripts never enter git | they contain account nickname, orders and cart | `.gitignore` (reviewed fixture exceptions in `tests/data/README.md`) |
| Failure codes, surfaces, ADRs, links, doc reachability and quoted coverage floor stay in sync | stale docs lie to the operator | `tests/test_docs.py` |

---

## Tooling baseline

| concern | choice | note |
|---|---|---|
| language & layout | Python 3.14, `src/aliexpress_coins/` | `requires-python = ">=3.14"`, flat package, `pyproject.toml` is the sole tool config |
| environment & pins | plain `python3 -m venv .venv` + `requirements.txt` / `requirements-dev.txt` | no uv, poetry, hatch or pdm; setuptools reads `requirements.txt` |
| lint & format | ruff (`line-length = 120`, single quotes, `E501` off) | checks `src/` and `tests/` (`tools/` exempted for pre-scaffold scripts) |
| types | mypy strict over `src/` and `tests/` | `disallow_untyped_defs`, `warn_unused_ignores` (stale ignores fail) |
| tests & coverage | pytest + pytest-cov | coverage ratchet in `pyproject.toml`; never lower the floor to pass |
| gate & CI | `just check` = GitHub Actions CI | lint → format check → mypy → tests; GHCR image built on `main` (`python:3.14-slim-trixie`, non-root, `tini`, provenance + SBOM) |

---

## Testing seams

The device is simulated at two seams, never mocked ad hoc:

- **transport seam** (`aliexpress_coins.transport.AdbRunner`) — `tests/support/adb.py:ScriptedAdbRunner`
  replays scripted responses or recorded transcripts into the real clients (`AdbSnapshotter`,
  `AdbInputDevice`, `AdbDevice`, `doctor`); unscripted calls raise `AssertionError`.
- **snapshot seam** (`Snapshotter`) — `tests/support/frames.py:ScriptedSnapshotter` replays
  reviewed PNG directories (`tests/data/`) into the real controller and executors.

Row-parsing fixtures use the measured geometry (`docs/01-recon-findings.md` §7b), not invented boxes.

---

## Verification & retry semantics

- **Absence is not evidence:** a task completes only on a positive signal (progress badge advanced,
  control flipped to completed, balance delta, or row gone from a complete surface such as the
  populated `Coins boost` list). The inline coin page is a partial subset; a missing row there
  proves nothing. A false `completed` is worse than `unverified`.
- **No effect observed ≠ effect unknown:** an unchanged, settled frame after a tap proves the tap
  was swallowed (`unverified_pre_action`) and may be retried once (two attempts total) with the
  frame difference logged. Any tap whose effect may have applied (`acted=True`) is never repeated;
  only side-effect-free stages (re-reading, re-navigating) may retry.

---

## Logging, documentation & decisions of record

- **Structured events:** `run.jsonl` emits JSON events (`task_skipped`, `policy_refusal`,
  `task_outcome`, `failure_retry`, `run_aborted`, etc.) with standard Python log levels.
- **Evidence legend:** every device/app claim is marked ✅ verified / ⬜ inferred / ❓ unknown,
  citing the command or session that produced it. Raw measurements live in
  [`01-recon-findings.md`](01-recon-findings.md); open app unknowns live in
  [`05-app-knowledge.md`](05-app-knowledge.md) §8.
- **Decisions of record:** architectural decisions live in [`adr/`](adr/README.md). Product and
  operational decisions Q1–Q7 (private repo until the #41 gate, unlocked-device assumption,
  question bank, Apprise, default-deny over risk) are recorded in
  [`00-brief.md`](00-brief.md) §6–§7.
