🇷🇺 [Читать на русском](README.ru.md)

# Tender Price Search

Search Kazakhstan public-procurement items (Самрук-Қазына / [zakup.sk.kz](https://zakup.sk.kz)) by their **ЕНС ТРУ code** or **product name**, and instantly get a market price — pulled from a local database of past tender lots, scraped live from the portal, or estimated with a regression-based price prediction when there's no exact match.

Built to answer one recurring procurement question: *"is this line item priced fairly?"*

![demo](docs/demo.gif)

## How it works

Every query goes through three fallback tiers:

1. **Local database** — instant lookup by ЕНС ТРУ code or fuzzy text/numeric-feature match against `data/out_with_prices.json` (TF-IDF + Russian morphology via `pymorphy2`, plus numeric-characteristic matching for things like voltage/capacity/cable cross-section).
2. **Live site search** *(CLI only)* — if nothing local matches, a headless Chromium session (Playwright) searches [zakup.sk.kz](https://zakup.sk.kz) for matching lots, downloads their technical-specification PDF, parses it, and saves the result back into the local database for next time.
3. **RAG-style price prediction** — if the site has nothing either, a linear regression over similar-category items (matched by numeric characteristics like voltage or capacity) estimates a price, with a confidence score based on sample size, regression fit (R²), and price consistency.

There's also a **batch Excel mode**: feed it a spreadsheet of tender items, and it appends market price / status / confidence columns, color-coded by **Переплата / Нормально / Ниже рынка** (overpaid / normal / below-market), based on comparing each item's actual tendered price to its found market price.

## Try it

The web UI (`app.py`) only exercises tiers 1 and 3 — it's read-only against the local database, so it's safe to run publicly without hammering the procurement portal. The full three-tier pipeline (including live scraping and the Excel batch mode) is CLI-only.

```bash
pip install -r requirements.txt
playwright install chromium   # only needed for live site scraping (CLI)

python app.py                 # web UI — http://127.0.0.1:7860
python -m tender_search       # interactive CLI (all 3 tiers)
```

CLI batch mode:

```bash
python -m tender_search --batch-excel input.xlsx --output results.xlsx \
    --col-name 4 --col-unit 7 --col-actual-price 15
```

## Project structure

```
tender_search/            # core package (no web/CLI-specific code)
  database.py              JSON-file store, keyed by lot_id / ЕНС ТРУ
  search_engine.py          TF-IDF + morphology + numeric-feature matching, price prediction
  zakup_client.py            Playwright session for zakup.sk.kz
  pdf_parser.py               technical-specification PDF → structured fields
  orchestrator.py              3-tier query pipeline (TenderSearchSystem)
  excel_export.py               batch Excel read/write with color-coded status
  price_status.py                Переплата / Нормально / Ниже рынка classification
  cli.py                          REPL + argparse entry point
app.py                    # Gradio web UI (tiers 1 + 3 only)
scripts/make_demo_gif.py  # regenerates docs/demo.gif
```

## License

MIT
