"""The simulator boundary: everything that talks to BeamNG.drive / BeamNGpy.

Ported verbatim from v1 (verified-correct against BeamNGpy 1.35.1 and the Steam
consumer build) and covered by contract tests. The undocumented, hard-won
integration knowledge lives here; redesign happens in ``analysis`` and ``timing``,
not here.
"""
