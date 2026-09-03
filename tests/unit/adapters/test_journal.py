"""The JSONL journal: one object per line, and every skip recorded."""

from __future__ import annotations

import json

import pytest

from tests.conftest import make_report, make_token
from trading_desk.adapters.journal import JsonlJournal
from trading_desk.domain.verdict import AdjudicationReport, ConsensusResult


@pytest.fixture
def journal(tmp_path) -> JsonlJournal:
    """A journal writing into a temporary directory."""
    return JsonlJournal(str(tmp_path / "d.jsonl"), str(tmp_path / "x.jsonl"))


def _lines(path) -> list[dict]:
    """Read a JSONL file back into dicts. A file never written to reads as empty."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_skip_is_recorded_with_its_reason(journal):
    await journal.record_skip(make_token(), "high_risk_score", "risk_score=9")
    (record,) = _lines(journal.decisions_path)
    assert record["event"] == "skip"
    assert record["reason"] == "high_risk_score"
    assert record["token"]["symbol"] == "TEST"


async def test_entry_carries_the_full_reasoning(journal):
    result = ConsensusResult(
        action="buy", confidence=0.7, agreement_ratio=1.0,
        bull_agents=("grok",), bear_agents=(), conflict_detail="",
        reports=(make_report("grok", 0.7),),
    )
    await journal.record_entry(
        make_token(), result, 0.05, 0.65,
        AdjudicationReport(approved=True, missed_risk="thin liquidity"),
        {"trades_today": 1}, dry_run=True,
    )
    (record,) = _lines(journal.decisions_path)
    assert record["event"] == "dry_run_entry"
    assert record["adjudication"]["missed_risk"] == "thin liquidity"
    assert record["consensus"]["reports"][0]["agent"] == "grok"


async def test_disagreement_goes_to_the_second_file(journal):
    result = ConsensusResult(
        action="conflict", confidence=0.4, agreement_ratio=0.5,
        bull_agents=("grok",), bear_agents=("claude",),
        conflict_detail="claude dissents",
        reports=(make_report("grok", 0.9), make_report("claude", 0.1, error="Timeout")),
    )
    await journal.record_disagreement(make_token(), result)
    assert _lines(journal.decisions_path) == []
    (record,) = _lines(journal.disagreements_path)
    assert record["scores"] == {"grok": 0.9, "claude": 0.1}
    assert record["degraded"] == ["claude"], "a seat that failed did not really vote"


async def test_concurrent_writes_do_not_interleave(journal):
    """One lock, so parallel monitor tasks cannot produce a half-written line."""
    import asyncio

    await asyncio.gather(
        *(journal.record_skip(make_token(symbol=f"T{i}"), "test") for i in range(50))
    )
    records = _lines(journal.decisions_path)
    assert len(records) == 50
    assert {r["token"]["symbol"] for r in records} == {f"T{i}" for i in range(50)}
