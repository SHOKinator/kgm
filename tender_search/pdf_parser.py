"""
Парсинг PDF технической спецификации лота (русская часть).
Адаптировано из parse_pdfs.py — независим от text_features (никакой лемматизации).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .optional_deps import HAS_PDFPLUMBER, pdfplumber

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
