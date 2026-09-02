"""Post-run analysis: which model dissents, which vetoes fire, and where the tokens went.

Run it after a session to see whether the four-model panel is actually earning its cost:

    python -m src.analysis --log logs/trades.jsonl --conflicts logs/conflicts.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterator

MODELS = ("grok", "claude", "gpt", "gemini")


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield each parseable object from a JSONL file, skipping corrupt lines."""
    if not path.is_file():
        print(f"[analysis] no such file: {path}")
        return
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"[analysis] skipping corrupt line {path}:{lineno}")
                continue
            if isinstance(record, dict):
                yield record


def _bar(value: float, width: int = 24) -> str:
    """A fixed-width ASCII bar for a 0-1 value."""
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "#" * filled + "." * (width - filled)


def analyse_trades(records: list[dict]) -> None:
    """Summarise outcomes: what was skipped and why, what was entered, what it cost."""
    events = Counter(r.get("event", "?") for r in records)
    print("\n=== decisions ===")
    for event, count in events.most_common():
        print(f"  {event:<16} {count}")

    skips = [r for r in records if r.get("event") == "skip"]
    if skips:
        print("\n=== skip reasons ===")
        reasons = Counter(r.get("reason", "?") for r in skips)
        total = sum(reasons.values())
        for reason, count in reasons.most_common():
            print(f"  {reason:<26} {count:>5}  {count / total:>6.1%}  {_bar(count / total)}")

    entries = [r for r in records if r.get("event", "").endswith("entry")]
    if entries:
        sizes = [float(r.get("amount_sol", 0.0)) for r in entries]
        confs = [float(r.get("final_confidence", 0.0)) for r in entries]
        print("\n=== entries ===")
        print(f"  count            {len(entries)}")
        print(f"  total size       {sum(sizes):.4f} SOL")
        print(f"  mean size        {statistics.fmean(sizes):.4f} SOL")
        print(f"  mean confidence  {statistics.fmean(confs):.3f}")
        adjustments = [
            float(r.get("checker", {}).get("confidence_adjustment", 0.0)) for r in entries
        ]
        if adjustments:
            print(f"  mean checker adj {statistics.fmean(adjustments):+.3f}")

    exits = [r for r in records if r.get("event") == "exit"]
    if exits:
        pnls = [float(r.get("pnl_sol", 0.0)) for r in exits]
        wins = [p for p in pnls if p > 0]
        print("\n=== exits ===")
        print(f"  count      {len(exits)}")
        print(f"  total pnl  {sum(pnls):+.4f} SOL")
        print(f"  win rate   {len(wins) / len(pnls):.1%}")


def analyse_conflicts(records: list[dict]) -> None:
    """Report per-model scoring behaviour, dissent frequency and latency."""
    if not records:
        print("\n[analysis] no conflicts logged")
        return

    scores: dict[str, list[float]] = defaultdict(list)
    latencies: dict[str, list[int]] = defaultdict(list)
    bull_counts: Counter[str] = Counter()
    bear_counts: Counter[str] = Counter()
    dissent: Counter[str] = Counter()
    veto_counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()

    for record in records:
        for model, score in (record.get("scores") or {}).items():
            scores[model].append(float(score))
        for model, ms in (record.get("latencies_ms") or {}).items():
            latencies[model].append(int(ms))
        bull_counts.update(record.get("bull_models") or [])
        bear_counts.update(record.get("bear_models") or [])

        detail = str(record.get("conflict_detail", ""))
        if "hard veto ->" in detail:
            for model in MODELS:
                if f"{model}:" in detail:
                    veto_counts[model] += 1
        for model in MODELS:
            if f"{model} is the" in detail:
                dissent[model] += 1
        for model, raw in (record.get("raw") or {}).items():
            if isinstance(raw, dict) and raw.get("error"):
                errors[f"{model}:{raw['error']}"] += 1

    print(f"\n=== model behaviour over {len(records)} disagreements ===")
    header = f"  {'model':<8} {'n':>4} {'mean':>6} {'stdev':>6} {'bull':>5} {'bear':>5} " \
             f"{'veto':>5} {'dissent':>8} {'p50 ms':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for model in MODELS:
        values = scores.get(model, [])
        if not values:
            continue
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        lat = latencies.get(model, [])
        p50 = int(statistics.median(lat)) if lat else 0
        print(
            f"  {model:<8} {len(values):>4} {statistics.fmean(values):>6.3f} {stdev:>6.3f} "
            f"{bull_counts[model]:>5} {bear_counts[model]:>5} {veto_counts[model]:>5} "
            f"{dissent[model]:>8} {p50:>8}"
        )

    if errors:
        print("\n=== agent failures ===")
        for key, count in errors.most_common():
            print(f"  {key:<32} {count}")

    print("\n=== reading this ===")
    print("  A model that is never the dissenter is not adding information: it is")
    print("  agreeing with the majority and costing you an API call. A model whose")
    print("  stdev is near zero is not discriminating between tokens at all.")


def main() -> None:
    """Entry point for `python -m src.analysis`."""
    parser = argparse.ArgumentParser(description="Analyse a pipeline run's JSONL logs.")
    parser.add_argument("--log", default="logs/trades.jsonl")
    parser.add_argument("--conflicts", default="logs/conflicts.jsonl")
    args = parser.parse_args()

    trades = list(read_jsonl(Path(args.log)))
    conflicts = list(read_jsonl(Path(args.conflicts)))
    print(f"[analysis] {len(trades)} decision records, {len(conflicts)} conflict records")
    analyse_trades(trades)
    analyse_conflicts(conflicts)


if __name__ == "__main__":
    main()
