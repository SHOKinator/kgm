"""
Gradio web UI for tender_search — search local price database and get an
RAG-style price prediction for tender items (Kazakhstan procurement, ЕНС ТРУ).

Only tiers 1 (local DB) and 3 (RAG prediction) are exposed here — live
scraping of zakup.sk.kz (tier 2, Playwright) is CLI-only, see README.

Run:
    python app.py
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Optional

import gradio as gr

from tender_search.database import Database
from tender_search.models import PricePrediction, SearchResult
from tender_search.orchestrator import QueryOutcome, TenderSearchSystem
from tender_search.units import normalize_unit
from tender_search.utils import fmt_price

DB_PATH = Path("data/out_with_prices.json")
PDF_DIR = Path("data/pdfs")

MATCH_LABELS = {
    "precise": ("EXACT", "#1e7e34", "#e8f8ee"),
    "range": ("RANGE", "#0d6efd", "#e7f1ff"),
    "close": ("CLOSE", "#b8860b", "#fff8e1"),
    "category": ("CATEGORY", "#e8590c", "#fff0e6"),
}


def _card(rec: dict, sr: Optional[SearchResult] = None) -> str:
    name = html.escape((rec.get("naimenovanie_kratkaya") or rec.get("name_from_site") or "—").strip())
    desc = html.escape((rec.get("dopolnitelnaya_harakteristika") or "").strip())
    unit = html.escape(normalize_unit(rec.get("edinitsa_izmereniya") or "шт"))
    price = rec.get("prise_per_unit")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    place = html.escape((rec.get("mesto_postavki") or "")[:100])

    badge = ""
    if sr is not None:
        label, color, bg = MATCH_LABELS.get(sr.match_type, (sr.match_type.upper(), "#666", "#eee"))
        badge = (
            f'<span style="background:{bg};color:{color};font-weight:600;'
            f'font-size:11px;padding:2px 8px;border-radius:10px;margin-left:8px;">'
            f'{label} · {sr.score:.0%}</span>'
        )

    price_html = (
        f'<b style="font-size:16px;">{html.escape(fmt_price(price_f))}</b> / {unit}'
        if price_f is not None else '<span style="color:#999;">price not listed</span>'
    )

    explanation = ""
    if sr is not None and sr.explanation:
        explanation = f'<div style="color:#888;font-size:12px;margin-top:4px;">{html.escape(sr.explanation)}</div>'

    return f"""
<div style="border:1px solid #e5e5e5;border-radius:10px;padding:14px 16px;margin-bottom:10px;">
  <div style="font-weight:600;">{name}{badge}</div>
  {f'<div style="color:#555;font-size:13px;margin-top:4px;">{desc}</div>' if desc else ''}
  <div style="margin-top:8px;">{price_html}</div>
  {f'<div style="color:#999;font-size:12px;margin-top:4px;">📍 {place}</div>' if place else ''}
  {explanation}
</div>
"""


def _prediction_card(prediction: PricePrediction) -> str:
    return f"""
<div style="border:2px dashed #b39ddb;border-radius:10px;padding:16px;margin-top:8px;background:#faf7ff;">
  <div style="font-weight:600;color:#6a1b9a;">🔮 No exact match — RAG price prediction</div>
  <div style="font-size:22px;font-weight:700;margin-top:6px;">≈ {html.escape(fmt_price(prediction.price))}</div>
  <div style="color:#666;font-size:13px;margin-top:6px;">{html.escape(prediction.explanation)}</div>
  <div style="color:#888;font-size:12px;margin-top:4px;">confidence ≈ {prediction.confidence_pct:.0f}%</div>
</div>
"""


def build_app(db_path: Path = DB_PATH, pdf_dir: Path = PDF_DIR) -> gr.Blocks:
    system = TenderSearchSystem(db_path=db_path, pdf_dir=pdf_dir)
    example_names = _pick_examples(system.db)

    # SearchEngine indexing (TF-IDF + pymorphy2 lemmatization over every record)
    # takes several seconds — build it once at startup instead of blocking the
    # first user request.
    print(f"Indexing {len(system.db.records)} records...")
    system.db.engine()
    print("Ready.")

    def handle_query(query: str) -> str:
        query = (query or "").strip()
        if not query:
            return '<div style="color:#999;">Type an ЕНС ТРУ code or a product name above.</div>'

        outcome: Optional[QueryOutcome] = system.query_structured(query, allow_site=False)
        if outcome is None:
            return '<div style="color:#999;">Type an ЕНС ТРУ code or a product name above.</div>'

        if outcome.local_hits:
            cards = "".join(_card(rec, sr) for rec, sr in outcome.local_hits)
            return f'<div style="color:#1e7e34;font-weight:600;margin-bottom:10px;">✓ Found in local database ({len(outcome.local_hits)})</div>{cards}'

        if outcome.prediction:
            similar = "".join(_card(r.product.record, r) for r in outcome.similar)
            return (
                f'<div style="color:#666;margin-bottom:10px;">No exact match — showing closest items and a price prediction:</div>'
                f"{similar}{_prediction_card(outcome.prediction)}"
            )

        return '<div style="color:#999;">No data found for this item in the local database.</div>'

    with gr.Blocks(title="Tender Price Search (KZ)") as demo:
        gr.Markdown(
            "# 🔎 Tender Price Search\n"
            "Search Kazakhstan public-procurement (Самрук-Қазына) items by **ЕНС ТРУ code** "
            "or **product name**, and get a market price straight from a local database of "
            "past tender lots — or an RAG-style price estimate when there's no exact match.\n\n"
            "*This demo only queries the local database. The full CLI also live-scrapes "
            "[zakup.sk.kz](https://zakup.sk.kz) and exports batch results to Excel — see the README.*"
        )
        with gr.Row():
            query_box = gr.Textbox(
                label="ЕНС ТРУ code or product name",
                placeholder="e.g. Аккумулятор, для ИБП, напряжение 12 В, емкость 20 А/ч",
                scale=4,
            )
            search_btn = gr.Button("Search", variant="primary", scale=1)
        output = gr.HTML()

        if example_names:
            gr.Examples(examples=[[n] for n in example_names], inputs=query_box, label="Try one of these")

        search_btn.click(handle_query, inputs=query_box, outputs=output)
        query_box.submit(handle_query, inputs=query_box, outputs=output)

    return demo


def _pick_examples(db: Database, n: int = 5) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in db.records:
        name = (r.get("naimenovanie_kratkaya") or "").strip()
        if name and name not in ("nan", "None") and name not in seen and r.get("prise_per_unit"):
            seen.add(name)
            out.append(name)
        if len(out) >= n:
            break
    return out


if __name__ == "__main__":
    build_app().launch()
