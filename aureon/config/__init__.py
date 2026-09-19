"""Configuration: environment-backed settings and session boundaries."""

from aureon.config.config import AureonConfig
from aureon.config.sessions import (
    SESSION_CONFIG_VERSION,
    SESSION_PRECEDENCE,
    SESSION_WINDOWS,
    SessionWindow,
    day_close,
    session_close,
    session_for,
)

__all__ = [
    "AureonConfig",
    "SESSION_CONFIG_VERSION",
    "SESSION_PRECEDENCE",
    "SESSION_WINDOWS",
    "SessionWindow",
    "day_close",
    "session_close",
    "session_for",
]
