# SPEC-005 — Adjudication

## Responsibility

Cross-examine a panel that has already voted to buy, and stop the trade if the four
accounts contradict each other.

This seat exists because **consensus among language models is much weaker evidence than it
looks.** Four models agreeing is not four independent confirmations: they share training
data and they share blind spots. The adjudicator is asked the one question none of the
panel was asked — do these four stories fit together?

## Contract

Implements `Adjudicator`: `review(context, result) -> AdjudicationReport`, `aclose()`.

1. **It only ever sees a buy.** Consensus runs first; a skip never reaches it. That halves
   the calls and keeps the prompt honest — it is arguing against a decision, not making one.
2. **It holds an absolute veto**, and `confidence_adjustment` is clamped to `[-0.5, +0.2]`.
   An unclamped adjustment would let one call overwhelm the panel in either direction.
3. **It fails closed.** If the call errors, refuses or returns unparseable output, the
   result is `approved=False`. The entire point of this seat is to stand between a
   confident panel and a bad trade; if it could not run, that thing was not there.
4. **It is told which seats were degraded.** A seat that fell back to pessimistic scores
   did not really vote, and the prompt asks explicitly whether the buy depends on one.

It runs at a higher effort (`adjudicator_effort`, default `xhigh`) than the panel, because
finding what four models missed is strictly harder than scoring a launch.

## Configuration

`models.claude`, `models.adjudicator_effort`, `vetoes.min_final_confidence`.

## Verification

`tests/unit/app/test_pipeline.py` — veto overrides the panel; a negative adjustment can
sink a marginal buy; confidence exactly at the floor still trades (`< floor` skips,
`== floor` proceeds — pinned deliberately, since an off-by-one here silently changes how
much the adjudicator may move before a trade dies).

**Not covered:** the prompt's actual behaviour. Whether this seat catches real
contradictions is an empirical question that needs a live run and a labelled set.
