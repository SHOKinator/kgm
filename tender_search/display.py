"""Вывод в консоль — вся ANSI-форматированная печать вынесена сюда."""

from __future__ import annotations

from typing import Optional

from .config import BOLD, CYAN, DIM, GREEN, MAGENTA, ORANGE, RESET, YELLOW
from .models import PricePrediction, SearchResult
from .optional_deps import HAS_PDFPLUMBER, HAS_PYMORPHY, HAS_SKLEARN
from .units import normalize_unit
from .utils import fmt_price


def sep(title: str = "") -> None:
    line = "─" * 72
    if title:
        print(f"\n{line}\n{title}\n{line}")
    else:
        print(f"\n{line}\n")


def print_intro(db_size: int) -> None:
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


def print_stats(records: list[dict]) -> None:
    total = len(records)
    with_price = sum(1 for r in records if r.get("prise_per_unit"))
    with_tru = sum(1 for r in records if r.get("tru_code"))
    with_pdf = sum(1 for r in records if r.get("file"))
    print(f"\n{BOLD}Статистика базы:{RESET}")
    print(f"  всего записей:           {total}")
    print(f"  с ценой (prise_per_unit): {with_price}")
    print(f"  с кодом ЕНС ТРУ:         {with_tru}")
    print(f"  со ссылкой на PDF:       {with_pdf}\n")


def print_local_hits(hits: list[tuple[dict, Optional[SearchResult]]]) -> None:
    print(f"{GREEN}{BOLD}✓ Найдено в локальной базе ({len(hits)}){RESET}\n")
    for i, (rec, sr) in enumerate(hits, 1):
        print_record(i, rec, sr=sr)


def print_new_records(records: list[dict]) -> None:
    print(f"\n{GREEN}{BOLD}✓ Спарсено с zakup.sk.kz ({len(records)}). "
          f"Добавлено в базу.{RESET}\n")
    for i, rec in enumerate(records, 1):
        print_record(i, rec, source="SITE")


def print_prediction(results: list[SearchResult], prediction: Optional[PricePrediction]) -> None:
    if results:
        print(f"\n{BOLD}▌ Похожие товары (по локальной базе):{RESET}\n")
        for i, r in enumerate(results, 1):
            print_record(i, r.product.record, sr=r)
    if prediction:
        print(f"\n{BOLD}▌ Прогноз цены:{RESET}")
        print(f"  {MAGENTA}{BOLD}≈ {fmt_price(prediction.price)}{RESET}")
        print(f"  {DIM}{prediction.explanation}{RESET}")
        print(f"  {DIM}уверенность ≈ {prediction.confidence_pct:.0f}%{RESET}\n")
    else:
        print(f"{DIM}Регрессионный прогноз невозможен "
              f"(мало точек или нет числовых характеристик).{RESET}\n")


def print_record(rank: int, rec: dict,
                  sr: Optional[SearchResult] = None,
                  source: str = "LOCAL") -> None:
    name = (rec.get("naimenovanie_kratkaya")
            or rec.get("name_from_site") or "—").strip()
    desc = (rec.get("dopolnitelnaya_harakteristika") or "").strip()
    unit = normalize_unit(rec.get("edinitsa_izmereniya") or "шт")
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
