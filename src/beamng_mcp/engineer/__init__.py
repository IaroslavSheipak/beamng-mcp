"""The AI race-engineer brain (pure stdlib, no game).

knowledge: the symptom -> cause -> remedy matrix + lever/$var specs (ported
verbatim from v1 engineer_kb — the review praised its motorsport grounding and
sign-monotonicity selftests).
advisor:   the orchestrator — driver words + a v2 lap report -> a ranked,
clamped $var setup plan + a pit-wall brief. Adapted to consume v2's report shape.
"""
