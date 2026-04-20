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
                except Exception:
                    continue
                for element in elements:
                    try:
                        if element.is_displayed() and element.is_enabled():
                            return element, context
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
        element, context = self._find_first_interactable(selectors)
        if element is None:
            return False
        if context is not None and not self._switch_context(context):
            return False
        try:
            element.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", element)
        return True

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

    def _wait_for_form_render(self) -> None:
        def _form_or_iframe_present(driver: webdriver.Chrome) -> bool:
            driver.switch_to.default_content()
            if driver.find_elements(By.CSS_SELECTOR, "iframe, frame"):
                LOGGER.debug("Form check: iframe found")
                return True
            selectors = [
                (By.CSS_SELECTOR, "input[type='email']"),
                (By.CSS_SELECTOR, "input[type='password']"),
                (By.CSS_SELECTOR, "input[name*='email' i]"),
                (By.CSS_SELECTOR, "input[name*='user' i]"),
                (By.CSS_SELECTOR, "input[name*='identifier' i]"),
            ]
            for by, selector in selectors:
                if driver.find_elements(by, selector):
                    LOGGER.debug(f"Form check: selector {selector} found")
                    return True
            return False

        try:
            self.wait.until(_form_or_iframe_present)
            LOGGER.info("Form render wait succeeded")
        except TimeoutException:
            LOGGER.error("Form render timeout")
            raise

    def _open_login_form_if_needed(self) -> None:
        clicked = self._click_first_any_context([
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'prihlas')]"),
            (By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'prihlas')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
            (By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'muj ucet')]"),
        ])
        LOGGER.info(f"_open_login_form_if_needed: clicked={clicked}")

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
        
        time.sleep(5)

        self._click_first([
            (By.CSS_SELECTOR, "#onetrust-accept-btn-handler"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept all')]")
        ])
        LOGGER.info("Cookie dialog clicked")

        self._open_login_form_if_needed()
        LOGGER.info("Opened login form if needed")

        time.sleep(2)

        email_filled = self._fill_login_field([
            (By.CSS_SELECTOR, "input[type='email']"),
            (By.CSS_SELECTOR, "input[name='email']"),
            (By.CSS_SELECTOR, "input[name*='email' i]"),
            (By.CSS_SELECTOR, "input[id*='email' i]"),
            (By.CSS_SELECTOR, "input[name='username']"),
            (By.CSS_SELECTOR, "input[name*='user' i]"),
            (By.CSS_SELECTOR, "input[name*='identifier' i]"),
            (By.CSS_SELECTOR, "input[autocomplete='username']"),
            (By.CSS_SELECTOR, "input[type='text']"),
        ], email)
        LOGGER.info(f"Email filled: {email_filled}")
        if not email_filled:
            self.driver.switch_to.default_content()
            iframes = self.driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            LOGGER.error(f"Email field not found in {len(iframes)} iframes, trying to inspect each...")
            for i, iframe in enumerate(iframes):
                try:
                    self.driver.switch_to.frame(iframe)
                    inputs = self.driver.find_elements(By.CSS_SELECTOR, "input")
                    LOGGER.error(f"  iframe {i}: {len(inputs)} inputs found")
                    for inp in inputs[:5]:
                        LOGGER.error(f"    input: type={inp.get_attribute('type')}, name={inp.get_attribute('name')}, id={inp.get_attribute('id')}")
                except Exception as e:
                    LOGGER.error(f"  iframe {i}: error: {e}")
                finally:
                    self.driver.switch_to.default_content()

        password_filled = self._fill_login_field([
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.CSS_SELECTOR, "input[name='password']"),
            (By.CSS_SELECTOR, "input[name*='password' i]"),
            (By.CSS_SELECTOR, "input[id*='password' i]"),
            (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
        ], password)
        LOGGER.info(f"Password filled: {password_filled}")

        if not email_filled or not password_filled:
            self.driver.switch_to.default_content()
            iframes = self.driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            LOGGER.error(f"Form fields not filled. iframe_count={len(iframes)}")
            for i, iframe in enumerate(iframes):
                try:
                    src = iframe.get_attribute("src")
                    frame_id = iframe.get_attribute("id")
                    LOGGER.error(f"  iframe {i}: id={frame_id}, src={src}")
                except Exception as e:
                    LOGGER.error(f"  iframe {i}: error reading attrs: {e}")
            raise RuntimeError(
                f"Nepodarilo se vyplnit prihlasovaci fieldy, email_filled={email_filled}, password_filled={password_filled}, iframe_count={len(iframes)}"
            )

        clicked = self._click_first_any_context([
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'prihlas')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
        ])
        LOGGER.info(f"Submit button clicked: {clicked}")
        if not clicked:
            self.driver.switch_to.default_content()
            page = self.driver.page_source[:2000]
            LOGGER.error(f"Submit button not found. page_start={page}")
            raise RuntimeError("Nepodarilo se najit tlacitko pro potvrzeni prihlaseni.")

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
