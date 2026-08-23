"""
Классификация Переплата/Ниже рынка/Нормально — перенесено из
tender_claude_code/price_engine.py (_set_status).

Используется только в батч-режиме Excel (excel_export.py), т.к. в локальной
JSON-базе нет отдельного поля "победившая цена тендера", с которой можно было бы
сравнивать найденную рыночную цену в интерактивном REPL.
"""

from __future__ import annotations

from typing import Optional

from .config import OVERPAYMENT_THRESHOLD, UNDERPAYMENT_THRESHOLD

NO_DATA = "Нет данных"
OVERPAID = "Переплата"
UNDERPAID = "Ниже рынка"
NORMAL = "Нормально"


def classify_price_status(actual_price: Optional[float], market_price: Optional[float]) -> str:
    """Сравнивает фактическую (тендерную) цену с найденной рыночной."""
    if market_price is None or not actual_price or actual_price <= 0:
        return NO_DATA
    ratio = actual_price / market_price
    if ratio > 1 + OVERPAYMENT_THRESHOLD:
        return OVERPAID
    if ratio < 1 - UNDERPAYMENT_THRESHOLD:
        return UNDERPAID
    return NORMAL
