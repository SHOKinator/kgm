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
  python -m tender_search
  python -m tender_search --headful           # видеть окно браузера
  python -m tender_search --db data/out_with_prices.json --pdf-dir data/pdfs
  python -m tender_search --batch-excel input.xlsx --output results.xlsx --col-name 4

Структура папок:
  data/
    out_with_prices.json     ← база (читается и пополняется)
    pdfs/                     ← скачанные PDF спецификаций
"""
