#!/usr/bin/env python3
"""Frankfurt-Halbmarathon-Börse überwachen + Telegram-Einladungen.

Speicherung der Gäste und des letzten verarbeiteten Telegram-Updates in der
Repository-Actions-Variable BOT_STATE (per eng begrenztem GitHub-PAT).
Keine Chat-IDs und keine Passwörter in den öffentlichen Repository-Dateien.
"""

import hashlib
import hmac
import json
import os
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URL = "https://static.anmeldungs-service.de/469/3279/boerse/12144/"
END_DATE = date(2027, 3, 14)
STATE_VARIABLE = "BOT_STATE"
USER_AGENT = "Mozilla/5.0 (compatible; FrankfurtHalbmarathonStartplatzMonitor/2.0)"


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


def page_status(html):
    parser = VisibleTextParser()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).casefold()
    required = ["mainova halbmarathon frankfurt", "startplatzbörse für", "offene plätze in der börse"]
    if not all(fragment in text for fragment in required):
        raise RuntimeError("Seitenstruktur unklar: Keine Alarmmeldung, bitte Seite prüfen.")
    return "closed" if "aktuell kein platz verfügbar" in text else "possibly_open"


def fetch_page():
    req = Request(URL, headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})
    with urlopen(req, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Börse liefert HTTP {response.status}")
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def telegram(method, data=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    # Niemals die Telegram-URL mit dem Token in Fehlerausgaben weiterreichen.
    url = f"https://api.telegram.org/bot{token}/{method}"
    if data is not None:
        req = Request(url, data=urlencode(data).encode("utf-8"),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        req = Request(url)
    try:
        with urlopen(req, timeout=20) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Telegram-API {method}: HTTP {exc.code}") from None
    except URLError:
        raise RuntimeError(f"Telegram-API {method}: Verbindungsfehler") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram-API {method}: Antwort nicht OK")
    return result.get("result")


def send(chat_id, message):
    telegram("sendMessage", {"chat_id": str(chat_id), "text": message, "disable_web_page_preview": "true"})


class GitHubState:
    def __init__(self):
        self.token = os.environ["REPO_VARIABLES_TOKEN"].strip()
        repo = os.environ["GITHUB_REPOSITORY"].strip()
        if not self.token or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise RuntimeError("GitHub-Variablen-Token oder Repository-Angabe fehlt.")
        self.base = f"https://api.github.com/repos/{repo}/actions/variables"
        self.exists = False

    def request(self, method, url, payload=None, allow_404=False):
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        content = None
        if payload is not None:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=content, headers=headers, method=method)
        try:
            with urlopen(req, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            raise RuntimeError(f"GitHub-Variablen-API {method}: HTTP {exc.code}. Token-Berechtigungen prüfen.") from None
        except URLError:
            raise RuntimeError("GitHub-Variablen-API: Verbindungsfehler") from None

    def load(self):
        result = self.request("GET", f"{self.base}/{STATE_VARIABLE}", allow_404=True)
        self.exists = result is not None
        if result is None:
            return {"guests": [], "last_update_id": 0, "status": None, "used_codes": []}
        try:
            state = json.loads(result["value"])
            if (not isinstance(state.get("guests"), list)
                or not isinstance(state.get("last_update_id"), int)
                or not isinstance(state.get("used_codes"), list)
                or state.get("status") not in {None, "closed", "possibly_open"}):
                raise ValueError("Invalid state fields")
            return state
        except (ValueError, TypeError, KeyError):
            raise RuntimeError("BOT_STATE enthält ungültige Daten. Nicht überschreiben.") from None

    def save(self, state):
        payload = {"name": STATE_VARIABLE, "value": json.dumps(state, separators=(",", ":"))}
        if self.exists:
            self.request("PATCH", f"{self.base}/{STATE_VARIABLE}", payload)
        else:
            self.request("POST", self.base, payload)
            self.exists = True


def get_updates(after_id):
    # 100 Updates sind das API-Maximum. Bei mehr wird in der Schleife nachgeladen.
    result = telegram("getUpdates", {
        "offset": after_id + 1,
        "timeout": 0,
        "limit": 100,
        "allowed_updates": json.dumps(["message"]),
    })
    if not isinstance(result, list):
        raise RuntimeError("Telegram lieferte keine gültige Update-Liste.")
    return result


def process_command(msg, state, owner_id, code, max_guests):
    """Wertet eine Nachricht aus, gibt nur ausstehende Antworten zurück."""
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return []
    chat_id = str(chat.get("id", ""))
    if not chat_id.isdigit():
        return []
    raw = msg.get("text", "").strip()
    if not raw.startswith("/"):
        return []
    command, _, arg = raw.partition(" ")
    command = command.split("@", 1)[0].casefold()
    arg = arg.strip()
    admin = chat_id == owner_id
    guest = chat_id in state["guests"]
    replies = []

    if command in {"/start", "/help"}:
        if admin:
            replies.append((chat_id, "Du bist Admin. Befehle: /users, /remove CHAT_ID, /status. Neue Gäste: /join CODE."))
        elif guest:
            replies.append((chat_id, "Du bist freigeschaltet. Du erhältst Ticket-Alarme. Mit /stop kannst du dich abmelden."))
        else:
            replies.append((chat_id, "Ticket-Monitor: Du benötigst einen Einladungscode. Sende /join DEIN_CODE."))

    elif command == "/join":
        if admin or guest:
            replies.append((chat_id, "Du bist bereits freigeschaltet."))
        elif not arg:
            replies.append((chat_id, "Bitte sende /join DEIN_CODE. Den Code bekommst du vom Betreiber."))
        elif not hmac.compare_digest(arg, code):
            replies.append((chat_id, "Einladungscode ungültig."))
        elif len(state["guests"]) >= max_guests:
            replies.append((chat_id, "Im Moment sind alle Gästeplätze belegt."))
        else:
            fingerprint = hashlib.sha256(code.encode("utf-8")).hexdigest()
            if fingerprint in state["used_codes"]:
                replies.append((chat_id, "Dieser Einladungscode wurde bereits verwendet. Bitte neuen Code anfordern."))
            else:
                state["guests"].append(chat_id)
                state["used_codes"].append(fingerprint)
                replies.append((chat_id, "Freigeschaltet! Du bekommst künftig Startplatz-Alarme für den Frankfurt Halbmarathon 2027."))
                replies.append((owner_id, f"Neuer Ticket-Monitor-Gast registriert: Chat-ID {chat_id}. Mit /remove {chat_id} kannst du den Zugang widerrufen."))
                if state.get("status") == "possibly_open":
                    replies.append((chat_id, "Hinweis: Die Börse wurde zuletzt als möglicherweise offen erkannt. Bitte prüfe: " + URL))

    elif command == "/users":
        if admin:
            guests = ", ".join(state["guests"]) if state["guests"] else "keine"
            replies.append((owner_id, f"Freigeschaltete Gäste ({len(state['guests'])}/{max_guests}): {guests}"))

    elif command == "/remove":
        if admin:
            if arg in state["guests"]:
                state["guests"].remove(arg)
                replies.append((owner_id, f"Gast {arg} entfernt. Für eine neue Einladung bitte einen neuen INVITE_CODE in GitHub speichern."))
                replies.append((arg, "Dein Ticket-Monitor-Zugang wurde vom Betreiber beendet."))
            else:
                replies.append((owner_id, "Chat-ID nicht unter Gästen gefunden. Hilfe: /users"))

    elif command == "/stop":
        if guest:
            state["guests"].remove(chat_id)
            replies.append((chat_id, "Du erhältst keine Ticket-Alarme mehr."))
            replies.append((owner_id, f"Gast {chat_id} hat sich abgemeldet."))
        elif admin:
            replies.append((owner_id, "Du bist der Admin. Entferne andere Nutzer mit /remove CHAT_ID."))

    elif command == "/status":
        if admin or guest:
            value = "ausverkauft" if state.get("status") == "closed" else "möglicherweise offen" if state.get("status") == "possibly_open" else "noch nicht geprüft"
            replies.append((chat_id, f"Letzter bekannter Börsenstatus: {value}.\n{URL}"))

    return replies


def process_inbox(store, state, owner_id, invite_code, max_guests):
    # Auch nach GitHub-Neustarts bleiben Update-Cursor und Gastliste dauerhaft erhalten.
    while True:
        updates = get_updates(state["last_update_id"])
        if not updates:
            return
        responses = []
        old_cursor = state["last_update_id"]
        for item in updates:
            update_id = item.get("update_id")
            if not isinstance(update_id, int) or update_id <= state["last_update_id"]:
                continue
            responses.extend(process_command(item.get("message") or {}, state, owner_id, invite_code, max_guests))
            state["last_update_id"] = update_id
        if state["last_update_id"] > old_cursor:
            # Eine einzige GitHub-Aktualisierung pro Stapel; erst speichern, dann antworten.
            store.save(state)
            for chat_id, reply in responses:
                send(chat_id, reply)
        if len(updates) < 100:
            return


def main():
    owner_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    invite_code = os.environ["INVITE_CODE"].strip()
    if not owner_id.isdigit():
        raise RuntimeError("TELEGRAM_CHAT_ID fehlt oder ist ungültig.")
    if len(invite_code) < 16 or len(invite_code) > 100:
        raise RuntimeError("INVITE_CODE muss mindestens 16 Zeichen haben (sicherer Zufallscode).")
    try:
        max_guests = int(os.environ.get("MAX_GUESTS", "1"))
    except ValueError:
        raise RuntimeError("MAX_GUESTS muss eine ganze Zahl sein.") from None
    if not 1 <= max_guests <= 20:
        raise RuntimeError("MAX_GUESTS muss zwischen 1 und 20 liegen.")

    store = GitHubState()
    state = store.load()
    process_inbox(store, state, owner_id, invite_code, max_guests)

    recipients = [owner_id] + [uid for uid in state["guests"] if uid != owner_id]
    if os.environ.get("SEND_TEST_MESSAGE", "").lower() == "true":
        for recipient in recipients:
            send(recipient, "Test erfolgreich: Dein Frankfurt-Halbmarathon-Ticket-Monitor kann dich erreichen!\n" + URL)
        print(f"Telegram-Test versendet. Empfänger: {len(recipients)}")
        return

    if datetime.now(ZoneInfo("Europe/Berlin")).date() > END_DATE:
        print("Startplatzbörse seit 14.03.2027 beendet. Keine Prüfung.")
        return

    new_status = page_status(fetch_page())
    old_status = state.get("status")
    print(f"Website geprüft. Alter Status: {old_status}, neuer Status: {new_status}. Empfänger: {len(recipients)}")
    if new_status == old_status:
        print("Keine Statusänderung, keine Ticket-Benachrichtigung.")
        return
    if new_status == "possibly_open":
        notice = (
            "STARTPLATZ-ALARM: Möglicherweise ist ein Platz beim Mainova Halbmarathon "
            "Frankfurt 2027 frei!\n\nBitte sofort die offizielle Börse prüfen:\n" + URL +
            "\n\nKeine Buchungsgarantie."
        )
        for recipient in recipients:
            send(recipient, notice)
        print("Startplatz-Alarm gesendet.")
    state["status"] = new_status
    state["checked_at"] = datetime.now(ZoneInfo("Europe/Berlin")).isoformat()
    store.save(state)
    print("Neuer Status gespeichert.")


if __name__ == "__main__":
    main()
