"""Ingest: recorder CSV -> typed Samples. Reuses the recorder's single CSV reader
so there is exactly one parser for the lap format.
"""

from __future__ import annotations

from ..timing.recorder import read_lap_csv
from .model import Sample, sample_from_row


def samples_from_rows(rows: list[dict]) -> list[Sample]:
    return [sample_from_row(r) for r in rows]


def load_lap(path: str) -> list[Sample]:
    """Load + parse a recorded lap CSV into Samples (empty list if unreadable)."""
    return samples_from_rows(read_lap_csv(path))
