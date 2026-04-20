# Lidl Agent pro GitHub Actions

Automatizace pro Lidl.cz:

1. Přihlášení do účtu.
2. Načtení nákupní historie.
3. Stažení aktuálního letáku.
4. Vyhodnocení nejčastěji nakupovaných kategorií a produktů.
5. Odeslání doporučení e-mailem.

Workflow běží každou sobotu v 09:00 UTC a lze ho spustit i ručně.

## Nastavení GitHub Secrets

V repozitáři otevřete `Settings -> Secrets and variables -> Actions` a nastavte:

- `LIDL_EMAIL`: `jachym98@gmail.com`
- `LIDL_PASSWORD`: vaše Lidl heslo
- `GMAIL_PASSWORD`: App password pro Gmail (https://myaccount.google.com/apppasswords)

## Ruční spuštění workflow

1. Otevřete záložku `Actions`.
2. Vyberte workflow `Lidl Agent`.
3. Klikněte na `Run workflow`.

## Lokální spuštění

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python scripts/lidl_agent.py
```

## Troubleshooting

- Chyba přihlášení do Lidl.cz:
   Ověřte správnost `LIDL_EMAIL` a `LIDL_PASSWORD` v Secrets nebo `.env`.
- Chyba při odesílání e-mailu:
   Zkontrolujte, že `GMAIL_PASSWORD` je Gmail App Password, ne běžné heslo.
- Selenium nebo Chrome chyba v CI:
   Restartujte workflow a zkontrolujte log kroku `Run Lidl agent`.
- Prázdná data historie/letáku:
   Lidl mohl změnit HTML strukturu, upravte selektory v `scripts/lidl_scraper.py`.
