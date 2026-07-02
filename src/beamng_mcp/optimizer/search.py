"""Search strategy: baseline -> Latin-hypercube seeding -> coordinate descent.

Chosen for a NOISY, EXPENSIVE objective (one eval = minutes of robot laps,
lap-time noise ~tenths): LHS covers the space without a model, coordinate
descent then refines the best point one lever at a time — which is also the
pit-wall ethos ("one change at a time") made mechanical, so the ledger stays
human-readable. Pure stdlib, deterministic under a seed, no game imports —
fully unit-testable against synthetic objectives.

An eval's objective is the median VALID lap time in seconds (lower is better);
``None`` means the config failed to produce enough valid laps and is treated
as arbitrarily bad.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .space import Param

#: A candidate must beat the incumbent by more than this to be "better"
#: (lap-time noise floor; below it, differences are luck, not setup).
MIN_GAIN_S = 0.10
#: Coordinate-descent step as a fraction of each param's span.
CD_STEP0 = 0.20
#: Step halves after a full no-improvement cycle; sweep is "converged" below this.
CD_STEP_MIN = 0.04


@dataclass
class Eval:
    """One ledger entry: a tried config and what it scored."""

    vars: dict
    objective: float | None  # median valid lap time, s; None = failed config


@dataclass
class Strategy:
    """Call :meth:`propose` with the history so far; ``None`` means done."""

    space: list[Param]
    budget: int                      # total evals including the baseline
    seed: int = 7
    lhs_evals: int = 0               # 0 -> auto (about a third of the budget)
    _rng: random.Random = field(init=False, repr=False)
    _lhs: list[dict] = field(init=False, repr=False)
    _cd_step: float = field(default=CD_STEP0, init=False)
    _cd_param: int = field(default=0, init=False)
    _cd_dir: int = field(default=+1, init=False)
    _cd_flat: int = field(default=0, init=False)   # moves since last improvement
    _pending: dict | None = field(default=None, init=False)
    _incumbent: Eval | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        n = self.lhs_evals or max(3, min(self.budget // 3, 4 * len(self.space)))
        n = min(n, max(0, self.budget - 1))
        self._lhs = self._latin_hypercube(n)

    # -- seeding ---------------------------------------------------------------
    def _latin_hypercube(self, n: int) -> list[dict]:
        if n <= 0 or not self.space:
            return []
        columns: list[list[float]] = []
        for _p in self.space:
            strata = [(i + self._rng.random()) / n for i in range(n)]
            self._rng.shuffle(strata)
            columns.append(strata)
        return [
            {p.var: p.lo + columns[j][i] * p.span
             for j, p in enumerate(self.space)}
            for i in range(n)
        ]

    # -- the loop ---------------------------------------------------------------
    def best(self, history: list[Eval]) -> Eval | None:
        scored = [e for e in history if e.objective is not None]
        return min(scored, key=lambda e: e.objective) if scored else None

    def propose(self, history: list[Eval]) -> dict | None:
        """Next config to try, given every eval so far (baseline included)."""
        if len(history) >= self.budget:
            return None
        if not history:
            return {p.var: p.start for p in self.space}   # eval 0: the baseline
        if len(history) <= len(self._lhs):
            return dict(self._lhs[len(history) - 1])
        return self._coordinate_descent(history)

    def _coordinate_descent(self, history: list[Eval]) -> dict | None:
        best = self.best(history)
        if best is None:
            return None  # nothing ever scored — the runner's abort rail handles it

        # Resolve the pending probe: did the last eval beat the incumbent?
        last = history[-1]
        if self._pending is not None and last.vars == self._pending:
            improved = (
                last.objective is not None
                and (self._incumbent is None or self._incumbent.objective is None
                     or last.objective < self._incumbent.objective - MIN_GAIN_S)
            )
            if improved:
                self._cd_flat = 0
            else:
                self._cd_flat += 1
                self._advance_pointer()
        self._incumbent = best
        self._pending = None

        # A full flat cycle (every param, both directions) -> halve the step.
        if self._cd_flat >= 2 * len(self.space):
            self._cd_step /= 2.0
            self._cd_flat = 0
            if self._cd_step < CD_STEP_MIN:
                return None  # converged before the budget — stop honestly

        for _ in range(2 * len(self.space)):
            p = self.space[self._cd_param]
            cur = float(best.vars.get(p.var, p.start))
            cand = p.clamp(cur + self._cd_dir * self._cd_step * p.span)
            if abs(cand - cur) > 1e-9:
                out = dict(best.vars)
                out[p.var] = cand
                if not self._already_tried(out, history):
                    self._pending = out
                    return out
            self._cd_flat += 1
            self._advance_pointer()
        return None  # every direction pinned/tried at this step

    def _advance_pointer(self) -> None:
        if self._cd_dir == +1:
            self._cd_dir = -1
        else:
            self._cd_dir = +1
            self._cd_param = (self._cd_param + 1) % len(self.space)

    @staticmethod
    def _already_tried(cand: dict, history: list[Eval]) -> bool:
        for e in history:
            if all(abs(float(e.vars.get(k, 0)) - v) < 1e-9 for k, v in cand.items()) \
                    and len(e.vars) == len(cand):
                return True
        return False
