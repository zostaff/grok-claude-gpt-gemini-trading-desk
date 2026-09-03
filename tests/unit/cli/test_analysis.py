"""Post-run analysis: reading the journals back and naming the dead weight."""

from __future__ import annotations

import json

import pytest

from trading_desk.analysis import analyse, read_jsonl


def write_jsonl(path, records) -> None:
    """Write records as JSONL."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def report(capsys, tmp_path, decisions, disagreements=()) -> str:
    """Run the analyser over the given journals and return everything it printed."""
    d, x = tmp_path / "d.jsonl", tmp_path / "x.jsonl"
    write_jsonl(d, decisions)
    write_jsonl(x, disagreements)
    analyse(str(d), str(x))
    return capsys.readouterr().out


def consensus_with(*seats) -> dict:
    """A consensus block containing one report per (agent, score, vetoed) triple."""
    return {
        "action": "skip",
        "reports": [
            {"agent": a, "quality_score": s, "vetoed": v, "latency_ms": 100, "error": None}
            for a, s, v in seats
        ],
    }


# --- reading -----------------------------------------------------------------

def test_a_missing_file_is_empty_not_an_error(tmp_path):
    assert list(read_jsonl(tmp_path / "never-written.jsonl")) == []


def test_a_malformed_line_is_skipped_not_fatal(tmp_path, capsys):
    """A truncated last line is what a killed process leaves behind; it must still read."""
    path = tmp_path / "d.jsonl"
    path.write_text('{"event": "skip"}\nnot json\n{"event": "exit"}\n')

    records = list(read_jsonl(path))

    assert [r["event"] for r in records] == ["skip", "exit"]
    assert "not valid JSON" in capsys.readouterr().out


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"event": "skip"}\n\n   \n{"event": "skip"}\n')
    assert len(list(read_jsonl(path))) == 2


# --- the report --------------------------------------------------------------

def test_an_empty_journal_says_so_instead_of_dividing_by_zero(tmp_path, capsys):
    analyse(str(tmp_path / "none.jsonl"), str(tmp_path / "none2.jsonl"))
    assert "Nothing to analyse yet" in capsys.readouterr().out


def test_skip_reasons_are_ranked(capsys, tmp_path):
    out = report(capsys, tmp_path, [
        {"event": "skip", "reason": "high_risk_score"},
        {"event": "skip", "reason": "high_risk_score"},
        {"event": "skip", "reason": "consensus_skip"},
    ])
    assert "high_risk_score" in out and "consensus_skip" in out
    assert "66.7%" in out


def test_entries_are_summarised(capsys, tmp_path):
    out = report(capsys, tmp_path, [
        {"event": "dry_run_entry", "amount_sol": 0.05, "final_confidence": 0.7},
        {"event": "dry_run_entry", "amount_sol": 0.03, "final_confidence": 0.5},
    ])
    assert "0.0800 SOL" in out, "total sized"
    assert "0.600" in out, "mean confidence"


def test_realised_pnl_and_win_rate_are_reported(capsys, tmp_path):
    out = report(capsys, tmp_path, [
        {"event": "exit", "pnl_sol": 0.02},
        {"event": "exit", "pnl_sol": -0.01},
        {"event": "exit", "pnl_sol": -0.03},
    ])
    assert "-0.0200 SOL" in out
    assert "33.3%" in out


# --- the question this module exists to answer -------------------------------

def test_a_seat_that_never_moved_an_outcome_is_named(capsys, tmp_path):
    """The whole point: an agent that never vetoes and never dissents is a cost."""
    out = report(capsys, tmp_path, [
        {"event": "skip", "reason": "consensus_skip",
         "consensus": consensus_with(("grok", 0.9, True), ("gpt", 0.5, False))},
    ])
    assert "never changed an outcome" in out
    lines = [line for line in out.splitlines() if "gpt" in line]
    assert any("never changed an outcome" in line for line in lines)


def test_a_seat_that_vetoes_is_credited(capsys, tmp_path):
    out = report(capsys, tmp_path, [
        {"event": "skip", "reason": "consensus_skip",
         "consensus": consensus_with(("claude", 0.2, True))},
    ])
    assert "vetoed 1x" in out


def test_a_dissenting_seat_is_credited_without_a_veto(capsys, tmp_path):
    out = report(
        capsys, tmp_path,
        [{"event": "skip", "reason": "conflict",
          "consensus": consensus_with(("gemini", 0.1, False))}],
        [{"action": "conflict", "bear_agents": ["gemini"], "bull_agents": [],
          "spread": 0.8, "conflict_detail": "gemini dissents", "token": {"symbol": "T"}}],
    )
    assert "no vetoes, but dissented 1x" in out


def test_degraded_seats_are_counted_separately(capsys, tmp_path):
    """A seat that fell back did not really vote; the report must not hide that."""
    out = report(capsys, tmp_path, [
        {"event": "skip", "reason": "consensus_skip", "consensus": {
            "action": "skip",
            "reports": [{"agent": "grok", "quality_score": 0.0, "vetoed": True,
                         "latency_ms": 30000, "error": "Timeout"}],
        }},
    ])
    assert "degraded" in out
    assert "grok" in out


def test_a_journal_with_no_panel_reports_says_so(capsys, tmp_path):
    out = report(capsys, tmp_path, [{"event": "skip", "reason": "high_risk_score"}])
    assert "no panel reports found" in out


def test_mean_spread_is_reported_over_disagreements(capsys, tmp_path):
    out = report(
        capsys, tmp_path,
        [{"event": "skip", "reason": "conflict"}],
        [{"action": "conflict", "spread": 0.6, "token": {"symbol": "A"}, "conflict_detail": "x"},
         {"action": "conflict", "spread": 0.4, "token": {"symbol": "B"}, "conflict_detail": "y"}],
    )
    assert "0.500" in out


@pytest.mark.parametrize("bad", [{"consensus": "not a dict"}, {"consensus": {"reports": ["x"]}}])
def test_malformed_consensus_blocks_do_not_crash_the_report(capsys, tmp_path, bad):
    report(capsys, tmp_path, [{"event": "skip", "reason": "r", **bad}])
