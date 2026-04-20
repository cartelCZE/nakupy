from __future__ import annotations

from dataclasses import asdict

import pandas as pd

from lidl_scraper import FlyerItem, PurchaseItem


class HistoryAnalyzer:
    def __init__(self, purchases: list[PurchaseItem]) -> None:
        self.purchases = purchases
        records = [asdict(item) for item in purchases]
        self.df = pd.DataFrame(records)

    def most_frequent_products(self, top_n: int = 15) -> list[str]:
        if self.df.empty:
            return []
        return (
            self.df.groupby("name", as_index=False)["quantity"]
            .sum()
            .sort_values("quantity", ascending=False)
            .head(top_n)["name"]
            .tolist()
        )

    def top_categories(self, top_n: int = 8) -> list[str]:
        if self.df.empty:
            return []
        return (
            self.df.groupby("category", as_index=False)["quantity"]
            .sum()
            .sort_values("quantity", ascending=False)
            .head(top_n)["category"]
            .tolist()
        )

    def recommend_from_flyer(self, flyer_items: list[FlyerItem], limit: int = 20) -> list[dict]:
        if not flyer_items:
            return []

        frequent_products = {name.lower() for name in self.most_frequent_products(100)}
        categories = {name.lower() for name in self.top_categories(100)}

        recommended: list[dict] = []
        for item in flyer_items:
            name_l = item.name.lower()
            cat_l = item.category.lower()

            exact_product_match = any(fp in name_l or name_l in fp for fp in frequent_products)
            category_match = cat_l in categories
            if not (exact_product_match or category_match):
                continue

            discount_pct = 0.0
            if item.original_price and item.original_price > item.price:
                discount_pct = round((item.original_price - item.price) / item.original_price * 100, 1)

            score = 0.0
            score += 4.0 if exact_product_match else 0.0
            score += 2.0 if category_match else 0.0
            score += min(discount_pct / 10.0, 4.0)
            score += 1.0 if item.available else -2.0

            recommended.append(
                {
                    "name": item.name,
                    "category": item.category,
                    "price": item.price,
                    "original_price": item.original_price,
                    "discount_pct": discount_pct,
                    "available": item.available,
                    "score": round(score, 2),
                }
            )

        recommended.sort(key=lambda row: row["score"], reverse=True)
        return recommended[:limit]
