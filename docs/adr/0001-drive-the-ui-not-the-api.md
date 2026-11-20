# ADR 0001 — Drive the UI, not the API

**Status:** accepted (2026-11-19) · **Revisit when:** a capture shows the coin awards are
plain, replayable HTTP calls.

## Context

The coin programme is an app feature with a server behind it. If the endpoints that award
coins are simple HTTP calls, a script could post them directly: no screenshots, no OCR, no
coordinate drift, seconds per night instead of minutes.

The signs that this is possible are real — the app is hybrid, the container is handed URLs, and
the widget already reads server-side sign-in state.

## Decision

Build the **UI path first**. Keep the API path as a time-boxed spike
(tracked as an issue), and only adopt it if the capture proves the calls are replayable
without a fresh signature, device attestation or per-request nonce.

## Reasons

1. **Observability.** A UI run produces screenshots and a balance delta a human can check.
   A replayed HTTP call that silently stops working looks identical to one that worked.
2. **Survivability.** A UI path breaks visibly — a label changes, a button moves — and the run
   report shows exactly where. A signature change breaks it invisibly (HTTP 200, no coins).
3. **Effort.** The hard part (popups, canvas pages, no accessibility tree) is unavoidable for
   the tasks that genuinely need a page visit; the API path would not remove it for those.
4. **Blast radius.** Driving the UI cannot accidentally issue a purchase the way a
   misconstructed POST can; the deny list is enforced against what is on screen.

## Consequences

* Every task needs a perception story (labels, waits, verification), so the project is bigger.
* Drift is a permanent maintenance cost: the app updates, labels change, and the project needs
  a re-capture.
* If the spike succeeds, the two paths can coexist: API for awards, UI for anything that
  requires a real page visit.
