from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

LOGGER = logging.getLogger(__name__)


class LidlScraper:
    def __init__(
        self,
        headless: bool = True,
        refresh_token: str = "",
        country: str = "CZ",
        language: str = "cs",
    ) -> None:
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 20)
        self._is_logged_in = False
        self._refresh_token = refresh_token.strip()
        self._api_access_token = ""
        self._api_token_expires_at = 0.0
        self._country = (country or "CZ").upper()
        self._language = (language or "cs").lower()

        # API endpoints discovered by reverse-engineering the Lidl Plus mobile app flow.
        self._accounts_api = "https://accounts.lidl.com"
        self._tickets_api = "https://tickets.lidlplus.com/api/v2"
        self._mre_api = "https://www.lidl.de/mre/api/v1"

    def _click_first(self, selectors: list[tuple[By, str]]) -> None:
        for by, selector in selectors:
            elements = self.driver.find_elements(by, selector)
            if elements:
                try:
                    elements[0].click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", elements[0])
                return

    def _iter_contexts(self) -> list[int | None]:
        contexts: list[int | None] = [None]
        frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        contexts.extend(range(len(frames)))
        return contexts

    def _switch_context(self, context: int | None) -> bool:
        self.driver.switch_to.default_content()
        if context is None:
            return True
        frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        if context >= len(frames):
            return False
        try:
            self.driver.switch_to.frame(frames[context])
            return True
        except Exception:
            return False

    def _find_first_interactable(self, selectors: list[tuple[By, str]]) -> tuple[object | None, int | None]:
        for context in self._iter_contexts():
            if not self._switch_context(context):
                continue
            for by, selector in selectors:
                try:
                    elements = self.driver.find_elements(by, selector)
                    if elements:
                        element = elements[0]
                        # Try without visibility check first - headless mode may have issues
                        try:
                            element.click()  # Test if clickable
                            return element, context
                        except Exception:
                            # If not clickable, try next selector
                            continue
                except Exception:
                    continue
        self.driver.switch_to.default_content()
        return None, None

    def _fill_login_field(self, selectors: list[tuple[By, str]], value: str) -> bool:
        contexts = self._iter_contexts()
        LOGGER.debug(f"_fill_login_field: trying {len(contexts)} contexts")
        for context in contexts:
            if not self._switch_context(context):
                continue
            for by, selector in selectors:
                try:
                    elements = self.driver.find_elements(by, selector)
                    if elements:
                        element = elements[0]
                        LOGGER.debug(f"Found field in context {context} with selector: {selector}")
                        try:
                            element.clear()
                        except Exception:
                            pass
                        element.send_keys(value)
                        LOGGER.debug(f"Field filled in context {context} with selector: {selector}")
                        self.driver.switch_to.default_content()
                        return True
                except Exception as e:
                    LOGGER.debug(f"Selector {selector} in context {context} failed: {e}")
                    continue
        self.driver.switch_to.default_content()
        return False

    def _click_first_any_context(self, selectors: list[tuple[By, str]]) -> bool:
        for context in self._iter_contexts():
            if not self._switch_context(context):
                continue
            for by, selector in selectors:
                try:
                    elements = self.driver.find_elements(by, selector)
                    if elements:
                        element = elements[0]
                        try:
                            element.click()
                        except Exception:
                            self.driver.execute_script("arguments[0].click();", element)
                        self.driver.switch_to.default_content()
                        LOGGER.debug(f"Clicked element with selector: {selector} in context {context}")
                        return True
                except Exception as e:
                    LOGGER.debug(f"Failed to click {selector} in context {context}: {e}")
                    continue
        self.driver.switch_to.default_content()
        return False

    def _looks_logged_in(self) -> bool:
        self.driver.switch_to.default_content()
        current_url = self.driver.current_url.lower()
        if "/login" in current_url:
            return False
        page_text = self.driver.page_source.lower()
        markers = [
            "odhlasit",
            "logout",
            "muj ucet",
            "profil",
        ]
        if any(marker in page_text for marker in markers):
            return True
        return "/account" in current_url

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        cleaned = (
            text.replace("Kc", "")
            .replace("Kc.", "")
            .replace("Kč", "")
            .replace("CZK", "")
            .replace(" ", "")
            .replace(",", ".")
        )
        cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _api_headers(self) -> dict[str, str]:
        if not self._refresh_token:
            raise RuntimeError("LIDL_REFRESH_TOKEN není nastaven.")
        now = time.time()
        if self._api_access_token and now < self._api_token_expires_at - 30:
            return {
                "Authorization": f"Bearer {self._api_access_token}",
                "App-Version": "999.99.9",
                "Operating-System": "iOs",
                "App": "com.lidl.eci.lidl.plus",
                "Accept-Language": self._language,
            }

        basic_secret = "TGlkbFBsdXNOYXRpdmVDbGllbnQ6c2VjcmV0"
        token_response = requests.post(
            f"{self._accounts_api}/connect/token",
            headers={
                "Authorization": f"Basic {basic_secret}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            timeout=20,
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        self._api_access_token = str(token_data.get("access_token", ""))
        expires_in = int(token_data.get("expires_in", 0) or 0)
        self._api_token_expires_at = now + max(expires_in, 1)
        if not self._api_access_token:
            raise RuntimeError("Nepodařilo se získat access token z refresh tokenu.")

        return {
            "Authorization": f"Bearer {self._api_access_token}",
            "App-Version": "999.99.9",
            "Operating-System": "iOs",
            "App": "com.lidl.eci.lidl.plus",
            "Accept-Language": self._language,
        }

    def _web_api_headers(self) -> dict[str, str]:
        self._api_headers()
        return {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Accept": "application/json",
            "Accept-Language": f"{self._language},{self._language}-{self._country};q=0.9",
            "content-type": "application/json",
            "Cookie": f"authToken={self._api_access_token}",
        }

    def _extract_purchase_items_from_receipt_html(self, html_receipt: str, purchased_at: str) -> list[dict]:
        if not html_receipt:
            return []

        soup = BeautifulSoup(html_receipt, "html.parser")
        purchases: list[dict] = []
        seen: set[tuple[str, str, float, float]] = set()

        for item in soup.select("span.article"):
            name = str(item.get("data-art-description") or "").strip()
            if not name:
                continue

            quantity = self._safe_float(item.get("data-art-quantity")) or 1.0
            price = self._safe_float(item.get("data-unit-price"))
            if price is None:
                continue

            article_id = str(item.get("data-art-id") or "")
            dedupe_key = (article_id, name, quantity, price)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            purchases.append(
                {
                    "name": name,
                    "category": self._guess_category(name),
                    "quantity": quantity,
                    "price": price,
                    "purchased_at": purchased_at,
                }
            )

        return purchases

    def _get_purchase_history_via_mre_api(self) -> list[dict]:
        headers = self._web_api_headers()
        purchases: list[dict] = []
        page = 1

        while True:
            response = requests.get(
                f"{self._mre_api}/tickets?country={self._country}&page={page}",
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            tickets = payload.get("items") or payload.get("tickets") or []
            if not tickets:
                break

            for ticket in tickets:
                if not isinstance(ticket, dict):
                    continue
                ticket_id = ticket.get("id")
                if not ticket_id:
                    continue

                detail_response = requests.get(
                    f"{self._mre_api}/tickets/{ticket_id}?country={self._country}&languageCode={self._language}-{self._country}",
                    headers=headers,
                    timeout=60,
                )
                detail_response.raise_for_status()
                detail_payload = detail_response.json()
                ticket_root = detail_payload.get("ticket", detail_payload)
                purchased_at = str(
                    ticket_root.get("date")
                    or ticket.get("date")
                    or datetime.now(timezone.utc).isoformat()
                )
                purchases.extend(
                    self._extract_purchase_items_from_receipt_html(
                        str(ticket_root.get("htmlPrintedReceipt") or ""),
                        purchased_at,
                    )
                )

            size = int(payload.get("size") or len(tickets) or 0)
            total_count = int(payload.get("totalCount") or 0)
            if size <= 0 or page * size >= total_count:
                break
            page += 1

        return purchases

    def _get_purchase_history_via_api(self) -> list[dict]:
        headers = self._api_headers()
        url = f"{self._tickets_api}/{self._country}/tickets"
        purchases: list[dict] = []

        page_number = 1
        total_count = 0
        page_size = 0
        while True:
            response = requests.get(
                f"{url}?pageNumber={page_number}&onlyFavorite=false",
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            tickets = payload.get("tickets") or []
            total_count = int(payload.get("totalCount") or 0)
            page_size = int(payload.get("size") or 0)
            if not tickets:
                break

            for ticket in tickets:
                ticket_id = ticket.get("id")
                if not ticket_id:
                    continue
                detail_response = requests.get(f"{url}/{ticket_id}", headers=headers, timeout=20)
                detail_response.raise_for_status()
                detail = detail_response.json()

                line_items = []
                for key in ["items", "articles", "positions", "products", "lineItems"]:
                    value = detail.get(key)
                    if isinstance(value, list):
                        line_items = value
                        break

                if not line_items and isinstance(detail, list):
                    line_items = detail

                for item in line_items:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("title") or "").strip()
                    if not name:
                        continue
                    price = self._safe_float(
                        item.get("currentUnitPrice")
                        or item.get("originalAmount")
                        or item.get("price")
                        or item.get("unitPrice")
                    )
                    if price is None:
                        continue
                    quantity = self._safe_float(item.get("quantity")) or 1.0

                    purchases.append(
                        {
                            "name": name,
                            "category": self._guess_category(name),
                            "quantity": quantity,
                            "price": price,
                            "purchased_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            if page_size <= 0:
                break
            if page_number * page_size >= total_count:
                break
            page_number += 1

        return purchases

    def _open_login_form_if_needed(self) -> None:
        LOGGER.debug("_open_login_form_if_needed: searching for login toggle")
        self.driver.switch_to.default_content()
        
        selectors = [
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'prihlas')]"),
            (By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'prihlas')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
            (By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'muj ucet')]"),
        ]
        
        for by, selector in selectors:
            try:
                btn = self.driver.find_element(by, selector)
                LOGGER.debug(f"Found login toggle with selector: {selector}")
                btn.click()
                LOGGER.debug("Clicked login toggle")
                time.sleep(2)
                return
            except Exception as e:
                LOGGER.debug(f"Selector {selector} not found")
                continue
        
        LOGGER.debug("No login toggle found, form may be already visible")

    def _fill_first(self, selectors: list[tuple[By, str]], value: str) -> bool:
        for by, selector in selectors:
            elements = self.driver.find_elements(by, selector)
            if elements:
                elements[0].clear()
                elements[0].send_keys(value)
                return True
        return False

    def _extract_price(self, text: str) -> float | None:
        match = re.search(r"(\d+[\.,]\d{1,2})\s*(Kc|Kc\.|CZK)", text, flags=re.IGNORECASE)
        if not match:
            return None
        return float(match.group(1).replace(",", "."))

    @staticmethod
    def _to_absolute_url(url: str, base: str = "https://www.lidl.cz") -> str:
        if not url:
            return ""
        return urljoin(base, url)

    def _discover_flyer_urls(self) -> list[str]:
        urls: list[str] = []
        try:
            response = requests.get("https://www.lidl.cz/c/letak", timeout=20)
            response.raise_for_status()
            html = response.text
            matches = re.findall(r"https://www\\.lidl\\.cz/l/cs/letak/[^\"'\s<]+", html)
            for raw in matches:
                clean = raw.split("#")[0]
                if "/view/flyer/page/" in clean or clean.endswith("/ar/0") or "/ar/0?" in clean:
                    if clean not in urls:
                        urls.append(clean)
        except Exception as exc:
            LOGGER.warning("Nepodarilo se nacist seznam letaku (%s)", exc)

        try:
            endpoint = "https://endpoints.leaflets.schwarz/v4/widgets/lidl/0627d331-5163-11ee-9b1d-fa163f6db1d0"
            response = requests.get(endpoint, timeout=20)
            response.raise_for_status()
            payload = response.json()
            widget_url = (
                payload.get("widget", {})
                .get("attributes", {})
                .get("url", "")
            )
            if isinstance(widget_url, str) and widget_url:
                absolute = self._to_absolute_url(widget_url)
                if absolute not in urls:
                    urls.append(absolute)
        except Exception as exc:
            LOGGER.debug("Leaflets widget endpoint nedostupny (%s)", exc)

        return urls

    def _discover_flyer_identifiers(self) -> list[str]:
        candidates = self._discover_flyer_candidates()
        return [str(item.get("flyer_identifier")) for item in candidates if item.get("flyer_identifier")]

    @staticmethod
    def _parse_cz_date(value: str) -> datetime | None:
        match = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", value or "")
        if not match:
            return None
        day, month, year = match.groups()
        try:
            return datetime(int(year), int(month), int(day))
        except ValueError:
            return None

    def _parse_cz_date_range(self, value: str) -> tuple[datetime | None, datetime | None]:
        text = value or ""
        # Example: "27. 4. - 3. 5. 2026"
        compact_range = re.search(
            r"(\d{1,2})\.\s*(\d{1,2})\.\s*[-–]\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})",
            text,
        )
        if compact_range:
            start_day, start_month, end_day, end_month, year = compact_range.groups()
            try:
                return (
                    datetime(int(year), int(start_month), int(start_day)),
                    datetime(int(year), int(end_month), int(end_day)),
                )
            except ValueError:
                return (None, None)

        full_dates = re.findall(r"\d{1,2}\.\s*\d{1,2}\.\s*\d{4}", text)
        if len(full_dates) >= 2:
            return (self._parse_cz_date(full_dates[0]), self._parse_cz_date(full_dates[1]))
        if len(full_dates) == 1:
            parsed = self._parse_cz_date(full_dates[0])
            return (parsed, None)
        return (None, None)

    @staticmethod
    def _monday_for(date_value: datetime) -> datetime:
        monday = datetime(date_value.year, date_value.month, date_value.day)
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        return monday - timedelta(days=monday.weekday())

    def _resolve_week_scope_for_date(self, valid_from: datetime) -> str:
        now_local = datetime.now()
        current_week_start = self._monday_for(now_local)
        next_week_start = current_week_start + timedelta(days=7)
        next_next_week_start = next_week_start + timedelta(days=7)
        if next_week_start <= valid_from < next_next_week_start:
            return "next"
        return "current"

    def _discover_flyer_candidates(self) -> list[dict[str, Any]]:
        identifiers: list[str] = []
        candidates: list[dict[str, Any]] = []
        sources = [
            "https://www.lidl.cz/c/letak",
            "https://www.lidl.cz/c/online-prospekty/s10008644",
        ]

        for source_url in sources:
            try:
                response = requests.get(source_url, timeout=20)
                response.raise_for_status()
                html = response.text
            except Exception as exc:
                LOGGER.debug("Nepodarilo se nacist %s (%s)", source_url, exc)
                continue

            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.select("a[data-track-id]"):
                href = str(anchor.get("href") or "")
                flyer_id = str(anchor.get("data-track-id") or "").strip()
                if "/letak/" in href and flyer_id and flyer_id not in identifiers:
                    identifiers.append(flyer_id)

                    name_node = anchor.select_one(".flyer__name")
                    name_text = name_node.get_text(" ", strip=True) if name_node else ""
                    title_node = anchor.select_one(".flyer__title")
                    title_text = title_node.get_text(" ", strip=True) if title_node else ""
                    valid_from, valid_to = self._parse_cz_date_range(title_text)
                    week_scope = self._resolve_week_scope_for_date(valid_from) if valid_from else "unknown"
                    candidates.append(
                        {
                            "flyer_identifier": flyer_id,
                            "url": self._to_absolute_url(href),
                            "name": name_text,
                            "title": title_text,
                            "valid_from": valid_from,
                            "valid_to": valid_to,
                            "week_scope": week_scope,
                        }
                    )

            patterns = [
                r'data-track-id="([^\"]+)"',
                r'"flyer_identifier"\s*:\s*"([^\"]+)"',
                r'"flyerIdentifier"\s*:\s*"([^\"]+)"',
            ]
            for pattern in patterns:
                for match in re.findall(pattern, html):
                    flyer_id = str(match).strip()
                    if flyer_id and flyer_id not in identifiers:
                        identifiers.append(flyer_id)
                        candidates.append(
                            {
                                "flyer_identifier": flyer_id,
                                "url": "",
                                "name": "",
                                "title": "",
                                "valid_from": None,
                                "valid_to": None,
                                "week_scope": "unknown",
                            }
                        )

        if candidates:
            LOGGER.info("Nalezeno kandidatu na flyer_identifier: %s", len(candidates))
        else:
            LOGGER.warning("Na overview strance nebyl nalezen zadny flyer_identifier")
        return candidates

    @staticmethod
    def _normalize_text(value: str) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.lower()
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _is_target_akce_flyer(self, candidate: dict[str, Any]) -> bool:
        name = self._normalize_text(str(candidate.get("name") or ""))
        if "akcni letak" not in name:
            return False
        return ("od pondeli" in name) or ("od ctvrtka" in name)

    def _target_akce_rank(self, candidate: dict[str, Any]) -> int:
        name = self._normalize_text(str(candidate.get("name") or ""))
        if "od ctvrtka" in name:
            return 0
        if "od pondeli" in name:
            return 1
        return 2

    @staticmethod
    def _dedupe_products(products: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        seen: set[tuple[str, float]] = set()
        for item in products:
            name = str(item.get("name") or "").strip().lower()
            price = item.get("price")
            price_value = float(price) if isinstance(price, (int, float)) else None
            if not name:
                continue
            key = (name, price_value if price_value is not None else -1.0)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _is_spotrebni_flyer(self, candidate: dict[str, Any]) -> bool:
        name = self._normalize_text(str(candidate.get("name") or ""))
        return "spotrebni zbozi" in name

    @staticmethod
    def _date_distance_days(first: datetime | None, second: datetime | None) -> int:
        if not first or not second:
            return 9999
        return abs((first.date() - second.date()).days)

    def _find_spotrebni_match_for_target(
        self,
        target: dict[str, Any],
        candidates: list[dict[str, Any]],
        used_identifiers: set[str],
    ) -> dict[str, Any] | None:
        spotrebni_candidates = [
            item
            for item in candidates
            if self._is_spotrebni_flyer(item)
            and str(item.get("flyer_identifier") or "")
            and str(item.get("flyer_identifier") or "") not in used_identifiers
        ]
        if not spotrebni_candidates:
            return None

        target_week = str(target.get("week_scope") or "")
        target_from = target.get("valid_from")

        same_week = [item for item in spotrebni_candidates if str(item.get("week_scope") or "") == target_week]
        pool = same_week if same_week else spotrebni_candidates

        pool.sort(key=lambda item: self._date_distance_days(target_from, item.get("valid_from")))
        return pool[0] if pool else None

    def _extract_products_from_viewer_flyer_payload(self, payload: dict[str, Any]) -> list[dict]:
        flyer = payload.get("flyer") if isinstance(payload, dict) else {}
        if not isinstance(flyer, dict):
            flyer = {}

        products_node = flyer.get("products")
        raw_products: list[dict[str, Any]] = []
        if isinstance(products_node, dict):
            raw_products = [value for value in products_node.values() if isinstance(value, dict)]
        elif isinstance(products_node, list):
            raw_products = [value for value in products_node if isinstance(value, dict)]

        products: list[dict] = []
        seen: set[tuple[str, float]] = set()

        for item in raw_products:
            name = str(item.get("title") or item.get("name") or "").strip()
            price = self._safe_float(item.get("price") or item.get("currentPrice") or item.get("salePrice"))
            if not name or price is None:
                continue

            key = (name.lower(), price)
            if key in seen:
                continue
            seen.add(key)

            category_hint = str(
                item.get("categoryPrimary")
                or item.get("wonCategoryPrimary")
                or item.get("category")
                or ""
            ).strip()

            products.append(
                {
                    "name": name,
                    "category": category_hint or self._guess_category(name),
                    "price": price,
                    "original_price": self._safe_float(item.get("regularPrice") or item.get("originalPrice")),
                    "discount": 0.0,
                }
            )

        return products

    def _extract_products_from_flyer_viewer_api(self, flyer_identifier: str) -> list[dict]:
        endpoint = "https://endpoints.leaflets.schwarz/v4/flyer"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Accept": "application/json",
            "Accept-Language": f"{self._language},{self._language}-{self._country};q=0.9",
        }
        try:
            response = requests.get(
                endpoint,
                params={"flyer_identifier": flyer_identifier},
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            products = self._extract_products_from_viewer_flyer_payload(payload)
            if products:
                LOGGER.info(
                    "Nacteno produktu z v4/flyer: %s (flyer_identifier=%s)",
                    len(products),
                    flyer_identifier,
                )
            else:
                LOGGER.info(
                    "v4/flyer vratil bez produktu (flyer_identifier=%s)",
                    flyer_identifier,
                )
            return products
        except Exception as exc:
            LOGGER.warning("Volani v4/flyer selhalo pro %s (%s)", flyer_identifier, exc)
            return []

    def _extract_products_from_json_like_payload(self, payload: Any) -> list[dict]:
        rows: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                lowered_keys = {str(k).lower() for k in node.keys()}
                # Prefer common name/title keys when present.
                name = ""
                for key in ["name", "title", "headline", "description", "offerTitle", "offerDescriptionShort"]:
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        name = value.strip()
                        break
                price_parts: list[str] = []
                for key in [
                    "price",
                    "currentPrice",
                    "salePrice",
                    "regularPrice",
                    "originalPrice",
                    "amount",
                    "value",
                ]:
                    value = node.get(key)
                    if isinstance(value, (str, int, float)):
                        price_parts.append(str(value))
                if name and price_parts:
                    rows.append(f"{name} {' '.join(price_parts)} Kč")
                elif any("price" in key or "amount" in key for key in lowered_keys):
                    rows.append(json.dumps(node, ensure_ascii=False))

                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return self._extract_products_from_text_rows(rows)

    def _extract_products_from_leaflet_json_feed(self) -> list[dict]:
        products: list[dict] = []
        endpoint = "https://endpoints.leaflets.schwarz/v4/widget"
        params = {
            "widget_id": "0627d331-5163-11ee-9b1d-fa163f6db1d0",
            "allow_discoverables": "true",
            "region_id": "0",
            "store_id": "0",
        }
        try:
            response = requests.get(endpoint, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            products.extend(self._extract_products_from_json_like_payload(payload))
        except Exception as exc:
            LOGGER.warning("JSON feed letaku selhal (%s)", exc)

        deduped: list[dict] = []
        seen: set[tuple[str, float]] = set()
        for item in products:
            name = str(item.get("name") or "").strip().lower()
            price = self._safe_float(item.get("price"))
            if not name or price is None:
                continue
            key = (name, price)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        if deduped:
            LOGGER.info("Nacteno produktu z letaku: %s (JSON feed)", len(deduped))
        else:
            LOGGER.info("JSON feed letaku je dostupny, ale neobsahuje primo polozky s cenami.")
        return deduped

    def _collect_leaflet_image_urls(self, flyer_url: str) -> list[str]:
        image_urls: list[str] = []
        self.driver.get(flyer_url)
        self.wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(3)

        self._click_first(
            [
                (By.CSS_SELECTOR, "#onetrust-accept-btn-handler"),
                (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'souhlasim') ]"),
                (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept')]"),
            ]
        )

        for _ in range(8):
            try:
                self.driver.execute_script("window.scrollBy(0, 800);")
            except Exception:
                pass
            time.sleep(0.4)

        try:
            js_urls = self.driver.execute_script(
                """
                const urls = new Set();
                document.querySelectorAll('img').forEach((img) => {
                    const src = (img.getAttribute('src') || '').trim();
                    if (src.includes('imgproxy.leaflets.schwarz')) urls.add(src);
                });
                return Array.from(urls);
                """
            )
            if isinstance(js_urls, list):
                for value in js_urls:
                    if isinstance(value, str) and value and value not in image_urls:
                        image_urls.append(value)
        except Exception as exc:
            LOGGER.debug("JS sbirani obrazku letaku selhalo (%s)", exc)

        html = self.driver.page_source
        for value in re.findall(r"https://imgproxy\\.leaflets\\.schwarz/[^\"'\s<]+", html):
            if value not in image_urls:
                image_urls.append(value)

        high_res_urls: list[str] = []
        for url in image_urls:
            high = re.sub(r"/rs:fit:\d+:\d+:1/", "/rs:fit:1800:1800:1/", url)
            high_res_urls.append(high)

        return high_res_urls

    def _expand_flyer_targets(self, urls: list[str]) -> list[str]:
        targets: list[str] = []
        for url in urls:
            if url not in targets:
                targets.append(url)
            try:
                response = requests.get(url, timeout=20)
                response.raise_for_status()
                html = response.text
                matches = re.findall(r"https://www\\.lidl\\.cz/l/cs/letak/[^\"'\s<]+", html)
                for match in matches:
                    clean = match.split("#")[0]
                    if clean not in targets:
                        targets.append(clean)
            except Exception:
                continue
        return targets

    def _extract_products_from_text_rows(self, rows: list[str]) -> list[dict]:
        products: list[dict] = []
        seen: set[tuple[str, float]] = set()
        price_pattern = re.compile(
            r"(?<!\d)(\d{1,4}(?:[\s\u00A0]\d{3})*[\.,]\d{2}|\d{1,4}[\.,]\d)\s*(Kč|Kc|KC|CZK|K)?",
            flags=re.IGNORECASE,
        )
        noise_tokens = {
            "lidl",
            "letak",
            "strana",
            "page",
            "www",
            "cz",
            "od",
            "do",
        }

        for row in rows:
            if not isinstance(row, str):
                continue
            text = re.sub(r"\s+", " ", row).strip()
            if len(text) < 6:
                continue
            match = price_pattern.search(text)
            if not match:
                continue
            numeric = match.group(1)
            price = self._safe_float(numeric)
            if price is None:
                continue
            if price < 2 or price > 20000:
                continue

            name = text[: match.start()].strip(" -,:;")
            if not name:
                name = text
            if len(name) > 140:
                name = name[:140].strip()
            lowered = name.lower()
            if len(lowered) < 3:
                continue
            if any(token in lowered for token in noise_tokens):
                continue
            if re.fullmatch(r"[0-9\s\.,\-+/]+", name):
                continue

            key = (name.lower(), price)
            if key in seen:
                continue
            seen.add(key)

            products.append(
                {
                    "name": name,
                    "category": self._guess_category(name),
                    "price": price,
                    "original_price": None,
                    "discount": 0.0,
                }
            )

        return products

    def _extract_products_via_ocr(self, image_urls: list[str]) -> list[dict]:
        if not image_urls:
            return []
        try:
            import pytesseract
            from PIL import Image
            from PIL import ImageOps
            from io import BytesIO
        except Exception as exc:
            LOGGER.warning("OCR fallback neni dostupny (%s). Nainstalujte pillow+pytesseract a Tesseract OCR.", exc)
            return []

        tesseract_candidates = [
            os.getenv("TESSERACT_CMD", "").strip(),
            "tesseract",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        tesseract_candidates = [value for value in tesseract_candidates if value]

        tesseract_ready = False
        for candidate in tesseract_candidates:
            try:
                pytesseract.pytesseract.tesseract_cmd = candidate
                _ = pytesseract.get_tesseract_version()
                tesseract_ready = True
                break
            except Exception:
                continue

        if not tesseract_ready:
            LOGGER.warning("Tesseract OCR binary neni dostupny. Nastavte TESSERACT_CMD nebo PATH.")
            return []

        rows: list[str] = []
        max_pages = min(10, len(image_urls))
        for index, url in enumerate(image_urls[:max_pages], start=1):
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                image = Image.open(BytesIO(response.content))
                image = image.convert("L")
                image = ImageOps.autocontrast(image)
                image = image.resize((image.width * 2, image.height * 2))
                text = pytesseract.image_to_string(image, config="--oem 1 --psm 6")
                rows.extend([line for line in text.splitlines() if line.strip()])
                LOGGER.info("OCR stranky letaku %s/%s hotovo", index, max_pages)
            except Exception as exc:
                LOGGER.debug("OCR selhalo pro %s (%s)", url, exc)

        if not rows:
            LOGGER.warning("OCR probehlo, ale nevratilo zadny text z obrazku letaku")
            return []

        return self._extract_products_from_text_rows(rows)

    def _extract_products_from_weekly_offers_page(self) -> list[dict]:
        try:
            response = requests.get("https://www.lidl.cz/c/letak", timeout=20)
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            LOGGER.warning("Nepodarilo se nacist textove nabidky tydne (%s)", exc)
            return []

        soup = BeautifulSoup(html, "html.parser")
        rows: list[str] = []
        numeric_price_pattern = re.compile(r"\d{1,4}(?:[\s\u00A0]\d{3})*[\.,]\d{1,2}")
        for node in soup.select("a, h2, h3, p, span"):
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if not text:
                continue
            lowered = text.lower()
            has_currency = (
                "kč" in lowered
                or "kc" in lowered
                or "czk" in lowered
                or "k�" in lowered
            )
            has_numeric_price = bool(numeric_price_pattern.search(text))
            if not has_currency and not has_numeric_price:
                continue
            rows.append(text)

        products = self._extract_products_from_text_rows(rows)
        if products:
            LOGGER.info("Nacteno produktu z textovych nabidek tydne: %s", len(products))
        return products

    def _guess_category(self, product_name: str) -> str:
        lowered = product_name.lower()
        if any(token in lowered for token in ["mlek", "jogurt", "syr", "maslo"]):
            return "Mlecne vyrobky"
        if any(token in lowered for token in ["maso", "kure", "sunka", "salam"]):
            return "Maso a uzeniny"
        if any(token in lowered for token in ["jabl", "banan", "zelen", "rajce", "okurka"]):
            return "Ovoce a zelenina"
        if any(token in lowered for token in ["pecivo", "chleb", "rohlik"]):
            return "Pecivo"
        return "Ostatni"

    def login(self, email: str, password: str) -> None:
        LOGGER.info("Prihlasuji se do Lidl.cz")
        self.driver.get("https://www.lidl.cz/c/login")
        LOGGER.info(f"Login page loaded, URL={self.driver.current_url}")
        
        time.sleep(8)

        self._click_first([
            (By.CSS_SELECTOR, "#onetrust-accept-btn-handler"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept all')]")
        ])
        LOGGER.info("Cookie dialog clicked")

        self._open_login_form_if_needed()
        LOGGER.info("Opened login form if needed")

        time.sleep(5)
        
        # Deep Shadow DOM injection - find ALL inputs recursively
        LOGGER.info("Attempting deep Shadow DOM penetration...")
        try:
            js_deep = """
            function findAllInputs() {
                let inputs = [];
                let collected = new Set();
                
                function walkTree(node, depth = 0) {
                    if (!node || collected.has(node) || depth > 10) return;
                    collected.add(node);
                    
                    // Check regular children
                    if (node.removeChild && node.children) {
                        for (let child of node.children) {
                            walkTree(child, depth + 1);
                            if (child.tagName === 'INPUT') {
                                inputs.push({
                                    type: child.type,
                                    name: child.name,
                                    id: child.id,
                                    visible: child.offsetHeight > 0,
                                    disabled: child.disabled,
                                    value: child.value
                                });
                            }
                        }
                    }
                    
                    // Check Shadow DOM
                    if (node.shadowRoot) {
                        walkTree(node.shadowRoot, depth + 1);
                    }
                }
                
                walkTree(document.documentElement);
                return inputs;
            }
            
            let all_inputs = findAllInputs();
            console.log('Found inputs:', all_inputs.length);
            return all_inputs;
            """
            
            all_inputs = self.driver.execute_script(js_deep)
            LOGGER.info(f"Deep search found {len(all_inputs) if all_inputs else 0} total inputs: {all_inputs}")
            
            # Try to fill first email-like and first password-like inputs
            if all_inputs and len(all_inputs) >= 2:
                # Find email input
                email_input = next((i for i, inp in enumerate(all_inputs) if inp['type'] in ['email', 'text'] and not inp['disabled']), None)
                password_input = next((i for i, inp in enumerate(all_inputs) if inp['type'] == 'password' and not inp['disabled']), None)
                
                if email_input is not None and password_input is not None:
                    js_fill = f"""
                    let all_inputs = document.querySelectorAll('input');
                    if (all_inputs.length > {email_input}) {{
                        all_inputs[{email_input}].focus();
                        all_inputs[{email_input}].value = arguments[0];
                        all_inputs[{email_input}].dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                    if (all_inputs.length > {password_input}) {{
                        all_inputs[{password_input}].focus();
                        all_inputs[{password_input}].value = arguments[1];
                        all_inputs[{password_input}].dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                    return true;
                    """
                    result = self.driver.execute_script(js_fill, email, password)
                    LOGGER.info(f"Deep fill via index [{email_input}, {password_input}] result: {result}")
                    time.sleep(2)
                else:
                    LOGGER.warning(f"Could not find email/password indices: email={email_input}, password={password_input}")
        except Exception as e:
            LOGGER.warning(f"Deep Shadow DOM approach failed: {e}")

        # Last resort - try ANY visible input approach
        LOGGER.info("Attempting last-resort 'any visible input' approach...")
        try:
            js_any = """
            let inputs = Array.from(document.querySelectorAll('input'));
            let visible_inputs = inputs.filter(i => i.offsetHeight > 0 && !i.disabled);
            console.log('Visible inputs:', visible_inputs.length);
            if (visible_inputs.length >= 2) {
                visible_inputs[0].focus();
                visible_inputs[0].value = arguments[0];
                visible_inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
                visible_inputs[0].dispatchEvent(new Event('change', {bubbles: true}));
                
                visible_inputs[1].focus();
                visible_inputs[1].value = arguments[1];
                visible_inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
                visible_inputs[1].dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            return false;
            """
            result = self.driver.execute_script(js_any, email, password)
            LOGGER.info(f"Last-resort any-input approach result: {result}")
        except Exception as e:
            LOGGER.error(f"Last-resort approach failed: {e}")
            raise RuntimeError("Nepodarilo se najit a vyplnit prihlasovaci fieldy (all methods failed)")

        # Click submit
        time.sleep(2)
        try:
            js_submit = """
            let btns = Array.from(document.querySelectorAll('button, input[type="submit"]'));
            let visible = btns.find(b => b.offsetHeight > 0 && !b.disabled && !b.hidden);
            if (visible) {
                visible.click();
                return true;
            }
            return false;
            """
            result = self.driver.execute_script(js_submit)
            LOGGER.info(f"Submit via JavaScript: {result}")
        except Exception as e:
            LOGGER.warning(f"Could not submit form: {e}")

        time.sleep(5)

        try:
            self.wait.until(lambda d: "/login" not in d.current_url.lower())
        except TimeoutException:
            LOGGER.warning("URL po submitu zůstala na loginu, ověřuji stav...")

        if not self._looks_logged_in():
            raise RuntimeError("Prihlaseni do Lidl.cz selhalo (nebyly nalezeny znamky prihlasene relace).")

        self._is_logged_in = True
        LOGGER.info("Prihlaseni uspesne")

    def get_purchase_history(self) -> list[dict]:
        if not self._is_logged_in and not self._refresh_token:
            raise RuntimeError("Nejdrive zavolejte login(email, password).")

        if self._refresh_token:
            LOGGER.info("Nacitam nakupni historii pres Lidl Plus API")
            try:
                mre_purchases = self._get_purchase_history_via_mre_api()
                LOGGER.info("Nacteno polozek z uctenek: %s (MRE API)", len(mre_purchases))
                if mre_purchases:
                    return mre_purchases
            except Exception as exc:
                LOGGER.warning("MRE API historie selhala (%s), zkousim puvodni mobile API", exc)

            try:
                api_purchases = self._get_purchase_history_via_api()
                LOGGER.info("Nacteno polozek z uctenek: %s (mobile API)", len(api_purchases))
                if api_purchases:
                    return api_purchases
                if not self._is_logged_in:
                    LOGGER.warning("API vratilo prazdny seznam a web login neni aktivni; web fallback preskakuji.")
                    return []
            except Exception as exc:
                LOGGER.warning("API historie selhala (%s), zkousim web fallback", exc)
                if not self._is_logged_in:
                    LOGGER.warning("Web fallback preskakuji, protoze neni aktivni web login relace.")
                    return []

        LOGGER.info("Nacitam nakupni historii")
        self.driver.get("https://www.lidl.cz/c/moje-uctenky")
        self.wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(3)  # Extra wait for dynamic content
        
        html = self.driver.page_source
        LOGGER.info(f"Receipt page length: {len(html)}")
        
        # Check what's on the page
        has_uctenka = "uctenka" in html.lower()
        has_receipt = "receipt" in html.lower()
        has_transakce = "transakce" in html.lower()
        LOGGER.info(f"Page contains: 'uctenka'={has_uctenka}, 'receipt'={has_receipt}, 'transakce'={has_transakce}")
        
        # Count key elements
        li_count = html.count("<li")
        div_count = html.count("<div")
        LOGGER.info(f"Basic HTML counts: <li>={li_count}, <div>={div_count}")
        
        soup = BeautifulSoup(html, "html.parser")
        purchases: list[dict] = []

        # Strategy: Look for ANY elements with price patterns first
        # This will tell us WHERE purchases might be hiding
        price_pattern = re.compile(r"\d+[\.,]\d{1,2}\s*(Kč|CZK|Kc)")
        elements_with_prices = []
        
        for elem in soup.find_all(["div", "li", "article", "tr", "section"]):
            text = elem.get_text(" ", strip=True)
            if price_pattern.search(text) and len(text) > 10:
                elements_with_prices.append({
                    "tag": elem.name,
                    "class": elem.get("class", []),
                    "text_preview": text[:100],
                    "price_match": price_pattern.search(text).group()
                })
        
        if elements_with_prices:
            LOGGER.info(f"Found {len(elements_with_prices)} elements containing prices")
            for sample in elements_with_prices[:3]:
                LOGGER.info(f"  {sample['tag']}.{'.'.join(sample['class'])}: {sample['text_preview'][:60]} ... PRICE: {sample['price_match']}")
        else:
            LOGGER.warning("NO elements with prices found on page! Page might not have loaded purchases.")
            return []

        # Now try targeted selectors specifically for purchase items
        # We know purchases have prices, so focus on that
        selectors_to_try = [
            "li[class*='item']",
            "li[class*='receipt']",
            "div[class*='receipt']",
            "div[class*='transaction']",
            "section[class*='transaction']",
            "article[class*='purchase']",
            "tr[class*='order']",
        ]
        
        for selector in selectors_to_try:
            elements = soup.select(selector)
            if elements:
                LOGGER.info(f"Selector '{selector}' found {len(elements)} elements")
                
                for elem in elements:
                    text = elem.get_text(" ", strip=True)
                    if not text or len(text) < 3:
                        continue
                    
                    # Extract price
                    price_match = price_pattern.search(text)
                    if price_match:
                        price_str = price_match.group()
                        try:
                            price = float(price_str.replace(",", ".").replace("Kč", "").replace("CZK", "").replace("Kc", "").strip())
                        except ValueError:
                            continue
                    else:
                        continue
                    
                    # Extract name by removing all prices from text
                    name = price_pattern.sub("", text).strip()
                    if len(name) < 2 or len(name) > 300:
                        continue
                    
                    purchases.append({
                        "name": name,
                        "category": self._guess_category(name),
                        "quantity": 1,
                        "price": price,
                        "purchased_at": datetime.now(timezone.utc).isoformat(),
                    })
                
                if purchases:
                    LOGGER.info(f"✓ Extracted {len(purchases)} purchases from selector '{selector}'")
                    break

        if not purchases:
            LOGGER.warning("No purchases extracted - trying generic search with price patterns...")
            # Last resort: find ANY text with price and try to parse it
            for elem in soup.find_all(["li", "div", "article"]):
                text = elem.get_text(" ", strip=True)
                if price_pattern.search(text) and len(text) > 10 and len(text) < 500:
                    price_match = price_pattern.search(text)
                    if price_match:
                        try:
                            price = float(price_match.group().replace(",", ".").replace("Kč", "").replace("CZK", "").replace("Kc", "").strip())
                            name = price_pattern.sub("", text).strip()[:200]
                            if len(name) > 2:
                                purchases.append({
                                    "name": name,
                                    "category": self._guess_category(name),
                                    "quantity": 1,
                                    "price": price,
                                    "purchased_at": datetime.now(timezone.utc).isoformat(),
                                })
                        except ValueError:
                            pass

        LOGGER.info(f"Nacteno polozek z uctenek: {len(purchases)}")
        return purchases

    def get_flyer(self) -> list[dict]:
        LOGGER.info("Stahuji aktualni Lidl letak")

        flyer_candidates = self._discover_flyer_candidates()
        target_candidates = [item for item in flyer_candidates if self._is_target_akce_flyer(item)]

        if target_candidates:
            LOGGER.info("Nalezeno cilovych akcnich letaku (ctvrtek/pondeli): %s", len(target_candidates))
            primary = target_candidates[-2:] if len(target_candidates) >= 2 else target_candidates
            primary_ids = {str(item.get("flyer_identifier") or "") for item in primary}
            backup = [
                item
                for item in target_candidates
                if str(item.get("flyer_identifier") or "") not in primary_ids
            ]
            LOGGER.info("Zpracovavam posledni 2 cilove akcni letaky: %s", len(primary))
        else:
            LOGGER.warning("Cilove akcni letaky (ctvrtek/pondeli) nenalezeny, pouzivam obecny vyber")
            primary = [item for item in flyer_candidates if item.get("week_scope") == "next"]
            primary.extend(item for item in flyer_candidates if item.get("week_scope") != "next")
            backup = []

        if primary and primary[0].get("week_scope") == "next":
            LOGGER.info("Preferuji dalsi letak (next week)")

        def collect_from(candidates: list[dict[str, Any]], all_candidates: list[dict[str, Any]], max_items: int = 8) -> list[dict]:
            selected_products: list[dict] = []
            used_identifiers: set[str] = set()
            for item in candidates[:max_items]:
                flyer_identifier = str(item.get("flyer_identifier") or "")
                if not flyer_identifier:
                    continue
                used_identifiers.add(flyer_identifier)
                products = self._extract_products_from_flyer_viewer_api(flyer_identifier)
                if products:
                    selected_products.extend(products)
                    continue

                if self._is_target_akce_flyer(item):
                    fallback_candidate = self._find_spotrebni_match_for_target(item, all_candidates, used_identifiers)
                    if fallback_candidate:
                        fallback_id = str(fallback_candidate.get("flyer_identifier") or "")
                        if fallback_id:
                            used_identifiers.add(fallback_id)
                            LOGGER.info(
                                "Akcni letak bez produktu, zkousim parovy spotrebni letak (%s)",
                                fallback_id,
                            )
                            fallback_products = self._extract_products_from_flyer_viewer_api(fallback_id)
                            if fallback_products:
                                selected_products.extend(fallback_products)
            return selected_products

        selected_products = collect_from(primary, flyer_candidates)

        if not selected_products and backup:
            LOGGER.info("Primarni vyber letaku vratil 0 produktu, zkousim zbyvajici cilove letaky")
            selected_products = collect_from(backup, flyer_candidates)

        if selected_products:
            merged = self._dedupe_products(selected_products)
            LOGGER.info("Nacteno produktu ze zvolenych letaku: %s", len(merged))
            return merged

        json_feed_products = self._extract_products_from_leaflet_json_feed()
        if json_feed_products:
            return json_feed_products

        flyer_urls = self._discover_flyer_urls()
        if not flyer_urls:
            LOGGER.warning("Nepodarilo se najit aktualni URL letaku")
            return []

        flyer_urls = self._expand_flyer_targets(flyer_urls)

        LOGGER.info("Nalezeno kandidatu na letak: %s", len(flyer_urls))
        products: list[dict] = []

        for candidate in flyer_urls[:3]:
            try:
                image_urls = self._collect_leaflet_image_urls(candidate)
                LOGGER.info("Letak %s obsahuje %s obrazku stranek", candidate, len(image_urls))
                products = self._extract_products_via_ocr(image_urls)
                if products:
                    LOGGER.info("Nacteno produktu z letaku: %s (OCR)", len(products))
                    return products
            except Exception as exc:
                LOGGER.warning("Zpracovani letaku selhalo pro %s (%s)", candidate, exc)

        products = self._extract_products_from_weekly_offers_page()
        if products:
            return products

        LOGGER.warning("Z letaku se nepodarilo vytezit zadne produkty")
        return []

    def close(self) -> None:
        self.driver.quit()
