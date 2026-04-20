from __future__ import annotations

import json
import logging
import re
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

        self._click_first([
            (By.CSS_SELECTOR, "#onetrust-accept-btn-handler"),
            (By.XPATH, "//button[contains(., 'Přijmout vše') or contains(., 'Accept all')]")
        ])

        email_filled = self._fill_first([
            (By.CSS_SELECTOR, "input[type='email']"),
            (By.CSS_SELECTOR, "input[name='email']"),
        ], email)
        password_filled = self._fill_first([
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.CSS_SELECTOR, "input[name='password']"),
        ], password)

        if not email_filled or not password_filled:
            raise RuntimeError("Nepodarilo se najit prihlasovaci formular Lidl.")

        self._click_first([
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//button[contains(., 'Přihlásit') or contains(., 'Sign in')]")
        ])

        try:
            self.wait.until(ec.url_changes("https://www.lidl.cz/c/login"))
        except TimeoutException:
            page_text = self.driver.page_source.lower()
            if "muj ucet" not in page_text and "odhlasit" not in page_text and "logout" not in page_text:
                raise RuntimeError("Prihlaseni do Lidl.cz selhalo.")

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
