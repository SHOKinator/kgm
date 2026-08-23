"""Мелкие утилиты общего назначения."""

from __future__ import annotations

import random
import re
import time
from typing import Optional

from .config import MIN_DELAY_SEC, MAX_DELAY_SEC

_TRU_DOTTED = re.compile(r"^\d{6}\.\d{3}\.\d{6}$")
_TRU_DIGITS = re.compile(r"^\d{15}$")


def looks_like_tru(s: str) -> bool:
    """Похожа ли строка на код ЕНС ТРУ."""
    s = s.strip()
    return bool(_TRU_DOTTED.match(s) or _TRU_DIGITS.match(s))


def normalize_tru(s: str) -> str:
    """262415.000.000004 ← оставляем как есть; 262415000000004 → 262415.000.000004"""
    s = s.strip()
    if "." in s:
        return s
    digits = re.sub(r"\D", "", s)
    if len(digits) == 15:
        return f"{digits[:6]}.{digits[6:9]}.{digits[9:]}"
    return s


def safe_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE)
    return s[:max_len] or "unnamed"


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "—"
    return f"{p:,.2f} ₸".replace(",", " ").replace(".00 ", " ")


def sleep_random(lo: float = MIN_DELAY_SEC, hi: float = MAX_DELAY_SEC) -> None:
    time.sleep(random.uniform(lo, hi))
