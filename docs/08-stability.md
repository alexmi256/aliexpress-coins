# 08 — Stability log

The ledger for the release gate in #41: **two consecutive weeks of unattended runs with no
unrecoverable failure**. This file is the evidence for that claim; the gate is not closed by
adding rows, and a row is not evidence until the run was actually unattended.

One row per run, appended after its report is final. The seed below is every run directory that
existed on the build box on 2026-11-22/23, including supervised development runs and dry runs.
All of them are on the local night of **2026-11-22 (America/Toronto)**; the run id is UTC and sorts
the table. None is an unattended night yet, and no run has completed a task yet, so this is a
ledger skeleton rather than a stability claim.

| Date (local) | Run id (UTC) | Outcome | Tasks completed | Abort condition | Run directory | Notes |
|---|---|---|---|---|---|---|
| 2026-11-22 | `20261122T130556Z` | failed | 0 | — | `aliexpress-coins_runs/20261122T130556Z` | Failed before dispatch: `Coins boost` not observed within 30 s (`StateTimeout`). |
| 2026-11-22 | `20261122T130836Z` | budget | 0 | — | `/tmp/opencode/budget-runs/20261122T130836Z` | Wall-clock budget of 1 s exhausted; no dispatch. |
| 2026-11-22 | `20261122T131125Z` | completed | 0 | — | `aliexpress-coins_runs/20261122T131125Z` | Daily check-in unverified (`StateTimeout`); no dispatchable row remained. |
| 2026-11-22 | `20261122T131508Z` | completed | 0 | — | `aecoins-schedule-demo_runs/20261122T131508Z` | Daily check-in unverified (`StateTimeout`); no dispatchable row remained. |
| 2026-11-22 | `20261122T135323Z` | budget | 0 | — | `/tmp/opencode/review-runs/20261122T135323Z` | Wall-clock budget of 1 s exhausted; no dispatch. |
| 2026-11-22 | `20261122T164738Z` | budget | 0 | — | `/tmp/opencode/ae32-runs/20261122T164738Z` | Task budget of 1 dispatch exhausted; Daily check-in unverified. |
| 2026-11-22 | `20261122T205857Z` | planned | 0 | — | `/tmp/opencode/issue20-runs/20261122T205857Z` | Dry run: 1 task planned, 2 refusals, 0 taps. |
| 2026-11-22 | `20261122T205919Z` | aborted | 0 | `dangerous_control` | `/tmp/opencode/issue20-runs/20261122T205919Z` | A `Place an order on this page` control appeared while Daily coins ran; stopped before tapping it. |
| 2026-11-22 | `20261122T210156Z` | aborted | 0 | `dangerous_control` | `/tmp/opencode/issue20-runs/20261122T210156Z` | A `Redeem` control appeared while Daily coins ran; stopped before tapping it. |
| 2026-11-22 | `20261122T210334Z` | budget | 0 | — | `/tmp/opencode/issue20-runs/20261122T210334Z` | Task budget of 1 dispatch exhausted; Daily coins unverified. |
| 2026-11-22 | `20261122T210520Z` | budget | 0 | — | `/tmp/opencode/issue20-runs/20261122T210520Z` | Task budget of 1 dispatch exhausted; Daily coins unverified. |
| 2026-11-22 | `20261122T221216Z` | planned | 0 | — | `/tmp/opencode/issue20-review-runs/20261122T221216Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T015314Z` | planned | 0 | — | `/tmp/opencode/aecoins-clean/runs/20261123T015314Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T015337Z` | planned | 0 | — | `/tmp/opencode/aecoins-clean/runs/20261123T015337Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T015402Z` | planned | 0 | — | `/tmp/opencode/aecoins-clean/runs/20261123T015402Z` | Dry run: 1 task planned, 2 refusals, 0 taps. |
| 2026-11-22 | `20261123T020321Z` | planned | 0 | — | `/tmp/opencode/aecoins-remote/runs/20261123T020321Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T020413Z` | planned | 0 | — | `/tmp/opencode/aecoins-remote/runs/20261123T020413Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T020441Z` | planned | 0 | — | `/tmp/opencode/aecoins-remote/runs/20261123T020441Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T020512Z` | planned | 0 | — | `/tmp/opencode/aecoins-remote/runs/20261123T020512Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T020625Z` | planned | 0 | — | `/tmp/opencode/aecoins-remote/runs/20261123T020625Z` | Dry run: `surface: unknown`, 0 steps (#76). |
| 2026-11-22 | `20261123T024726Z` | planned | 0 | — | `/tmp/opencode/aecoins-main-review/runs/20261123T024726Z` | Dry run: `surface: unknown`, 0 steps (#76). |

`aliexpress-coins_runs` and `aecoins-schedule-demo_runs` are Docker named volumes; the path after
the volume name is the directory inside it. The `/tmp/opencode/...` directories are supervised
development artifacts from the same box and are recorded because they were already on disk, not
because they count toward #41.

## Append rule

Each night appends **one** row on the following morning, after the report is final:

1. **Date** is the local night, from `started_at` converted to `AECOINS_TIMEZONE` (the device's
   zone), not the UTC date in the run id. Keep the UTC run id beside it for sorting.
2. **Outcome** is `status` from `summary.md` / `run.json`: `completed`, `budget`, `stopped`,
   `planned`, `aborted` or `failed`.
3. **Tasks completed** is the number of `tasks[]` rows whose `outcome` is `completed`. Do not
   count `unverified`, `skipped` or a row that merely issued taps.
4. **Abort condition** is `abort.code` when the run aborted, otherwise `—`. A task failure or a
   budget stop belongs in Notes, not in this column.
5. **Run directory** is the directory that holds `summary.md` and `run.json`: `runs/<run-id>/` in a
   checkout, or `<compose-volume>/<run-id>/` for the container.

Never rewrite an earlier row. A correction is a new note on the same row plus a link to the issue
that explains it. A run with no report is recorded as `no report` with its directory, because a
night that left no artifact is itself a stability fact. Two consecutive weeks of rows is #41's
criterion; this issue supplies the ledger, not the evidence.
