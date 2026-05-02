"""
Единая система поиска товаров для тендерного планирования.

Сценарии:
  1. Пользователь в консоли вводит ЕНС ТРУ или название.
  2. Сначала ищем в локальной базе data/out_with_prices.json (мгновенно).
  3. Если нет — идём на сайт zakup.sk.kz через Playwright:
     - находим лоты,
     - за один проход получаем цену + скачиваем PDF + парсим PDF,
     - сохраняем готовые записи в базу.
  4. Если и на сайте пусто — RAG-прогноз цены по похожим товарам в базе.

Запуск:
  python tender_search.py
  python tender_search.py --headful           # видеть окно браузера
  python tender_search.py --db data/out_with_prices.json --pdf-dir data/pdfs

Структура папок:
  data/
    out_with_prices.json     ← база (читается и пополняется)
    pdfs/                     ← скачанные PDF спецификаций
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ─── Кодировка консоли (Windows) ──────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─── Опциональные зависимости ─────────────────────────────────────────────────
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
    HAS_NUMPY = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as _sk_cos
    HAS_SKLEARN = True
except Exception:
    HAS_SKLEARN = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except Exception:
    HAS_PDFPLUMBER = False

# Playwright грузим лениво — только когда реально нужен браузер.
# (так интерактивный поиск по локальной базе работает мгновенно).

# ─── Константы / настройки ────────────────────────────────────────────────────
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

# ─── ANSI цвета ───────────────────────────────────────────────────────────────
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, CYAN, YELLOW = "\033[92m", "\033[96m", "\033[93m"
RED, MAGENTA, ORANGE = "\033[91m", "\033[95m", "\033[33m"

# ─── Логирование (только в файл, в консоль выводим через print) ──────────────
log = logging.getLogger("tender")


def setup_logging(log_path: Path) -> None:
    log.setLevel(logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(fh)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                              УТИЛИТЫ                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

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


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                          ПАРСИНГ PDF                                     ║
# ║            (адаптировано из parse_pdfs.py — без main)                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

PDF_RU_LABEL_TO_KEY: dict[str, str] = {
    "Номер строки":                          "nomer_stroki",
    "Наименование и краткая характеристика": "naimenovanie_kratkaya",
    "Дополнительная характеристика":         "dopolnitelnaya_harakteristika",
    "Единица измерения":                     "edinitsa_izmereniya",
    "Место поставки":                        "mesto_postavki",
    "Условия поставки":                      "usloviya_postavki",
}
PDF_RU_HEADER_DESCRIPTION = ("Наименование", "Значение")


def _pdf_norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = re.sub(r"-\s*\n\s*", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _pdf_norm_label(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.replace("-\n", "").replace("-\r\n", "")
    return _pdf_norm(s)


def _pdf_find_russian_pages(pdf) -> list[int]:
    russian: list[int] = []
    started = False
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if "ТЕХНИЧЕСКАЯ СПЕЦИФИКАЦИЯ" in text:
            started = True
        if started:
            russian.append(i)
    return russian or list(range(len(pdf.pages)))


def _pdf_extract_lot_id(text: str) -> Optional[str]:
    m = re.search(r"Лот\s*№\s*\d+\s*\(\s*[^,()]+,\s*(\d+)\s*\)", text)
    return m.group(1) if m else None


def _pdf_extract_description_table(tables: list) -> dict[str, str]:
    result: dict[str, str] = {}
    for table in tables:
        if not table or len(table[0]) < 2:
            continue
        if (_pdf_norm(table[0][0]), _pdf_norm(table[0][1])) != PDF_RU_HEADER_DESCRIPTION:
            continue
        for row in table[1:]:
            if len(row) < 2:
                continue
            label = _pdf_norm(row[0])
            value = _pdf_norm(row[1])
            key = PDF_RU_LABEL_TO_KEY.get(label)
            if key:
                result[key] = value
        break
    return result


def _pdf_extract_doc_number(tables: list) -> Optional[str]:
    doc_col_idx: Optional[int] = None
    for table in tables:
        if not table:
            continue
        normalized = [_pdf_norm_label(c) for c in table[0]]
        if "Номер документа" in normalized and "Обозначение" in normalized:
            doc_col_idx = normalized.index("Номер документа")
            for row in table[1:]:
                if len(row) > doc_col_idx and _pdf_norm(row[doc_col_idx]).isdigit():
                    return _pdf_norm(row[doc_col_idx])
            break
    if doc_col_idx is None:
        return None
    for table in tables:
        if not table:
            continue
        normalized = [_pdf_norm_label(c) for c in table[0]]
        if "Номер документа" in normalized:
            continue
        for row in table:
            if len(row) > doc_col_idx:
                val = _pdf_norm(row[doc_col_idx])
                if val.isdigit():
                    return val
    return None


def _pdf_extract_section_2(full_ru_text: str) -> str:
    m_start = re.search(r"2\.\s*Описание[^\n]*", full_ru_text)
    if not m_start:
        return ""
    body = full_ru_text[m_start.end():]
    m_end = re.search(r"\n\s*3\.\s*Технические\s+стандарты", body)
    if m_end:
        body = body[:m_end.start()]
    body = re.sub(r".*Документ сформирован порталом.*\n?", "", body)
    body = re.sub(r".*Құжат «Самұрық-Қазына».*\n?", "", body)
    paragraphs = [_pdf_norm(p) for p in re.split(r"\n\s*\n", body) if p.strip()]
    return "\n\n".join(paragraphs)


def parse_pdf_file(pdf_path: Path) -> dict[str, Any]:
    """Парсит русскую часть тех.спецификации в словарь."""
    if not HAS_PDFPLUMBER:
        return {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        ru_idxs = _pdf_find_russian_pages(pdf)
        ru_pages = [pdf.pages[i] for i in ru_idxs]
        full_ru_text = "\n".join((p.extract_text() or "") for p in ru_pages)
        all_tables: list = []
        for page in ru_pages:
            all_tables.extend(page.extract_tables() or [])
        fields = _pdf_extract_description_table(all_tables)
        return {
            "lot_id_from_pdf":               _pdf_extract_lot_id(full_ru_text),
            "nomer_stroki":                  fields.get("nomer_stroki"),
            "naimenovanie_kratkaya":         fields.get("naimenovanie_kratkaya"),
            "dopolnitelnaya_harakteristika": fields.get("dopolnitelnaya_harakteristika"),
            "edinitsa_izmereniya":           fields.get("edinitsa_izmereniya"),
            "mesto_postavki":                fields.get("mesto_postavki"),
            "usloviya_postavki":             fields.get("usloviya_postavki"),
            "opisanie_section_2":            _pdf_extract_section_2(full_ru_text),
            "nomer_dokumenta":               _pdf_extract_doc_number(all_tables),
        }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║       ИЗВЛЕЧЕНИЕ ЧИСЛОВЫХ ХАРАКТЕРИСТИК + ЛЕММАТИЗАЦИЯ                   ║
# ║                  (адаптировано из search.py)                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

STOPWORDS = {
    "для", "и", "в", "с", "на", "по", "из", "от", "до", "или", "при",
    "как", "а", "но", "да", "не", "ни", "то", "же", "бы", "ли", "что",
    "к", "за", "об", "под", "над", "между", "через", "без", "после",
    "во", "со", "это", "которые", "которая", "который",
}

UNIT_MAP: dict[str, str] = {
    "в": "В", "volt": "В", "v": "В", "вольт": "В", "вольта": "В",
    "а/ч": "А/ч", "ач": "А/ч", "а·ч": "А/ч", "а*ч": "А/ч", "а.ч": "А/ч",
    "ah": "А/ч", "ампер-час": "А/ч",
    "ма/ч": "мА/ч", "мач": "мА/ч", "mah": "мА/ч",
    "а": "А", "amp": "А", "ампер": "А",
    "вт": "Вт", "w": "Вт", "ватт": "Вт", "квт": "кВт", "kw": "кВт",
    "ква": "кВА", "va": "ВА", "ва": "ВА", "kva": "кВА",
    "гц": "Гц", "hz": "Гц", "кгц": "кГц", "мгц": "МГц",
    "мм": "мм", "мм²": "мм²", "мм2": "мм²", "см": "см", "м": "м", "км": "км",
    "г": "г", "кг": "кг", "т": "т",
    "об/мин": "об/мин", "rpm": "об/мин",
}

CRITICAL_UNITS = {"В", "А/ч", "мА/ч", "Вт", "кВт", "ВА", "кВА", "А", "Гц"}

_NUM_RANGE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[-–—]\s*(\d+(?:[.,]\d+)?)\s*"
    r"((?:мА|кВА|кВт|мм²|мм2|об/мин|А/ч|А·ч|А\*ч|А\.ч|мА/ч|кВА|ВА|кВт|кВ|[а-яёА-ЯЁa-zA-Z²/·*]+))",
    re.IGNORECASE,
)
_NUM_SINGLE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"((?:мА|кВА|кВт|мм²|мм2|об/мин|А/ч|А·ч|А\*ч|А\.ч|мА/ч|ВА|кВт|кВ|[а-яёА-ЯЁa-zA-Z²/·*]+))",
    re.IGNORECASE,
)
_THOUSANDS_SEP = re.compile(r"(\d)\s{1,2}(\d{3})(?!\d)")
_CABLE_X = re.compile(
    r"(\d+)\s*[хx×Xx*]\s*(\d+(?:[.,]\d+)?)\s*(мм²|мм2|mm²|mm2)?",
    re.IGNORECASE,
)


def _norm_unit(u: str) -> str:
    return UNIT_MAP.get(u.lower().strip().replace(" ", ""), u)


def _parse_num(s: str) -> float:
    return float(s.replace(",", "."))


def _normalize_thousands(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _THOUSANDS_SEP.sub(r"\1\2", text)
    return text


@dataclass
class NumFeat:
    value: float
    unit: str
    is_range: bool = False
    lo: float = 0.0
    hi: float = 0.0

    def match_score(self, q: float) -> float:
        if self.is_range:
            if self.lo <= q <= self.hi:
                return 1.0
            if q < self.lo:
                ratio = q / self.lo if self.lo > 0 else 0
            else:
                ratio = self.hi / q if q > 0 else 0
            return max(0.04, ratio ** 2)
        if abs(self.value) < 1e-12 and abs(q) < 1e-12:
            return 1.0
        denom = max(abs(self.value), abs(q))
        if denom < 1e-12:
            return 1.0
        return max(0.04, (min(abs(self.value), abs(q)) / denom) ** 2)


def extract_features(text: str) -> list[NumFeat]:
    text = _normalize_thousands(text or "")
    feats: list[NumFeat] = []
    seen: set = set()
    for m in _NUM_RANGE.finditer(text):
        v1, v2 = _parse_num(m.group(1)), _parse_num(m.group(2))
        unit = _norm_unit(m.group(3))
        lo, hi = min(v1, v2), max(v1, v2)
        if (lo, hi, unit) not in seen:
            seen.add((lo, hi, unit))
            feats.append(NumFeat((lo + hi) / 2, unit, True, lo, hi))
    for m in _NUM_SINGLE.finditer(text):
        if any(f.is_range and abs(f.lo - _parse_num(m.group(1))) < 1e-9 for f in feats):
            continue
        v = _parse_num(m.group(1))
        unit = _norm_unit(m.group(2))
        if (v, v, unit) not in seen:
            seen.add((v, v, unit))
            feats.append(NumFeat(v, unit))
    for m in _CABLE_X.finditer(text):
        cross = _parse_num(m.group(2))
        if 0.5 <= cross <= 1000 and (cross, cross, "мм²") not in seen:
            seen.add((cross, cross, "мм²"))
            feats.append(NumFeat(cross, "мм²"))
    return feats


def lemmatize(text: str) -> str:
    clean = re.sub(
        r"\d+(?:[.,]\d+)?(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?\s*"
        r"(?:мА|кВА|кВт|мм²|мм2|об/мин|А/ч|А·ч|А\*ч|А\.ч|мА/ч|ВА|кВт|кВ|[а-яёА-ЯЁa-zA-Z²/·*]+)?",
        " ", text or "",
    )
    tokens = re.findall(r"[а-яёА-ЯЁa-zA-Z]+", clean)
    out = []
    for tok in tokens:
        low = tok.lower()
        if low in STOPWORDS or len(low) < 2:
            continue
        if HAS_PYMORPHY and _MORPH:
            parsed = _MORPH.parse(low)
            out.append(parsed[0].normal_form if parsed else low)
        else:
            out.append(low)
    return " ".join(out)


def get_root_word(text: str) -> str:
    tokens = re.findall(r"[а-яёА-ЯЁ]{3,}", text or "")
    if not tokens:
        return ""
    if HAS_PYMORPHY and _MORPH:
        for tok in tokens:
            parsed = _MORPH.parse(tok.lower())
            if parsed and "NOUN" in str(parsed[0].tag):
                return parsed[0].normal_form
    return tokens[0].lower()


def jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                      Product / SearchResult                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@dataclass
class Product:
    """Адаптация записи из out_with_prices.json для поискового движка."""
    idx: int
    record: dict             # ссылка на оригинальную запись в базе
    name: str
    description: str
    extra: str
    unit: str
    price: float

    @property
    def full_text(self) -> str:
        return " ".join(p for p in (self.name, self.description, self.extra)
                        if p and str(p).strip() not in ("nan", "None"))

    @property
    def display_name(self) -> str:
        return " — ".join(p.strip() for p in (self.name, self.description)
                          if p and str(p).strip() not in ("nan", "None"))


@dataclass
class SearchResult:
    product: Product
    score: float
    match_type: str          # precise | range | close | category
    num_score: float
    text_score: float
    explanation: str = ""


def _record_to_product(idx: int, rec: dict) -> Optional[Product]:
    price = rec.get("prise_per_unit") or rec.get("price")
    try:
        price = float(price) if price is not None else 0.0
    except (TypeError, ValueError):
        price = 0.0
    name = (rec.get("naimenovanie_kratkaya")
            or rec.get("name_from_site") or "").strip()
    if not name or name in ("nan", "None"):
        return None
    return Product(
        idx=idx, record=rec,
        name=name,
        description=str(rec.get("dopolnitelnaya_harakteristika") or "").strip(),
        extra=str(rec.get("opisanie_section_2") or "").strip(),
        unit=str(rec.get("edinitsa_izmereniya") or "шт").strip(),
        price=price,
    )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                       SearchEngine                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class SearchEngine:
    """TF-IDF + морфология + числовые признаки + предсказание цены."""

    def __init__(self, products: list[Product]):
        self.products = products
        self._lemmas: list[str] = []
        self._feats: list[list[NumFeat]] = []
        self._roots: list[str] = []
        for p in products:
            self._lemmas.append(lemmatize(p.full_text))
            self._feats.append(extract_features(p.full_text))
            self._roots.append(get_root_word(p.name))
        self._tfidf: Optional[TfidfVectorizer] = None
        self._matrix = None
        if HAS_SKLEARN and len(products) >= 2:
            try:
                self._tfidf = TfidfVectorizer(
                    analyzer="word", min_df=1, sublinear_tf=True,
                    ngram_range=(1, 2),
                )
                self._matrix = self._tfidf.fit_transform(self._lemmas)
            except Exception:
                self._tfidf = None

    def _text_sim(self, q_lemma: str, idx: int) -> float:
        if self._tfidf is not None and self._matrix is not None:
            q_vec = self._tfidf.transform([q_lemma])
            score = float(_sk_cos(q_vec, self._matrix[idx])[0][0])
            jac = jaccard(q_lemma, self._lemmas[idx])
            return 0.7 * score + 0.3 * jac
        return jaccard(q_lemma, self._lemmas[idx])

    def _numeric_score(self, q_feats: list[NumFeat], p_idx: int):
        if not q_feats:
            return 1.0, "", 1.0
        p_feats = self._feats[p_idx]
        scores, details, critical_scores = [], [], []
        for qf in q_feats:
            cand = [pf for pf in p_feats if pf.unit == qf.unit]
            if not cand:
                sc = 0.2 if qf.unit in CRITICAL_UNITS else 0.5
                scores.append(sc)
                if qf.unit in CRITICAL_UNITS:
                    critical_scores.append(sc)
                details.append(f"нет {qf.unit}")
                continue
            best = max(cand, key=lambda pf: pf.match_score(qf.value))
            sc = best.match_score(qf.value)
            scores.append(sc)
            if qf.unit in CRITICAL_UNITS:
                critical_scores.append(sc)
            if sc >= 0.99:
                if best.is_range:
                    details.append(f"{qf.value} {qf.unit} ∈ [{best.lo}–{best.hi}]✓")
                else:
                    details.append(f"{qf.value} {qf.unit}✓")
            elif sc >= 0.5:
                ref = f"[{best.lo}–{best.hi}]" if best.is_range else str(best.value)
                details.append(f"{qf.value}≈{ref} {qf.unit}")
            else:
                ref = f"[{best.lo}–{best.hi}]" if best.is_range else str(best.value)
                details.append(f"{qf.value}✗{ref} {qf.unit}")
        log_sum = sum(math.log(max(s, 1e-6)) for s in scores)
        geo = math.exp(log_sum / len(scores))
        crit_min = min(critical_scores, default=1.0)
        if crit_min < 0.25:
            geo *= 0.4
        return geo, ", ".join(details), crit_min

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        if not self.products:
            return []
        q_lemma = lemmatize(query)
        q_feats = extract_features(query)
        q_root = get_root_word(query)

        scored = []
        for i, _ in enumerate(self.products):
            text_sc = self._text_sim(q_lemma, i)
            num_sc, detail, crit_min = self._numeric_score(q_feats, i)
            final = (0.35 * text_sc + 0.65 * num_sc) if q_feats else text_sc
            if q_root and self._roots[i] == q_root:
                final = min(1.0, final + 0.03)
            scored.append((i, final, text_sc, num_sc, detail, crit_min))

        scored.sort(key=lambda x: -x[1])
        results: list[SearchResult] = []
        seen: set = set()
        has_nums = bool(q_feats)

        for i, final, text_sc, num_sc, detail, crit_min in scored:
            if len(results) >= top_k:
                break
            if i in seen or final < 0.08:
                continue
            same_root = q_root and (self._roots[i] == q_root)
            if not has_nums:
                if text_sc >= 0.28 and same_root:
                    mtype = "precise"
                elif text_sc >= 0.18 and same_root:
                    mtype = "range"
                elif text_sc >= 0.10 and same_root:
                    mtype = "close"
                else:
                    continue
            else:
                text_ok = (text_sc >= 0.05 and same_root) or text_sc >= 0.22
                if crit_min >= 0.99 and num_sc >= 0.85 and text_ok:
                    mtype = "precise"
                elif num_sc >= 0.60 and (text_sc >= 0.12 or same_root):
                    mtype = "range"
                elif num_sc >= 0.30 and (text_sc >= 0.15 or same_root):
                    mtype = "close"
                else:
                    continue
            results.append(SearchResult(
                product=self.products[i], score=final, match_type=mtype,
                num_score=num_sc, text_score=text_sc, explanation=detail,
            ))
            seen.add(i)

        if len(results) < top_k and q_root:
            for i, final, text_sc, num_sc, detail, crit_min in scored:
                if len(results) >= top_k:
                    break
                if i in seen:
                    continue
                if self._roots[i] == q_root:
                    results.append(SearchResult(
                        product=self.products[i], score=final, match_type="category",
                        num_score=num_sc, text_score=text_sc,
                        explanation=f"Та же категория: {q_root}",
                    ))
                    seen.add(i)
        return results

    def predict_price(self, query: str):
        """Линейная регрессия по характеристикам товаров той же категории."""
        q_feats = extract_features(query)
        q_root = get_root_word(query)
        q_lemma = lemmatize(query)
        if not q_feats or not q_root:
            return None

        TEXT_SIM_THRESHOLD = 0.12
        anchors = [(self.products[i], self._feats[i])
                   for i in range(len(self.products))
                   if self._roots[i] == q_root and self.products[i].price > 0
                   and self._text_sim(q_lemma, i) >= TEXT_SIM_THRESHOLD]
        if len(anchors) < 2:
            anchors = [(self.products[i], self._feats[i])
                       for i in range(len(self.products))
                       if self._roots[i] == q_root and self.products[i].price > 0]
        if len(anchors) < 2:
            return None

        best_result = None
        best_r2 = -999.0
        for qf in q_feats:
            if qf.unit not in CRITICAL_UNITS:
                continue
            points = []
            for p, pfeats in anchors:
                pf_list = [f for f in pfeats if f.unit == qf.unit]
                if not pf_list:
                    continue
                pf = pf_list[0]
                x = qf.value if (pf.is_range and pf.lo <= qf.value <= pf.hi) else pf.value
                points.append((x, p.price))
            if len(points) < 2:
                continue
            if len(points) >= 3 and HAS_NUMPY:
                prices = sorted(pt[1] for pt in points)
                median = prices[len(prices) // 2]
                points = [pt for pt in points if median / 10 <= pt[1] <= median * 10]
            if len(points) < 2:
                continue
            points.sort(key=lambda x: x[0])
            xs = [pt[0] for pt in points]
            ys = [pt[1] for pt in points]
            if HAS_NUMPY:
                xs_np = np.array(xs, dtype=float); ys_np = np.array(ys, dtype=float)
                n, sx, sy = len(xs_np), xs_np.sum(), ys_np.sum()
                sxx = (xs_np ** 2).sum(); sxy = (xs_np * ys_np).sum()
                denom = n * sxx - sx ** 2
                if abs(denom) < 1e-12:
                    continue
                a = (n * sxy - sx * sy) / denom
                b = (sy - a * sx) / n
                pred = a * qf.value + b
                y_mean = ys_np.mean(); ss_tot = ((ys_np - y_mean) ** 2).sum()
                ss_res = ((ys_np - (a * xs_np + b)) ** 2).sum()
                r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
            else:
                x1, y1 = points[0]; x2, y2 = points[-1]
                if abs(x2 - x1) < 1e-12:
                    continue
                a = (y2 - y1) / (x2 - x1); b = y1 - a * x1
                pred = a * qf.value + b
                r2 = 0.5
            if r2 > best_r2 and pred > 0:
                best_r2 = r2
                pts_str = ", ".join(f"{x} {qf.unit}→{y:,.0f}₸" for x, y in points[:4])
                expl = (f"Регрессия по {len(points)} товарам категории "
                        f"«{q_root}» (R²={r2:.2f}) | {pts_str}")
                if a > 0:
                    expl += f" | тренд: +{a:,.0f}₸/{qf.unit}"
                best_result = (max(0.0, pred), expl)
        return best_result


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                           БАЗА ДАННЫХ                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class Database:
    """Простой JSON-store с поиском по lot_id, ЕНС ТРУ и тексту."""

    def __init__(self, path: Path):
        self.path = path
        self._records: list[dict] = []
        self._engine: Optional[SearchEngine] = None
        self._engine_dirty = True
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                self._records = data if isinstance(data, list) else []
            except Exception as e:
                log.error("Не смог прочитать базу %s: %s", self.path, e)
                self._records = []
        else:
            self._records = []
        self._engine_dirty = True
        log.info("База: %d записей", len(self._records))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
        log.info("База сохранена: %d записей", len(self._records))

    @property
    def records(self) -> list[dict]:
        return self._records

    def find_by_lot_id(self, lot_id) -> Optional[dict]:
        sid = str(lot_id)
        for r in self._records:
            if str(r.get("lot_id")) == sid:
                return r
        return None

    def find_by_tru(self, code: str) -> list[dict]:
        code = code.strip()
        return [r for r in self._records
                if str(r.get("tru_code") or "").strip() == code]

    def upsert(self, record: dict) -> None:
        """Добавляет или обновляет запись по lot_id (мерж полей)."""
        lot_id = record.get("lot_id")
        if not lot_id:
            self._records.append(record)
            self._engine_dirty = True
            return
        sid = str(lot_id)
        for i, r in enumerate(self._records):
            if str(r.get("lot_id")) == sid:
                merged = {**r}
                for k, v in record.items():
                    if v is not None and v != "":
                        merged[k] = v
                self._records[i] = merged
                self._engine_dirty = True
                return
        self._records.append(record)
        self._engine_dirty = True

    def engine(self) -> SearchEngine:
        if self._engine is None or self._engine_dirty:
            products = []
            for i, r in enumerate(self._records):
                p = _record_to_product(i, r)
                if p:
                    products.append(p)
            self._engine = SearchEngine(products)
            self._engine_dirty = False
        return self._engine


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                ZakupSession (Playwright, lazy start)                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class ZakupSession:
    """Lazy-инициализированная сессия браузера для zakup.sk.kz."""

    def __init__(self, headful: bool = False):
        self.headful = headful
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def is_started(self) -> bool:
        return self._page is not None

    def _start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            raise RuntimeError(
                "Playwright не установлен. Выполните:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
        self._PWTimeout = PWTimeout
        print(f"{DIM}[открываю браузер для zakup.sk.kz...]{RESET}")
        log.info("Запуск Playwright (headful=%s)", self.headful)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=not self.headful,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/147.0.0.0 Safari/537.36"),
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
        )
        # Блокируем тяжёлые трекеры
        def _route(route):
            url = route.request.url
            if any(d in url for d in BLOCKED_DOMAINS):
                return route.abort()
            return route.continue_()
        self._context.route("**/*", _route)

        page = self._context.new_page()
        try:
            page.goto(PORTAL_ENTRY, timeout=PAGE_LOAD_TIMEOUT_MS,
                      wait_until="domcontentloaded")
        except PWTimeout:
            pass
        try:
            page.wait_for_selector("sk-app", timeout=30000, state="attached")
        except PWTimeout:
            pass
        page.wait_for_timeout(4000)
        self._page = page
        print(f"{DIM}[браузер готов]{RESET}")
        log.info("Сессия Playwright запущена")

    def _ensure(self):
        if self._page is None:
            self._start()
        return self._page

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._pw = self._context = self._page = None

    # ─── API: поиск лотов по ЕНС ТРУ ─────────────────────────────────────────

    def search_by_tru(self, code: str) -> list[dict]:
        page = self._ensure()
        lots: list[dict] = []
        page_num = 1
        while page_num <= 50:
            target = (f"{BASE_URL}/#/ext?tabs=lot&adst=PUBLISHED&lst=PUBLISHED"
                      f"&tru={code}&page={page_num}")
            log.info("[ТРУ] страница %d: %s", page_num, target)
            try:
                page.goto("about:blank", wait_until="commit", timeout=8000)
            except Exception:
                pass
            try:
                with page.expect_response(
                    lambda r: API_LOT_SEARCH_PATH in r.url and r.request.method == "POST",
                    timeout=30000,
                ) as resp_info:
                    page.goto(target, wait_until="commit", timeout=60000)
                resp = resp_info.value
            except self._PWTimeout:
                log.error("ответ от API за 30 сек не пришёл")
                break
            if resp.status != 200:
                log.error("status=%d", resp.status)
                break
            try:
                data = resp.json()
            except Exception:
                break
            if isinstance(data, list):
                items, total_pages = data, 1
            elif isinstance(data, dict):
                items = data.get("content") or data.get("items") or []
                total_pages = data.get("totalPages") or 1
            else:
                items, total_pages = [], 1
            log.info("получено лотов: %d", len(items))
            lots.extend(items)
            if not items or page_num >= total_pages:
                break
            page_num += 1
            sleep_random()
        return lots

    # ─── API: поиск лотов по названию ────────────────────────────────────────

    def search_by_name(self, name: str) -> tuple[list[dict], Optional[str]]:
        """
        Пробует разные степени специфичности запроса (1, 2, 3 слова, потом всё)
        и фильтрует результат по совпадению названия лота. Возвращает (лоты, query).
        """
        variants = self._gen_query_variants(name)
        log.info("[NAME] варианты: %s", variants)
        last_lots: list[dict] = []
        last_query: Optional[str] = None
        for q in variants:
            try:
                lots = self._search_by_query_kwd(q)
            except Exception as e:
                log.exception("ошибка запроса '%s': %s", q, e)
                sleep_random()
                continue
            sleep_random()
            relevant = [l for l in lots if self._matches_query(l, q)]
            log.info("[NAME] '%s': API=%d, релевантных=%d", q, len(lots), len(relevant))
            if not relevant:
                continue
            if len(relevant) > MAX_LOTS_PER_NAME_QUERY:
                last_lots, last_query = relevant, q
                continue
            return relevant, q
        if last_lots:
            return last_lots, last_query
        return [], None

    def _gen_query_variants(self, name: str) -> list[str]:
        cleaned = re.sub(r"[\r\n\t]+", " ", name)
        cleaned = re.sub(r"[,;:]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return []
        words = cleaned.split(" ")
        sig = next((i for i, w in enumerate(words)
                    if len(w) >= MIN_WORD_LEN and not w.replace(".", "").isdigit()), 0)
        out = [words[sig]]
        if len(words) > sig + 1:
            out.append(" ".join(words[sig:sig + 2]))
        if len(words) > sig + 2:
            out.append(" ".join(words[sig:sig + 3]))
        if cleaned not in out:
            out.append(cleaned)
        seen, result = set(), []
        for v in out:
            if v and v not in seen:
                seen.add(v); result.append(v)
        return result

    def _matches_query(self, lot: dict, query: str) -> bool:
        name = str(lot.get("nameRu") or lot.get("name") or "").lower()
        first = query.strip().split()[0].lower() if query.strip() else ""
        if not first:
            return True
        return bool(re.search(r"\b" + re.escape(first) + r"\b", name))

    def _search_by_query_kwd(self, query: str) -> list[dict]:
        page = self._ensure()
        lots: list[dict] = []
        page_num = 1
        while page_num <= 20:
            target = (f"{BASE_URL}/#/ext?tabs=lot&adst=PUBLISHED&lst=PUBLISHED"
                      f"&q={query}&page={page_num}")
            try:
                page.goto("about:blank", wait_until="commit", timeout=8000)
            except Exception:
                pass
            try:
                with page.expect_response(
                    lambda r: API_LOT_SEARCH_PATH in r.url and r.request.method == "POST",
                    timeout=30000,
                ) as resp_info:
                    page.goto(target, wait_until="commit", timeout=60000)
                resp = resp_info.value
            except self._PWTimeout:
                break
            if resp.status != 200:
                break
            try:
                data = resp.json()
            except Exception:
                break
            if isinstance(data, list):
                items, total_pages = data, 1
            elif isinstance(data, dict):
                items = data.get("content") or data.get("items") or []
                total_pages = data.get("totalPages") or 1
            else:
                items, total_pages = [], 1
            lots.extend(items)
            if not items or page_num >= total_pages:
                break
            page_num += 1
            sleep_random()
        return lots

    # ─── API: детали лота (один проход = всё что нужно) ──────────────────────

    def fetch_lot_full_info(self, lot_id) -> Optional[dict]:
        """
        Открывает popup лота, собирает все JSON-ответы.
        Возвращает: {lot_id, name, price, count, tru_code, spec_file_uid,
                     spec_filename} или None.
        """
        page = self._ensure()
        target = f"{BASE_URL}/#/ext(popup:item/{lot_id}/lot)"
        collected = []

        def _capture(response):
            url = response.url
            if "eproc" not in url and "/api/" not in url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "application/json" not in ct:
                    return
                collected.append({"url": url, "body": response.json()})
            except Exception:
                pass

        page.on("response", _capture)
        try:
            try:
                page.goto("about:blank", wait_until="commit", timeout=8000)
            except Exception:
                pass
            try:
                with page.expect_response(
                    lambda r: ("/api/" in r.url and r.request.method == "GET"
                               and "json" in r.headers.get("content-type", "").lower()),
                    timeout=15000,
                ):
                    page.goto(target, wait_until="commit", timeout=30000)
            except self._PWTimeout:
                pass
            page.wait_for_timeout(2500)
        finally:
            try:
                page.remove_listener("response", _capture)
            except Exception:
                pass

        # Ищем основной объект лота (где есть price/lotDocuments/truHistory)
        lot_data = None
        for resp in collected:
            body = resp.get("body")
            if isinstance(body, dict) and body.get("id") == lot_id and "price" in body:
                lot_data = body
                break
        # Фолбэк: первый dict, у которого есть нужные поля
        if lot_data is None:
            for resp in collected:
                body = resp.get("body")
                if isinstance(body, dict) and "lotDocuments" in body:
                    lot_data = body
                    break
        if not lot_data:
            log.warning("Не нашёл данных лота %s в ответах", lot_id)
            return None

        # Ищем spec PDF
        spec_uid = None
        spec_filename = None
        for d in (lot_data.get("lotDocuments") or []):
            if (d.get("documentCategory") == "LOT_TECHNICAL_SPECIFICATION"
                    and d.get("fileUid")):
                spec_uid = d["fileUid"]
                spec_filename = d.get("fileName")
                break
        if not spec_uid:
            # Поищем во всех собранных JSON по всему дереву
            spec_uid, spec_filename = self._scan_for_spec(collected)

        return {
            "lot_id": lot_data.get("id") or lot_id,
            "name": lot_data.get("nameRu") or lot_data.get("name"),
            "price": lot_data.get("price"),
            "count": lot_data.get("count"),
            "tru_code": ((lot_data.get("truHistory") or {}).get("code")
                         if isinstance(lot_data.get("truHistory"), dict) else None),
            "spec_file_uid": spec_uid,
            "spec_filename": spec_filename,
        }

    @staticmethod
    def _scan_for_spec(collected: list[dict]) -> tuple[Optional[str], Optional[str]]:
        result_uid, result_name = None, None

        def walk(node):
            nonlocal result_uid, result_name
            if result_uid:
                return
            if isinstance(node, dict):
                if (node.get("documentCategory") == "LOT_TECHNICAL_SPECIFICATION"
                        and node.get("fileUid")):
                    result_uid = node["fileUid"]
                    result_name = node.get("fileName")
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        for resp in collected:
            walk(resp.get("body"))
            if result_uid:
                break
        return result_uid, result_name

    # ─── Скачивание файла через fetch внутри страницы ────────────────────────

    def download_file(self, file_uid: str, dest: Path) -> bool:
        page = self._ensure()
        url = f"{FILE_DOWNLOAD_PATH}/{file_uid}"
        script = """
        async (url) => {
            try {
                const r = await fetch(url, {method: 'GET', credentials: 'same-origin'});
                if (!r.ok) return {status: r.status, data: null};
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                return {status: r.status, data: btoa(binary), size: bytes.length};
            } catch(e) { return {status: -1, data: null, error: String(e)}; }
        }
        """
        try:
            res = page.evaluate(script, url)
        except Exception as e:
            log.error("download failed: %s", e)
            return False
        if res.get("status") != 200 or not res.get("data"):
            log.error("download status=%s, error=%s", res.get("status"), res.get("error"))
            return False
        import base64 as _b64
        data = _b64.b64decode(res["data"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                       TenderSearchSystem                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TenderSearchSystem:

    def __init__(self, db_path: Path, pdf_dir: Path,
                 headful: bool = False,
                 site_lot_limit: int = SITE_PROCESSING_LIMIT_DEFAULT):
        self.db = Database(db_path)
        self.pdf_dir = pdf_dir
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.session = ZakupSession(headful=headful)
        self.site_lot_limit = site_lot_limit

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    # ─── Обработка пользовательского запроса ─────────────────────────────────

    def query(self, user_input: str) -> None:
        user_input = user_input.strip()
        if not user_input:
            return

        is_tru = looks_like_tru(user_input)
        normalized = normalize_tru(user_input) if is_tru else user_input

        sep("Запрос: " + (
            f"{BOLD}ЕНС ТРУ {normalized}{RESET}" if is_tru
            else f"{BOLD}название «{user_input}»{RESET}"))

        # 1) Локальная база
        local = self._search_local(is_tru, normalized, user_input)
        if local:
            self._print_local_hits(local)
            return

        # 2) Сайт
        print(f"{DIM}В локальной базе ничего. Иду на zakup.sk.kz...{RESET}")
        new_records = self._search_site_and_ingest(is_tru, normalized, user_input)
        if new_records:
            self._print_new_records(new_records, is_tru)
            return

        # 3) RAG-прогноз
        print(f"{DIM}На сайте тоже пусто. Прогнозирую цену по похожим товарам...{RESET}")
        self._predict_and_print(user_input)

    # ─── (1) Локальный поиск ─────────────────────────────────────────────────

    def _search_local(self, is_tru: bool, normalized: str, user_input: str):
        if is_tru:
            recs = self.db.find_by_tru(normalized)
            return [(r, None) for r in recs] if recs else []
        engine = self.db.engine()
        results = engine.search(user_input, top_k=10)
        return [(r.product.record, r) for r in results] if results else []

    def _print_local_hits(self, hits: list[tuple[dict, Optional[SearchResult]]]):
        print(f"{GREEN}{BOLD}✓ Найдено в локальной базе ({len(hits)}){RESET}\n")
        for i, (rec, sr) in enumerate(hits, 1):
            self._print_record(i, rec, sr=sr)

    # ─── (2) Поиск на сайте + парсинг ───────────────────────────────────────

    def _search_site_and_ingest(self, is_tru: bool, normalized: str,
                                user_input: str) -> list[dict]:
        try:
            if is_tru:
                lot_summaries = self.session.search_by_tru(normalized)
                used_query = None
            else:
                lot_summaries, used_query = self.session.search_by_name(user_input)
        except Exception as e:
            print(f"{RED}Ошибка обращения к сайту: {e}{RESET}")
            log.exception("site search failed")
            return []

        if not lot_summaries:
            return []

        if used_query:
            print(f"{DIM}Найдено {len(lot_summaries)} лотов "
                  f"(использован запрос: «{used_query}»){RESET}")
        else:
            print(f"{DIM}Найдено {len(lot_summaries)} лотов{RESET}")

        # Не подтягиваем больше site_lot_limit штук за один пользовательский запрос,
        # чтобы не зависнуть надолго. Остальные уже попадут в базу при следующих
        # запросах при совпадении.
        if len(lot_summaries) > self.site_lot_limit:
            print(f"{DIM}Обрабатываю первые {self.site_lot_limit} (для скорости){RESET}")
            lot_summaries = lot_summaries[: self.site_lot_limit]

        new_records: list[dict] = []
        for i, lot in enumerate(lot_summaries, 1):
            lot_id = lot.get("id") or lot.get("lotId") or lot.get("number")
            if not lot_id:
                continue
            short_name = str(lot.get("nameRu") or lot.get("name") or "")[:60]
            print(f"  [{i}/{len(lot_summaries)}] лот {lot_id} — {short_name}", flush=True)
            try:
                rec = self._process_lot(lot_id, fallback_tru=normalized if is_tru else None)
            except Exception as e:
                print(f"      {RED}ошибка: {e}{RESET}")
                log.exception("process_lot failed")
                continue
            if rec:
                self.db.upsert(rec)
                new_records.append(rec)
            sleep_random()

        if new_records:
            self.db.save()

        return new_records

    def _process_lot(self, lot_id, fallback_tru: Optional[str] = None) -> Optional[dict]:
        """За один заход: детали лота → PDF → парсинг → готовая запись."""
        info = self.session.fetch_lot_full_info(lot_id)
        if info is None:
            return None

        # Скачиваем PDF (если есть)
        pdf_data: dict[str, Any] = {}
        pdf_filename: Optional[str] = None
        spec_uid = info.get("spec_file_uid")
        if spec_uid:
            raw_name = info.get("spec_filename") or f"Lot_{lot_id}.pdf"
            fname = safe_filename(raw_name)
            if not fname.lower().endswith(".pdf"):
                fname += ".pdf"
            dest = self.pdf_dir / f"lot_{lot_id}_{fname}"
            ok = self.session.download_file(spec_uid, dest)
            if ok:
                pdf_filename = dest.name
                if HAS_PDFPLUMBER:
                    try:
                        pdf_data = parse_pdf_file(dest)
                    except Exception as e:
                        log.warning("PDF parse failed for %s: %s", dest, e)
                        pdf_data = {}
                else:
                    log.warning("pdfplumber не установлен — PDF не разобран")

        record = {
            "file": pdf_filename,
            "lot_id": info.get("lot_id") or lot_id,
            "tru_code": info.get("tru_code") or fallback_tru,
            "name_from_site": info.get("name"),
            "prise_per_unit": info.get("price"),
            "count": info.get("count"),
            "nomer_stroki":                  pdf_data.get("nomer_stroki"),
            "naimenovanie_kratkaya":         pdf_data.get("naimenovanie_kratkaya")
                                              or info.get("name"),
            "dopolnitelnaya_harakteristika": pdf_data.get("dopolnitelnaya_harakteristika"),
            "edinitsa_izmereniya":           pdf_data.get("edinitsa_izmereniya"),
            "mesto_postavki":                pdf_data.get("mesto_postavki"),
            "usloviya_postavki":             pdf_data.get("usloviya_postavki"),
            "opisanie_section_2":            pdf_data.get("opisanie_section_2"),
            "nomer_dokumenta":               pdf_data.get("nomer_dokumenta"),
        }
        return record

    def _print_new_records(self, records: list[dict], is_tru: bool):
        print(f"\n{GREEN}{BOLD}✓ Спарсено с zakup.sk.kz ({len(records)}). "
              f"Добавлено в базу.{RESET}\n")
        for i, rec in enumerate(records, 1):
            self._print_record(i, rec, source="SITE")

    # ─── (3) RAG-прогноз ─────────────────────────────────────────────────────

    def _predict_and_print(self, user_input: str):
        engine = self.db.engine()
        # Покажем 5 ближайших похожих товаров
        results = engine.search(user_input, top_k=5)
        prediction = engine.predict_price(user_input)
        if not results and not prediction:
            print(f"{YELLOW}Не нашлось похожих товаров для прогноза.{RESET}")
            return
        if results:
            print(f"\n{BOLD}▌ Похожие товары (по локальной базе):{RESET}\n")
            for i, r in enumerate(results, 1):
                self._print_record(i, r.product.record, sr=r)
        if prediction:
            price, expl = prediction
            print(f"\n{BOLD}▌ Прогноз цены:{RESET}")
            print(f"  {MAGENTA}{BOLD}≈ {fmt_price(price)}{RESET}")
            print(f"  {DIM}{expl}{RESET}\n")
        else:
            print(f"{DIM}Регрессионный прогноз невозможен "
                  f"(мало точек или нет числовых характеристик).{RESET}\n")

    # ─── Вывод одной записи ──────────────────────────────────────────────────

    def _print_record(self, rank: int, rec: dict,
                      sr: Optional[SearchResult] = None,
                      source: str = "LOCAL"):
        name = (rec.get("naimenovanie_kratkaya")
                or rec.get("name_from_site") or "—").strip()
        desc = (rec.get("dopolnitelnaya_harakteristika") or "").strip()
        unit = rec.get("edinitsa_izmereniya") or "шт"
        price = rec.get("prise_per_unit")
        lot_id = rec.get("lot_id")
        tru = rec.get("tru_code") or "—"
        nomer = rec.get("nomer_stroki") or "—"
        nomer_doc = rec.get("nomer_dokumenta") or "—"
        place = rec.get("mesto_postavki") or "—"
        terms = rec.get("usloviya_postavki") or "—"

        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None

        print(f"  {BOLD}{rank}. {name}{RESET}")
        if desc:
            print(f"      {desc}")
        meta = []
        if lot_id:
            meta.append(f"lot={lot_id}")
        if tru and tru != "—":
            meta.append(f"ТРУ={tru}")
        if nomer != "—":
            meta.append(f"строка={nomer}")
        if nomer_doc != "—":
            meta.append(f"док={nomer_doc}")
        if meta:
            print(f"      {DIM}{' · '.join(meta)}{RESET}")
        if price_f is not None:
            print(f"      Цена за единицу: {BOLD}{fmt_price(price_f)}{RESET} / {unit}")
        else:
            print(f"      Цена: {DIM}не указана{RESET}")
        if place != "—":
            print(f"      {DIM}место: {place[:80]}{RESET}")
        if terms != "—":
            print(f"      {DIM}условия: {terms}{RESET}")
        if sr is not None:
            mtype_label = {
                "precise": f"{GREEN}ТОЧНОЕ{RESET}",
                "range":   f"{CYAN}ДИАПАЗОН{RESET}",
                "close":   f"{YELLOW}БЛИЗКОЕ{RESET}",
                "category":f"{ORANGE}КАТЕГОРИЯ{RESET}",
            }.get(sr.match_type, sr.match_type)
            print(f"      [{mtype_label}] релевантность {sr.score:.0%} "
                  f"(текст {sr.text_score:.0%}, числа {sr.num_score:.0%})")
            if sr.explanation:
                print(f"      {DIM}{sr.explanation}{RESET}")
        if source == "SITE" and rec.get("file"):
            print(f"      {DIM}PDF: {rec['file']}{RESET}")
        print()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                              CLI / main                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def sep(title: str = "") -> None:
    line = "─" * 72
    if title:
        print(f"\n{line}\n{title}\n{line}")
    else:
        print(f"\n{line}\n")


def print_intro(db_size: int):
    print(f"\n{BOLD}════════ Tender Search System ════════{RESET}")
    print(f"  Локальная база: {db_size} записей")
    if not HAS_PYMORPHY:
        print(f"  {YELLOW}⚠ pymorphy2 не установлен — морфология отключена{RESET}")
    if not HAS_SKLEARN:
        print(f"  {YELLOW}⚠ scikit-learn не установлен — Jaccard вместо TF-IDF{RESET}")
    if not HAS_PDFPLUMBER:
        print(f"  {YELLOW}⚠ pdfplumber не установлен — PDF не будет разобран{RESET}")
    print()
    print(f"  Введите {BOLD}ЕНС ТРУ{RESET} (формат XXXXXX.XXX.XXXXXX) "
          f"или {BOLD}название{RESET} товара.")
    print(f"  Команды: {DIM}stats{RESET} — статистика, "
          f"{DIM}reload{RESET} — перечитать базу, "
          f"{DIM}exit{RESET} — выход.\n")


def print_stats(system: TenderSearchSystem) -> None:
    recs = system.db.records
    total = len(recs)
    with_price = sum(1 for r in recs if r.get("prise_per_unit"))
    with_tru = sum(1 for r in recs if r.get("tru_code"))
    with_pdf = sum(1 for r in recs if r.get("file"))
    print(f"\n{BOLD}Статистика базы:{RESET}")
    print(f"  всего записей:           {total}")
    print(f"  с ценой (prise_per_unit): {with_price}")
    print(f"  с кодом ЕНС ТРУ:         {with_tru}")
    print(f"  со ссылкой на PDF:       {with_pdf}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("data/out_with_prices.json"),
                    help="Путь к JSON-базе (она же — место для пополнения)")
    ap.add_argument("--pdf-dir", type=Path, default=Path("data/pdfs"),
                    help="Куда складывать скачиваемые PDF")
    ap.add_argument("--log", type=Path, default=Path("data/tender_search.log"),
                    help="Файл лога (служебный)")
    ap.add_argument("--headful", action="store_true",
                    help="Показывать окно браузера (для отладки)")
    ap.add_argument("--site-limit", type=int, default=SITE_PROCESSING_LIMIT_DEFAULT,
                    help=f"Сколько лотов обрабатывать с сайта за один запрос "
                         f"(default: {SITE_PROCESSING_LIMIT_DEFAULT})")
    args = ap.parse_args()

    setup_logging(args.log)

    system = TenderSearchSystem(
        db_path=args.db, pdf_dir=args.pdf_dir,
        headful=args.headful, site_lot_limit=args.site_limit,
    )
    print_intro(len(system.db.records))

    try:
        while True:
            try:
                q = input(f"{BOLD}>{RESET} ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if not q:
                continue
            low = q.lower()
            if low in ("exit", "quit", "q", "выход", "й"):
                break
            if low in ("stats", "stat", "статистика"):
                print_stats(system)
                continue
            if low in ("reload", "перезагрузка"):
                system.db.load()
                print(f"{DIM}База перечитана: {len(system.db.records)} записей{RESET}\n")
                continue
            if low in ("help", "помощь", "?"):
                print_intro(len(system.db.records))
                continue
            try:
                system.query(q)
            except Exception as e:
                print(f"{RED}Ошибка обработки запроса: {e}{RESET}")
                log.exception("query failed")
    finally:
        system.close()
        print("Выход.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
