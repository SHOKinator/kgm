"""Простой JSON-store с поиском по lot_id, ЕНС ТРУ и текстовым индексом."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .logging_setup import log
from .models import _record_to_product
from .search_engine import SearchEngine


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
