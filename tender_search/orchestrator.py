"""TenderSearchSystem — оркестрирует 3-уровневый поиск: локальная база →
сайт zakup.sk.kz → RAG-прогноз цены."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import display
from .config import BOLD, DIM, RED, RESET, YELLOW
from .database import Database
from .logging_setup import log
from .models import PricePrediction, SearchResult
from .optional_deps import HAS_PDFPLUMBER
from .pdf_parser import parse_pdf_file
from .price_status import classify_price_status
from .utils import looks_like_tru, normalize_tru, safe_filename, sleep_random
from .zakup_client import ZakupSession

SITE_PROCESSING_LIMIT_DEFAULT = 10


@dataclass
class QueryOutcome:
    """Результат запроса без побочных эффектов вывода — используется и REPL'ом,
    и батч-режимом Excel (см. excel_export.py)."""
    is_tru: bool
    normalized: str
    local_hits: list[tuple[dict, Optional[SearchResult]]] = field(default_factory=list)
    new_records: list[dict] = field(default_factory=list)
    similar: list[SearchResult] = field(default_factory=list)   # для тира 3 (похожие товары)
    prediction: Optional[PricePrediction] = None
    market_price: Optional[float] = None
    status: Optional[str] = None


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

    # ─── Обработка пользовательского запроса (без печати) ────────────────────

    def query_structured(self, user_input: str,
                          want_status_against: Optional[float] = None,
                          announce: bool = False,
                          allow_site: bool = True) -> Optional[QueryOutcome]:
        """announce=True печатает те же транзитные сообщения ("иду на сайт...",
        "прогнозирую цену...") что и старый монолитный query() — нужно для REPL,
        чтобы порядок вывода не менялся. Батч-режим (excel_export.py) вызывает
        с announce=False и работает молча.

        allow_site=False пропускает тир 2 (Playwright-скрапинг zakup.sk.kz) и при
        пустых локальных хитах сразу идёт в тир 3 (RAG-прогноз) — используется
        Gradio-фронтом (app.py), где живой браузер в веб-интерфейсе не нужен."""
        user_input = user_input.strip()
        if not user_input:
            return None

        is_tru = looks_like_tru(user_input)
        normalized = normalize_tru(user_input) if is_tru else user_input
        outcome = QueryOutcome(is_tru=is_tru, normalized=normalized)

        # 1) Локальная база
        outcome.local_hits = self._search_local(is_tru, normalized, user_input)
        if outcome.local_hits:
            outcome.market_price = self._best_price_from_hits(outcome.local_hits)
            if want_status_against is not None:
                outcome.status = classify_price_status(want_status_against, outcome.market_price)
            return outcome

        # 2) Сайт
        if allow_site:
            if announce:
                print(f"{DIM}В локальной базе ничего. Иду на zakup.sk.kz...{RESET}")
            outcome.new_records = self._search_site_and_ingest(is_tru, normalized, user_input)
            if outcome.new_records:
                outcome.market_price = self._best_price_from_records(outcome.new_records)
                if want_status_against is not None:
                    outcome.status = classify_price_status(want_status_against, outcome.market_price)
                return outcome

        # 3) RAG-прогноз
        if announce:
            print(f"{DIM}На сайте тоже пусто. Прогнозирую цену по похожим товарам...{RESET}")
        engine = self.db.engine()
        outcome.similar = engine.search(user_input, top_k=5)
        outcome.prediction = engine.predict_price(user_input)
        if outcome.prediction:
            outcome.market_price = outcome.prediction.price
        if want_status_against is not None:
            outcome.status = classify_price_status(want_status_against, outcome.market_price)
        return outcome

    @staticmethod
    def _best_price_from_hits(hits: list[tuple[dict, Optional[SearchResult]]]) -> Optional[float]:
        for rec, _ in hits:
            price = rec.get("prise_per_unit")
            try:
                if price is not None:
                    return float(price)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _best_price_from_records(records: list[dict]) -> Optional[float]:
        for rec in records:
            price = rec.get("prise_per_unit")
            try:
                if price is not None:
                    return float(price)
            except (TypeError, ValueError):
                continue
        return None

    # ─── Обработка пользовательского запроса (с печатью в консоль) ───────────

    def query(self, user_input: str) -> None:
        user_input = user_input.strip()
        if not user_input:
            return

        is_tru = looks_like_tru(user_input)
        normalized = normalize_tru(user_input) if is_tru else user_input

        display.sep("Запрос: " + (
            f"{BOLD}ЕНС ТРУ {normalized}{RESET}" if is_tru
            else f"{BOLD}название «{user_input}»{RESET}"))

        outcome = self.query_structured(user_input, announce=True)
        if outcome is None:
            return

        if outcome.local_hits:
            display.print_local_hits(outcome.local_hits)
            return

        if outcome.new_records:
            display.print_new_records(outcome.new_records)
            return

        if not outcome.similar and not outcome.prediction:
            print(f"{YELLOW}Не нашлось похожих товаров для прогноза.{RESET}")
            return
        display.print_prediction(outcome.similar, outcome.prediction)

    # ─── (1) Локальный поиск ─────────────────────────────────────────────────

    def _search_local(self, is_tru: bool, normalized: str, user_input: str):
        if is_tru:
            recs = self.db.find_by_tru(normalized)
            return [(r, None) for r in recs] if recs else []
        engine = self.db.engine()
        results = engine.search(user_input, top_k=10)
        return [(r.product.record, r) for r in results] if results else []

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
