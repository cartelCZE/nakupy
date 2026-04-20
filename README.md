# Lidl Agent pro automatizaci nákupních doporučení

Tento repozitář obsahuje GitHub Actions agenta, který:

- přihlásí Lidl účet,
- načte nákupní historii,
- stáhne aktuální leták,
- vybere relevantní produkty podle historie,
- každou sobotu odešle HTML e-mail s doporučeními.

## Povinné soubory

- `.github/workflows/lidl-agent.yml`
- `scripts/lidl_agent.py`
- `scripts/lidl_scraper.py`
- `scripts/email_sender.py`
- `scripts/history_analyzer.py`
- `requirements.txt`
- `.env.example`

## Nastavení GitHub Secrets

V repozitáři otevřete **Settings → Secrets and variables → Actions** a vytvořte:

1. `LIDL_EMAIL`: `jachym98@gmail.com`
2. `LIDL_PASSWORD`: vaše Lidl heslo
3. `GMAIL_PASSWORD`: Gmail App Password

> `LIDL_EMAIL` je použit jako odesílatel i jako výchozí příjemce e-mailu.
> Obecně používejte vlastní adresu; pro požadované nasazení v tomto zadání nastavte `jachym98@gmail.com`.

## Spouštění workflow

Workflow **Lidl Agent** se spouští:

- automaticky každou sobotu v 09:00 UTC (`0 9 * * 6`),
- ručně přes **Actions → Lidl Agent → Run workflow**.

## Lokální spuštění

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python scripts/lidl_agent.py
```

## Jak to funguje

1. `scripts/lidl_scraper.py`:
   - přihlášení přes Playwright,
   - načtení účtenek a letáku,
   - parsování dat přes BeautifulSoup.
2. `scripts/history_analyzer.py`:
   - analýza nejčastějších produktů/kategorií v pandas,
   - scoring doporučení podle shody, ceny, slevy a dostupnosti.
3. `scripts/email_sender.py`:
   - generování HTML e-mailu,
   - odeslání přes SMTP (Gmail).
4. `scripts/lidl_agent.py`:
   - orchestrace celého procesu,
   - logging a error handling.

## Poznámky k bezpečnosti

- Hesla nejsou ukládána v kódu.
- Citlivé údaje se načítají z GitHub Secrets / `.env`.
- Do repozitáře nikdy neukládejte skutečná hesla.
