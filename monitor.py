#!/usr/bin/env python3
"""Monitor der offiziellen Startplatzbörse Mainova Halbmarathon Frankfurt 2027.

Benachrichtigt über Telegram, wenn die bisherige Meldung
'aktuell kein Platz verfügbar' auf der offiziellen Börsenseite verschwunden ist.
Die tatsächliche Buchbarkeit muss man danach selbst auf der Seite prüfen.
"""

import json
import os
import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URL = "https://static.anmeldungs-service.de/469/3279/boerse/12144/"
END_DATE = date(2027, 3, 14)  # offizielles Ende der Startplatzbörse
STATE_FILE = Path("state.json")
STATE_UPDATED_FILE = Path("state_updated.txt")
USER_AGENT = "Mozilla/5.0 (compatible; FrankfurtHalbmarathonStartplatzMonitor/1.0)"


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def read_page() -> str:
    request = Request(URL, headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Website liefert HTTP {response.status}")
        # Bei unbekanntem Zeichensatz ist die OCR-freie HTML-Decodierung ausreichend.
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def get_status(html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).casefold()
    expected = [
        "mainova halbmarathon frankfurt",
        "startplatzbörse für",
        "offene plätze in der börse",
    ]
    if not all(part in text for part in expected):
        raise RuntimeError("Seitenstruktur verändert oder Seite nicht vollständig geladen. Keine Alarmmeldung.")

    # Wichtig: 'freie Startplätze: 0' bezieht sich auf das allgemeine Teilnehmerlimit,
    # nicht zwangsläufig auf die verfügbaren Angebote in der Tauschbörse.
    if "aktuell kein platz verfügbar" in text:
        return "closed"
    return "possibly_open"


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt in den GitHub Secrets.")

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    req = Request(endpoint, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError("Telegram konnte die Nachricht nicht versenden.")


def read_previous_status() -> str | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("status")
    except (ValueError, OSError):
        raise RuntimeError("Statusdatei defekt. Bitte Cache prüfen.")


def store_status(status: str) -> None:
    STATE_FILE.write_text(
        json.dumps({"status": status, "checked_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat()}),
        encoding="utf-8",
    )
    STATE_UPDATED_FILE.write_text("status_changed", encoding="utf-8")


def main() -> None:
    if os.getenv("SEND_TEST_MESSAGE", "").lower() == "true":
        send_telegram("Test erfolgreich: Dein Frankfurt-Halbmarathon-Ticket-Monitor kann dich erreichen!\n" + URL)
        print("Telegram-Testnachricht verschickt.")
        return

    if datetime.now(ZoneInfo("Europe/Berlin")).date() > END_DATE:
        print("Die Startplatzbörse ist laut Veranstalter seit dem 14.03.2027 beendet. Keine Prüfung.")
        return

    html = read_page()
    status = get_status(html)
    previous = read_previous_status()
    print(f"Website geprüft. Alter Status: {previous}, neuer Status: {status}")

    if status == previous:
        print("Keine Statusänderung: keine Nachricht.")
        return

    if status == "possibly_open":
        # Keine automatische Reservierung. Die Seite kann in der Zwischenzeit wieder ausgebucht sein.
        send_telegram(
            "STARTPLATZ-ALARM: Möglicherweise ist ein Platz beim "
            "Mainova Halbmarathon Frankfurt 2027 frei!\n\n"
            "Die Meldung 'aktuell kein Platz verfügbar' ist verschwunden. "
            "Prüfe sofort die offizielle Börse:\n" + URL + "\n\n"
            "Wichtig: Dies ist noch keine garantierte Buchungsbestätigung."
        )
        print("Alarm über Telegram versendet.")

    store_status(status)
    print("Neuer Status gespeichert.")


if __name__ == "__main__":
    main()
