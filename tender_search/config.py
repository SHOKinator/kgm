"""Все настраиваемые константы проекта в одном месте."""

from __future__ import annotations

# ─── zakup.sk.kz ───────────────────────────────────────────────────────────────
BASE_URL = "https://zakup.sk.kz"
PORTAL_ENTRY = f"{BASE_URL}/#/ext"
API_LOT_SEARCH_PATH = "/eprocsearch/api/external/lots/filter"
API_LOT_PATH = "/eprocsearch/api/external/lots/{lot_id}"
FILE_DOWNLOAD_PATH = "/eprocfilestorage/open-api/files/download"

MIN_DELAY_SEC = 1.5
MAX_DELAY_SEC = 3.0
PAGE_LOAD_TIMEOUT_MS = 90000
REQUEST_TIMEOUT_MS = 25000

BLOCKED_DOMAINS = [
    "googletagmanager.com", "google-analytics.com", "connect.facebook.net",
    "facebook.com", "mc.yandex.ru", "mc.yandex.com", "gstatic.com/recaptcha",
    "google.com/recaptcha", "googleadservices.com", "doubleclick.net",
]

# Если по слову нашлось > порога лотов на сайте — слово слишком общее
MAX_LOTS_PER_NAME_QUERY = 50
MIN_WORD_LEN = 3

# Ограничение на число лотов, которые подтягиваем с сайта за один пользовательский запрос
SITE_PROCESSING_LIMIT_DEFAULT = 10

# ─── ANSI цвета ─────────────────────────────────────────────────────────────────
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, CYAN, YELLOW = "\033[92m", "\033[96m", "\033[93m"
RED, MAGENTA, ORANGE = "\033[91m", "\033[95m", "\033[33m"

# ─── Статус Переплата/Ниже рынка/Нормально (перенесено из tender_claude_code) ──
OVERPAYMENT_THRESHOLD = 0.15    # факт.цена > рыночной на 15% → Переплата
UNDERPAYMENT_THRESHOLD = 0.20   # факт.цена < рыночной на 20% → подозрительно дёшево

# ─── Веса confidence-score для predict_price() ────────────────────────────────
# Нет LLM/URL-сигналов (в отличие от tender_claude_code), поэтому вес распределён
# между числом точек-якорей, качеством регрессии (R²) и согласованностью цен.
WEIGHT_PRED_ANCHOR_COUNT = 0.35
WEIGHT_PRED_R2 = 0.35
WEIGHT_PRED_CONSISTENCY = 0.30

# ─── Excel batch-режим ──────────────────────────────────────────────────────────
EXCEL_FILLS = {
    "Переплата":  "FFCDD2",   # светло-красный
    "Нормально":  "C8E6C9",   # светло-зелёный
    "Ниже рынка": "BBDEFB",   # светло-синий
    "Нет данных": "F5F5F5",   # светло-серый
}
EXCEL_HEADER_FILL = "1565C0"
EXCEL_HEADER_FONT_COLOR = "FFFFFF"

# Дефолтные номера колонок (1-based) во входном xlsx — переопределяются флагами CLI,
# т.к. готового эталонного файла для батч-режима пока нет.
DEFAULT_COL_NAME = 4
DEFAULT_COL_UNIT = 7
DEFAULT_COL_ACTUAL_PRICE = 15

NEW_EXCEL_HEADERS = [
    "Рыночная цена (тг)",
    "Мин. цена (тг)",
    "Макс. цена (тг)",
    "Источник",
    "Статус",
    "Уверенность (%)",
]
