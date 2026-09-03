# SPEC-008 — The decision record

## Responsibility

Record what was decided and why, in a form that can be read back and argued with.

## Contract

Implements `DecisionJournal`: `record_entry`, `record_skip`, `record_exit`,
`record_disagreement`.

1. **Every skip is recorded, not just the buys.** The value of a four-model panel is in the
   record of what it refused and why. A journal of only entries cannot answer whether the
   panel earned its cost.
2. **One JSON object per line, append-only.** Written through `asyncio.to_thread` behind a
   single lock, so concurrent monitor tasks cannot interleave a half-written line.
3. **Disagreements go to a second file** with every seat's score, summary, latency and
   degraded flag — the record `analyse` reads to ask which seat ever changed an outcome.
4. **A degraded seat is marked as such.** A seat that fell back did not really vote, and
   the disagreement record says so explicitly.

## Configuration

`journal.decisions_path`, `journal.disagreements_path`.

## Verification

`tests/unit/adapters/test_journal.py` — skip reasons round-trip, entries carry the full
reasoning, disagreements land in the second file with the degraded seat flagged, and 50
concurrent writes produce 50 intact lines.
