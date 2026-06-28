"""The Simulator: the single long-lived BeamNGpy connection + shared registries.

Holds ``bng``, the active scenario, the vehicle/sensor registries, and the ONE
lock that guards every BeamNGpy call (a re-entrant lock so the split-out service
modules can nest calls without self-deadlock). Lifecycle only — telemetry/tuning/
vehicle logic live in their own modules and operate on a Simulator.

Ported from v1 ``session.py`` (connect/disconnect/reconnect/status), with two
review fixes baked in: disconnect runs teardown hooks BEFORE taking the lock
(no join-under-lock deadlock), and reconnect replays the ORIGINAL launch flag
(v1 hardcoded ``launch=False``, killing a self-launched session on reconnect).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from beamngpy import BeamNGpy, Scenario, Vehicle

from ..config import SETTINGS, Settings
from ..errors import NotConnected


class Simulator:
    """Owns the connection + registries + the global BeamNGpy lock."""

    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.settings = settings
        self.bng: BeamNGpy | None = None
        self.scenario: Scenario | None = None
        self.vehicles: dict[str, Vehicle] = {}
        self.sensors: dict[str, dict] = {}
        #: Re-entrant so nested service calls under one boundary don't deadlock.
        self.lock = threading.RLock()
        self._conn: dict = {}  # effective params of the last successful connect
        self._before_disconnect: list[Callable[[], None]] = []

    # -- state ---------------------------------------------------------------
    def is_connected(self) -> bool:
        return self.bng is not None

    def require_connected(self) -> None:
        """Raise :class:`NotConnected` if there is no live session."""
        if self.bng is None:
            raise NotConnected("not connected; call connect first")

    def add_disconnect_hook(self, fn: Callable[[], None]) -> None:
        """Register a callback run (best-effort) at the start of disconnect, before
        the lock is taken — used by the timing layer to stop its worker threads."""
        self._before_disconnect.append(fn)

    # -- lifecycle -----------------------------------------------------------
    def connect(
        self,
        *,
        home: str | None = None,
        user: str | None = None,
        host: str | None = None,
        port: int | None = None,
        launch: bool = False,
    ) -> dict:
        """Attach to a running game (launch=False) or start our own (launch=True).

        ``quit_on_close`` is tied to ``launch`` so disconnecting an ATTACHED
        session leaves the user's game running. Raises on failure.
        """
        s = self.settings
        eff = {
            "home": home or s.game_home,
            "user": user or s.userpath_root,
            "host": host or s.host,
            "port": port or s.port,
            "launch": launch,
        }
        with self.lock:
            try:
                self.bng = BeamNGpy(
                    eff["host"], eff["port"],
                    home=eff["home"], user=eff["user"],
                    quit_on_close=launch,
                )
                self.bng.open(launch=launch)
                self._conn = eff
                return {
                    "connected": True,
                    "attached": not launch,
                    "host": eff["host"],
                    "port": eff["port"],
                }
            except Exception:
                self.bng = None
                raise

    def disconnect(self) -> dict:
        # Teardown hooks (timing workers) run BEFORE the lock: they join threads
        # that take this same lock, so holding it here would deadlock.
        for fn in self._before_disconnect:
            try:
                fn()
            except Exception:
                pass
        with self.lock:
            if self.bng is None:
                return {"connected": False}
            try:
                self.bng.close()
            finally:
                self.bng = None
                self.scenario = None
                self.vehicles = {}
                self.sensors = {}
            return {"connected": False}

    def reconnect(self) -> dict:
        """Close + reopen, replaying the original connection params (incl. launch)."""
        eff = dict(self._conn)
        self.disconnect()
        if eff:
            return self.connect(
                home=eff["home"], user=eff["user"],
                host=eff["host"], port=eff["port"], launch=eff["launch"],
            )
        return self.connect()

    def status(self) -> dict:
        c = self._conn
        return {
            "connected": self.is_connected(),
            "host": c.get("host", self.settings.host),
            "port": c.get("port", self.settings.port),
            "home": c.get("home", self.settings.game_home),
            "user": c.get("user", self.settings.userpath_root),
            "scenario": self.scenario.name if self.scenario is not None else None,
            "vehicles": list(self.vehicles.keys()),
        }
