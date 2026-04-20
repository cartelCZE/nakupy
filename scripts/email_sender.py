from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class EmailSender:
    def __init__(
        self,
        sender_email: str,
        sender_password: str,
        smtp_server: str = "smtp.gmail.com",
        smtp_port: int = 587,
    ) -> None:
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port

    def _format_price(self, value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f} Kc"

    def build_html(self, products: list[dict], categories: list[str]) -> str:
        rows = ""
        for item in products:
            discount = f"{item.get('discount', 0)} %" if item.get("discount") else "-"
            rows += (
                "<tr>"
                f"<td>{item.get('name', '-')}</td>"
                f"<td>{item.get('category', '-')}</td>"
                f"<td>{self._format_price(item.get('price'))}</td>"
                f"<td>{self._format_price(item.get('original_price'))}</td>"
                f"<td>{discount}</td>"
                "</tr>"
            )

        top_categories_html = "".join(f"<li>{category}</li>" for category in categories) or "<li>Bez dat</li>"

        return f"""
        <html>
          <body style=\"font-family: Arial, sans-serif; color: #222;\">
            <h2>Lidl Agent - doporuceni z aktualniho letaku</h2>
            <p>Automaticka analyza nakupni historie a aktualnich akci Lidl.</p>

            <h3>Preferovane kategorie</h3>
            <ul>{top_categories_html}</ul>

            <h3>Doporucene produkty z letaku</h3>
            <table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" style=\"border-collapse: collapse; width: 100%;\">
              <thead>
                <tr style=\"background: #f3f3f3;\">
                  <th>Produkt</th>
                  <th>Kategorie</th>
                  <th>Akcni cena</th>
                  <th>Puvodni cena</th>
                  <th>Sleva</th>
                </tr>
              </thead>
              <tbody>
                {rows or '<tr><td colspan="5">Nebyl nalezen zadny relevantni produkt.</td></tr>'}
              </tbody>
            </table>
          </body>
        </html>
        """

    def send_recommendations(self, recipient: str, products: list[dict], categories: list[str]) -> None:
        message = MIMEMultipart("alternative")
        message["Subject"] = "Lidl doporuceni na tento tyden"
        message["From"] = self.sender_email
        message["To"] = recipient

        html_content = self.build_html(products, categories)
        message.attach(MIMEText(html_content, "html", "utf-8"))

        with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
            server.starttls()
            server.login(self.sender_email, self.sender_password)
            server.sendmail(self.sender_email, recipient, message.as_string())
