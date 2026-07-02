"""Canopy-lite: overnight empirical setup optimization.

Professional teams sweep setups in lap-time simulation (Canopy et al.) because
a human driver's lap-to-lap noise buries setup deltas. We get the same effect
empirically: the game's AI is a CONSISTENT robot driver (fixed aggression =
repeatable laps), our line-crossing timer measures, the validity gate throws
out dirty laps, and a budgeted search walks the car's real tuning surface.
Consistency beats pace: a stable 90% driver separates setups better than a
fast human.
"""
