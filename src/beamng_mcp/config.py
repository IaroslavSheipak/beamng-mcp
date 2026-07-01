"""Typed runtime settings: paths, host, port — all env-overridable.

Consumer-build (Steam, no BeamNG.tech) defaults, ported verbatim from v1
``pc_config.py``. Paths are Windows-style: the server runs under Windows python;
do NOT translate to POSIX. The integration socket is opened in-game with
``extensions.load('tech/techCore'); tech_techCore.openServer(25252)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_GAME_HOME = r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"
DEFAULT_USER_FOLDER = r"C:\Users\Iaroslav\AppData\Local\BeamNG\BeamNG.drive\current"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 25252


@dataclass(frozen=True)
class Settings:
    """Resolved, immutable runtime configuration."""

    game_home: str
    user_folder: str
    userpath_root: str
    host: str
    port: int
    #: Where drive/lap CSVs are written (RichLapRecorder + DriveLogger share it).
    logs_dir: str

    @property
    def user_vehicles(self) -> str:
        """Where user ``.pc`` configs live (``<user_folder>/vehicles``)."""
        return os.path.join(self.user_folder, "vehicles")

    @property
    def install_vehicles(self) -> str:
        """Stock vehicle zips (read-only): ``<game_home>/content/vehicles``."""
        return os.path.join(self.game_home, "content", "vehicles")

    @classmethod
    def from_env(cls) -> Settings:
        game_home = os.environ.get("BEAMNG_HOME", DEFAULT_GAME_HOME)
        user_folder = os.environ.get("BEAMNG_USER", DEFAULT_USER_FOLDER)
        # BeamNG's -userpath wants the PARENT of the version folder (the dir that
        # contains 'current'/version subdirs), not the version folder itself --
        # else it spins up an empty first-run profile (EULA screen). Only used
        # when we launch the game ourselves (connect launch=True).
        userpath_root = os.environ.get("BEAMNG_USERPATH", os.path.dirname(user_folder))
        host = os.environ.get("BEAMNG_HOST", DEFAULT_HOST)
        port = int(os.environ.get("BEAMNG_PORT", str(DEFAULT_PORT)))
        logs_dir = os.environ.get("BEAMNG_LOGS_DIR", os.path.join(os.getcwd(), "logs"))
        return cls(game_home, user_folder, userpath_root, host, port, logs_dir)


#: Process-wide settings, resolved from the environment at import time.
SETTINGS = Settings.from_env()
