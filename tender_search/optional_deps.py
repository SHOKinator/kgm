"""Опциональные зависимости — импортируем один раз, флаги HAS_* используются везде."""

from __future__ import annotations

try:
    import pymorphy2
    _MORPH = pymorphy2.MorphAnalyzer()
    HAS_PYMORPHY = True
except Exception:
    _MORPH = None
    HAS_PYMORPHY = False

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:
    np = None  # type: ignore
    HAS_NUMPY = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as _sk_cos
    HAS_SKLEARN = True
except Exception:
    TfidfVectorizer = None  # type: ignore
    _sk_cos = None  # type: ignore
    HAS_SKLEARN = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except Exception:
    pdfplumber = None  # type: ignore
    HAS_PDFPLUMBER = False

# Playwright грузим лениво — только когда реально нужен браузер
# (так интерактивный поиск по локальной базе работает мгновенно). См. zakup_client.py.
