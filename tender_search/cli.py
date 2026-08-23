"""CLI / REPL точка входа."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

# Принудительно UTF-8 для Windows-консоли
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from . import display
from .config import BOLD, DIM, RED, RESET, SITE_PROCESSING_LIMIT_DEFAULT
from .logging_setup import setup_logging
from .orchestrator import TenderSearchSystem


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

    ap.add_argument("--batch-excel", type=Path, default=None,
                    help="Входной .xlsx для батч-режима (вместо интерактивного REPL)")
    ap.add_argument("--output", type=Path, default=None,
                    help="Куда сохранить результат батч-режима (обязателен с --batch-excel)")
    ap.add_argument("--col-name", type=int, default=None,
                    help="Номер колонки (1-based) с названием товара во входном xlsx")
    ap.add_argument("--col-unit", type=int, default=None,
                    help="Номер колонки с единицей измерения (опционально)")
    ap.add_argument("--col-actual-price", type=int, default=None,
                    help="Номер колонки с фактической/тендерной ценой (опционально, "
                         "для расчёта статуса Переплата/Нормально/Ниже рынка)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Ограничить число обрабатываемых строк (для теста батч-режима)")

    args = ap.parse_args()

    setup_logging(args.log)

    system = TenderSearchSystem(
        db_path=args.db, pdf_dir=args.pdf_dir,
        headful=args.headful, site_lot_limit=args.site_limit,
    )

    try:
        if args.batch_excel:
            return _run_batch_mode(system, args)
        return _run_repl(system)
    finally:
        system.close()


def _run_batch_mode(system: TenderSearchSystem, args: argparse.Namespace) -> int:
    from .excel_export import run_batch
    from .config import DEFAULT_COL_NAME, DEFAULT_COL_UNIT, DEFAULT_COL_ACTUAL_PRICE

    if not args.output:
        print("Батч-режим требует --output <файл.xlsx>")
        return 1

    run_batch(
        system,
        input_path=str(args.batch_excel),
        output_path=str(args.output),
        name_col=args.col_name or DEFAULT_COL_NAME,
        unit_col=args.col_unit or DEFAULT_COL_UNIT,
        actual_price_col=args.col_actual_price or DEFAULT_COL_ACTUAL_PRICE,
        limit=args.limit,
    )
    return 0


def _run_repl(system: TenderSearchSystem) -> int:
    display.print_intro(len(system.db.records))

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
            display.print_stats(system.db.records)
            continue
        if low in ("reload", "перезагрузка"):
            system.db.load()
            print(f"{DIM}База перечитана: {len(system.db.records)} записей{RESET}\n")
            continue
        if low in ("help", "помощь", "?"):
            display.print_intro(len(system.db.records))
            continue
        try:
            system.query(q)
        except Exception as e:
            print(f"{RED}Ошибка обработки запроса: {e}{RESET}")
            from .logging_setup import log
            log.exception("query failed")

    print("Выход.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
