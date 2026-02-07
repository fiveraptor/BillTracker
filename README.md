# BillTracker

Ein einfaches Tool zum Verwalten von Rechnungen mit KI-Unterstützung (Google Gemini), E-Mail-Import und Benachrichtigungen.

## Features
- 📄 Upload von PDF und Bildern
- 🤖 Automatische Erkennung von Datum, Titel und Betrag via Google Gemini AI
- 📧 Automatischer Import aus E-Mail-Postfächern (IMAP) - pro Benutzer konfigurierbar
- 🔔 Benachrichtigungen bei Fälligkeit (via Apprise / ntfy.sh) - pro Benutzer konfigurierbar
- 📊 Statistik-Dashboard für monatliche Ausgaben
- 🔍 Suchfunktion
- 📱 Mobile-optimiertes Design

## Installation

1. Repository klonen:
   ```bash
   git clone https://github.com/DEIN_USER/BillTracker.git
   cd BillTracker
   ```

2. Konfiguration erstellen:
   ```bash
   cp .env.example .env
   # Bearbeite die .env Datei und trage deine API-Keys ein
   ```
   
   **Tipp:** Einen sicheren `SECRET_KEY` kannst du einfach im Terminal generieren:
   ```bash
   python3 -c 'import secrets; print(secrets.token_hex(32))'
   ```

3. Starten:
   ```bash
   docker-compose up -d --build
   ```