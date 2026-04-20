from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from email_sender import EmailSender
from history_analyzer import HistoryAnalyzer
from lidl_scraper import LidlScraper


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Chybí požadovaná proměnná prostředí: {name}")
    return value


def main() -> int:
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger = logging.getLogger("lidl_agent")

    try:
        lidl_email = _required_env("LIDL_EMAIL")
        lidl_password = _required_env("LIDL_PASSWORD")
        gmail_password = _required_env("GMAIL_PASSWORD")

        recipient = os.getenv("EMAIL_RECIPIENT", lidl_email)
        headless = os.getenv("HEADLESS", "true").lower() == "true"

        logger.info("Startuji Lidl agenta")
        scraper = LidlScraper(email=lidl_email, password=lidl_password, headless=headless)
        purchases = scraper.fetch_receipts()
        flyer_items = scraper.fetch_weekly_flyer()

        analyzer = HistoryAnalyzer(purchases)
        top_products = analyzer.most_frequent_products()
        top_categories = analyzer.top_categories()
        recommendations = analyzer.recommend_from_flyer(flyer_items)

        logger.info("Počet doporučených produktů: %s", len(recommendations))

        email_sender = EmailSender(sender_email=lidl_email, sender_password=gmail_password)
        email_sender.send_recommendations(
            recipient_email=recipient,
            recommendations=recommendations,
            top_products=top_products,
            top_categories=top_categories,
        )

        logger.info("E-mail byl úspěšně odeslán na %s", recipient)
        return 0

    except Exception:
        logger.exception("Běh Lidl agenta skončil chybou")
        return 1


if __name__ == "__main__":
    sys.exit(main())
