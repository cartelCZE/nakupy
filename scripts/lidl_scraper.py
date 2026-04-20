from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from bs4 import BeautifulSoup
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

LOGGER = logging.getLogger(__name__)


@dataclass
class PurchaseItem:
    name: str
    category: str
    quantity: int
    price: float
    purchased_at: datetime


@dataclass
class FlyerItem:
    name: str
    category: str
    price: float
    original_price: float | None
    available: bool


class LidlScraper:
    def __init__(self, email: str, password: str, headless: bool = True) -> None:
        self.email = email
        self.password = password
        self.headless = headless

    def _click_if_visible(self, page: Page, selectors: Iterable[str]) -> None:
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click()
                return

    def _fill_first(self, page: Page, selectors: Iterable[str], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.fill(value)
                return True
        return False

    def _login(self, page: Page) -> None:
        LOGGER.info("Otevírám přihlašovací stránku Lidl účtu")
        page.goto("https://www.lidl.cz/c/login", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        self._click_if_visible(page, [
            "button:has-text('Přijmout vše')",
            "button:has-text('Accept all')",
            "#onetrust-accept-btn-handler",
        ])

        email_filled = self._fill_first(page, [
            "input[type='email']",
            "input[name='email']",
            "#input-email",
        ], self.email)
        password_filled = self._fill_first(page, [
            "input[type='password']",
            "input[name='password']",
            "#input-password",
        ], self.password)

        if not (email_filled and password_filled):
            raise RuntimeError("Nepodařilo se najít přihlašovací formulář Lidl.")

        self._click_if_visible(page, [
            "button[type='submit']",
            "button:has-text('Přihlásit')",
            "button:has-text('Sign in')",
        ])

        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            LOGGER.warning("Timeout při čekání na dokončení přihlášení")

        html = page.content().lower()
        if "odhl" in html or "logout" in html or "můj účet" in html:
            LOGGER.info("Přihlášení bylo úspěšné")
            return

        raise RuntimeError("Přihlášení do Lidl účtu selhalo.")

    def _category_from_name(self, name: str) -> str:
        lowered = name.lower()
        if any(word in lowered for word in ["mlé", "jogurt", "sýr", "máslo"]):
            return "Mléčné výrobky"
        if any(word in lowered for word in ["maso", "kuře", "šunka", "salám"]):
            return "Maso a uzeniny"
        if any(word in lowered for word in ["jabl", "banán", "zelen", "rajče", "okurka"]):
            return "Ovoce a zelenina"
        if any(word in lowered for word in ["pečivo", "chléb", "rohlík"]):
            return "Pečivo"
        return "Ostatní"

    def _extract_price(self, text: str) -> float | None:
        match = re.search(r"(\d+[\.,]\d{1,2})\s*(Kč|CZK)", text, flags=re.IGNORECASE)
        if not match:
            return None
        return float(match.group(1).replace(",", "."))

    def _parse_receipts(self, html: str) -> list[PurchaseItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[PurchaseItem] = []

        for row in soup.select("[class*='receipt'] [class*='product'], [class*='item']"):
            text = row.get_text(" ", strip=True)
            if len(text) < 3:
                continue
            price = self._extract_price(text)
            if price is None:
                continue
            name = re.sub(r"\s+\d+[\.,]\d{1,2}\s*(Kč|CZK).*", "", text, flags=re.IGNORECASE).strip()
            if not name:
                continue
            items.append(
                PurchaseItem(
                    name=name,
                    category=self._category_from_name(name),
                    quantity=1,
                    price=price,
                    purchased_at=datetime.now(timezone.utc),
                )
            )

        if items:
            return items

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "{}")
            except json.JSONDecodeError:
                continue
            candidates = data if isinstance(data, list) else [data]
            for candidate in candidates:
                if candidate.get("@type") != "Product":
                    continue
                name = (candidate.get("name") or "").strip()
                offers = candidate.get("offers") or {}
                raw_price = offers.get("price")
                if not name or raw_price is None:
                    continue
                try:
                    price = float(str(raw_price).replace(",", "."))
                except ValueError:
                    continue
                items.append(
                    PurchaseItem(
                        name=name,
                        category=self._category_from_name(name),
                        quantity=1,
                        price=price,
                        purchased_at=datetime.now(timezone.utc),
                    )
                )

        return items

    def _parse_flyer(self, html: str) -> list[FlyerItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[FlyerItem] = []

        for tile in soup.select("article, [class*='product'], [class*='offer']"):
            text = tile.get_text(" ", strip=True)
            if len(text) < 4:
                continue
            name = tile.get("aria-label") or text.split("Kč")[0].strip()
            price = self._extract_price(text)
            if not name or price is None:
                continue

            original_price = None
            prices = re.findall(r"(\d+[\.,]\d{1,2})\s*(Kč|CZK)", text, flags=re.IGNORECASE)
            if len(prices) > 1:
                try:
                    original_price = float(prices[1][0].replace(",", "."))
                except ValueError:
                    original_price = None

            items.append(
                FlyerItem(
                    name=name,
                    category=self._category_from_name(name),
                    price=price,
                    original_price=original_price,
                    available="není skladem" not in text.lower(),
                )
            )

        dedup: dict[str, FlyerItem] = {}
        for item in items:
            dedup.setdefault(item.name.lower(), item)
        return list(dedup.values())

    def fetch_receipts(self) -> list[PurchaseItem]:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            self._login(page)
            LOGGER.info("Načítám stránku účtenek")
            page.goto("https://www.lidl.cz/c/moje-uctenky", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            content = page.content()
            browser.close()

        items = self._parse_receipts(content)
        LOGGER.info("Načteno položek z historie: %s", len(items))
        return items

    def fetch_weekly_flyer(self) -> list[FlyerItem]:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            LOGGER.info("Načítám aktuální leták")
            page.goto("https://www.lidl.cz/c/letak/s10008688", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            content = page.content()
            browser.close()

        items = self._parse_flyer(content)
        LOGGER.info("Načteno položek z letáku: %s", len(items))
        return items
