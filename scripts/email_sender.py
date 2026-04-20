from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class EmailSender:
    def __init__(self, sender_email: str, sender_password: str, smtp_server: str = "smtp.gmail.com", smtp_port: int = 587) -> None:
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port

    def _format_price(self, value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f} Kč"

    def build_html(
        self,
        recommendations: list[dict],
        top_products: list[str],
        top_categories: list[str],
    ) -> str:
        rows = ""
        for item in recommendations:
            availability = "✅ Skladem" if item["available"] else "❌ Nedostupné"
            discount = f"{item['discount_pct']} %" if item["discount_pct"] else "-"
            rows += (
                "<tr>"
                f"<td>{item['name']}</td>"
                f"<td>{item['category']}</td>"
                f"<td>{self._format_price(item['price'])}</td>"
                f"<td>{self._format_price(item['original_price'])}</td>"
                f"<td>{discount}</td>"
                f"<td>{availability}</td>"
                "</tr>"
            )

        top_products_html = "".join(f"<li>{product}</li>" for product in top_products) or "<li>Bez dat</li>"
        top_categories_html = "".join(f"<li>{category}</li>" for category in top_categories) or "<li>Bez dat</li>"

        return f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #222;">
            <h2>Lidl Agent - doporučení z aktuálního letáku</h2>
            <p>Automatická analýza nákupní historie a aktuálních akcí Lidl.</p>

            <h3>Nejčastěji nakupované produkty</h3>
            <ul>{top_products_html}</ul>

            <h3>Preferované kategorie</h3>
            <ul>{top_categories_html}</ul>

            <h3>Doporučené produkty z letáku</h3>
            <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%;">
              <thead>
                <tr style="background: #f3f3f3;">
                  <th>Produkt</th>
                  <th>Kategorie</th>
                  <th>Akční cena</th>
                  <th>Původní cena</th>
                  <th>Sleva</th>
                  <th>Dostupnost</th>
                </tr>
              </thead>
              <tbody>
                {rows or '<tr><td colspan="6">Nebyl nalezen žádný relevantní produkt.</td></tr>'}
              </tbody>
            </table>
          </body>
        </html>
        """

    def send_recommendations(
        self,
        recipient_email: str,
        recommendations: list[dict],
        top_products: list[str],
        top_categories: list[str],
    ) -> None:
        message = MIMEMultipart("alternative")
        message["Subject"] = "Lidl doporučení na tento týden"
        message["From"] = self.sender_email
        message["To"] = recipient_email

        html_content = self.build_html(recommendations, top_products, top_categories)
        message.attach(MIMEText(html_content, "html", "utf-8"))

        with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
            server.starttls()
            server.login(self.sender_email, self.sender_password)
            server.sendmail(self.sender_email, recipient_email, message.as_string())
