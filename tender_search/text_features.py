"""
Извлечение числовых характеристик из текста + лемматизация/морфология.
UNIT_MAP здесь — единицы физических величин ("220 В", "12 А·ч"), используются для
сопоставления характеристик товара. Это НЕ то же самое, что units.py (единицы
закупки вроде "шт"/"кг" для отображения) — совмещать их не нужно, задачи разные.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .optional_deps import HAS_PYMORPHY, _MORPH

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
