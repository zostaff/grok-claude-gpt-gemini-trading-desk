"""Post-run analysis: did any seat on the panel ever change an outcome?

This is the module that makes the whole design falsifiable. Four models are only worth
four API calls if they disagree in ways that matter -- so the report below is built to
show the opposite when it is true: a seat that never dissents, never vetoes and never
moves a decision is an expensive fifth opinion, and this will say so.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

BAR_WIDTH = 24


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield records from a JSONL file, skipping malformed lines rather than dying."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"  ! {path.name}:{line_no} is not valid JSON, skipped")
                continue
            if isinstance(record, dict):
                yield record


def _bar(value: float, width: int = BAR_WIDTH) -> str:
    """A fixed-width unicode bar for a 0-1 value."""
    filled = int(max(0.0, min(1.0, value)) * width)
    return "█" * filled + "·" * (width - filled)


def _section(title: str) -> None:
    """Print a section header."""
    print(f"\n{title}\n{'─' * len(title)}")


def analyse_decisions(records: list[dict]) -> None:
    """Summarise what the pipeline did and why it declined what it declined."""
    events = Counter(r.get("event", "?") for r in records)
    skips = Counter(r.get("reason", "?") for r in records if r.get("event") == "skip")
    entries = [r for r in records if r.get("event", "").endswith("entry")]

    _section("Outcomes")
    total = sum(events.values())
    for event, count in events.most_common():
        print(f"  {event:<22} {count:>6}  {_bar(count / total if total else 0)}")

    if skips:
        _section("Why launches were declined")
        skip_total = sum(skips.values())
        for reason, count in skips.most_common():
            share = count / skip_total
            print(f"  {reason:<26} {count:>6}  {share:>6.1%}  {_bar(share)}")

    if entries:
        _section("Entries")
        sizes = [r.get("amount_sol", 0.0) for r in entries]
        confs = [r.get("final_confidence", 0.0) for r in entries]
        print(f"  count            {len(entries)}")
        print(f"  total sized      {sum(sizes):.4f} SOL")
        print(f"  mean size        {sum(sizes) / len(sizes):.4f} SOL")
        print(f"  mean confidence  {sum(confs) / len(confs):.3f}")

    exits = [r for r in records if r.get("event") == "exit"]
    if exits:
        pnl = sum(r.get("pnl_sol", 0.0) for r in exits)
        wins = sum(1 for r in exits if r.get("pnl_sol", 0.0) > 0)
        _section("Exits")
        print(f"  count            {len(exits)}")
        print(f"  realised PnL     {pnl:+.4f} SOL")
        print(f"  win rate         {wins / len(exits):.1%}")


def analyse_panel(records: list[dict], disagreements: list[dict]) -> None:
    """Per-seat behaviour: who vetoes, who dissents, who is dead weight."""
    scores: dict[str, list[float]] = defaultdict(list)
    latencies: dict[str, list[int]] = defaultdict(list)
    vetoes: Counter[str] = Counter()
    degraded: Counter[str] = Counter()
    seen: Counter[str] = Counter()

    for record in records:
        consensus = record.get("consensus")
        if not isinstance(consensus, dict):
            continue
        for report in consensus.get("reports", []):
            if not isinstance(report, dict):
                continue
            agent = report.get("agent", "?")
            seen[agent] += 1
            scores[agent].append(float(report.get("quality_score", 0.0)))
            latencies[agent].append(int(report.get("latency_ms", 0)))
            if report.get("vetoed"):
                vetoes[agent] += 1
            if report.get("error"):
                degraded[agent] += 1

    if not seen:
        print("\n  (no panel reports found -- run the pipeline first)")
        return

    _section("Panel seats")
    print(f"  {'seat':<14}{'evals':>7}{'mean':>8}{'vetoes':>9}{'degraded':>10}{'p50 ms':>9}")
    for agent in sorted(seen, key=lambda a: -seen[a]):
        values = scores[agent]
        mean = sum(values) / len(values) if values else 0.0
        lat = sorted(latencies[agent])
        p50 = lat[len(lat) // 2] if lat else 0
        print(
            f"  {agent:<14}{seen[agent]:>7}{mean:>8.3f}"
            f"{vetoes[agent]:>9}{degraded[agent]:>10}{p50:>9}"
        )

    _section("Did each seat earn its call?")
    for agent in sorted(seen):
        moved = vetoes[agent]
        dissents = sum(
            1 for d in disagreements
            if agent in (d.get("bear_agents") or []) or agent in (d.get("bull_agents") or [])
        )
        if moved == 0 and dissents == 0:
            verdict = "never changed an outcome -- candidate for removal"
        elif moved == 0:
            verdict = f"no vetoes, but dissented {dissents}x"
        else:
            verdict = f"vetoed {moved}x, dissented {dissents}x"
        print(f"  {agent:<14} {verdict}")


def analyse_disagreements(records: list[dict]) -> None:
    """Who argues with whom, and about what."""
    if not records:
        return
    _section("Disagreements")
    actions = Counter(r.get("action", "?") for r in records)
    for action, count in actions.most_common():
        print(f"  {action:<14} {count:>5}")

    spreads = [r.get("spread", 0.0) for r in records if isinstance(r.get("spread"), (int, float))]
    if spreads:
        print(f"  mean spread    {sum(spreads) / len(spreads):.3f}")

    print("\n  Most recent:")
    for record in records[-5:]:
        token = record.get("token", {})
        print(f"    {token.get('symbol', '?'):<10} {record.get('conflict_detail', '')[:96]}")


def analyse(decisions_path: str, disagreements_path: str) -> None:
    """Read both journals and print the full report."""
    decisions = list(read_jsonl(Path(decisions_path)))
    disagreements = list(read_jsonl(Path(disagreements_path)))

    print(f"\nRead {len(decisions)} decisions and {len(disagreements)} disagreements.")
    if not decisions:
        print("Nothing to analyse yet. Run the pipeline in dry-run first.")
        return

    analyse_decisions(decisions)
    analyse_panel(decisions, disagreements)
    analyse_disagreements(disagreements)
    print()
