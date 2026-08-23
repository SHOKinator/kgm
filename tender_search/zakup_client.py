"""Lazy-инициализированная сессия браузера (Playwright) для zakup.sk.kz."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import (
    API_LOT_SEARCH_PATH, BASE_URL, BLOCKED_DOMAINS, DIM, FILE_DOWNLOAD_PATH,
    MAX_LOTS_PER_NAME_QUERY, MIN_WORD_LEN, PAGE_LOAD_TIMEOUT_MS, PORTAL_ENTRY, RESET,
)
from .logging_setup import log
from .utils import sleep_random


class ZakupSession:
    """Lazy-инициализированная сессия браузера для zakup.sk.kz."""

    def __init__(self, headful: bool = False):
        self.headful = headful
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def is_started(self) -> bool:
        return self._page is not None

    def _start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            raise RuntimeError(
                "Playwright не установлен. Выполните:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
        self._PWTimeout = PWTimeout
        print(f"{DIM}[открываю браузер для zakup.sk.kz...]{RESET}")
        log.info("Запуск Playwright (headful=%s)", self.headful)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=not self.headful,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/147.0.0.0 Safari/537.36"),
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
        )
        # Блокируем тяжёлые трекеры
        def _route(route):
            url = route.request.url
            if any(d in url for d in BLOCKED_DOMAINS):
                return route.abort()
            return route.continue_()
        self._context.route("**/*", _route)

        page = self._context.new_page()
        try:
            page.goto(PORTAL_ENTRY, timeout=PAGE_LOAD_TIMEOUT_MS,
                      wait_until="domcontentloaded")
        except PWTimeout:
            pass
        try:
            page.wait_for_selector("sk-app", timeout=30000, state="attached")
        except PWTimeout:
            pass
        page.wait_for_timeout(4000)
        self._page = page
        print(f"{DIM}[браузер готов]{RESET}")
        log.info("Сессия Playwright запущена")

    def _ensure(self):
        if self._page is None:
            self._start()
        return self._page

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._pw = self._context = self._page = None

    # ─── API: поиск лотов по ЕНС ТРУ ─────────────────────────────────────────

    def search_by_tru(self, code: str) -> list[dict]:
        page = self._ensure()
        lots: list[dict] = []
        page_num = 1
        while page_num <= 50:
            target = (f"{BASE_URL}/#/ext?tabs=lot&adst=PUBLISHED&lst=PUBLISHED"
                      f"&tru={code}&page={page_num}")
            log.info("[ТРУ] страница %d: %s", page_num, target)
            try:
                page.goto("about:blank", wait_until="commit", timeout=8000)
            except Exception:
                pass
            try:
                with page.expect_response(
                    lambda r: API_LOT_SEARCH_PATH in r.url and r.request.method == "POST",
                    timeout=30000,
                ) as resp_info:
                    page.goto(target, wait_until="commit", timeout=60000)
                resp = resp_info.value
            except self._PWTimeout:
                log.error("ответ от API за 30 сек не пришёл")
                break
            if resp.status != 200:
                log.error("status=%d", resp.status)
                break
            try:
                data = resp.json()
            except Exception:
                break
            if isinstance(data, list):
                items, total_pages = data, 1
            elif isinstance(data, dict):
                items = data.get("content") or data.get("items") or []
                total_pages = data.get("totalPages") or 1
            else:
                items, total_pages = [], 1
            log.info("получено лотов: %d", len(items))
            lots.extend(items)
            if not items or page_num >= total_pages:
                break
            page_num += 1
            sleep_random()
        return lots

    # ─── API: поиск лотов по названию ────────────────────────────────────────

    def search_by_name(self, name: str) -> tuple[list[dict], Optional[str]]:
        """
        Пробует разные степени специфичности запроса (1, 2, 3 слова, потом всё)
        и фильтрует результат по совпадению названия лота. Возвращает (лоты, query).
        """
        variants = self._gen_query_variants(name)
        log.info("[NAME] варианты: %s", variants)
        last_lots: list[dict] = []
        last_query: Optional[str] = None
        for q in variants:
            try:
                lots = self._search_by_query_kwd(q)
            except Exception as e:
                log.exception("ошибка запроса '%s': %s", q, e)
                sleep_random()
                continue
            sleep_random()
            relevant = [l for l in lots if self._matches_query(l, q)]
            log.info("[NAME] '%s': API=%d, релевантных=%d", q, len(lots), len(relevant))
            if not relevant:
                continue
            if len(relevant) > MAX_LOTS_PER_NAME_QUERY:
                last_lots, last_query = relevant, q
                continue
            return relevant, q
        if last_lots:
            return last_lots, last_query
        return [], None

    def _gen_query_variants(self, name: str) -> list[str]:
        cleaned = re.sub(r"[\r\n\t]+", " ", name)
        cleaned = re.sub(r"[,;:]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return []
        words = cleaned.split(" ")
        sig = next((i for i, w in enumerate(words)
                    if len(w) >= MIN_WORD_LEN and not w.replace(".", "").isdigit()), 0)
        out = [words[sig]]
        if len(words) > sig + 1:
            out.append(" ".join(words[sig:sig + 2]))
        if len(words) > sig + 2:
            out.append(" ".join(words[sig:sig + 3]))
        if cleaned not in out:
            out.append(cleaned)
        seen, result = set(), []
        for v in out:
            if v and v not in seen:
                seen.add(v); result.append(v)
        return result

    def _matches_query(self, lot: dict, query: str) -> bool:
        name = str(lot.get("nameRu") or lot.get("name") or "").lower()
        first = query.strip().split()[0].lower() if query.strip() else ""
        if not first:
            return True
        return bool(re.search(r"\b" + re.escape(first) + r"\b", name))

    def _search_by_query_kwd(self, query: str) -> list[dict]:
        page = self._ensure()
        lots: list[dict] = []
        page_num = 1
        while page_num <= 20:
            target = (f"{BASE_URL}/#/ext?tabs=lot&adst=PUBLISHED&lst=PUBLISHED"
                      f"&q={query}&page={page_num}")
            try:
                page.goto("about:blank", wait_until="commit", timeout=8000)
            except Exception:
                pass
            try:
                with page.expect_response(
                    lambda r: API_LOT_SEARCH_PATH in r.url and r.request.method == "POST",
                    timeout=30000,
                ) as resp_info:
                    page.goto(target, wait_until="commit", timeout=60000)
                resp = resp_info.value
            except self._PWTimeout:
                break
            if resp.status != 200:
                break
            try:
                data = resp.json()
            except Exception:
                break
            if isinstance(data, list):
                items, total_pages = data, 1
            elif isinstance(data, dict):
                items = data.get("content") or data.get("items") or []
                total_pages = data.get("totalPages") or 1
            else:
                items, total_pages = [], 1
            lots.extend(items)
            if not items or page_num >= total_pages:
                break
            page_num += 1
            sleep_random()
        return lots

    # ─── API: детали лота (один проход = всё что нужно) ──────────────────────

    def fetch_lot_full_info(self, lot_id) -> Optional[dict]:
        """
        Открывает popup лота, собирает все JSON-ответы.
        Возвращает: {lot_id, name, price, count, tru_code, spec_file_uid,
                     spec_filename} или None.
        """
        page = self._ensure()
        target = f"{BASE_URL}/#/ext(popup:item/{lot_id}/lot)"
        collected = []

        def _capture(response):
            url = response.url
            if "eproc" not in url and "/api/" not in url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "application/json" not in ct:
                    return
                collected.append({"url": url, "body": response.json()})
            except Exception:
                pass

        page.on("response", _capture)
        try:
            try:
                page.goto("about:blank", wait_until="commit", timeout=8000)
            except Exception:
                pass
            try:
                with page.expect_response(
                    lambda r: ("/api/" in r.url and r.request.method == "GET"
                               and "json" in r.headers.get("content-type", "").lower()),
                    timeout=15000,
                ):
                    page.goto(target, wait_until="commit", timeout=30000)
            except self._PWTimeout:
                pass
            page.wait_for_timeout(2500)
        finally:
            try:
                page.remove_listener("response", _capture)
            except Exception:
                pass

        # Ищем основной объект лота (где есть price/lotDocuments/truHistory)
        lot_data = None
        for resp in collected:
            body = resp.get("body")
            if isinstance(body, dict) and body.get("id") == lot_id and "price" in body:
                lot_data = body
                break
        # Фолбэк: первый dict, у которого есть нужные поля
        if lot_data is None:
            for resp in collected:
                body = resp.get("body")
                if isinstance(body, dict) and "lotDocuments" in body:
                    lot_data = body
                    break
        if not lot_data:
            log.warning("Не нашёл данных лота %s в ответах", lot_id)
            return None

        # Ищем spec PDF
        spec_uid = None
        spec_filename = None
        for d in (lot_data.get("lotDocuments") or []):
            if (d.get("documentCategory") == "LOT_TECHNICAL_SPECIFICATION"
                    and d.get("fileUid")):
                spec_uid = d["fileUid"]
                spec_filename = d.get("fileName")
                break
        if not spec_uid:
            # Поищем во всех собранных JSON по всему дереву
            spec_uid, spec_filename = self._scan_for_spec(collected)

        return {
            "lot_id": lot_data.get("id") or lot_id,
            "name": lot_data.get("nameRu") or lot_data.get("name"),
            "price": lot_data.get("price"),
            "count": lot_data.get("count"),
            "tru_code": ((lot_data.get("truHistory") or {}).get("code")
                         if isinstance(lot_data.get("truHistory"), dict) else None),
            "spec_file_uid": spec_uid,
            "spec_filename": spec_filename,
        }

    @staticmethod
    def _scan_for_spec(collected: list[dict]) -> tuple[Optional[str], Optional[str]]:
        result_uid, result_name = None, None

        def walk(node):
            nonlocal result_uid, result_name
            if result_uid:
                return
            if isinstance(node, dict):
                if (node.get("documentCategory") == "LOT_TECHNICAL_SPECIFICATION"
                        and node.get("fileUid")):
                    result_uid = node["fileUid"]
                    result_name = node.get("fileName")
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        for resp in collected:
            walk(resp.get("body"))
            if result_uid:
                break
        return result_uid, result_name

    # ─── Скачивание файла через fetch внутри страницы ────────────────────────

    def download_file(self, file_uid: str, dest: Path) -> bool:
        page = self._ensure()
        url = f"{FILE_DOWNLOAD_PATH}/{file_uid}"
        script = """
        async (url) => {
            try {
                const r = await fetch(url, {method: 'GET', credentials: 'same-origin'});
                if (!r.ok) return {status: r.status, data: null};
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                return {status: r.status, data: btoa(binary), size: bytes.length};
            } catch(e) { return {status: -1, data: null, error: String(e)}; }
        }
        """
        try:
            res = page.evaluate(script, url)
        except Exception as e:
            log.error("download failed: %s", e)
            return False
        if res.get("status") != 200 or not res.get("data"):
            log.error("download status=%s, error=%s", res.get("status"), res.get("error"))
            return False
        import base64 as _b64
        data = _b64.b64decode(res["data"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
