from __future__ import annotations

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

    def match_flyer_products(self, flyer_products: list[dict]) -> list[dict]:
        if not flyer_products:
            return []

        top_categories = {cat.lower() for cat in self.get_top_categories()}
        top_products = {prod.lower() for prod in self.get_top_products()}
        matched: list[dict] = []

        for product in flyer_products:
            name = str(product.get("name", "")).strip()
            category = str(product.get("category", "Ostatni")).strip()
            if not name:
                continue

            name_l = name.lower()
            category_l = category.lower()
            product_match = any(token in name_l or name_l in token for token in top_products)
            category_match = category_l in top_categories
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
