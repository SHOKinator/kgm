"""Логирование (только в файл, в консоль выводим через print)."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("tender")


def setup_logging(log_path: Path) -> None:
    log.setLevel(logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(fh)
