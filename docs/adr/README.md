# Architecture decision records

Every decision that changes the shape of the build lives here, one file per decision, named
`NNNN-short-title.md`. An ADR records the context, the decision, its reasons and its consequences;
it is not a changelog. A decision that is replaced gets a new ADR, and the old one is marked
superseded rather than rewritten.

The index below is checked by `tests/test_docs.py`: a new file in this directory that is not listed
here fails `just check`. Keep the summary to one line so the index stays scannable.

| ADR | status | decision in one line |
|---|---|---|
| [0001 — Drive the UI, not the API](0001-drive-the-ui-not-the-api.md) | accepted (2026-11-19) | Build the screenshot-driven UI path first; adopt an API path only if a capture proves the award calls are replayable without a fresh signature. |
| [0002 — Screenshot + OCR is the primary perception channel](0002-screenshot-ocr-perception.md) | accepted (2026-11-19) | Read the coins page and its popups from OCR boxes, using the accessibility tree only where it actually exposes native chrome. |

## Adding an ADR

1. Copy the shape of an existing record: title, status and revisit trigger, context, decision,
   reasons, consequences.
2. Number it with the next four-digit prefix; never reuse a number.
3. Add one row to the table above in the same commit.
4. If the decision supersedes another, mark the old status as superseded and link the replacement.
