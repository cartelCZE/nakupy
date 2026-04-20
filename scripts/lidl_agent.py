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

    scraper: LidlScraper | None = None
    try:
        # Zadani pozaduje tuto adresu pro login i odeslani doporuceni.
        lidl_email = os.getenv("LIDL_EMAIL", "jachym98@gmail.com")
        gmail_password = _required_env("GMAIL_PASSWORD")
        lidl_refresh_token = (os.getenv("LIDL_REFRESH_TOKEN") or "").strip()
        lidl_country = (os.getenv("LIDL_COUNTRY") or "CZ").strip()
        lidl_language = (os.getenv("LIDL_LANGUAGE") or "cs").strip()
        lidl_password = os.getenv("LIDL_PASSWORD", "")
        if not lidl_refresh_token:
            lidl_password = _required_env("LIDL_PASSWORD")
        recipient = "jachym98@gmail.com"

        headless = os.getenv("HEADLESS", "true").lower() == "true"

        logger.info("Startuji Lidl agenta")
        scraper = LidlScraper(
            headless=headless,
            refresh_token=lidl_refresh_token,
            country=lidl_country,
            language=lidl_language,
        )

        if lidl_refresh_token:
            logger.info("Krok 1: Preskakuji web login, pouziji Lidl Plus API token")
        else:
            logger.info("Krok 1: Prihlaseni do Lidl.cz")
            scraper.login(lidl_email, lidl_password)

        logger.info("Krok 2: Nacteni nakupni historie")
        purchases = scraper.get_purchase_history()

        logger.info("Krok 3: Stazeni aktualniho Lidl letaku")
        flyer_items = scraper.get_flyer()

        logger.info("Krok 4: Analyza historie a vyber relevantnich produktu")
        analyzer = HistoryAnalyzer()
        analyzer.analyze(purchases)
        top_categories = analyzer.get_top_categories()
        top_products = analyzer.get_top_products()
        recommendations = analyzer.match_flyer_products(flyer_items)

        logger.info("Nejcastejsi kategorie: %s", ", ".join(top_categories) if top_categories else "bez dat")
        logger.info("Nejcastejsi produkty: %s", ", ".join(top_products[:5]) if top_products else "bez dat")
        logger.info("Pocet doporucenych produktu: %s", len(recommendations))

        logger.info("Krok 5: Odeslani emailu s doporucenimi")
        email_sender = EmailSender(sender_email=lidl_email, sender_password=gmail_password)
        email_sender.send_recommendations(recipient=recipient, products=recommendations, categories=top_categories)

        logger.info("E-mail byl uspesne odeslan na %s", recipient)
        return 0

    except Exception:
        logger.exception("Beh Lidl agenta skoncil chybou")
        return 1
    finally:
        if scraper is not None:
            scraper.close()


if __name__ == "__main__":
    sys.exit(main())
