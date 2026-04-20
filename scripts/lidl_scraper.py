from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

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
        self.driver.get("https://www.lidl.cz/c/letak/s10008688")
        self.wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(3)  # Extra wait for dynamic content
        
        html = self.driver.page_source
        LOGGER.info(f"Flyer page length: {len(html)}, contains 'letak': {'letak' in html.lower()}")

        soup = BeautifulSoup(html, "html.parser")
        products: list[dict] = []

        # Runtime JS extraction first - page_source can miss data rendered after hydration.
        try:
            js_result = self.driver.execute_script(
                r"""
                const priceRegex = /\d+[\.,]\d{1,2}\s*(Kč|Kc|CZK)/i;
                const selectors = [
                    "[data-testid*='product']",
                    "[class*='product']",
                    "[class*='offer']",
                    "article",
                    "li",
                    "div"
                ];
                const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
                const seen = new Set();
                const rows = [];
                for (const node of nodes) {
                    const text = (node.innerText || "").replace(/\s+/g, " ").trim();
                    if (!text || text.length < 6 || text.length > 260) continue;
                    if (!priceRegex.test(text)) continue;
                    if (seen.has(text)) continue;
                    seen.add(text);
                    rows.push(text);
                    if (rows.length >= 300) break;
                }
                return rows;
                """
            )
            if js_result:
                for text in js_result:
                    if not isinstance(text, str):
                        continue
                    name = text.split("Kč")[0].split("Kc")[0].split("CZK")[0].strip()
                    price = self._extract_price(text)
                    if not name or price is None:
                        continue
                    old_price = None
                    all_prices = re.findall(r"(\d+[\.,]\d{1,2})\s*(Kč|Kc|Kc\.|CZK)", text, flags=re.IGNORECASE)
                    if len(all_prices) > 1:
                        old_price = self._safe_float(all_prices[1][0])
                    discount_percent = 0.0
                    if old_price and old_price > price:
                        discount_percent = round((old_price - price) / old_price * 100, 1)
                    products.append(
                        {
                            "name": name,
                            "category": self._guess_category(name),
                            "price": price,
                            "original_price": old_price,
                            "discount": discount_percent,
                        }
                    )
                if products:
                    LOGGER.info(f"Nacteno produktu z letaku: {len(products)} (runtime JS)")
                    return products
        except Exception as exc:
            LOGGER.debug(f"Runtime JS extraction failed: {exc}")

        # Try multiple selectors
        selectors = [
            "article",
            "[class*='product']",
            "[class*='offer']",
            "[class*='tile']",
            "li",
            ".product-item",
            "[data-testid*='product']",
        ]
        
        found_elements = {}
        for selector in selectors:
            try:
                elements = soup.select(selector)
                if elements:
                    found_elements[selector] = len(elements)
                    LOGGER.debug(f"Selector '{selector}' found {len(elements)} elements")
            except Exception:
                pass
        
        if found_elements:
            LOGGER.info(f"Flyer element search results: {found_elements}")

        for tile in soup.select(", ".join(selectors)):
            text = tile.get_text(" ", strip=True)
            if len(text) < 4:
                continue

            name = (tile.get("aria-label") or text.split("Kc")[0].split("Kč")[0]).strip()
            price = self._extract_price(text)
            if not name or price is None:
                continue

            old_price = None
            all_prices = re.findall(r"(\d+[\.,]\d{1,2})\s*(Kč|Kc|Kc\.|CZK)", text, flags=re.IGNORECASE)
            if len(all_prices) > 1:
                try:
                    old_price = float(all_prices[1][0].replace(",", "."))
                except ValueError:
                    old_price = None

            discount_percent = 0.0
            if old_price and old_price > price:
                discount_percent = round((old_price - price) / old_price * 100, 1)

            products.append(
                {
                    "name": name,
                    "category": self._guess_category(name),
                    "price": price,
                    "original_price": old_price,
                    "discount": discount_percent,
                }
            )

        LOGGER.info(f"Nacteno produktu z letaku: {len(products)}")
        return products

    def close(self) -> None:
        self.driver.quit()
