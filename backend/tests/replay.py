"""Совместимость: проигрыватель записи теперь живёт в app/replay.py."""
from app.replay import *  # noqa: F401,F403
from app.replay import DUMMY, RECORDINGS, FrozenDateTime, frozen_datetime_module, install, load, record_tool  # noqa: F401
