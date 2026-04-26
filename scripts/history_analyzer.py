from __future__ import annotations

import re
import unicodedata

import pandas as pd


class HistoryAnalyzer:
    def __init__(self) -> None:
        self.df = pd.DataFrame()

    def analyze(self, purchase_data: list[dict]) -> None:
        self.df = pd.DataFrame(purchase_data)
        if self.df.empty:
            return

        if "quantity" not in self.df.columns:
            self.df["quantity"] = 1
        self.df["quantity"] = pd.to_numeric(self.df["quantity"], errors="coerce").fillna(1)
        if "name" not in self.df.columns:
            self.df["name"] = "Unknown"
        if "category" not in self.df.columns:
            self.df["category"] = "Ostatni"

    def get_top_categories(self) -> list[str]:
        if self.df.empty:
            return []
        return (
            self.df.groupby("category", as_index=False)["quantity"]
            .sum()
            .sort_values("quantity", ascending=False)
            .head(5)["category"]
            .astype(str)
            .tolist()
        )

    def get_top_products(self) -> list[str]:
        if self.df.empty:
            return []
        return (
            self.df.groupby("name", as_index=False)["quantity"]
            .sum()
            .sort_values("quantity", ascending=False)
            .head(10)["name"]
            .astype(str)
            .tolist()
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s/]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _category_matches(self, category: str, top_categories: set[str]) -> bool:
        normalized_category = self._normalize_text(category)
        if not normalized_category:
            return False

        category_tokens = {
            token
            for token in re.split(r"[\s/]+", normalized_category)
            if len(token) >= 3
        }

        for top_category in top_categories:
            normalized_top = self._normalize_text(top_category)
            if not normalized_top:
                continue

            if normalized_top == normalized_category:
                return True
            if normalized_top in normalized_category or normalized_category in normalized_top:
                return True

            top_tokens = {
                token
                for token in re.split(r"[\s/]+", normalized_top)
                if len(token) >= 3
            }
            if category_tokens and top_tokens and (category_tokens & top_tokens):
                return True

        return False

    def match_flyer_products(self, flyer_products: list[dict]) -> list[dict]:
        if not flyer_products:
            return []

        top_categories = set(self.get_top_categories())
        top_products = {self._normalize_text(prod) for prod in self.get_top_products() if str(prod).strip()}
        matched: list[dict] = []

        for product in flyer_products:
            name = str(product.get("name", "")).strip()
            category = str(product.get("category", "Ostatni")).strip()
            if not name:
                continue

            name_l = self._normalize_text(name)
            product_match = any(token and (token in name_l or name_l in token) for token in top_products)
            category_match = self._category_matches(category, top_categories)
            if not product_match and not category_match:
                continue

            score = 3 if product_match else 0
            score += 2 if category_match else 0

            discount = float(product.get("discount") or 0.0)
            score += min(discount / 10, 3)

            enriched = dict(product)
            enriched["score"] = round(score, 2)
            matched.append(enriched)

        matched.sort(key=lambda item: item.get("score", 0), reverse=True)
        return matched
