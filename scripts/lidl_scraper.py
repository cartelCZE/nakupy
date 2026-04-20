from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

LOGGER = logging.getLogger(__name__)


class LidlScraper:
    def __init__(self, headless: bool = True) -> None:
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 20)
        self._is_logged_in = False

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
        page_text = self.driver.page_source.lower()
        markers = [
            "odhlasit",
            "logout",
            "muj ucet",
            "moje uctenky",
            "profil",
        ]
        if any(marker in page_text for marker in markers):
            return True
        current_url = self.driver.current_url.lower()
        return "/account" in current_url or "moje-uctenky" in current_url

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
            self.wait.until(lambda d: self._looks_logged_in() or "error" in d.page_source.lower())
            LOGGER.info("Login check passed")
        except TimeoutException:
            LOGGER.warning("Login check timeout, verifying logged-in status")
            if not self._looks_logged_in():
                raise RuntimeError("Prihlaseni do Lidl.cz selhalo (timeout po submitu).")

        self._is_logged_in = True
        LOGGER.info("Prihlaseni uspesne")

    def get_purchase_history(self) -> list[dict]:
        if not self._is_logged_in:
            raise RuntimeError("Nejdrive zavolejte login(email, password).")

        LOGGER.info("Nacitam nakupni historii")
        self.driver.get("https://www.lidl.cz/c/moje-uctenky")
        self.wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        html = self.driver.page_source

        soup = BeautifulSoup(html, "html.parser")
        purchases: list[dict] = []

        for row in soup.select("article, li, [class*='receipt'], [class*='item']"):
            text = row.get_text(" ", strip=True)
            price = self._extract_price(text)
            if not text or price is None:
                continue
            name = re.sub(r"\s+\d+[\.,]\d{1,2}\s*(Kc|Kc\.|CZK).*", "", text, flags=re.IGNORECASE).strip(" -")
            if len(name) < 2:
                continue
            purchases.append(
                {
                    "name": name,
                    "category": self._guess_category(name),
                    "quantity": 1,
                    "price": price,
                    "purchased_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        if purchases:
            return purchases

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "{}")
            except json.JSONDecodeError:
                continue
            objects = data if isinstance(data, list) else [data]
            for obj in objects:
                if obj.get("@type") != "Product":
                    continue
                name = (obj.get("name") or "").strip()
                offers = obj.get("offers") or {}
                raw_price = offers.get("price")
                if not name or raw_price is None:
                    continue
                try:
                    price = float(str(raw_price).replace(",", "."))
                except ValueError:
                    continue
                purchases.append(
                    {
                        "name": name,
                        "category": self._guess_category(name),
                        "quantity": 1,
                        "price": price,
                        "purchased_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

        LOGGER.info("Nacteno polozek z uctenek: %s", len(purchases))
        return purchases

    def get_flyer(self) -> list[dict]:
        LOGGER.info("Stahuji aktualni Lidl letak")
        self.driver.get("https://www.lidl.cz/c/letak/s10008688")
        self.wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        html = self.driver.page_source

        soup = BeautifulSoup(html, "html.parser")
        products: list[dict] = []

        for tile in soup.select("article, [class*='product'], [class*='offer']"):
            text = tile.get_text(" ", strip=True)
            if len(text) < 4:
                continue

            name = (tile.get("aria-label") or text.split("Kc")[0].split("Kč")[0]).strip()
            price = self._extract_price(text)
            if not name or price is None:
                continue

            old_price = None
            all_prices = re.findall(r"(\d+[\.,]\d{1,2})\s*(Kc|Kc\.|CZK)", text, flags=re.IGNORECASE)
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

        unique: dict[str, dict] = {}
        for product in products:
            unique.setdefault(product["name"].lower(), product)
        result = list(unique.values())
        LOGGER.info("Nacteno produktu z letaku: %s", len(result))
        return result

    def close(self) -> None:
        self.driver.quit()
