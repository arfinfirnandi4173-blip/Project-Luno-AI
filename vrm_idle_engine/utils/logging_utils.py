"""utils/logging_utils.py - a tiny shared logger factory so every module
gets consistent, timestamped console output without each one reconfiguring
the `logging` module (which would risk duplicate handlers)."""

from __future__ import annotations

import logging


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    return logger
