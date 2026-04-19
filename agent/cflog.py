"""Centralized logging with rotating file handler."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup(name="agent", log_dir=None, level=logging.INFO):
    """Configure root logger with rotating file + console output."""
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
