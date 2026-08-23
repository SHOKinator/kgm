"""Продукт / результат поиска / прогноз цены."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .units import normalize_unit


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


@dataclass
class PricePrediction:
    """Результат SearchEngine.predict_price() — прогноз цены + оценка уверенности."""
    price: float
    explanation: str
    confidence_pct: float    # 0-100
    r2: float
    n_anchors: int


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
        unit=normalize_unit(str(rec.get("edinitsa_izmereniya") or "шт").strip()),
        price=price,
    )
