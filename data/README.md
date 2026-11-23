# Quiz question bank

`quiz_bank.json` is the committed, human-editable record of quiz questions and
the answer each one turned out to have. The executor reads it at the start of a
quiz visit and answers a recognised question from the recorded correct option,
matched by text against the options on screen. It never taps a remembered
position.

The file is data, not code: adding an entry needs no release. A first encounter
learns it from the result page (`source: "result_page"`), and a person may add
or correct an entry by hand (`source: "manual"`). A manual entry stays
authoritative while its recorded answer is still one of the question's options;
when the options change, the result page replaces it.

```json
{
  "schema": 1,
  "questions": [
    {
      "key": "<sha256 of the normalised question>",
      "question": "Q : Which of the following statements is correct?",
      "normalized": "q which 0f the f0110wing statements is c0rrect",
      "options": ["True", "False"],
      "chosen": "False",
      "correct": "True",
      "source": "result_page",
      "seen_at": "2026-11-22T12:00:00+00:00"
    }
  ]
}
```

`question` is the raw OCR text a human reads; `key` and `normalized` are derived
from it and are never displayed. The derivation, in order: NFKC, case-fold, fold
the digit-confusables **towards the digit** (`l`→`1`, `o`→`0`), strip punctuation,
collapse whitespace. `key` is the SHA-256 of `normalized`.

The fold direction is worth stating plainly, because it is what makes the example
above look corrupted when it is not: a clean `following` normalises to
`f0110wing`, exactly as an OCR'd `f0110wing` does. That is the point — one entry,
however the glyph was read, instead of a new entry on every visit. The cost is
that strings differing only by `l`/`1` or `o`/`0` collapse together; `match_option`
shares the same normalisation, so an option set containing both `10` and `lO`
cannot be distinguished (no such set has been observed on this surface).

Entries are sorted by `key`, and replaying an unchanged observation does not rewrite the
file. Questions and options only: no account data belongs here.

## Container persistence

The committed `quiz_bank.json` is the seed. `compose.yaml` mounts the named
volume `data` at `/app/data`; Docker fills that volume from the image's `data/`
directory the first time it is created. After that, entries learned at runtime
live in the volume and survive container restarts, image rebuilds and
`docker compose up --build`.

A changed committed bank does not automatically replace an existing volume. To
copy a learned bank back into the repository, with the collector container
running:

```sh
docker compose cp collector:/app/data/quiz_bank.json data/quiz_bank.json
```

To publish a committed bank into an existing volume:

```sh
docker compose cp data/quiz_bank.json collector:/app/data/quiz_bank.json
```

Manual rebuild check: let a run learn an entry, run `docker compose up -d
--build`, then confirm it is still present with `docker compose exec collector
cat /app/data/quiz_bank.json`. Only `docker compose down -v` removes the volume
and its learned entries.
