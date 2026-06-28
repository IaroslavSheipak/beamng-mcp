"""Lap analysis — the redesign.

Where v1's analysis lost trust (live findings): a 400 m crash-loop with a full
stop read as the "best lap"; a 16.9 g wall impact poisoned the grip envelope; the
balance index sat pinned at +1.0. This package fixes each at the source —
validity gating, impact-spike rejection, and a balance metric that's trustworthy
or honestly null — and every metric carries a confidence/validity flag.
"""
