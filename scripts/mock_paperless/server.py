#!/usr/bin/env python3
"""Mock Paperless-ngx API server for testing swift-paperless with very large libraries.

Standard library only:

    python3 scripts/mock_paperless/server.py --documents 100000

Documents are generated deterministically from (seed, id). Metadata lives in
compact column arrays; OCR text, PDFs and thumbnails are generated on demand
and never held in memory. Mutations are kept in memory until the process
exits. See README.md next to this file for endpoints, filters, the needle
documents and the /__mock/ control endpoints.
"""

from __future__ import annotations

import argparse
import array
import bisect
import colorsys
import datetime as dt
import hashlib
import http
import json
import random
import re
import select
import socket
import struct
import sys
import threading
import time
import traceback
import unicodedata
import uuid
import zlib
from collections import Counter, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

DEFAULT_REFERENCE_DATE = "2026-11-01"
EPOCH_ORD = dt.date(1970, 1, 1).toordinal()
UTC = dt.timezone.utc
BIG = 1 << 62
TRUNCATED_CONTENT_LENGTH = 550  # paperless-ngx: `content[:550]` for truncate_content=true


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def J(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, UTC).isoformat()[:-6] + "Z"


def ord_iso(o: int) -> str:
    return dt.date.fromordinal(o).isoformat()


def ord_ts(o: int) -> float:
    return (o - EPOCH_ORD) * 86400.0


def ts_ord(ts: float) -> int:
    return int(ts // 86400) + EPOCH_ORD


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("ß", "ss")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def ascii_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("ß", "ss")).encode("ascii", "ignore").decode()
    return re.sub(r'[\\"\r\n]', "_", s)


def truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def csv_ints(v: str) -> list[int]:
    return [int(x) for x in re.split(r"[,\s]+", v.strip()) if x.strip().lstrip("-").isdigit()]


def parse_date_ord(v: str) -> int:
    return dt.date.fromisoformat(v.strip()[:10]).toordinal()


def parse_ts(v: str) -> float:
    v = v.strip()
    if len(v) <= 10:
        return ord_ts(parse_date_ord(v))
    d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.timestamp()


class ApiError(Exception):
    def __init__(self, status: int, payload):
        super().__init__(str(payload))
        self.status = status
        self.payload = payload


def not_found():
    return ApiError(404, {"detail": "Not found."})


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

MONTHS = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
}

CITIES = [("München", "80331"), ("Köln", "50667"), ("Düsseldorf", "40213"), ("Nürnberg", "90402"),
          ("Würzburg", "97070"), ("Lübeck", "23552"), ("Hamburg", "20095"), ("Berlin", "10115"),
          ("Frankfurt am Main", "60311"), ("Stuttgart", "70173"), ("Göttingen", "37073"),
          ("Münster", "48143"), ("Osnabrück", "49074"), ("Saarbrücken", "66111"),
          ("Leipzig", "04109"), ("Dresden", "01067"), ("Bremen", "28195"), ("Tübingen", "72070")]

SURNAMES = ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker",
            "Schulz", "Hoffmann", "Schäfer", "Koch", "Bauer", "Richter", "Klein", "Wolf",
            "Schröder", "Neumann", "Schwarz", "Zimmermann", "Braun", "Krüger", "Hofmann",
            "Hartmann", "Lange", "Schmitt", "Werner", "Krause", "Meier", "Lehmann", "Köhler",
            "Herrmann", "König", "Walter", "Mayer", "Huber", "Kaiser", "Fuchs", "Peters", "Lang",
            "Scholz", "Möller", "Weiß", "Jung", "Hahn", "Schubert", "Vogel", "Friedrich", "Keller",
            "Günther"]

STREETS = ["Hauptstraße", "Bahnhofstraße", "Gartenweg", "Schillerstraße", "Goethestraße",
           "Lindenallee", "Marktplatz", "Am Mühlbach", "Rosenweg", "Kirchgasse",
           "Industriestraße", "Berliner Allee", "Königsweg", "Ringstraße"]

PRODUCTS = ["Waschmaschine", "Kühlschrank", "Fernseher", "Staubsauger", "Kaffeevollautomat",
            "Geschirrspüler", "Bohrmaschine", "Drucker", "WLAN-Router", "E-Bike", "Backofen",
            "Rasenmäher", "Heizungsthermostat", "Mikrowelle", "Akkuschrauber"]

CORR_GROUPS = OrderedDict([
    ("telecom", ["Telekom Deutschland GmbH", "Vodafone GmbH", "O2 Telefónica", "1&1 Telecom GmbH",
                 "congstar", "freenet"]),
    ("energy", ["Stadtwerke München", "E.ON Energie Deutschland", "Vattenfall Europe", "EnBW",
                "LichtBlick SE", "Naturstrom AG", "RheinEnergie"]),
    ("insurance", ["Allianz Versicherungs-AG", "HUK-COBURG", "ERGO Versicherung",
                   "AXA Versicherung", "Debeka", "R+V Versicherung", "Signal Iduna",
                   "CosmosDirekt", "Gothaer"]),
    ("bank", ["Deutsche Bank", "ING-DiBa", "DKB Deutsche Kreditbank", "Commerzbank",
              "Sparkasse KölnBonn", "Volksbank Mittelhessen", "comdirect", "Postbank", "N26 Bank",
              "Consorsbank"]),
    ("tax", ["Finanzamt München", "Finanzamt Köln-Mitte", "Bundeszentralamt für Steuern",
             "Steuerberatung Krüger & Partner"]),
    ("employer", ["Musterfirma GmbH", "Nordlicht Software AG", "Bäckerei Schäfer",
                  "Ingenieurbüro Lehmann", "Acme Corp.", "Globex Ltd."]),
    ("health", ["Techniker Krankenkasse", "AOK Bayern", "Barmer", "DAK-Gesundheit",
                "Zahnarztpraxis Dr. König", "Hausarztpraxis Dr. Weiß",
                "Universitätsklinikum Würzburg", "Apotheke am Markt"]),
    ("landlord", ["Hausverwaltung Schmidt", "Vonovia SE", "Wohnungsbaugenossenschaft Süd eG",
                  "Immobilien Hartmann GmbH"]),
    ("shop", ["Amazon EU S.à r.l.", "IKEA Deutschland", "MediaMarkt", "Zalando SE", "OTTO",
              "Saturn", "Bauhaus", "OBI", "dm-drogerie markt", "REWE",
              "Apple Distribution International", "Conrad Electronic", "Thomann"]),
    ("authority", ["Stadt München", "Bürgeramt Köln", "Kraftfahrt-Bundesamt",
                   "Deutsche Rentenversicherung", "Bundesagentur für Arbeit",
                   "ARD ZDF Deutschlandradio Beitragsservice", "Zulassungsstelle Düsseldorf",
                   "Standesamt Nürnberg"]),
    ("mobility", ["Deutsche Bahn AG", "ADAC e.V.", "Autohaus Becker", "Lufthansa", "TÜV Süd",
                  "Sixt", "Kfz-Werkstatt Wagner"]),
    ("misc", ["Sportverein Blau-Weiß e.V.", "Volkshochschule Lübeck", "Stadtbibliothek",
              "Kindergarten Sonnenschein", "Tierarztpraxis Dr. Vogel", "Notar Dr. Friedrich",
              "Rechtsanwälte Keller & Jung", "Schornsteinfegermeister Braun"]),
    ("intl", ["HM Revenue & Customs", "British Gas", "Barclays", "Vodafone UK", "State Farm",
              "Bank of America", "Internal Revenue Service", "Comcast", "Verizon",
              "National Grid", "Aviva", "Lloyds Bank", "Thames Water"]),
])

CORR_PATTERNS = [
    ("shop", "{S} Elektrotechnik"), ("misc", "Malerbetrieb {S}"), ("health", "Dr. med. {S}"),
    ("energy", "Stadtwerke {C}"), ("tax", "Finanzamt {C}"), ("landlord", "Hausverwaltung {S}"),
    ("mobility", "Autohaus {S}"), ("employer", "{S} Maschinenbau GmbH"), ("bank", "Sparkasse {C}"),
    ("authority", "Stadt {C}"), ("shop", "Möbelhaus {S}"), ("misc", "Schreinerei {S}"),
    ("health", "Zahnarztpraxis Dr. {S}"), ("tax", "Steuerbüro {S}"),
    ("landlord", "Immobilien {S} GmbH"), ("mobility", "Fahrschule {S}"),
    ("employer", "{S} Consulting AG"), ("bank", "Volksbank {C}"), ("authority", "Landratsamt {C}"),
    ("shop", "Buchhandlung {S}"), ("misc", "Sanitär {S} GmbH"), ("health", "Physiotherapie {S}"),
    ("shop", "Fahrradladen {S}"), ("misc", "{S} & {S2} Rechtsanwälte"), ("health", "Apotheke {S}"),
    ("shop", "Gärtnerei {S}"), ("misc", "Dachdeckerei {S}"), ("shop", "Optik {S}"),
    ("misc", "Notariat {S}"), ("shop", "Metzgerei {S}"),
]

# (name, parent) in id order; "Inbox" is the inbox tag.
TAGS = [
    ("Inbox", None),
    ("Finanzen", None), ("Bank", "Finanzen"), ("Steuer", "Finanzen"), ("Steuer 2022", "Steuer"),
    ("Steuer 2023", "Steuer"), ("Steuer 2024", "Steuer"), ("Steuer 2025", "Steuer"),
    ("Rechnungen", "Finanzen"), ("Depot", "Finanzen"), ("Kredit", "Finanzen"),
    ("Versicherung", None), ("Haftpflicht", "Versicherung"), ("Hausrat", "Versicherung"),
    ("KFZ-Versicherung", "Versicherung"),
    ("Wohnung", None), ("Miete", "Wohnung"), ("Nebenkosten", "Wohnung"), ("Strom", "Wohnung"),
    ("Internet", "Wohnung"),
    ("Auto", None), ("Werkstatt", "Auto"),
    ("Gesundheit", None), ("Arzt", "Gesundheit"), ("Krankenkasse", "Gesundheit"),
    ("Zahnarzt", "Gesundheit"),
    ("Arbeit", None), ("Gehalt", "Arbeit"), ("Fortbildung", "Arbeit"),
    ("Familie", None), ("Kinder", "Familie"), ("Schule", "Familie"),
    ("Wichtig", None), ("Erledigt", None), ("TODO", None), ("Zu bezahlen", None),
    ("Bezahlt", None), ("Archiv", None), ("Garantie", None), ("Reise", None), ("Urlaub", None),
    ("Verein", None), ("Spende", None), ("Telefon", None), ("Mobilfunk", None), ("Abo", None),
    ("Online-Shopping", None), ("Rente", None), ("Altersvorsorge", None), ("Haus", None),
    ("Garten", None), ("Handwerker", None), ("Elektronik", None), ("Haustier", None),
    ("Behörde", None), ("Ausweis", None), ("Scan", None), ("Privat", None),
    ("Geschäftlich", None), ("Steuerrelevant", None), ("Original vorhanden", None),
    ("important", None), ("receipt", None), ("to-review", None),
]

TAG_COLORS = ["#a6cee3", "#1f78b4", "#b2df8a", "#33a02c", "#fb9a99", "#e31a1c", "#fdbf6f",
              "#ff7f00", "#cab2d6", "#6a3d9a", "#b15928", "#000000", "#cccccc", "#701b6c",
              "#d24a6b", "#432db5", "#2a9d8f", "#e9c46a", "#264653", "#f4a261"]

DOCUMENT_TYPES = ["Rechnung", "Kontoauszug", "Vertrag", "Versicherung", "Gehaltsabrechnung",
                  "Steuerbescheid", "Brief", "Arztbrief", "Kündigung", "Mahnung", "Lieferschein",
                  "Bedienungsanleitung", "Zeugnis", "Bescheid", "Abrechnung", "Bescheinigung",
                  "Quittung", "Reisebeleg", "Invoice", "Bank statement", "Contract", "Letter",
                  "Payslip", "Tax document", "Insurance policy"]

STORAGE_PATHS = [
    ("Finanzen", "Finanzen/{{ created_year }}/{{ correspondent }}/{{ title }}"),
    ("Versicherungen", "Versicherungen/{{ correspondent }}/{{ created_year }} {{ title }}"),
    ("Wohnung", "Wohnung/{{ created_year }}/{{ title }}"),
    ("Auto", "Auto/{{ created_year }}/{{ title }}"),
    ("Gesundheit", "Gesundheit/{{ correspondent }}/{{ created }} {{ title }}"),
    ("Arbeit", "Arbeit/{{ created_year }}/{{ created_month }}/{{ title }}"),
    ("Steuer", "Steuer/{{ created_year }}/{{ document_type }}/{{ title }}"),
    ("Behörden", "Behoerden/{{ correspondent }}/{{ title }}"),
    ("Familie", "Familie/{{ created_year }}/{{ title }}"),
    ("Archiv", "Archiv/{{ created_year }}/{{ asn }} {{ title }}"),
]

CUSTOM_FIELDS = [
    (1, "Betrag", "monetary", {"select_options": [], "default_currency": "EUR"}),
    (2, "Fällig am", "date", {"select_options": [], "default_currency": None}),
    (3, "Bezahlt", "boolean", {"select_options": [], "default_currency": None}),
    (4, "Kundennummer", "string", {"select_options": [], "default_currency": None}),
    (5, "Status", "select", {"select_options": [
        {"id": "st-offen", "label": "Offen"},
        {"id": "st-arbeit", "label": "In Bearbeitung"},
        {"id": "st-erledigt", "label": "Erledigt"}], "default_currency": None}),
    (6, "Verknüpfte Dokumente", "documentlink", {"select_options": [], "default_currency": None}),
]


def _kind(key, lang, dtype, headline, titles, groups, tags, spath, money, weight, nouns):
    return dict(key=key, lang=lang, dtype=dtype, headline=headline, titles=titles, groups=groups,
                tags=tags, spath=spath, money=money, weight=weight, nouns=nouns)


KINDS = [
    _kind("rechnung", "de", "Rechnung", "Rechnung",
          ["Rechnung {c} {M} {Y}", "Rechnung Nr. {n} {c}", "{c} Rechnung {Y}-{m}"],
          ["telecom", "energy", "shop", "mobility", "misc"], ["Rechnungen", "Finanzen"],
          "Finanzen", True, 16,
          ["Rechnungsbetrag", "Rechnungsnummer", "Mehrwertsteuer", "Umsatzsteuer", "Zahlungsziel",
           "Lieferadresse", "Bestellnummer", "Skonto", "Lastschriftmandat", "Überweisung"]),
    _kind("mobilfunk", "de", "Rechnung", "Mobilfunkrechnung",
          ["Mobilfunkrechnung {M} {Y}", "{c} Mobilfunk {Y}-{m}", "Festnetz und Internet {M} {Y}"],
          ["telecom"], ["Mobilfunk", "Telefon", "Internet"], "Finanzen", True, 6,
          ["Mobilfunkvertrag", "Datenvolumen", "Grundgebühr", "Roaming", "Rufnummernmitnahme",
           "Einzelverbindungsnachweis", "Tarifwechsel", "Vertragsverlängerung"]),
    _kind("kontoauszug", "de", "Kontoauszug", "Kontoauszug",
          ["Kontoauszug {Y}-{m}", "Kontoauszug Nr. {k}/{Y}", "{c} Kontoauszug {M} {Y}"],
          ["bank"], ["Bank", "Finanzen"], "Finanzen", False, 10,
          ["Kontostand", "Girokonto", "Dauerauftrag", "Überweisung", "Lastschrift", "Sollzinsen",
           "Habenzinsen", "Kontoführungsgebühr", "Buchungstag", "Wertstellung",
           "Überziehungskredit"]),
    _kind("strom", "de", "Abrechnung", "Jahresabrechnung",
          ["Stromabrechnung {Y}", "Jahresabrechnung Strom {Y}", "Abschlagsplan {Y} {c}",
           "Gasabrechnung {Y}"],
          ["energy"], ["Strom", "Wohnung"], "Wohnung", True, 4,
          ["Zählerstand", "Stromzähler", "Abschlagszahlung", "Jahresverbrauch", "Kilowattstunde",
           "Grundpreis", "Arbeitspreis", "Netzentgelt", "Ökostrom", "Preisanpassung"]),
    _kind("miete", "de", "Vertrag", "Mietangelegenheit",
          ["Mietvertrag Wohnung", "Nebenkostenabrechnung {Y}", "Mieterhöhung {Y}",
           "Betriebskostenabrechnung {Y}", "Wohnungsübergabeprotokoll {D}"],
          ["landlord"], ["Miete", "Nebenkosten", "Wohnung"], "Wohnung", True, 4,
          ["Mietvertrag", "Kaution", "Nebenkosten", "Betriebskosten", "Heizkosten", "Kaltmiete",
           "Warmmiete", "Hausordnung", "Wohnungsübergabe", "Mieterhöhung", "Müllabfuhr",
           "Treppenhausreinigung"]),
    _kind("versicherung", "de", "Versicherung", "Versicherungsschein",
          ["Haftpflichtversicherung Beitragsrechnung {Y}", "Versicherungsschein {c}",
           "Hausratversicherung Police {Y}", "Beitragsanpassung {Y} {c}",
           "Schadensmeldung {D} {c}"],
          ["insurance"], ["Versicherung", "Haftpflicht", "Hausrat"], "Versicherungen", True, 7,
          ["Haftpflichtversicherung", "Hausratversicherung", "Versicherungsschein",
           "Versicherungsnummer", "Beitragsanpassung", "Selbstbeteiligung", "Schadensmeldung",
           "Deckungssumme", "Rechtsschutzversicherung", "Berufsunfähigkeitsversicherung",
           "Versicherungsnehmer"]),
    _kind("kfz", "de", "Rechnung", "Fahrzeugunterlagen",
          ["Kfz-Versicherung {Y}", "Kfz-Steuerbescheid {Y}", "Werkstattrechnung {c}",
           "TÜV-Bericht {Y}", "Reifenwechsel {M} {Y}"],
          ["mobility", "insurance"], ["Auto", "Werkstatt", "KFZ-Versicherung"], "Auto", True, 4,
          ["Fahrzeugschein", "Hauptuntersuchung", "Kraftfahrzeugsteuer", "Schadenfreiheitsklasse",
           "Kennzeichen", "Werkstattrechnung", "Ölwechsel", "Bremsbeläge", "Winterreifen",
           "Teilkasko", "Vollkasko"]),
    _kind("gehalt", "de", "Gehaltsabrechnung", "Entgeltabrechnung",
          ["Gehaltsabrechnung {M} {Y}", "Lohnsteuerbescheinigung {Y}",
           "Entgeltabrechnung {Y}-{m}"],
          ["employer"], ["Gehalt", "Arbeit"], "Arbeit", True, 8,
          ["Bruttogehalt", "Nettogehalt", "Lohnsteuer", "Sozialversicherungsbeiträge",
           "Rentenversicherung", "Arbeitslosenversicherung", "Pflegeversicherung", "Überstunden",
           "Urlaubsanspruch", "Steuerklasse", "Kirchensteuer", "Solidaritätszuschlag"]),
    _kind("steuer", "de", "Steuerbescheid", "Bescheid",
          ["Einkommensteuerbescheid {Y}", "Steuererklärung {Y}",
           "Umsatzsteuervoranmeldung {Y}-{m}", "Vorauszahlungsbescheid {Y}"],
          ["tax"], ["Steuer", "Steuerrelevant"], "Steuer", True, 5,
          ["Steuererklärung", "Einkommensteuerbescheid", "Steuernummer", "Werbungskosten",
           "Sonderausgaben", "Vorauszahlung", "Erstattung", "Einspruchsfrist",
           "Steueridentifikationsnummer", "Freibetrag", "Säumniszuschlag"]),
    _kind("arzt", "de", "Arztbrief", "Befundbericht",
          ["Arztbrief {c}", "Rezept {D}", "Laborbefund {D}",
           "Arbeitsunfähigkeitsbescheinigung {D}", "Zahnarztrechnung {Y}"],
          ["health"], ["Arzt", "Gesundheit", "Zahnarzt"], "Gesundheit", False, 5,
          ["Behandlung", "Diagnose", "Überweisungsschein", "Arbeitsunfähigkeitsbescheinigung",
           "Rezeptgebühr", "Zuzahlung", "Laborbefund", "Impfung", "Vorsorgeuntersuchung",
           "Heil- und Kostenplan", "Physiotherapie"]),
    _kind("krankenkasse", "de", "Bescheinigung", "Mitteilung",
          ["Beitragsbescheid Krankenkasse {Y}", "Mitgliedsbescheinigung {Y}",
           "Bonusprogramm {Y} {c}"],
          ["health"], ["Krankenkasse", "Gesundheit"], "Gesundheit", False, 3,
          ["Krankenversicherung", "Mitgliedsbescheinigung", "Zusatzbeitrag", "Gesundheitskarte",
           "Familienversicherung", "Kostenerstattung", "Bonusprogramm", "Krankengeld"]),
    _kind("kuendigung", "de", "Kündigung", "Kündigung",
          ["Kündigung {c}", "Kündigungsbestätigung {c}", "Kündigung {c} zum {D}"],
          ["telecom", "insurance", "energy", "misc"], ["Wichtig", "Abo"], None, False, 3,
          ["Kündigung", "Kündigungsfrist", "Kündigungsbestätigung", "Vertragsende",
           "Sonderkündigungsrecht", "Widerruf", "Vertragslaufzeit", "Rücksendung"]),
    _kind("mahnung", "de", "Mahnung", "Zahlungserinnerung",
          ["Mahnung {c}", "Zahlungserinnerung {c} {M} {Y}", "Letzte Mahnung {c}"],
          ["shop", "telecom", "energy"], ["Zu bezahlen", "Wichtig"], "Finanzen", True, 2,
          ["Zahlungserinnerung", "Mahngebühr", "Verzugszinsen", "Inkasso", "Forderung",
           "Zahlungsverzug", "Ratenzahlung"]),
    _kind("bestellung", "de", "Lieferschein", "Bestellung",
          ["Bestellbestätigung {c}", "Lieferschein {c} {n}", "Garantieschein {c}",
           "Rücksendeschein {c}"],
          ["shop"], ["Online-Shopping", "Garantie"], None, True, 6,
          ["Bestellnummer", "Lieferschein", "Sendungsverfolgung", "Widerrufsrecht",
           "Gewährleistung", "Garantie", "Rücksendung", "Lieferadresse", "Artikelnummer",
           "Versandkosten"]),
    _kind("behoerde", "de", "Bescheid", "Bescheid",
          ["Meldebescheinigung", "Bescheid {c} {Y}", "Rundfunkbeitrag {Y}",
           "Grundsteuerbescheid {Y}", "Bußgeldbescheid {D}"],
          ["authority"], ["Behörde", "Ausweis"], "Behörden", True, 4,
          ["Meldebescheinigung", "Personalausweis", "Rundfunkbeitrag", "Grundsteuer",
           "Abwassergebühren", "Bußgeldbescheid", "Aktenzeichen", "Rechtsbehelfsbelehrung",
           "Widerspruch", "Führungszeugnis", "Reisepass"]),
    _kind("rente", "de", "Bescheinigung", "Jahresmitteilung",
          ["Renteninformation {Y}", "Depotauszug {Y}", "Jahressteuerbescheinigung {Y} {c}",
           "Wertpapierabrechnung {D}"],
          ["bank", "authority"], ["Rente", "Altersvorsorge", "Depot"], "Finanzen", False, 3,
          ["Renteninformation", "Altersvorsorge", "Rentenanspruch", "Depotauszug",
           "Kapitalertragsteuer", "Freistellungsauftrag", "Wertpapierabrechnung", "Dividende",
           "Sparplan", "Verlustverrechnung"]),
    _kind("vertrag", "de", "Vertrag", "Vertrag",
          ["Arbeitsvertrag {c}", "Vertrag {c}", "Darlehensvertrag {c}", "Kaufvertrag {D}"],
          ["employer", "bank", "misc"], ["Wichtig", "Original vorhanden", "Kredit"], "Archiv",
          False, 3,
          ["Arbeitsvertrag", "Probezeit", "Darlehensvertrag", "Tilgung", "Zinsbindung",
           "Grundbuch", "Kaufvertrag", "Gerichtsstand", "Vertragspartner", "Unterschrift"]),
    _kind("schule", "de", "Zeugnis", "Mitteilung",
          ["Zeugnis {Y}", "Elternbrief {D}", "Teilnahmebescheinigung {c}",
           "Mitgliedsbeitrag {c} {Y}", "Spendenquittung {c} {Y}"],
          ["misc"], ["Schule", "Kinder", "Verein", "Spende"], "Familie", False, 3,
          ["Zeugnis", "Elternabend", "Klassenfahrt", "Mitgliedsbeitrag",
           "Jahreshauptversammlung", "Teilnahmebescheinigung", "Spendenquittung", "Fortbildung",
           "Betreuungsvertrag"]),
    _kind("reise", "de", "Reisebeleg", "Reiseunterlagen",
          ["Reisekostenabrechnung {M} {Y}", "Buchungsbestätigung {c}", "Fahrkarte {D}",
           "Hotelrechnung {D}"],
          ["mobility"], ["Reise", "Urlaub"], None, True, 3,
          ["Reisekostenabrechnung", "Buchungsbestätigung", "Fahrkarte", "Sitzplatzreservierung",
           "Stornierung", "Reiserücktrittsversicherung", "Verpflegungsmehraufwand",
           "Kilometerpauschale", "Übernachtung"]),
    _kind("brief", "de", "Brief", "Schreiben",
          ["Schreiben {c} {D}", "Brief {c}", "Informationsschreiben {c} {Y}", "Einladung {c}"],
          ["authority", "bank", "insurance", "misc", "health", "landlord"], ["Privat", "Scan"],
          None, False, 6,
          ["Mitteilung", "Rückfrage", "Stellungnahme", "Einladung", "Terminvereinbarung",
           "Datenschutzerklärung", "Einverständniserklärung", "Vollmacht"]),
    _kind("anleitung", "de", "Bedienungsanleitung", "Bedienungsanleitung",
          ["Bedienungsanleitung {p}", "Garantiekarte {p}", "Kaufbeleg {p} {c}"],
          ["shop"], ["Garantie", "Elektronik", "Haus"], None, False, 2,
          ["Inbetriebnahme", "Sicherheitshinweise", "Gewährleistung", "Ersatzteile",
           "Entkalkung", "Wartung", "Seriennummer", "Kundendienst", "Fehlerbehebung"]),
    _kind("invoice", "en", "Invoice", "Invoice",
          ["Invoice {c} {M} {Y}", "Invoice #{n}", "Receipt {c}", "{c} invoice {Y}-{m}"],
          ["intl", "shop"], ["receipt", "Finanzen"], "Finanzen", True, 8,
          ["invoice amount", "payment terms", "purchase order", "VAT", "shipping address",
           "order number", "subtotal", "late fee", "direct debit", "wire transfer"]),
    _kind("statement", "en", "Bank statement", "Statement",
          ["Bank statement {Y}-{m}", "Credit card statement {M} {Y}",
           "{c} account statement {Y}-{m}"],
          ["intl", "bank"], ["Bank", "Finanzen"], "Finanzen", False, 5,
          ["account balance", "checking account", "standing order", "overdraft", "interest rate",
           "transaction", "direct deposit", "statement period", "minimum payment"]),
    _kind("insurance_en", "en", "Insurance policy", "Policy schedule",
          ["Insurance policy renewal {Y}", "Policy schedule {c}", "Claim settlement {D}",
           "Home insurance certificate {Y}"],
          ["intl", "insurance"], ["Versicherung", "important"], "Versicherungen", True, 4,
          ["liability insurance", "policy number", "premium", "deductible", "claim", "coverage",
           "renewal", "beneficiary", "underwriter", "household contents"]),
    _kind("payslip", "en", "Payslip", "Payslip",
          ["Payslip {M} {Y}", "Annual salary statement {Y}", "Bonus letter {Y}"],
          ["employer"], ["Gehalt", "Arbeit"], "Arbeit", True, 3,
          ["gross salary", "net salary", "income tax", "pension contribution", "overtime",
           "vacation days", "payroll", "bonus", "social security"]),
    _kind("tax_en", "en", "Tax document", "Tax notice",
          ["Tax return {Y}", "Tax assessment {Y}", "Council tax bill {Y}"],
          ["intl"], ["Steuer", "Steuerrelevant"], "Steuer", True, 2,
          ["tax return", "tax assessment", "allowance", "refund", "deduction", "filing deadline",
           "taxpayer reference", "withholding", "capital gains"]),
    _kind("lease", "en", "Contract", "Agreement",
          ["Lease agreement", "Utility bill {M} {Y}", "Rent increase notice {Y}",
           "Tenancy deposit certificate"],
          ["intl", "landlord"], ["Wohnung", "important"], "Wohnung", True, 2,
          ["lease agreement", "security deposit", "utility bill", "meter reading",
           "notice period", "landlord", "tenant", "maintenance", "service charge"]),
    _kind("letter", "en", "Letter", "Letter",
          ["Letter from {c}", "Cancellation confirmation {c}", "Welcome letter {c}",
           "Warranty certificate {c}"],
          ["intl", "shop"], ["to-review", "Privat"], None, False, 3,
          ["cancellation", "confirmation", "notice period", "warranty", "customer service",
           "reference number", "subscription", "terms and conditions"]),
]

GEN_NOUNS = {
    "de": ["Unterlagen", "Schreiben", "Vertragsnummer", "Antrag", "Bescheid", "Zahlungseingang",
           "Kundenkonto", "Frist", "Terminvereinbarung", "Anlage", "Vollmacht", "Unterschrift",
           "Bearbeitung", "Kundenservice", "Datenschutz", "Widerspruch", "Mitteilung",
           "Überweisung", "Lastschriftmandat", "Kündigung", "Steuererklärung",
           "Haftpflichtversicherung", "Ansprechpartner", "Rückfrage", "Zahlungserinnerung",
           "Vertragslaufzeit", "Bankverbindung", "Geschäftsstelle München",
           "Niederlassung Köln", "Bestätigung", "Änderungsmitteilung", "Jahresübersicht"],
    "en": ["documents", "letter", "contract number", "application", "notice", "payment",
           "customer account", "deadline", "appointment", "attachment", "power of attorney",
           "signature", "processing", "customer service", "privacy policy", "objection",
           "wire transfer", "direct debit mandate", "cancellation", "tax return",
           "liability insurance", "contact person", "enquiry", "payment reminder",
           "contract term", "bank details", "Munich office", "confirmation", "change notice",
           "annual summary"],
}

LINES = {
    "de": [
        "Sehr geehrte Damen und Herren,",
        "vielen Dank für Ihr Schreiben vom {D}.",
        "hiermit bestätigen wir den Eingang Ihrer Unterlagen zum Thema {N}.",
        "Betreff: {N} - Ihr Zeichen {NUM}/{Y2}",
        "Der Vorgang {N} wird zum {D} wirksam.",
        "Bitte prüfen Sie die Angaben im Abschnitt {N} sorgfältig.",
        "Für Rückfragen zum Thema {N} erreichen Sie uns unter der Rufnummer {TEL}.",
        "Der Betrag in Höhe von {A} EUR wird per Lastschrift von Ihrem Konto eingezogen.",
        "Bitte überweisen Sie {A} EUR bis zum {D} auf das unten genannte Konto.",
        "Die Unterlagen zu {N} und {N2} sind diesem Schreiben beigefügt.",
        "Gemäß unseren Allgemeinen Geschäftsbedingungen gilt eine Frist von {NUM} Tagen.",
        "Ihr Anliegen ({N}) wurde am {D} in {C} bearbeitet.",
        "Wir bitten um Verständnis, dass die Bearbeitung einige Zeit in Anspruch nimmt.",
        "Sollten Sie die Zahlung bereits veranlasst haben, ist dieses Schreiben gegenstandslos.",
        "Mit freundlichen Grüßen",
        "Dieses Schreiben wurde maschinell erstellt und ist ohne Unterschrift gültig.",
        "Änderungen zu {N} teilen Sie uns bitte unverzüglich schriftlich mit.",
        "Die Kündigungsfrist beträgt {NUM} Tage zum Ende der Vertragslaufzeit.",
        "Im Anhang finden Sie eine Übersicht: {N}, {N2} sowie weitere Positionen.",
        "Nach Prüfung ({N}) ergibt sich eine Nachzahlung von {A} EUR.",
        "Nach Prüfung ({N}) ergibt sich ein Guthaben von {A} EUR.",
        "Bitte bewahren Sie dieses Dokument für Ihre Steuererklärung auf.",
        "Die Mehrwertsteuer in Höhe von 19 % ist im Gesamtbetrag enthalten.",
        "Zuständig für den Bereich {N} ist unsere Geschäftsstelle in {C}.",
        "Position {NUM}: {N} - Einzelpreis {A} EUR - Menge 1 - Summe {A} EUR",
        "Zeitraum: {D} bis {D2}, Abrechnungsart: {N}",
        "Stichwort {N}: Bitte beachten Sie die geänderten Bedingungen ab dem {D}.",
        "Eine Übersicht zu {N2} erhalten Sie jederzeit in Ihrem Online-Kundenkonto.",
        "Ihre Angaben werden gemäß Datenschutz-Grundverordnung vertraulich behandelt.",
        "Bei Zahlungsverzug berechnen wir Verzugszinsen sowie eine Mahngebühr von {A} EUR.",
        "Bitte geben Sie bei der Überweisung stets den Verwendungszweck {NUM}-{Y2} an.",
        "Wir freuen uns, Ihnen folgende Änderung mitzuteilen: {N} ab {D}.",
        "Hinweis: {N} und {N2} können Sie auch online einsehen.",
        "Rückfragen richten Sie bitte an unsere Niederlassung in {C}, Telefon {TEL}.",
    ],
    "en": [
        "Dear Sir or Madam,",
        "Thank you for your letter dated {D}.",
        "We hereby confirm receipt of your documents regarding {N}.",
        "Subject: {N} - your reference {NUM}/{Y2}",
        "The {N} will take effect on {D}.",
        "Please review the details in the section on {N} carefully.",
        "If you have any questions about {N}, please call us on {TEL}.",
        "The amount of EUR {A} will be collected by direct debit from your account.",
        "Please transfer EUR {A} by {D} to the account stated below.",
        "The documents concerning {N} and {N2} are enclosed with this letter.",
        "In accordance with our terms and conditions, a period of {NUM} days applies.",
        "Your request ({N}) was processed on {D} at our {C} branch.",
        "We ask for your understanding that processing may take some time.",
        "If you have already made the payment, please disregard this letter.",
        "Yours sincerely,",
        "This letter was generated automatically and is valid without a signature.",
        "Please notify us in writing of any changes to {N} without delay.",
        "The notice period is {NUM} days before the end of the contract term.",
        "Attached you will find an overview: {N}, {N2} and further items.",
        "After review ({N}) an additional payment of EUR {A} is due.",
        "After review ({N}) you are entitled to a credit of EUR {A}.",
        "Please keep this document for your tax return.",
        "VAT at the applicable rate is included in the total amount.",
        "Our {C} office is responsible for matters concerning {N}.",
        "Item {NUM}: {N} - unit price EUR {A} - quantity 1 - total EUR {A}",
        "Period: {D} to {D2}, billing type: {N}",
        "Keyword {N}: please note the revised conditions effective {D}.",
        "An overview of {N2} is available at any time in your online account.",
        "Your details are treated confidentially in line with data protection law.",
        "Late payments incur interest and a reminder fee of EUR {A}.",
        "Always quote the payment reference {NUM}-{Y2} when making a transfer.",
        "We are pleased to inform you of the following change: {N} from {D}.",
    ],
}

RARE_LINE = {
    "de": "Ergänzender Hinweis zum Stichwort {W}: Einzelheiten entnehmen Sie bitte der Anlage.",
    "en": "Additional note regarding {W}: please refer to the enclosed appendix for details.",
}

RARE_WORDS = {
    "de": """Schornsteinfeger Grundbuchauszug Erbschein Patientenverfügung Vorsorgevollmacht
Testament Heiratsurkunde Geburtsurkunde Scheidungsurteil Zwangsvollstreckung Pfändungsbeschluss
Insolvenzverfahren Mahnbescheid Vollstreckungsbescheid Elterngeld Kindergeld Wohngeld
Studienbescheinigung Immatrikulation Exmatrikulation Promotionsurkunde Meisterbrief Gesellenbrief
Gewerbeanmeldung Handelsregisterauszug Betriebsprüfung Zollanmeldung Einfuhrumsatzsteuer
Erbbaurecht Teilungserklärung Eigentümerversammlung Hausgeldabrechnung Instandhaltungsrücklage
Energieausweis Wärmepumpe Photovoltaikanlage Einspeisevergütung Wallbox Glasfaseranschluss
Kabelanschluss Satellitenschüssel Rauchwarnmelder Legionellenprüfung Kehrbescheid
Feuerstättenschau Dichtheitsprüfung Baugenehmigung Flurkarte Vermessungsamt Grunderwerbsteuer
Notarkosten Maklerprovision Bausparvertrag Riesterrente Direktversicherung Pensionskasse
Abfindung Aufhebungsvertrag Arbeitszeugnis Zwischenzeugnis Elternzeit Mutterschutz
Kurzarbeitergeld Insolvenzgeld Schwerbehindertenausweis Pflegegrad Pflegegeld Rollator Hörgerät
Brillenpass Impfpass Röntgenpass Mutterpass Organspendeausweis Blutspendeausweis Tauchschein
Angelschein Jagdschein Bootsführerschein Fluglizenz Hundesteuer Hundehaftpflicht
Tierkrankenversicherung Heimtierausweis Kleingartenverein Pachtvertrag Parkausweis
Anwohnerparken Umweltplakette Mautgebühr Vignette Zulassungsbescheinigung Abmeldebescheinigung
Kurzzeitkennzeichen Saisonkennzeichen Oldtimergutachten Leasingvertrag Restwert Fahrtenbuch
Tankkarte Ladekarte Bahncard Jobticket Deutschlandticket Semesterticket Fahrgastrechte
Gepäckverlust Visumantrag Auslandskrankenversicherung Reisepassantrag Zollerklärung
Kirchenaustritt Taufurkunde Sterbeurkunde Grabnutzungsrecht Bestattungsvorsorge Nachlassgericht
Erbauseinandersetzung Pflichtteil Schenkungsteuer Vermögensaufstellung Schließfach
Edelmetallkauf Kryptowährung Genossenschaftsanteil Sparbrief Festgeldkonto Tagesgeldkonto
Bonitätsprüfung Bürgschaft Mietbürgschaft Wohnungsgeberbestätigung Untermietvertrag
Zweitwohnungssteuer Kurtaxe""".split(),
    "en": [w.strip() for w in """affidavit; apostille; notarized copy; probate; mortgage deed;
escrow; lien; easement; homeowners association; property survey; building permit; jury summons;
parking permit; vehicle registration; emissions test; passport renewal; visa application;
customs declaration; birth certificate; marriage certificate; living will; organ donor card;
immunization record; dental x-ray; hearing aid; prescription glasses; pet insurance;
dog license; fishing license; boating license; tuition statement; student loan;
scholarship award; transcript; diploma; apprenticeship; severance agreement; stock options;
pension transfer; annuity; brokerage statement; dividend voucher; cryptocurrency;
safe deposit box; credit report; guarantor; sublease; television licence; water rates;
broadband installation; solar panels; heat pump; energy performance certificate;
chimney sweep; pest control; home inventory; flood insurance; umbrella policy;
voter registration; charity donation receipt; inheritance tax; trust deed; executor;
roadside assistance; toll invoice; congestion charge; railcard; frequent flyer;
lost luggage; travel vaccination""".split(";")],
}

# Deterministic "needle" documents: (word, where, ids). "content" needles are
# extra lines in the OCR text only; "title" needles only change the title.
NEEDLES = [
    ("zebrafink", "content", [137, 4242, 17011]),
    ("quittungsnummer-7781", "content", [2718]),
    ("Glühwürmchenstraße", "content", [808, 15150]),
    ("mondscheintarif", "content", [555, 1234, 7777, 12345, 19999]),
    ("Mondscheintarif", "title", [1234]),
    ("Xylophonwartung", "title", [314, 9001]),
]
NEEDLE_LINES = {
    "zebrafink": "Notiz des Sachbearbeiters: Stichwort zebrafink, Vorgang abgeschlossen.",
    "quittungsnummer-7781": "Beleg quittungsnummer-7781 wurde dem Vorgang zugeordnet.",
    "Glühwürmchenstraße": "Neue Anschrift: Glühwürmchenstraße 12, 97070 Würzburg",
    "mondscheintarif": "Gewählter Tarif: mondscheintarif (gültig von 22 bis 6 Uhr)",
}
NEEDLE_TITLES = {314: "Xylophonwartung Angebot", 9001: "Protokoll Xylophonwartung",
                 1234: "Mondscheintarif Vertragsunterlagen"}
NEEDLE_POSITION = ["start", "middle", "end"]

NB_TABLE = ([3] * 12 + [4] * 16 + [5] * 16 + [6] * 13 + [7] * 11 + [8] * 9 + [9] * 8 + [10] * 7
            + [11] * 5 + [12] * 3)
PAGE_TABLE = [1] * 45 + [2] * 25 + [3] * 12 + [4] * 7 + [5, 5, 6, 6, 7, 8, 9, 10, 11, 12, 12]
BITS_TO_01 = bytes.maketrans(b"01", b"\x00\x01")

FONT = {  # 5x7 bitmap font for the document number on thumbnails
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11110 00001 00001 01110 00001 00001 11110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    "#": "01010 01010 11111 01010 11111 01010 01010",
}
FONT = {k: v.split() for k, v in FONT.items()}


# --------------------------------------------------------------------------- #
# PDF / PNG builders
# --------------------------------------------------------------------------- #


def pdf_text(s: str) -> bytes:
    return (s.encode("cp1252", "replace").replace(b"\\", b"\\\\")
            .replace(b"(", b"\\(").replace(b")", b"\\)"))


def wrap_line(line: str, width: int = 98) -> list[str]:
    if len(line) <= width:
        return [line]
    out = []
    while len(line) > width:
        cut = line.rfind(" ", 0, width)
        if cut <= 0:
            cut = width
        out.append(line[:cut])
        line = line[cut:].lstrip()
    out.append(line)
    return out


def build_pdf(title: str, lines: list[str], pages: int, producer: str, footer: str,
              ident: bytes, target: int = 0, pool: bytes = b"", pool_off: int = 0) -> bytes:
    """A minimal, valid PDF 1.4: Helvetica text streams on `pages` pages.

    When `target` exceeds the size of the text-only file, an unreferenced stream
    object of bytes taken from `pool` pads the file to roughly `target` bytes.
    """
    per_page = 52
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_line(line))
    objs: list[bytes] = [b""] * 5
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objs[3] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
               b"/Encoding /WinAnsiEncoding >>")
    objs[4] = (b"<< /Title <FEFF" + title.encode("utf-16-be").hex().upper().encode()
               + b"> /Producer (" + pdf_text(producer) + b") /Creator (mock-paperless) >>")
    kids = []
    for p in range(pages):
        chunk = wrapped[p * per_page:(p + 1) * per_page]
        parts = []
        if p == 0:
            parts.append(b"BT /F2 15 Tf 50 792 Td (" + pdf_text(title[:80]) + b") Tj ET")
        if chunk:
            body = [b"BT /F1 9.5 Tf 13 TL 50 764 Td"]
            for line in chunk:
                body.append(b"(" + pdf_text(line) + b") Tj T*" if line else b"T*")
            body.append(b"ET")
            parts.append(b"\n".join(body))
        parts.append(b"BT /F1 8 Tf 50 30 Td ("
                     + pdf_text(footer.format(page=p + 1, pages=pages)) + b") Tj ET")
        stream = b"\n".join(parts)
        page_no = len(objs) + 1
        kids.append(b"%d 0 R" % page_no)
        objs.append(b"<< /Type /Page /Parent 2 0 R /Contents %d 0 R >>" % (page_no + 1))
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs[1] = (b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d " % pages
               + b"/MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>")
    need = target - sum(map(len, objs)) - 40 * len(objs) - 220
    if need > 0 and pool:
        padding = pool[pool_off:pool_off + need]
        while len(padding) < need:
            padding += pool[:need - len(padding)]
        objs.append(b"<< /Type /MockPadding /Length %d >>\nstream\n" % len(padding)
                    + padding + b"\nendstream")
    out = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    pos = len(out[0])
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(pos)
        chunk = b"%d 0 obj\n" % num + body + b"\nendobj\n"
        out.append(chunk)
        pos += len(chunk)
    out.append(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    out.append(b"".join(b"%010d 00000 n \n" % o for o in offsets))
    out.append(b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R /ID [<%s> <%s>] >>\n"
               % (len(objs) + 1, ident, ident))
    out.append(b"startxref\n%d\n%%%%EOF\n" % pos)
    return b"".join(out)


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def build_page_png(doc_id: int, h: int, scale: int = 1, padding: bytes = b"") -> bytes:
    """Palette PNG of a 'page': coloured header band with the id, grey text lines.

    300x424 px at scale 1. `padding` goes into a private ancillary chunk.
    """
    W = 300
    hue = (doc_id * 0.6180339887) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.78)
    ar, ag, ab = colorsys.hsv_to_rgb((hue + 0.5) % 1.0, 0.45, 0.9)
    palette = bytes([255, 255, 255, int(r * 255), int(g * 255), int(b * 255), 190, 190, 196,
                     110, 110, 118, 214, 214, 220, int(ar * 255), int(ag * 255), int(ab * 255)])
    border = bytes([4]) * W
    blank = bytes([4]) + bytes(W - 2) + bytes([4])
    band = bytes([4]) + bytes([1]) * (W - 2) + bytes([4])
    rows: list[bytes] = [border]
    # header band (rows 1..53) with the document number in a 5x7 font, 4 px per dot
    text = "#%d" % doc_id
    glyph_rows = []
    for gy in range(7):
        row = bytearray(band)
        x = 16
        for ch in text[:11]:
            bits = FONT[ch][gy]
            for bx in range(5):
                if bits[bx] == "1":
                    row[x + bx * 4:x + bx * 4 + 4] = bytes(4)
            x += 24
        glyph_rows.append(bytes(row))
    for y in range(1, 54):
        gy = (y - 13) // 4
        rows.append(glyph_rows[gy] if y >= 13 and gy < 7 else band)
    # "text": grey bars of varying length, 5 px high every 12 px
    line = blank
    for y in range(54, 423):
        if 72 <= y < 400 and (y - 72) % 12 < 5:
            if (y - 72) % 12 == 0:
                n = (y - 72) // 12
                k = (h >> (n * 7 % 240)) & 0xFF
                if k % 7 == 0 and n > 1:
                    line = blank  # paragraph gap
                else:
                    colour = 3 if (n == 0 or k % 11 == 0) else (5 if n >= 22 and k % 5 == 0 else 2)
                    width = (60 + k % 80) if colour == 3 else (90 + k % 160)
                    bar = bytearray([colour]) * width
                    x, state = 0, k * 2654435761 + n
                    while True:  # split the bar into "words"
                        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
                        x += 12 + (state >> 8) % 34
                        if x + 3 >= width:
                            break
                        bar[x:x + 3] = bytes(3)
                        x += 3
                    line = bytes([4]) + bytes(23) + bytes(bar) + bytes(W - 25 - width) + bytes([4])
            rows.append(line)
        else:
            rows.append(blank)
    rows.append(border)
    if scale > 1:
        cache: dict[bytes, bytes] = {}
        scaled = []
        for row in rows:
            wide = cache.get(row)
            if wide is None:
                wide = cache[row] = bytes(v for v in row for _ in range(scale))
            scaled.extend([wide] * scale)
        rows = scaled
    raw = b"".join([b"\x00" + row for row in rows])
    out = [b"\x89PNG\r\n\x1a\n",
           png_chunk(b"IHDR", struct.pack(">IIBBBBB", W * scale, len(rows), 8, 3, 0, 0, 0)),
           png_chunk(b"PLTE", palette),
           png_chunk(b"IDAT", zlib.compress(raw, 6))]
    if padding:
        out.append(png_chunk(b"paDd", padding))
    out.append(png_chunk(b"IEND", b""))
    return b"".join(out)


# --------------------------------------------------------------------------- #
# The mock backend
# --------------------------------------------------------------------------- #


class Mock:
    def __init__(self, args):
        self.args = args
        self.seed = args.seed
        self.key = str(args.seed).encode()[:64] or b"0"
        self.token = args.token
        self.api_version = args.api_version
        self.server_version = args.server_version
        self.sv = tuple(int(x) for x in re.findall(r"\d+", args.server_version)[:3])
        self.v3 = self.api_version >= 10 or self.sv >= (3, 0, 0)
        self.nested_tags = self.sv >= (2, 19, 0)
        self.checksum_algo = "sha256" if self.v3 else "md5"
        self.null_key = BIG if args.null_ordering == "postgres" else -1
        self.pdf_bytes = int(args.pdf_kb * 1024)
        self.latency = args.latency_ms / 1000.0
        self.quiet = args.quiet
        self.lock = threading.RLock()
        ref = dt.date.today() if args.reference_date == "today" else dt.date.fromisoformat(
            args.reference_date)
        self.ref_ord = ref.toordinal()
        self.ref_ts = ord_ts(self.ref_ord) + 12 * 3600
        self.fail = {"mode": "off", "path": None, "remaining": None}
        self.fail_generation = 0
        self.stats = {"started": time.time(), "requests": 0, "by_class": Counter(),
                      "by_status": Counter(), "bytes_served": 0, "pdfs_served": 0,
                      "pdf_bytes": 0, "thumbnails_served": 0, "not_modified": 0, "hung": 0,
                      "unknown_params": {}, "unknown_endpoints": Counter()}
        self.version = 1
        self._qcache: OrderedDict = OrderedDict()
        self._derived: dict = {}
        self._mask_ids: dict[int, list[int]] = {0: []}
        self.finfo: dict = {}
        self.pad_pool = random.Random("pad:%s" % self.seed).randbytes(1 << 21)
        self._build_elements()
        self._build_units()
        self._build_documents(args.documents)
        self._last_now = max(self.ref_ts, max(self.modified) if self.n else 0.0)
        self.index_ready = threading.Event()
        threading.Thread(target=self._build_index, name="content-index", daemon=True).start()

    # ------------------------------------------------------------------ #
    # deterministic hashing
    # ------------------------------------------------------------------ #

    def H(self, i: int, salt: bytes) -> int:
        return int.from_bytes(
            hashlib.blake2b(b"%d:%s" % (i, salt), key=self.key, digest_size=64).digest(), "little")

    def now(self) -> float:
        t = time.time()
        if t <= self._last_now:
            t = self._last_now + 0.001
        self._last_now = t
        return t

    def bump(self):
        self.version += 1
        self._qcache.clear()
        self._derived.clear()

    # ------------------------------------------------------------------ #
    # element collections
    # ------------------------------------------------------------------ #

    def _build_elements(self):
        rng = random.Random("elements:%s" % self.seed)
        self.users = {
            1: {"id": 1, "username": "admin", "email": "admin@example.com", "password": "**********",
                "first_name": "Ada", "last_name": "Admin", "date_joined": "2021-01-04T09:12:44.120000Z",
                "is_staff": True, "is_active": True, "is_superuser": True, "groups": [1],
                "user_permissions": [], "inherited_permissions": [], "is_mfa_enabled": False},
            2: {"id": 2, "username": "anna", "email": "anna@example.com", "password": "**********",
                "first_name": "Anna", "last_name": "Beispiel",
                "date_joined": "2021-03-18T17:40:02.500000Z", "is_staff": False, "is_active": True,
                "is_superuser": False, "groups": [1], "user_permissions": ["view_document"],
                "inherited_permissions": ["view_document", "view_tag", "view_correspondent"],
                "is_mfa_enabled": False},
        }
        self.groups = {1: {"id": 1, "name": "Familie",
                           "permissions": ["view_document", "view_tag", "view_correspondent",
                                           "view_documenttype", "view_storagepath"]}}
        self.me = 1

        # correspondents
        corrs = []
        self.corr_group: dict[str, list[int]] = {g: [] for g in CORR_GROUPS}
        names = set()

        def add_corr(name, group):
            if name in names:
                return False
            names.add(name)
            cid = len(corrs) + 1
            city, zipc = rng.choice(CITIES)
            iban = "DE%02d %04d %04d %04d %04d %02d" % (
                rng.randint(10, 99), rng.randint(1000, 9999), rng.randint(0, 9999),
                rng.randint(0, 9999), rng.randint(0, 9999), rng.randint(0, 99))
            short = name
            for _ in range(3):
                short = re.sub(r"\s+(GmbH|AG|SE|e\.V\.|eG|Ltd\.|Corp\.|S\.à r\.l\.|Deutschland|"
                               r"Versicherungs-AG|Versicherung|Europe|Energie)$", "", short)
            corrs.append({
                "id": cid, "slug": slugify(name), "name": name, "match": "",
                "matching_algorithm": 6 if cid % 3 else 1, "is_insensitive": True, "owner": 1,
                "_short": short, "_group": group, "_iban": iban,
                "_bic": "".join(rng.choice("ABCDEFGHKLMNOPRSTUW") for _ in range(4)) + "DEFFXXX",
                "_addr": "%s %d, %s %s" % (rng.choice(STREETS), rng.randint(1, 140), zipc, city),
            })
            self.corr_group[group].append(cid)
            return True

        for group, items in CORR_GROUPS.items():
            for name in items:
                add_corr(name, group)
        k = 0
        while len(corrs) < 200:
            group, pattern = CORR_PATTERNS[k % len(CORR_PATTERNS)]
            k += 1
            add_corr(pattern.format(S=rng.choice(SURNAMES), S2=rng.choice(SURNAMES),
                                    C=rng.choice(CITIES)[0]), group)
        self.correspondents = {c["id"]: c for c in corrs}
        # what generated text/files print; unaffected by later renames or deletes
        self.p_corr_info = {c["id"]: (c["name"], c["_addr"], c["_iban"], c["_bic"], c["_short"])
                            for c in corrs}

        # tags
        tags = {}
        by_name = {}
        for idx, (name, parent) in enumerate(TAGS, 1):
            color = TAG_COLORS[(idx * 7) % len(TAG_COLORS)]
            lum = sum(int(color[j:j + 2], 16) * w for j, w in ((1, .299), (3, .587), (5, .114)))
            tags[idx] = {"id": idx, "slug": slugify(name), "name": name, "color": color,
                         "text_color": "#000000" if lum > 150 else "#ffffff", "match": "",
                         "matching_algorithm": 6 if idx % 4 else 0, "is_insensitive": True,
                         "is_inbox_tag": name == "Inbox", "owner": 1,
                         "parent": by_name[parent] if parent else None}
            by_name[name] = idx
        self.tags = tags
        self.tag_by_name = by_name
        self.inbox_tag = by_name["Inbox"]
        self._tag_closure()

        self.doctypes = {
            i: {"id": i, "slug": slugify(n), "name": n, "match": "", "matching_algorithm": 6,
                "is_insensitive": True, "owner": 1}
            for i, n in enumerate(DOCUMENT_TYPES, 1)}
        self.dtype_by_name = {v["name"]: k for k, v in self.doctypes.items()}
        self.spaths = {
            i: {"id": i, "slug": slugify(n), "name": n, "path": p, "match": "",
                "matching_algorithm": 0, "is_insensitive": True, "owner": 1}
            for i, (n, p) in enumerate(STORAGE_PATHS, 1)}
        self.spath_by_name = {v["name"]: k for k, v in self.spaths.items()}
        self.custom_fields = {
            i: {"id": i, "name": n, "data_type": t, "extra_data": e} for i, n, t, e in CUSTOM_FIELDS}

        search_rule = 49 if self.v3 else 19
        year = dt.date.fromordinal(self.ref_ord).year
        views = [
            ("Inbox", "added", True, [(6, str(self.inbox_tag))], True, True),
            ("Rechnungen", "created", True,
             [(28, str(self.dtype_by_name["Rechnung"]))], True, True),
            ("Telekom", "created", True, [(26, "1")], False, True),
            ("Dieses Jahr", "created", True, [(20, 'created:"this year"')], True, False),
            ("Steuer offen", "archive_serial_number", False,
             [(22, str(by_name["Steuer"])), (17, str(by_name["Erledigt"]))], False, True),
            ("Versicherung ab %d" % (year - 3), "title", False,
             [(search_rule, "Versicherung"), (44, "%d-01-01" % (year - 3))], False, True),
            ("Ohne ASN, zuletzt geändert", "modified", True, [(18, "true")], False, False),
        ]
        self.saved_views = {}
        for i, (name, sort, rev, rules, dash, side) in enumerate(views, 1):
            self.saved_views[i] = {
                "id": i, "name": name, "show_on_dashboard": dash, "show_in_sidebar": side,
                "sort_field": sort, "sort_reverse": rev,
                "filter_rules": [{"rule_type": t, "value": v} for t, v in rules],
                "page_size": None, "display_mode": "smallCards" if i % 2 else None,
                "display_fields": ["title", "created", "tag", "correspondent", "documenttype",
                                   "storagepath", "note", "owner", "asn", "pagecount"]
                if i % 2 else None,
                "owner": 1}
        self.resources = {
            "correspondents": self.correspondents, "tags": self.tags,
            "document_types": self.doctypes, "storage_paths": self.spaths,
            "saved_views": self.saved_views, "custom_fields": self.custom_fields,
            "users": self.users, "groups": self.groups,
        }
        self.next_ids = {name: max(items) + 1 for name, items in self.resources.items()}
        resources = ["document", "tag", "correspondent", "documenttype", "storagepath",
                     "savedview", "paperlesstask", "uisettings", "note", "mailaccount", "mailrule",
                     "user", "group", "sharelink", "customfield", "workflow",
                     "applicationconfiguration", "history", "appconfig", "sharelinkbundle"]
        self.ui_settings = {
            "update_checking": {"enabled": False, "backend_setting": "default"},
            "trash_delay": 30, "app_title": "Mock Paperless", "app_logo": None,
            "auditlog_enabled": True, "tour_complete": True, "email_enabled": False,
            "bulk_edit": {"apply_on_close": False, "confirmation_dialogs": True},
            "documentListSize": 50, "slim_sidebar": False,
            "dark_mode": {"use_system": True, "enabled": "false", "thumb_inverted": "true"},
            "theme": {"color": ""},
            "document_details": {"native_pdf_viewer": False},
            "date_display": {"date_locale": "", "date_format": "mediumDate"},
            "notifications": {"consumer_new_documents": True, "consumer_success": True,
                              "consumer_failed": True, "consumer_suppress_on_dashboard": True},
            "notes_enabled": True,
            "saved_views": {
                "warn_on_unsaved_change": True, "sidebar_views_show_count": True,
                "dashboard_views_visible_ids": [i for i, v in self.saved_views.items()
                                                if v["show_on_dashboard"]],
                "sidebar_views_visible_ids": [i for i, v in self.saved_views.items()
                                              if v["show_in_sidebar"]]},
            "permissions": {"default_owner": 1, "default_view_users": [],
                            "default_view_groups": [], "default_edit_users": [],
                            "default_edit_groups": []},
            "document_editing": {"remove_inbox_tags": False, "overlay_thumbnail": True},
            "search": {"db_only": False, "more_link": "title-content"},
            "language": "", "version": self.server_version, "ai_enabled": False,
        }
        self.user_permissions = ["%s_%s" % (op, r) for r in resources
                                 for op in ("add", "change", "delete", "view")]
        self.tasks: list[dict] = []
        self.share_links: dict[int, dict] = {}
        self.next_share = 1

    def _tag_closure(self):
        """tag id -> bitmask of the tag and its ancestors; inbox bitmask."""
        closure = {}
        for tid, tag in self.tags.items():
            mask, cur, guard = 0, tid, 0
            while cur is not None and cur in self.tags and guard < 20:
                mask |= 1 << cur
                cur = self.tags[cur].get("parent")
                guard += 1
            closure[tid] = mask
        self.tag_closure = closure
        self.inbox_mask = 0
        for tid, tag in self.tags.items():
            if tag.get("is_inbox_tag"):
                self.inbox_mask |= 1 << tid

    # ------------------------------------------------------------------ #
    # text units (shared paragraphs) for OCR content
    # ------------------------------------------------------------------ #

    def _build_units(self):
        rng = random.Random("units:%s" % self.seed)
        ref_year = dt.date.fromordinal(self.ref_ord).year

        def rdate(lang):
            y, m, d = rng.randint(ref_year - 14, ref_year), rng.randint(1, 12), rng.randint(1, 28)
            return "%02d.%02d.%d" % (d, m, y) if lang == "de" else "%d %s %d" % (
                d, MONTHS["en"][m - 1], y)

        def fill(tpl, nouns, lang):
            amount = "%d,%02d" % (rng.randint(5, 2500), rng.randint(0, 99))
            if lang == "en":
                amount = amount.replace(",", ".")
            return tpl.format(
                N=rng.choice(nouns), N2=rng.choice(GEN_NOUNS[lang]), D=rdate(lang), D2=rdate(lang),
                A=amount, C=rng.choice(CITIES)[0], NUM=rng.choice([7, 10, 14, 21, 28, 30, 42, 60]),
                Y2=rng.randint(10, 26),
                TEL="0%d %d" % (rng.randint(30, 989), rng.randint(100000, 9999999)))

        def block(nouns, lang):
            tpls = rng.sample(LINES[lang], rng.randint(4, 8))
            return "\n".join(fill(t, nouns, lang) for t in tpls)

        units: list[str] = []
        self.kind_units: list[list[int]] = []
        for kind in KINDS:
            ids = []
            for _ in range(16):
                ids.append(len(units))
                units.append(block(kind["nouns"], kind["lang"]))
            self.kind_units.append(ids)
        self.gen_units = {}
        for lang in ("de", "en"):
            ids = []
            for _ in range(80):
                ids.append(len(units))
                units.append(block(GEN_NOUNS[lang], lang))
            self.gen_units[lang] = ids
        self.rare_units = {}
        for lang in ("de", "en"):
            ids = []
            for word in RARE_WORDS[lang]:
                ids.append(len(units))
                units.append(RARE_LINE[lang].format(W=word))
            self.rare_units[lang] = ids
        self.units = units
        self.units_lc = [u.lower() for u in units]
        self.unit_len = [len(u) for u in units]

        # kind lookup tables
        self.kind_pick = []
        for idx, kind in enumerate(KINDS):
            self.kind_pick.extend([idx] * kind["weight"])
            corr_ids: list[int] = []
            groups = [self.corr_group[g] for g in kind["groups"]]
            for j in range(max(len(g) for g in groups)):
                for g in groups:
                    if j < len(g):
                        corr_ids.append(g[j])
            kind["_corr"] = corr_ids
            kind["_dtype"] = self.dtype_by_name[kind["dtype"]]
            kind["_spath"] = self.spath_by_name[kind["spath"]] if kind["spath"] else 0
            kind["_tags"] = [self.tag_by_name[t] for t in kind["tags"]]
        parents = {p for _, p in TAGS if p}
        self.flat_tags = [self.tag_by_name[n] for n, p in TAGS
                          if p is None and n not in parents and n != "Inbox"]
        self.needle_lines: dict[int, list[tuple[str, str]]] = {}
        for word, where, ids in NEEDLES:
            if where != "content":
                continue
            for pos, i in enumerate(ids):
                self.needle_lines.setdefault(i, []).append(
                    (NEEDLE_POSITION[pos % 3], NEEDLE_LINES[word]))

    # ------------------------------------------------------------------ #
    # documents (column store)
    # ------------------------------------------------------------------ #

    def _build_documents(self, n: int):
        self.n = 0
        self.title: list[str] = [""]
        self.title_lc: list[str] = [""]
        self.tagmask: list[int] = [0]
        self.corr = array.array("H", [0])
        self.dtype = array.array("H", [0])
        self.spath = array.array("H", [0])
        self.owner = array.array("H", [0])
        self.created = array.array("i", [0])
        self.added = array.array("d", [0.0])
        self.modified = array.array("d", [0.0])
        self.asn = array.array("I", [0])
        self.pages = array.array("B", [0])
        # 1 = no archive version, 2 = PNG original, 4 = generated notes,
        # 8 = generated custom fields, 16/32 = generated permissions (variant 1/2)
        self.flags = array.array("B", [0])
        self.alive = bytearray(1)
        # pristine values the generated file/text depend on (never mutated)
        self.p_kind = array.array("B", [0])
        self.p_corr = array.array("H", [0])
        self.p_created = array.array("i", [0])
        self.p_pages = array.array("B", [0])
        self.clen = array.array("I", [0])
        # Notes, custom fields and permissions are generated on demand from
        # (seed, id); these dicts only hold what a mutation replaced them with.
        self.notes: dict[int, list[dict]] = {}
        self.cfields: dict[int, list[dict]] = {}
        self.perms: dict[int, dict | None] = {}
        self._mask_intern: dict[int, int] = {}
        self.trashed: dict[int, float] = {}
        self.content_override: dict[int, str] = {}
        self.p_title: dict[int, str] = {}  # generated title of documents retitled later
        self.filename_override: dict[int, str] = {}
        self.extra_spec: dict[int, str] = {}
        self.next_asn = 1
        self.next_note = 2_000_000_001  # ids of notes created through the API
        for i in range(1, n + 1):
            self._append_document(i)

    def _append_document(self, i: int, now: float | None = None):
        """Generate document `i` from (seed, i) and append it to the columns."""
        h = self.H(i, b"meta")
        h, r = divmod(h, len(self.kind_pick))
        kind_idx = self.kind_pick[r]
        kind = KINDS[kind_idx]
        lang = kind["lang"]
        # correspondent: skewed towards the first entries of the kind's list
        h, r = divmod(h, 100)
        corr = 0
        cids = kind["_corr"]
        h, a = divmod(h, len(cids))
        h, b = divmod(h, len(cids))
        if r >= 7:
            corr = cids[min(a, b)]
        # dates
        span = 15 * 365
        h, a = divmod(h, span)
        h, b = divmod(h, span)
        created = self.ref_ord - min(a, b)
        h, r = divmod(h, 21 * 86400)
        h, micro = divmod(h, 1000000)
        added = ord_ts(created) + 8 * 3600 + r
        adoption = self.ref_ts - 6 * 365 * 86400
        h, r = divmod(h, 150 * 86400)
        if added < adoption:
            added = adoption + r
        added = min(added, self.ref_ts - (r % 3600)) + micro / 1e6
        h, r = divmod(h, 100)
        h, f = divmod(h, 10000)
        if r < 55:
            modified = added + 1 + f / 4000.0
        else:
            modified = added + 2 + (f / 10000.0) ** 2 * max(0.0, self.ref_ts - added - 2)
        if now is not None:
            added = modified = now
        h, r = divmod(h, 100)
        pages = PAGE_TABLE[r]
        h, r = divmod(h, 100)
        owner = 1 if r < 72 else (2 if r < 92 else 0)
        h, r = divmod(h, 100)
        dtype = kind["_dtype"] if r < 92 else 0
        h, r = divmod(h, 100)
        spath = kind["_spath"] if r < 60 else 0
        # tags
        mask = 0
        h, untagged = divmod(h, 100)
        for tid in kind["_tags"]:
            h, r = divmod(h, 10)
            if r < 7:
                mask |= self.tag_closure[tid]
        h, r = divmod(h, 100)
        nflat = len(self.flat_tags)
        h, a = divmod(h, nflat)
        h, b = divmod(h, nflat)
        h, c = divmod(h, nflat)
        if r < 28:
            mask |= self.tag_closure[self.flat_tags[min(a, b)]]
        if r < 7:
            mask |= self.tag_closure[self.flat_tags[c]]
        if untagged < 7:
            mask = 0
        year = dt.date.fromordinal(created).year
        if kind["key"] == "steuer" and untagged >= 7:
            tid = self.tag_by_name.get("Steuer %d" % year)
            if tid:
                mask |= self.tag_closure[tid]
        h, r = divmod(h, 1000)
        recent = added > self.ref_ts - 45 * 86400
        if now is not None or (recent and r < 500) or r < 4:
            mask |= 1 << self.inbox_tag
        h, r = divmod(h, 100)
        asn = 0
        if r < 30 and now is None:
            asn = self.next_asn
            self.next_asn += 1
        h, r = divmod(h, 100)
        flags = 1 if r < 3 else (2 if r < 8 else 0)
        # title
        h, r = divmod(h, len(kind["titles"]))
        h, num = divmod(h, 90000)
        h, prod = divmod(h, len(PRODUCTS))
        d = dt.date.fromordinal(created)
        title = NEEDLE_TITLES.get(i)
        if title is None:
            title = kind["titles"][r].format(
                c=self.p_corr_info[corr][4] if corr else "",
                M=MONTHS[lang][d.month - 1], Y=d.year, m="%02d" % d.month, k=d.month,
                D=d.strftime("%d.%m.%Y") if lang == "de" else d.isoformat(),
                n=10000 + num, p=PRODUCTS[prod])
            title = re.sub(r"\s+(from|zum)$", "", " ".join(title.split()))
        # sparse extras
        h, r = divmod(h, 100)
        if r < 5:
            flags |= 4
            if now is None:  # a note bumps `modified`
                modified = max(modified, added + 3 * 3600 + 60)
        h, r = divmod(h, 100)
        if r < (36 if kind["money"] else 5):
            flags |= 8
        h, r = divmod(h, 100)
        if r < 4:
            flags |= 16 if r < 2 else 32
        self.title.append(title)
        self.title_lc.append(title.lower())
        self.tagmask.append(self._mask_intern.setdefault(mask, mask))
        self.corr.append(corr)
        self.dtype.append(dtype)
        self.spath.append(spath)
        self.owner.append(owner)
        self.created.append(created)
        self.added.append(added)
        self.modified.append(modified)
        self.asn.append(asn)
        self.pages.append(pages)
        self.flags.append(flags)
        self.alive.append(1)
        self.p_kind.append(kind_idx)
        self.p_corr.append(corr)
        self.p_created.append(created)
        self.p_pages.append(pages)
        self.clen.append(0)
        self.n = i

    NOTE_TEXTS = ["Bitte bis Monatsende prüfen.", "Original liegt im Ordner Steuer.",
                  "Bezahlt per Überweisung.", "Rückfrage beim Absender offen.",
                  "Checked and filed.", "Kopie an Steuerberater geschickt.",
                  "Frist im Kalender eingetragen.", "Duplicate of an earlier scan?",
                  "Betrag stimmt nicht mit Angebot überein.", "Erledigt."]
    NO_PERMS = {"view": {"users": [], "groups": []}, "change": {"users": [], "groups": []}}
    GEN_PERMS = {16: {"view": {"users": [2], "groups": []}, "change": {"users": [], "groups": []}},
                 32: {"view": {"users": [], "groups": [1]}, "change": {"users": [2], "groups": []}}}

    def get_notes(self, i: int) -> list[dict]:
        """Notes of document `i`: 1-3 generated ones (ids 4*i+k) unless replaced."""
        notes = self.notes.get(i)
        if notes is not None:
            return notes
        if not self.flags[i] & 4:
            return []
        h = self.H(i, b"notes")
        h, count = divmod(h, 6)
        out = []
        for k in range((1, 1, 1, 2, 2, 3)[count]):
            h, r = divmod(h, len(self.NOTE_TEXTS))
            h, u = divmod(h, 4)
            out.append({"id": 4 * i + k, "note": self.NOTE_TEXTS[r],
                        "created": self.added[i] + 3600 * (k + 1),
                        "user": None if u == 3 else (1 if u < 2 else 2)})
        return out

    def get_cf(self, i: int) -> list[dict]:
        """Custom field instances of document `i`: generated unless replaced."""
        entries = self.cfields.get(i)
        if entries is not None:
            return entries
        if not self.flags[i] & 8:
            return []
        h = self.H(i, b"cf")
        created = self.p_created[i]
        out = []
        if KINDS[self.p_kind[i]]["money"]:
            h, cents = divmod(h, 180000)
            out.append({"value": "EUR%d.%02d" % divmod(499 + cents, 100), "field": 1})
            h, r = divmod(h, 4)
            if r:
                out.append({"value": ord_iso(created + 14), "field": 2})
            if r > 1:
                h, paid = divmod(h, 3)
                out.append({"value": paid > 0, "field": 3})
        else:
            h, num = divmod(h, 9000000)
            out.append({"value": "KD-%07d" % (1000000 + num), "field": 4})
        h, r = divmod(h, 10)
        if r < 3:
            out.append({"value": ["st-offen", "st-arbeit", "st-erledigt"][r], "field": 5})
        if r == 9 and i > 3:
            out.append({"value": [i - 1, i - 3], "field": 6})
        fields = self.custom_fields
        return out if len(fields) >= 6 and all(e["field"] in fields for e in out) else [
            e for e in out if e["field"] in fields]

    def get_perms(self, i: int) -> dict | None:
        if i in self.perms:
            return self.perms[i]
        return self.GEN_PERMS.get(self.flags[i] & 48)

    def cf_docs(self, ids) -> list[int]:
        """The subset of `ids` that can have custom fields (cheap pre-filter)."""
        fl, ov = self.flags, self.cfields
        return [i for i in ids if fl[i] & 8 or i in ov]

    # ------------------------------------------------------------------ #
    # OCR content
    # ------------------------------------------------------------------ #

    def _plan(self, i: int):
        h = self.H(i, b"content")
        kind_idx = self.p_kind[i]
        lang = KINDS[kind_idx]["lang"]
        h, r = divmod(h, len(NB_TABLE))
        nb = NB_TABLE[r]
        topic_n = (nb * 3 + 2) // 5
        pool = list(self.kind_units[kind_idx])
        units = []
        for j in range(topic_n):
            h, r = divmod(h, len(pool) - j)
            pool[j], pool[j + r] = pool[j + r], pool[j]
            units.append(pool[j])
        gen = self.gen_units[lang]
        for _ in range(nb - topic_n):
            h, r = divmod(h, len(gen))
            if gen[r] not in units:
                units.append(gen[r])
        h, r = divmod(h, len(units))
        units = units[r:] + units[:r]
        rare = self.rare_units[lang]
        h, r = divmod(h, 100)
        h, a = divmod(h, len(rare))
        h, b = divmod(h, len(rare))
        if r < 45:
            units.insert(min(1, len(units)), rare[a])
        if r < 10 and b != a:
            units.append(rare[b])
        h, ref = divmod(h, 1000000)
        h, cust = divmod(h, 10000000)
        h, a = divmod(h, 250000)
        h, b = divmod(h, 250000)
        return units, ref, cust, 500 + min(a, b)

    def _spec_lines(self, i: int, ref: int, cust: int, cents: int):
        kind = KINDS[self.p_kind[i]]
        de = kind["lang"] == "de"
        d = dt.date.fromordinal(self.p_created[i])
        corr = self.p_corr_info.get(self.p_corr[i])
        head = []
        if corr:
            head.append(corr[0])
            head.append(corr[1])
        prefix = kind["key"][:2].upper()
        if de:
            head.append("%s Nr. %s-%d-%06d" % (kind["headline"], prefix, d.year, ref))
            head.append("Datum: %s" % d.strftime("%d.%m.%Y"))
            head.append("Kundennummer: %07d" % cust)
        else:
            head.append("%s No. %s-%d-%06d" % (kind["headline"], prefix, d.year, ref))
            head.append("Date: %d %s %d" % (d.day, MONTHS["en"][d.month - 1], d.year))
            head.append("Customer number: %07d" % cust)
        foot = []
        if kind["money"]:
            eur, ct = divmod(cents, 100)
            foot.append(("Gesamtbetrag: %d,%02d EUR" if de else "Total amount: %d.%02d EUR")
                        % (eur, ct))
            if corr:
                foot.append("IBAN: %s  BIC: %s" % (corr[2], corr[3]))
        foot.append(("Seite 1 von %d" if de else "Page 1 of %d") % self.p_pages[i])
        return head, foot

    def _content_parts(self, i: int):
        units, ref, cust, cents = self._plan(i)
        head, foot = self._spec_lines(i, ref, cust, cents)
        text = self.units
        paras = [text[u] for u in units]
        needles = self.needle_lines.get(i)
        needle_text = []
        if needles:
            for pos, line in needles:
                at = 0 if pos == "start" else (len(paras) // 2 if pos == "middle" else len(paras))
                paras.insert(at, line)
                needle_text.append(line)
        return head, paras, foot, units, needle_text

    def generated_content(self, i: int) -> str:
        head, paras, foot, _, _ = self._content_parts(i)
        return "\n".join(head) + "\n\n" + "\n\n".join(paras) + "\n\n" + "\n".join(foot)

    def content(self, i: int) -> str:
        override = self.content_override.get(i)
        return override if override is not None else self.generated_content(i)

    def _build_index(self):
        """Background pass: per-unit document bitsets, the doc-specific text blob
        and content lengths, so content search never has to generate all texts."""
        t0 = time.perf_counter()
        try:
            n = self.n
            nbytes = n // 8 + 1
            bits = [bytearray(nbytes) for _ in self.units]
            spec: list[str] = []
            chunks: list[str] = []
            offs = array.array("I", [0])
            pos = 0
            clen = self.clen
            unit_len = self.unit_len
            for i in range(1, n + 1):
                head, paras, foot, units, needle_text = self._content_parts(i)
                byte, bit = i >> 3, 1 << (i & 7)
                for u in units:
                    bits[u][byte] |= bit
                s = "\n".join(head + needle_text + foot).lower()
                spec.append(s)
                pos += len(s) + 1
                offs.append(pos)
                clen[i] = (sum(map(len, head)) + len(head) - 1 + 2 + sum(map(len, paras))
                           + 2 * (len(paras) - 1) + 2 + sum(map(len, foot)) + len(foot) - 1)
                if not i & 0x3FF:
                    time.sleep(0)  # let request threads run
                    if not i & 0x1FFF:
                        chunks.append("\x1e".join(spec) + "\x1e")
                        spec = []
            if spec:
                chunks.append("\x1e".join(spec) + "\x1e")
            for u in range(len(bits)):
                bits[u] = int.from_bytes(bits[u], "little")
            self.unit_bits = bits
            self.blob = "".join(chunks)
            self.blob_offs = offs
            self.blob_n = n
            self.index_seconds = time.perf_counter() - t0
        except Exception:
            traceback.print_exc()
        finally:
            self.index_ready.set()
        if not self.quiet:
            sys.stderr.write("[mock] content index for %d documents ready after %.2fs\n"
                             % (self.n, time.perf_counter() - t0))

    @staticmethod
    def _matcher(q: str, mode: str):
        """Predicate `text -> bool` for a lower-cased needle.

        "sub": substring; "word": whole word; "prefix": start of a word.
        """
        if mode == "sub":
            return lambda text: q in text
        rx = re.compile(r"(?<!\w)" + re.escape(q) + (r"(?!\w)" if mode == "word" else ""))
        return lambda text: q in text and rx.search(text) is not None

    def content_flags(self, q: str, mode: str = "sub") -> bytearray:
        """bytearray[n+1]; 1 where the document's OCR text contains `q`.

        Exact with respect to what `content()` returns, without generating the
        texts: shared paragraphs are matched once and expanded through their
        document bitsets, the per-document lines are scanned in one string.
        """
        self.index_ready.wait()
        n = self.n
        match = self._matcher(q, mode)
        mask = 0
        unit_bits = self.unit_bits
        for u, text in enumerate(self.units_lc):
            if match(text):
                mask |= unit_bits[u]
        if mask:
            flags = bytearray(format(mask, "b")[::-1].encode("ascii").translate(BITS_TO_01))
            if len(flags) < n + 1:
                flags.extend(bytes(n + 1 - len(flags)))
        else:
            flags = bytearray(n + 1)
        blob, offs = self.blob, self.blob_offs
        if "\x1e" not in q:
            find = blob.find
            pos = find(q)
            if mode == "sub":
                while pos != -1:
                    d = bisect.bisect_right(offs, pos)
                    flags[d] = 1
                    pos = find(q, offs[d])
            else:
                end_check = mode == "word"
                size, qlen = len(blob), len(q)
                while pos != -1:
                    before = blob[pos - 1] if pos else " "
                    after = blob[pos + qlen] if pos + qlen < size else " "
                    if (before.isalnum() or before == "_") or (
                            end_check and (after.isalnum() or after == "_")):
                        pos = find(q, pos + 1)
                        continue
                    d = bisect.bisect_right(offs, pos)
                    flags[d] = 1
                    pos = find(q, offs[d])
            for i, text in self.extra_spec.items():
                if match(text):
                    flags[i] = 1
        for i, text in self.content_override.items():
            flags[i] = 1 if match(text.lower()) else 0
        return flags

    # ------------------------------------------------------------------ #
    # files
    # ------------------------------------------------------------------ #

    def _target_size(self, i: int, salt: bytes) -> tuple[int, int]:
        """(target file size, offset into the padding pool) for one generated file."""
        h, frac = divmod(self.H(i, salt), 10000)
        return int(self.pdf_bytes * (0.5 + frac / 10000.0)), h % (len(self.pad_pool) - 1)

    def file_bytes(self, i: int, original: bool) -> tuple[bytes, str]:
        """(bytes, mime) of the original or archive file of document `i`.

        Depends only on (seed, id), never on later edits, like a stored file.
        """
        flags = self.flags[i]
        if original and flags & 2:
            png = build_page_png(i, self.H(i, b"thumb"), scale=2)
            target, off = self._target_size(i, b"pad-png")
            need = target - len(png) - 12
            if need > 0:
                pad = self.pad_pool[off:off + need]
                while len(pad) < need:
                    pad += self.pad_pool[:need - len(pad)]
                png = png[:-12] + png_chunk(b"paDd", pad) + png[-12:]
            return png, "image/png"
        if flags & 1:
            original = True  # no archive version: an archive request serves the original
        de = KINDS[self.p_kind[i]]["lang"] == "de"
        title = self.p_title.get(i) or self.title[i]
        lines = self.generated_content(i).split("\n")
        producer = "mock-paperless scanner" if original else "mock-paperless OCRmyPDF (PDF/A)"
        footer = ("Seite {page} von {pages}" if de else "Page {page} of {pages}") + (
            "" if original else " (archive)")
        ident = hashlib.md5(b"%d:%d:%s" % (i, original, self.key)).hexdigest().encode()
        target, off = self._target_size(i, b"pad-orig" if original else b"pad-arch")
        return build_pdf(title, lines, self.p_pages[i], producer, footer, ident, target,
                         self.pad_pool, off), "application/pdf"

    def set_title(self, i: int, title: str):
        """Retitle a document; the generated files keep printing the original title."""
        self.p_title.setdefault(i, self.title[i])
        self.title[i] = title
        self.title_lc[i] = title.lower()

    def file_info(self, i: int, original: bool) -> tuple[int, str, str]:
        """(size, checksum, mime), cached; computing it generates the file once."""
        key = (i, original or bool(self.flags[i] & 1))
        info = self.finfo.get(key)
        if info is None:
            data, mime = self.file_bytes(i, original)
            info = (len(data), hashlib.new(self.checksum_algo, data).hexdigest(), mime)
            self.finfo[key] = info
        return info

    def thumbnail(self, i: int) -> bytes:
        return build_page_png(i, self.H(i, b"thumb"))

    # ------------------------------------------------------------------ #
    # filtering
    # ------------------------------------------------------------------ #

    def alive_ids(self) -> list[int]:
        ids = self._derived.get("alive")
        if ids is None:
            alive = self.alive
            ids = [i for i in range(1, self.n + 1) if alive[i]]
            self._derived["alive"] = ids
        return ids

    def mask_ids(self, mask: int) -> list[int]:
        ids = self._mask_ids.get(mask)
        if ids is None:
            ids = [b for b in range(mask.bit_length()) if mask >> b & 1]
            if len(self._mask_ids) < 50000:
                self._mask_ids[mask] = ids
        return ids

    def note_unknown(self, where: str, name: str, value: str):
        key = "%s?%s" % (where, name)
        seen = self.stats["unknown_params"]
        if key not in seen:
            sys.stderr.write("[mock] UNKNOWN query parameter %r on %s (value %r) - ignored\n"
                             % (name, where, value[:80]))
        seen[key] = seen.get(key, 0) + 1

    def _text_filter(self, ids, value, title=False, content=False, mode="sub", tokens=False):
        terms = value.lower().split() if tokens else [value.lower().strip()]
        tl = self.title_lc
        for q in terms:
            if not q:
                continue
            flags = self.content_flags(q, mode) if content else None
            if mode == "sub":
                if title and content:
                    ids = [i for i in ids if flags[i] or q in tl[i]]
                elif title:
                    ids = [i for i in ids if q in tl[i]]
                else:
                    ids = [i for i in ids if flags[i]]
            else:
                match = self._matcher(q, mode)
                if title and content:
                    ids = [i for i in ids if flags[i] or match(tl[i])]
                elif title:
                    ids = [i for i in ids if match(tl[i])]
                else:
                    ids = [i for i in ids if flags[i]]
        return ids

    def _range(self, expr: str):
        """Parse a Whoosh/Tantivy-style date range into [lo, hi) timestamps."""
        now = dt.datetime.now(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        e = expr.strip().strip('"').strip().lower()

        def shift_months(d, months):
            idx = d.year * 12 + d.month - 1 + months
            return d.replace(year=idx // 12, month=idx % 12 + 1, day=1)

        def point(p, end=False):
            p = p.strip()
            if p in ("now", "*", ""):
                return now + dt.timedelta(seconds=1) if end else (now if p == "now" else None)
            m = re.fullmatch(r"(?:now)?\s*([+-]?\d+)\s*(second|minute|hour|day|week|month|year)s?", p)
            if m:
                num, unit = int(m.group(1)), m.group(2)
                if unit == "month":
                    return shift_months(now, num).replace(day=min(now.day, 28))
                if unit == "year":
                    return now.replace(year=now.year + num, day=min(now.day, 28))
                return now + dt.timedelta(**{unit + "s": num})
            digits = re.sub(r"\D", "", p)
            if len(digits) >= 8:
                d = dt.datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]), tzinfo=UTC)
                return d + dt.timedelta(days=1) if end else d
            return None

        if e.startswith("["):
            m = re.fullmatch(r"\[\s*(.*?)\s+to\s+(.*?)\s*\]", e)
            if not m:
                return None
            lo, hi = point(m.group(1)), point(m.group(2), end=True)
        elif e == "today":
            lo, hi = day, day + dt.timedelta(days=1)
        elif e == "yesterday":
            lo, hi = day - dt.timedelta(days=1), day
        elif e == "this year":
            lo, hi = day.replace(month=1, day=1), day.replace(year=day.year + 1, month=1, day=1)
        elif e == "this month":
            lo = day.replace(day=1)
            hi = shift_months(lo, 1)
        elif e == "previous week":
            hi = day - dt.timedelta(days=day.weekday())
            lo = hi - dt.timedelta(days=7)
        elif e == "previous month":
            hi = day.replace(day=1)
            lo = shift_months(hi, -1)
        elif e == "previous quarter":
            hi = day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
            lo = shift_months(hi, -3)
        elif e == "previous year":
            hi = day.replace(month=1, day=1)
            lo = hi.replace(year=hi.year - 1)
        else:
            lo = point(e)
            hi = point(e, end=True)
            if lo is None:
                return None
        return (lo.timestamp() if lo else -1e18, hi.timestamp() if hi else 1e18)

    def _query_filter(self, ids, value: str):
        """The `query` parameter: date ranges, field:value terms and whole words."""
        rest = value

        def date_part(m):
            nonlocal ids
            rng = self._range(m.group(2))
            if rng is None:
                self.note_unknown("/api/documents/ query=", m.group(0), value)
                return " "
            lo, hi = rng
            field = m.group(1)
            if field == "created":
                col = self.created
                lo_o = ts_ord(lo) if lo > -1e17 else -BIG
                ids = [i for i in ids if lo_o <= col[i] and ord_ts(col[i]) < hi]
            else:
                col = self.added if field == "added" else self.modified
                ids = [i for i in ids if lo <= col[i] < hi]
            return " "

        rest = re.sub(r'\b(created|added|modified):(\[[^\]]*\]|"[^"]*"|[^\s,]+)', date_part, rest)
        negate = False
        for m in re.finditer(r'(-?)(?:(\w+):)?("[^"]*"|[^\s,]+)', rest):
            neg, field, term = m.group(1) == "-", m.group(2), m.group(3)
            if field is None and term in ("AND", "OR", "NOT"):
                if term == "NOT":
                    negate = True
                elif term == "OR":
                    self.note_unknown("/api/documents/ query=", "OR", value)
                continue
            neg = neg or negate
            negate = False
            phrase = term.startswith('"')
            term = term.strip('"').lower()
            if not term or term == "*":
                continue
            mode = "sub" if phrase else "word"
            if term.endswith("*"):
                term, mode = term.rstrip("*"), "prefix"
            before = ids
            if field in (None, "title", "content"):
                ids = self._text_filter(ids, term, title=field != "content",
                                        content=field != "title", mode=mode)
            elif field in ("correspondent", "type", "document_type", "tag", "path",
                           "storage_path"):
                res, col = {"correspondent": (self.correspondents, self.corr),
                            "type": (self.doctypes, self.dtype),
                            "document_type": (self.doctypes, self.dtype),
                            "path": (self.spaths, self.spath),
                            "storage_path": (self.spaths, self.spath),
                            "tag": (self.tags, None)}[field]
                hit = {k for k, v in res.items() if term in v["name"].lower()}
                if col is None:
                    m_any = sum(1 << t for t in hit)
                    tm = self.tagmask
                    ids = [i for i in ids if tm[i] & m_any]
                else:
                    ids = [i for i in ids if col[i] in hit]
            elif field == "asn" and term.isdigit():
                ids = [i for i in ids if self.asn[i] == int(term)]
            elif field == "id" and term.isdigit():
                ids = [i for i in ids if i == int(term)]
            else:
                self.note_unknown("/api/documents/ query=", "%s:" % field, value)
                continue
            if neg:
                keep = set(ids)
                ids = [i for i in before if i not in keep]
        return ids

    def _cf_filter(self, ids, expr: str):
        try:
            tree = json.loads(expr)
        except ValueError:
            raise ApiError(400, {"custom_field_query": ["Value must be valid JSON."]})
        names = {v["name"]: k for k, v in self.custom_fields.items()}

        def num(v):
            if isinstance(v, bool):
                return float(v)
            if isinstance(v, (int, float)):
                return float(v)
            m = re.search(r"-?\d+(?:\.\d+)?", str(v))
            return float(m.group(0)) if m else None

        def ev(node, vals):
            if not isinstance(node, list):
                return True
            if len(node) == 2 and node[0] in ("AND", "OR"):
                results = [ev(x, vals) for x in node[1]]
                return all(results) if node[0] == "AND" else any(results)
            if len(node) == 2 and node[0] == "NOT":
                return not ev(node[1], vals)
            if len(node) != 3:
                return True
            fid, op, arg = node
            if isinstance(fid, str):
                fid = names.get(fid, int(fid) if fid.isdigit() else -1)
            present = fid in vals
            v = vals.get(fid)
            if op == "exists":
                return present == truthy(arg)
            if op == "isnull":
                return (present and v is None) == truthy(arg)
            if not present or v is None:
                return False
            if op == "exact":
                return v == arg or str(v).lower() == str(arg).lower()
            if op in ("gt", "gte", "lt", "lte"):
                a, b = num(v), num(arg)
                if isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", v):
                    a, b = v, str(arg)[:10]
                if a is None or b is None:
                    return False
                return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
            if op == "range" and isinstance(arg, list) and len(arg) == 2:
                a = num(v)
                return a is not None and num(arg[0]) <= a <= num(arg[1])
            if op == "in":
                return v in arg if isinstance(arg, list) else v == arg
            if op == "contains":
                want = arg if isinstance(arg, list) else [arg]
                return all(x in v for x in want) if isinstance(v, list) else str(arg) in str(v)
            if op in ("icontains", "istartswith", "iendswith"):
                a, b = str(v).lower(), str(arg).lower()
                return b in a if op == "icontains" else (
                    a.startswith(b) if op == "istartswith" else a.endswith(b))
            return False

        get_cf = self.get_cf
        return [i for i in ids if ev(tree, {e["field"]: e["value"] for e in get_cf(i)})]

    def _apply_filter(self, ids, name: str, v: str):
        """Apply one query parameter. Returns None when the parameter is unknown."""
        fk = {"correspondent": (self.corr, self.correspondents),
              "document_type": (self.dtype, self.doctypes),
              "storage_path": (self.spath, self.spaths), "owner": (self.owner, self.users)}
        base, _, op = name.partition("__")
        if base in fk:
            col, res = fk[base]
            if op in ("id", ""):
                x = int(v)
                return [i for i in ids if col[i] == x]
            if op == "id__in":
                s = set(csv_ints(v))
                return [i for i in ids if col[i] in s]
            if op == "id__none":
                s = set(csv_ints(v))
                return [i for i in ids if col[i] not in s]
            if op == "isnull":
                want = truthy(v)
                return [i for i in ids if (col[i] == 0) == want]
            m = re.fullmatch(r"name__(icontains|istartswith|iendswith|iexact)", op)
            if m and base != "owner":
                q = v.lower()
                test = {"icontains": lambda s: q in s, "istartswith": lambda s: s.startswith(q),
                        "iendswith": lambda s: s.endswith(q), "iexact": lambda s: s == q}[m.group(1)]
                hit = {k for k, o in res.items() if test(o["name"].lower())}
                return [i for i in ids if col[i] in hit]
            return None
        tm = self.tagmask
        if name == "tags__id__all":
            m = sum(1 << t for t in set(csv_ints(v)))
            return [i for i in ids if tm[i] & m == m]
        if name in ("tags__id__in", "tags__id"):
            m = sum(1 << t for t in set(csv_ints(v)))
            return [i for i in ids if tm[i] & m]
        if name == "tags__id__none":
            m = sum(1 << t for t in set(csv_ints(v)))
            return [i for i in ids if not tm[i] & m]
        if name == "is_tagged":
            want = truthy(v)
            return [i for i in ids if (tm[i] != 0) == want]
        if name == "is_in_inbox":
            want, m = truthy(v), self.inbox_mask
            return [i for i in ids if (tm[i] & m != 0) == want]
        if name in ("tags__name__icontains", "tags__name__iexact"):
            q = v.lower()
            m = sum(1 << k for k, t in self.tags.items()
                    if (q in t["name"].lower() if name.endswith("icontains")
                        else q == t["name"].lower()))
            return [i for i in ids if tm[i] & m]
        if base == "archive_serial_number":
            col = self.asn
            if op == "isnull":
                want = truthy(v)
                return [i for i in ids if (col[i] == 0) == want]
            x = int(v)
            if op == "":
                return [i for i in ids if col[i] == x and col[i]]
            if op == "gt":
                return [i for i in ids if col[i] > x]
            if op == "gte":
                return [i for i in ids if col[i] >= x and col[i]]
            if op == "lt":
                return [i for i in ids if 0 < col[i] < x]
            if op == "lte":
                return [i for i in ids if 0 < col[i] <= x]
            return None
        if base in ("created", "added", "modified"):
            if base == "created":
                col = self.created
                if op in ("year", "month", "day"):
                    x = int(v)
                    f = dt.date.fromordinal
                    return [i for i in ids if getattr(f(col[i]), op) == x]
                cmp_op = op[6:] if op.startswith("date__") else op
                if cmp_op not in ("gt", "gte", "lt", "lte"):
                    return None
                x = parse_date_ord(v)
                return {"gt": lambda: [i for i in ids if col[i] > x],
                        "gte": lambda: [i for i in ids if col[i] >= x],
                        "lt": lambda: [i for i in ids if col[i] < x],
                        "lte": lambda: [i for i in ids if col[i] <= x]}[cmp_op]()
            col = self.added if base == "added" else self.modified
            if op in ("year", "month", "day"):
                x = int(v)
                f = dt.date.fromordinal
                return [i for i in ids if getattr(f(ts_ord(col[i])), op) == x]
            if op.startswith("date__"):
                cmp_op = op[6:]
                if cmp_op not in ("gt", "gte", "lt", "lte"):
                    return None
                day = ord_ts(parse_date_ord(v))
                nxt = day + 86400
                return {"gt": lambda: [i for i in ids if col[i] >= nxt],
                        "gte": lambda: [i for i in ids if col[i] >= day],
                        "lt": lambda: [i for i in ids if col[i] < day],
                        "lte": lambda: [i for i in ids if col[i] < nxt]}[cmp_op]()
            if op in ("gt", "gte", "lt", "lte"):
                x = parse_ts(v)
                return {"gt": lambda: [i for i in ids if col[i] > x],
                        "gte": lambda: [i for i in ids if col[i] >= x],
                        "lt": lambda: [i for i in ids if col[i] < x],
                        "lte": lambda: [i for i in ids if col[i] <= x]}[op]()
            return None
        if name in ("id__in", "id"):
            s = set(csv_ints(v))
            return [i for i in ids if i in s]
        if base == "title" and op in ("icontains", "istartswith", "iendswith", "iexact"):
            q, tl = v.lower(), self.title_lc
            if op == "icontains":
                return [i for i in ids if q in tl[i]]
            if op == "istartswith":
                return [i for i in ids if tl[i].startswith(q)]
            if op == "iendswith":
                return [i for i in ids if tl[i].endswith(q)]
            return [i for i in ids if tl[i] == q]
        if name == "content__icontains":
            return self._text_filter(ids, v, content=True)
        if name == "title_content":
            return self._text_filter(ids, v, title=True, content=True)
        if name == "text" and self.v3:
            return self._text_filter(ids, v, title=True, content=True, tokens=True)
        if name == "title_search" and self.v3:
            return self._text_filter(ids, v, title=True, tokens=True)
        if name == "query":
            return self._query_filter(ids, v)
        if name == "more_like_id":
            x = int(v)
            if not (0 < x <= self.n):
                return []
            kind, corr, pk = self.p_kind[x], self.p_corr[x], self.p_kind
            pc = self.p_corr
            return [i for i in ids if i != x and pk[i] == kind and (pc[i] == corr or i % 5 == 0)]
        if name == "has_custom_fields":
            want, get_cf = truthy(v), self.get_cf
            return [i for i in ids if bool(get_cf(i)) == want]
        if name in ("custom_fields__id__all", "custom_fields__id__in", "custom_fields__id__none"):
            s, get_cf = set(csv_ints(v)), self.get_cf
            have = lambda i: {e["field"] for e in get_cf(i)}  # noqa: E731
            if name.endswith("all"):
                return [i for i in ids if s <= have(i)]
            if name.endswith("in"):
                return [i for i in ids if s & have(i)]
            return [i for i in ids if not s & have(i)]
        if name == "custom_fields__icontains":
            q, get_cf = v.lower(), self.get_cf
            fname = {k: f["name"].lower() for k, f in self.custom_fields.items()}
            return [i for i in self.cf_docs(ids) if any(
                q in str(e["value"]).lower() or q in fname.get(e["field"], "")
                for e in get_cf(i))]
        if name == "custom_field_query":
            return self._cf_filter(ids, v)
        if name == "mime_type":
            fl = self.flags
            if v == "application/pdf":
                return [i for i in ids if not fl[i] & 2]
            return [i for i in ids if fl[i] & 2] if v == "image/png" else []
        if name == "has_duplicates" and self.sv >= (3, 2, 0):
            return [] if truthy(v) else ids
        if name == "shared_by__id":
            x, ow, get_perms = int(v), self.owner, self.get_perms
            return [i for i in ids if ow[i] == x and get_perms(i)]
        if name in ("original_filename__icontains", "checksum__icontains"):
            return ids
        return None

    NON_FILTER = {"page", "page_size", "ordering", "fields", "full_perms", "truncate_content",
                  "format"}

    def sort_key(self, field: str):
        if field in ("created", "created_date"):
            return self.created.__getitem__
        if field == "added":
            return self.added.__getitem__
        if field == "modified":
            return self.modified.__getitem__
        if field == "title":
            return self.title_lc.__getitem__
        if field == "page_count":
            return self.pages.__getitem__
        key = ("sort", field)
        col = self._derived.get(key)
        if col is not None:
            return col.__getitem__
        n = self.n
        null = self.null_key  # where NULLs sort: last ascending (PostgreSQL) or first (SQLite)
        if field == "archive_serial_number":
            col = [a or null for a in self.asn]
        elif field in ("correspondent__name", "document_type__name", "storage_path__name"):
            src, res = {"correspondent__name": (self.corr, self.correspondents),
                        "document_type__name": (self.dtype, self.doctypes),
                        "storage_path__name": (self.spath, self.spaths)}[field]
            rank = {k: r for r, (k, _) in enumerate(
                sorted(res.items(), key=lambda kv: kv[1]["name"].lower()))}
            col = [rank.get(c, null) for c in src]
        elif field == "owner":
            col = [o or null for o in self.owner]
        elif field in ("notes", "num_notes"):
            col = [0] * (n + 1)
            fl, ov = self.flags, self.notes
            for i in range(1, n + 1):
                if fl[i] & 4 or i in ov:
                    col[i] = len(self.get_notes(i))
        elif re.fullmatch(r"custom_field_\d+", field):
            fid = int(field.rsplit("_", 1)[1])
            col = [(2 if null > 0 else -1, 0, "")] * (n + 1)
            for i in self.cf_docs(range(1, n + 1)):
                for e in self.get_cf(i):
                    if e["field"] == fid and e["value"] is not None:
                        val = e["value"]
                        m = re.search(r"-?\d+(?:\.\d+)?", val) if isinstance(val, str) else None
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            col[i] = (0, float(val), "")
                        elif m and fid == 1:
                            col[i] = (0, float(m.group(0)), "")
                        else:
                            col[i] = (1, 0, str(val).lower())
        elif field in ("score", "random"):
            return None
        else:
            return False
        self._derived[key] = col
        return col.__getitem__

    def query(self, q: list[tuple[str, str]]) -> dict:
        """Filter + sort; returns a cached entry {"ids": [...]} for the query."""
        filters = [(k, v) for k, v in q if k not in self.NON_FILTER]
        ordering = next((v for k, v in q if k == "ordering"), "") or "-created"
        key = (tuple(sorted(filters)), ordering)
        entry = self._qcache.get(key)
        if entry is not None:
            self._qcache.move_to_end(key)
            return entry
        if self.v3:
            excl = [k for k, _ in filters if k in ("text", "title_search", "query", "more_like_id")]
            if len(excl) > 1:
                raise ApiError(400, {"detail": "Specify only one of text, title_search, query, "
                                               "or more_like_id."})
        ids = self.alive_ids()
        # cheap, selective filters first
        filters.sort(key=lambda kv: (kv[0] not in ("id__in", "id"), kv[0] in (
            "query", "text", "title_content", "content__icontains", "custom_field_query")))
        for name, value in filters:
            try:
                res = self._apply_filter(ids, name, value)
            except ApiError:
                raise
            except (ValueError, KeyError, OverflowError):
                raise ApiError(400, {name: ["Enter a valid value."]})
            if res is None:
                self.note_unknown("/api/documents/", name, value)
            else:
                ids = res
        if ids is self._derived.get("alive"):
            ids = list(ids)
        for term in reversed([t.strip() for t in ordering.split(",") if t.strip()]):
            desc = term.startswith("-")
            field = term.lstrip("-")
            if field in ("id", "pk"):
                ids.sort(reverse=desc)
                continue
            keyfn = self.sort_key(field)
            if keyfn is False:
                self.note_unknown("/api/documents/", "ordering=" + field, ordering)
            elif keyfn is not None:
                ids.sort(key=keyfn, reverse=desc)
        entry = {"ids": ids, "all": None}
        self._qcache[key] = entry
        while len(self._qcache) > 8:
            self._qcache.popitem(last=False)
        return entry

    # ------------------------------------------------------------------ #
    # serialisation
    # ------------------------------------------------------------------ #

    def note_json(self, note: dict) -> dict:
        user = self.users.get(note["user"]) if note["user"] else None
        return {"id": note["id"], "note": note["note"], "created": iso(note["created"]),
                "user": {"id": user["id"], "username": user["username"],
                         "first_name": user["first_name"], "last_name": user["last_name"]}
                if user else None}

    def original_name(self, i: int) -> str:
        name = self.filename_override.get(i)
        if name:
            return name
        return "IMG_%05d.png" % i if self.flags[i] & 2 else "scan_%06d.pdf" % i

    def archive_name(self, i: int, original: bool = False) -> str:
        corr = self.correspondents.get(self.corr[i])
        name = "%s %s%s" % (ord_iso(self.created[i]), corr["name"] + " " if corr else "",
                            self.title[i])
        ext = ".png" if original and self.flags[i] & 2 else ".pdf"
        return re.sub(r'[/\\:*?"<>|]', "-", name)[:120] + ext

    def doc_json(self, i: int, full_perms=False, truncate=False, fields=None, deleted=False):
        want_content = fields is None or "content" in fields
        want_versions = self.v3 and (fields is None or "versions" in fields)
        content = None
        if want_content:
            content = self.content(i)
            if truncate:
                content = content[:TRUNCATED_CONTENT_LENGTH]
        created = ord_iso(self.created[i])
        notes = self.get_notes(i) if self.flags[i] & 4 or i in self.notes else None
        perms = self.get_perms(i)
        d = {
            "id": i,
            "correspondent": self.corr[i] or None,
            "document_type": self.dtype[i] or None,
            "storage_path": self.spath[i] or None,
            "title": self.title[i],
            "content": content,
            "tags": self.mask_ids(self.tagmask[i]),
            "created": created,
            "created_date": created,
            "modified": iso(self.modified[i]),
            "added": iso(self.added[i]),
            "deleted_at": iso(self.trashed[i]) if deleted and i in self.trashed else None,
            "archive_serial_number": self.asn[i] or None,
            "original_file_name": self.original_name(i),
            "archived_file_name": None if self.flags[i] & 1 else self.archive_name(i),
            "owner": self.owner[i] or None,
        }
        if full_perms:
            d["permissions"] = perms or self.NO_PERMS
        else:
            d["user_can_change"] = True
            d["is_shared_by_requester"] = bool(perms) and self.owner[i] == self.me
        d["notes"] = [self.note_json(x) for x in notes] if notes else []
        d["custom_fields"] = self.get_cf(i) if self.flags[i] & 8 or i in self.cfields else []
        d["page_count"] = self.pages[i]
        d["mime_type"] = "image/png" if self.flags[i] & 2 else "application/pdf"
        if self.v3:
            d["root_document"] = None
            d["duplicate_documents"] = []
            if want_versions:
                d["versions"] = [{"id": i, "added": d["added"], "version_label": None,
                                  "checksum": self.file_info(i, True)[1], "is_root": True}]
        if fields is not None:
            d = {k: v for k, v in d.items() if k in fields}
        return d

    # ------------------------------------------------------------------ #
    # document mutations
    # ------------------------------------------------------------------ #

    def require_doc(self, i: int):
        if not (0 < i <= self.n) or not self.alive[i]:
            raise not_found()

    def set_asn(self, i: int, value):
        value = int(value) if value not in (None, "") else 0
        if value and value != self.asn[i] and value in self.asn:
            raise ApiError(400, {"archive_serial_number": [
                "Document with this Archive serial number already exists."]})
        self.asn[i] = value
        self.next_asn = max(self.next_asn, value + 1)

    def update_doc(self, i: int, data: dict):
        def fk(name, res, col):
            if name in data:
                v = data[name]
                if v is not None and int(v) not in res:
                    raise ApiError(400, {name: ['Invalid pk "%s" - object does not exist.' % v]})
                col[i] = int(v) if v is not None else 0

        if "title" in data:
            title = str(data["title"] or "")
            if not title.strip():
                raise ApiError(400, {"title": ["This field may not be blank."]})
            self.set_title(i, title[:128])
        fk("correspondent", self.correspondents, self.corr)
        fk("document_type", self.doctypes, self.dtype)
        fk("storage_path", self.spaths, self.spath)
        fk("owner", self.users, self.owner)
        if "tags" in data:
            mask = 0
            for t in data["tags"] or []:
                if int(t) not in self.tags:
                    raise ApiError(400, {"tags": ['Invalid pk "%s" - object does not exist.' % t]})
                mask |= self.tag_closure[int(t)]
            self.tagmask[i] = self._mask_intern.setdefault(mask, mask)
        if "archive_serial_number" in data:
            self.set_asn(i, data["archive_serial_number"])
        if data.get("created") or data.get("created_date"):
            try:
                self.created[i] = parse_date_ord(str(data.get("created") or data["created_date"]))
            except ValueError:
                raise ApiError(400, {"created": ["Date has wrong format."]})
        if "custom_fields" in data:
            entries = []
            raw = data["custom_fields"] or []
            if isinstance(raw, dict):
                raw = [{"field": int(k), "value": v} for k, v in raw.items()]
            for e in raw:
                if int(e.get("field", 0)) not in self.custom_fields:
                    raise ApiError(400, {"custom_fields": [{"field": ["Invalid pk."]}]})
                entries.append({"value": e.get("value"), "field": int(e["field"])})
            self.cfields[i] = entries
        if isinstance(data.get("set_permissions"), dict):
            p = data["set_permissions"]
            perms = {k: {"users": list((p.get(k) or {}).get("users") or []),
                         "groups": list((p.get(k) or {}).get("groups") or [])}
                     for k in ("view", "change")}
            self.perms[i] = perms if any(perms[k][x] for k in perms for x in perms[k]) else None
        if data.get("remove_inbox_tags"):
            self.tagmask[i] &= ~self.inbox_mask
        self.modified[i] = self.now()
        self.bump()

    def trash_docs(self, ids):
        now = self.now()
        for i in ids:
            if 0 < i <= self.n and self.alive[i]:
                self.alive[i] = 0
                self.trashed[i] = now
        self.bump()

    def add_documents(self, count: int, **override) -> list[int]:
        """Append freshly 'consumed' documents (ids continue after the highest id)."""
        self.index_ready.wait()
        out = []
        for _ in range(count):
            i = self.n + 1
            now = self.now()
            self._append_document(i, now=now)
            head, paras, foot, units, needle_text = self._content_parts(i)
            for u in units:
                self.unit_bits[u] |= 1 << i
            self.extra_spec[i] = "\n".join(head + needle_text + foot).lower()
            self.clen[i] = len(self.content(i))
            out.append(i)
        self.bump()
        return out

    # ------------------------------------------------------------------ #
    # counters derived from the documents
    # ------------------------------------------------------------------ #

    def counts(self, kind: str):
        """document_count per element id (Counter), cached until the next mutation."""
        key = ("counts", kind)
        c = self._derived.get(key)
        if c is not None:
            return c
        ids = self.alive_ids()
        if kind == "tags":
            c = Counter()
            for mask, num in Counter(map(self.tagmask.__getitem__, ids)).items():
                for t in self.mask_ids(mask):
                    c[t] += num
        elif kind == "last_correspondence":
            c = {}
            corr, created = self.corr, self.created
            for i in ids:
                cc = corr[i]
                if cc and created[i] > c.get(cc, 0):
                    c[cc] = created[i]
        elif kind == "custom_fields":
            c = Counter(e["field"] for i in self.cf_docs(ids) for e in self.get_cf(i))
        else:
            col = {"correspondents": self.corr, "document_types": self.dtype,
                   "storage_paths": self.spath}.get(kind)
            c = Counter(map(col.__getitem__, ids)) if col is not None else Counter()
        self._derived[key] = c
        return c

    def statistics(self) -> dict:
        self.index_ready.wait()
        ids = self.alive_ids()
        tm, inbox = self.tagmask, self.inbox_mask
        png = sum(1 for i in ids if self.flags[i] & 2)
        chars = sum(map(self.clen.__getitem__, ids))
        for i, text in self.content_override.items():
            if self.alive[i]:
                chars += len(text) - self.clen[i]
        inbox_tags = [t for t, tag in self.tags.items() if tag.get("is_inbox_tag")]
        types = [{"mime_type": "application/pdf", "mime_type_count": len(ids) - png}]
        if png:
            types.append({"mime_type": "image/png", "mime_type_count": png})
        return {
            "documents_total": len(ids),
            "documents_inbox": sum(1 for i in ids if tm[i] & inbox) if inbox_tags else None,
            "inbox_tag": inbox_tags[0] if inbox_tags else None,
            "inbox_tags": inbox_tags or None,
            "document_file_type_counts": types,
            "character_count": chars,
            "tag_count": len(self.tags),
            "correspondent_count": len(self.correspondents),
            "document_type_count": len(self.doctypes),
            "storage_path_count": len(self.spaths),
            "current_asn": max(self.asn),
        }

    # ------------------------------------------------------------------ #
    # element serialisation / CRUD
    # ------------------------------------------------------------------ #

    def element_json(self, kind: str, obj: dict, full_perms=False, nested=True) -> dict:
        out = {k: v for k, v in obj.items() if not k.startswith("_")}
        if kind in ("tags", "correspondents", "document_types", "storage_paths", "custom_fields"):
            out["document_count"] = self.counts(kind).get(obj["id"], 0)
        if kind == "correspondents":
            last = self.counts("last_correspondence").get(obj["id"])
            out["last_correspondence"] = ord_iso(last) if last else None
        if kind in ("tags", "correspondents", "document_types", "storage_paths", "saved_views"):
            if full_perms:
                out["permissions"] = obj.get("_perms") or {
                    "view": {"users": [], "groups": []}, "change": {"users": [], "groups": []}}
            else:
                out["user_can_change"] = True
        if kind == "tags":
            if self.nested_tags:
                out["children"] = [
                    self.element_json("tags", t, full_perms)
                    for t in sorted(self.tags.values(), key=lambda t: t["name"].lower())
                    if t.get("parent") == obj["id"]] if nested else []
            else:
                out.pop("parent", None)
        if kind == "saved_views" and self.v3:
            out.pop("show_on_dashboard", None)
            out.pop("show_in_sidebar", None)
        return out

    ELEMENT_DEFAULTS = {
        "tags": {"name": "", "slug": "", "color": "#a6cee3", "text_color": "#000000",
                 "match": "", "matching_algorithm": 1, "is_insensitive": True,
                 "is_inbox_tag": False, "owner": None, "parent": None},
        "correspondents": {"slug": "", "name": "", "match": "", "matching_algorithm": 1,
                           "is_insensitive": True, "owner": None},
        "document_types": {"slug": "", "name": "", "match": "", "matching_algorithm": 1,
                           "is_insensitive": True, "owner": None},
        "storage_paths": {"slug": "", "name": "", "path": "", "match": "",
                          "matching_algorithm": 1, "is_insensitive": True, "owner": None},
        "saved_views": {"name": "", "show_on_dashboard": False, "show_in_sidebar": False,
                        "sort_field": None, "sort_reverse": False, "filter_rules": [],
                        "page_size": None, "display_mode": None, "display_fields": None,
                        "owner": None},
        "custom_fields": {"name": "", "data_type": "string",
                          "extra_data": {"select_options": [], "default_currency": None}},
    }

    def element_write(self, kind: str, obj: dict | None, data: dict) -> dict:
        items = self.resources[kind]
        creating = obj is None
        if creating:
            obj = json.loads(json.dumps(self.ELEMENT_DEFAULTS[kind]))
            if "owner" in obj:
                obj["owner"] = self.me
        name = data.get("name", obj.get("name"))
        if not isinstance(name, str) or not name.strip():
            raise ApiError(400, {"name": ["This field may not be blank."]})
        for other in items.values():
            if other is not obj and other.get("name") == name:
                raise ApiError(400, {"error": ["Object violates owner / name unique constraint"]})
        for key in self.ELEMENT_DEFAULTS[kind]:
            if key in data:
                obj[key] = data[key]
        if isinstance(data.get("set_permissions"), dict):
            obj["_perms"] = data["set_permissions"]
        if "slug" in obj and (creating or "name" in data) and not data.get("slug"):
            obj["slug"] = slugify(obj["name"])
        if kind == "tags":
            parent = obj.get("parent")
            if parent is not None and (parent not in self.tags or parent == obj.get("id")):
                raise ApiError(400, {"parent": ["Invalid parent tag."]})
            color = str(obj.get("color") or "#a6cee3")
            if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                lum = sum(int(color[j:j + 2], 16) * w for j, w in ((1, .299), (3, .587), (5, .114)))
                obj["text_color"] = "#000000" if lum > 150 else "#ffffff"
        if creating:
            obj["id"] = self.next_ids[kind]
            self.next_ids[kind] += 1
            items[obj["id"]] = obj
            if kind == "saved_views":
                vis = self.ui_settings["saved_views"]
                if obj.get("show_on_dashboard"):
                    vis["dashboard_views_visible_ids"].append(obj["id"])
                if obj.get("show_in_sidebar"):
                    vis["sidebar_views_visible_ids"].append(obj["id"])
        if kind == "tags":
            self._tag_closure()
        self.bump()
        return obj

    def element_delete(self, kind: str, eid: int):
        items = self.resources[kind]
        if eid not in items:
            raise not_found()
        del items[eid]
        col = {"correspondents": self.corr, "document_types": self.dtype,
               "storage_paths": self.spath}.get(kind)
        if col is not None:
            for i in range(1, self.n + 1):
                if col[i] == eid:
                    col[i] = 0
        elif kind == "tags":
            clear = ~(1 << eid)
            tm = self.tagmask
            for i in range(1, self.n + 1):
                if tm[i] >> eid & 1:
                    tm[i] &= clear
            for t in self.tags.values():
                if t.get("parent") == eid:
                    t["parent"] = None
            self._tag_closure()
        elif kind == "custom_fields":
            for i in list(self.cfields):
                self.cfields[i] = [e for e in self.cfields[i] if e["field"] != eid]
        elif kind == "saved_views":
            vis = self.ui_settings["saved_views"]
            for k in ("dashboard_views_visible_ids", "sidebar_views_visible_ids"):
                vis[k] = [x for x in vis[k] if x != eid]
        self.bump()


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

EMPTY_COLLECTIONS = {"mail_accounts", "mail_rules", "workflows", "workflow_triggers",
                     "workflow_actions", "share_link_bundles", "processed_mail", "logs"}
ELEMENT_KINDS = ("correspondents", "tags", "document_types", "storage_paths", "saved_views",
                 "custom_fields", "users", "groups")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-paperless"
    mock: Mock = None  # type: ignore[assignment]

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):  # silence the default access log
        pass

    def do_GET(self):
        self._dispatch()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    # ---- plumbing ------------------------------------------------------ #

    def _dispatch(self):
        t0 = time.perf_counter()
        self._status = 0
        self._sent = 0
        self._class = "other"
        self._defer = False
        self._pending = None
        mock = self.mock
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            split = urlsplit(self.path)
            self.url_path = split.path
            self.raw_query = split.query
            self.q = parse_qsl(split.query, keep_blank_values=True)
            self.qd = dict(self.q)
            self.body = body
            self._route()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            try:
                self._send(500, J({"detail": "mock server error, see stderr"}))
            except OSError:
                self.close_connection = True
        ms = (time.perf_counter() - t0) * 1000
        st = mock.stats
        st["requests"] += 1
        st["by_class"][self._class] += 1
        st["by_status"][str(self._status or "hang")] += 1
        st["bytes_served"] += self._sent
        if not mock.quiet:
            path = self.path if len(self.path) <= 240 else self.path[:240] + "...(%d chars)" % len(
                self.path)
            sys.stderr.write("%s %s %s %dB %.1fms\n" % (
                self.command, path, self._status or "HANG", self._sent, ms))

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              headers: list | None = None, compress: bool = True):
        if self._defer:
            self._pending = (status, body, ctype, headers, compress)
            return
        mock = self.mock
        head = ["HTTP/1.1 %d %s" % (status, http.HTTPStatus(status).phrase),
                "Server: mock-paperless", "Date: " + self.date_time_string()]
        if getattr(self, "_versioned", False):
            head.append("X-Api-Version: %d" % mock.api_version)
            head.append("X-Version: %s" % mock.server_version)
        if headers:
            head.extend("%s: %s" % kv for kv in headers)
        if status not in (204, 304):
            if (compress and mock.args.gzip and len(body) > 1024
                    and "gzip" in self.headers.get("Accept-Encoding", "")):
                co = zlib.compressobj(mock.args.gzip_level, zlib.DEFLATED, 31)
                body = co.compress(body) + co.flush()
                head.append("Content-Encoding: gzip")
                head.append("Vary: Accept-Encoding")
            head.append("Content-Type: " + ctype)
            head.append("Content-Length: %d" % len(body))
        else:
            body = b""
        if self.close_connection:
            head.append("Connection: close")
        data = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1")
        if self.command != "HEAD":
            data += body
        self._status = status
        self._sent = len(data)
        self.wfile.write(data)

    def _json(self, status: int, payload, headers=None):
        self._send(status, payload if isinstance(payload, (bytes, bytearray)) else J(payload),
                   headers=headers)

    def json_body(self) -> dict:
        if not self.body:
            return {}
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype or self.body[:1] in (b"{", b"["):
            try:
                data = json.loads(self.body.decode("utf-8"))
            except ValueError:
                raise ApiError(400, {"detail": "JSON parse error"})
            return data if isinstance(data, dict) else {"_": data}
        if "x-www-form-urlencoded" in ctype:
            return dict(parse_qsl(self.body.decode("utf-8", "replace")))
        return {}

    def multipart(self):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        fields: dict[str, list[str]] = {}
        files: dict[str, tuple[str, bytes]] = {}
        if not m:
            return fields, files
        boundary = b"--" + m.group(1).encode()
        for part in self.body.split(boundary)[1:]:
            if part.startswith(b"--"):
                break
            head, _, data = part.partition(b"\r\n\r\n")
            if data.endswith(b"\r\n"):
                data = data[:-2]
            disp = head.decode("utf-8", "replace")
            name = re.search(r'name="([^"]*)"', disp)
            filename = re.search(r'filename="([^"]*)"', disp)
            if not name:
                continue
            if filename:
                files[name.group(1)] = (filename.group(1), data)
            else:
                fields.setdefault(name.group(1), []).append(data.decode("utf-8", "replace"))
        return fields, files

    def base_url(self) -> str:
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return "%s://%s" % (proto, self.headers.get("Host") or "%s:%d" % self.server.server_address)

    def paged(self, total: int, results: bytes, page: int, page_size: int, all_json=None) -> bytes:
        def link(p):
            pairs = [(k, v) for k, v in self.q if k != "page"]
            if p != 1:
                pairs.insert(0, ("page", str(p)))
            url = self.base_url() + self.url_path + ("?" + urlencode(pairs) if pairs else "")
            return json.dumps(url).encode()

        nxt = link(page + 1) if page * page_size < total else b"null"
        prev = link(page - 1) if page > 1 else b"null"
        parts = [b'{"count":%d,"next":' % total, nxt, b',"previous":', prev]
        if all_json is not None:
            parts += [b',"all":', all_json]
        parts += [b',"results":', results, b"}"]
        return b"".join(parts)

    def page_args(self, default_size=25):
        try:
            page = int(self.qd.get("page") or 1)
            size = int(self.qd.get("page_size") or default_size)
        except ValueError:
            raise ApiError(404, {"detail": "Invalid page."})
        return page, max(1, min(size, 100000))

    # ---- routing ------------------------------------------------------- #

    def _route(self):
        mock = self.mock
        path = self.url_path
        parts = [p for p in path.split("/") if p]
        self._versioned = False
        if parts[:1] == ["__mock"]:
            self._class = "control"
            return self._control(parts[1:])
        if parts[:1] == ["share"] and len(parts) == 2:
            self._class = "share"
            return self._share(parts[1])
        if parts[:1] != ["api"]:
            self._class = "non-api"
            if not parts:
                return self._send(200, b"<html><body><h1>mock-paperless</h1></body></html>",
                                  ctype="text/html")
            return self._json(404, {"detail": "Not found."})
        parts = parts[1:]
        self._class = self._classify(parts)

        fail = mock.fail
        mode = fail["mode"]
        if mode != "off" and (not fail["path"] or fail["path"] in self.path):
            if fail["remaining"] is not None:
                fail["remaining"] -= 1
                if fail["remaining"] <= 0:
                    fail["mode"], fail["remaining"] = "off", None
            if mode == "hang":
                return self._hang()
            if mock.latency:
                time.sleep(mock.latency)
            return self._send(int(mode), J({"detail": "mock failure mode %s" % mode}),
                              headers=[("Retry-After", "1")] if mode == "503" else None)

        if mock.latency:
            time.sleep(mock.latency)

        m = re.search(r"version=(\d+)", self.headers.get("Accept", ""))
        if m and not (1 <= int(m.group(1)) <= mock.api_version):
            return self._json(406, {"detail": 'Invalid version in "Accept" header.'})

        if parts == ["token"]:
            return self._token()
        if parts[:2] == ["auth", "headless"]:
            # django-allauth headless API; the login screen asks it for OIDC providers.
            if parts[2:] == ["app", "v1", "config"] or parts[2:] == ["browser", "v1", "config"]:
                return self._json(200, {"status": 200, "data": {
                    "account": {"authentication_method": "username", "is_open_for_signup": False,
                                "login_by_code_enabled": False},
                    "socialaccount": {"providers": []},
                    "mfa": {"supported_types": ["totp", "recovery_codes"]},
                    "usersessions": {"track_activity": False}}})
            return self._json(404, {"status": 404})

        if mock.token:
            auth = self.headers.get("Authorization", "")
            if not auth:
                return self._json(401, {"detail": "Authentication credentials were not provided."},
                                  headers=[("WWW-Authenticate", "Token")])
            if auth.split() != ["Token", mock.token]:
                return self._json(401, {"detail": "Invalid token."},
                                  headers=[("WWW-Authenticate", "Token")])
        self._versioned = True
        # File requests generate their bytes without the state lock. Everything
        # else runs under it, and its response is written after releasing it, so
        # a slow client (or gzip) never blocks other requests.
        is_file = (len(parts) == 3 and parts[0] == "documents"
                   and parts[2] in ("download", "preview", "thumb"))
        self._defer = not is_file
        try:
            if is_file:
                self._api(parts)
            else:
                with mock.lock:
                    self._api(parts)
        except ApiError as e:
            self._json(e.status, e.payload)
        finally:
            self._defer = False
        if self._pending is not None:
            pending, self._pending = self._pending, None
            self._send(*pending)

    def _classify(self, parts) -> str:
        if not parts:
            return "root"
        if parts[0] != "documents":
            return parts[0]
        if len(parts) == 1:
            if self.command != "GET":
                return "documents.write"
            fields = self.qd.get("fields")
            if fields == "id":
                return "documents.ids"
            if fields and "content" in fields:
                return "documents.contents"
            return "documents.list"
        if len(parts) == 2:
            return "documents.detail" if parts[1].isdigit() else "documents." + parts[1]
        return "documents." + parts[2]

    def _hang(self):
        mock = self.mock
        mock.stats["hung"] += 1
        sock = self.connection
        generation = mock.fail_generation
        # held until the next /__mock/fail call or until the client gives up
        while mock.fail_generation == generation:
            readable, _, _ = select.select([sock], [], [], 0.25)
            if readable:
                try:
                    if not sock.recv(1, socket.MSG_PEEK):
                        break
                except OSError:
                    break
                time.sleep(0.25)
        self.close_connection = True

    def _token(self):
        if self.command != "POST":
            return self._json(405, {"detail": 'Method "%s" not allowed.' % self.command},
                              headers=[("Allow", "POST, OPTIONS")])
        try:
            data = self.json_body()
        except ApiError as e:
            return self._json(e.status, e.payload)
        missing = {k: ["This field is required."] for k in ("username", "password")
                   if not data.get(k)}
        if missing:
            return self._json(400, missing)
        self._json(200, {"token": self.mock.token or "mock-token"})

    # ---- control endpoints --------------------------------------------- #

    def _control(self, parts):
        mock = self.mock
        qd = self.qd
        action = parts[0] if parts else ""
        with mock.lock:
            if action == "stats":
                st = mock.stats
                out = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in st.items()}
                out.update({
                    "uptime_seconds": round(time.time() - st["started"], 1),
                    "documents": len(mock.alive_ids()), "highest_id": mock.n,
                    "trashed": len(mock.trashed), "fail": mock.fail,
                    "latency_ms": mock.latency * 1000, "mutation_version": mock.version,
                    "index_ready": mock.index_ready.is_set(),
                    "file_info_cached": len(mock.finfo),
                    "unknown_params": mock.stats["unknown_params"]})
                return self._json(200, out)
            if self.command not in ("POST", "PUT"):
                return self._json(405, {"detail": "use POST"})
            if action == "fail":
                mode = qd.get("mode", "off")
                if mode not in ("off", "hang", "503", "500", "502", "401", "403", "429"):
                    return self._json(400, {"detail": "mode must be 503|hang|off (or 500/502/401/"
                                                      "403/429)"})
                mock.fail_generation += 1
                mock.fail = {"mode": mode, "path": qd.get("path") or None,
                             "remaining": int(qd["count"]) if qd.get("count") else None}
                return self._json(200, {"fail": mock.fail})
            if action == "latency":
                mock.latency = float(qd.get("ms", 0)) / 1000.0
                return self._json(200, {"latency_ms": mock.latency * 1000})
            if action == "touch":
                ids = self._id_list(qd.get("id", ""))
                done = []
                for i in ids:
                    if not (0 < i <= mock.n) or not mock.alive[i]:
                        continue
                    if "title" in qd:
                        mock.set_title(i, qd["title"])
                    if "content" in qd:
                        mock.content_override[i] = qd["content"]
                    if "tags" in qd:
                        mock.tagmask[i] = sum(mock.tag_closure.get(t, 0) for t in csv_ints(qd["tags"]))
                    for name, col, res in (("correspondent", mock.corr, mock.correspondents),
                                           ("document_type", mock.dtype, mock.doctypes),
                                           ("storage_path", mock.spath, mock.spaths)):
                        if name in qd:
                            v = int(qd[name] or 0)
                            col[i] = v if v in res else 0
                    mock.modified[i] = mock.now()
                    done.append(i)
                mock.bump()
                return self._json(200, {"touched": len(done), "ids": done[:1000],
                                        "modified": iso(mock._last_now) if done else None})
            if action == "delete":
                ids = [i for i in self._id_list(qd.get("id", "")) if 0 < i <= mock.n and mock.alive[i]]
                mock.trash_docs(ids)
                if truthy(qd.get("hard", "0")):
                    for i in ids:
                        mock.trashed.pop(i, None)
                return self._json(200, {"deleted": len(ids), "ids": ids[:1000],
                                        "trash": not truthy(qd.get("hard", "0"))})
            if action == "add":
                count = max(1, min(int(qd.get("count", 1)), 100000))
                ids = mock.add_documents(count)
                return self._json(200, {"added": len(ids), "first_id": ids[0], "last_id": ids[-1]})
        self._json(404, {"detail": "unknown control endpoint",
                         "endpoints": ["GET /__mock/stats", "POST /__mock/fail?mode=503|hang|off",
                                       "POST /__mock/latency?ms=", "POST /__mock/touch?id=&title=",
                                       "POST /__mock/delete?id=", "POST /__mock/add?count="]})

    @staticmethod
    def _id_list(spec: str) -> list[int]:
        out: list[int] = []
        for part in spec.split(","):
            part = part.strip()
            m = re.fullmatch(r"(\d+)-(\d+)", part)
            if m:
                out.extend(range(int(m.group(1)), int(m.group(2)) + 1))
            elif part.isdigit():
                out.append(int(part))
        return out

    # ---- API ------------------------------------------------------------ #

    def _api(self, parts):
        mock = self.mock
        method = self.command
        if method == "HEAD":
            method = "GET"
        if method == "OPTIONS":
            return self._json(200, {"name": "mock", "renders": ["application/json"]})
        head = parts[0] if parts else ""
        if head == "documents":
            return self._documents(parts[1:], method)
        with mock.lock:
            if not parts:
                base = self.base_url() + "/api/"
                return self._json(200, {k: base + k + "/" for k in (
                    "correspondents", "document_types", "documents", "logs", "tags", "saved_views",
                    "storage_paths", "tasks", "users", "groups", "mail_accounts", "mail_rules",
                    "share_links", "workflows", "custom_fields", "config")})
            if head in ELEMENT_KINDS:
                return self._elements(head, parts[1:], method)
            if head == "ui_settings":
                if method == "POST":
                    data = self.json_body()
                    if isinstance(data.get("settings"), dict):
                        mock.ui_settings = data["settings"]
                    return self._json(200, {"success": True})
                me = mock.users[mock.me]
                return self._json(200, {
                    "user": {"id": me["id"], "username": me["username"], "is_staff": me["is_staff"],
                             "is_superuser": me["is_superuser"], "groups": me["groups"]},
                    "settings": mock.ui_settings, "permissions": mock.user_permissions})
            if head == "remote_version":
                return self._json(200, {"version": "v" + mock.server_version,
                                        "update_available": False})
            if head == "statistics":
                return self._json(200, mock.statistics())
            if head == "config":
                return self._json(200, [{
                    "id": 1, "user_args": None, "barcode_tag_mapping": None, "output_type": None,
                    "pages": None, "language": "deu+eng", "mode": None, "skip_archive_file": None,
                    "image_dpi": None, "unpaper_clean": None, "deskew": None, "rotate_pages": None,
                    "rotate_pages_threshold": None, "max_image_pixels": None,
                    "color_conversion_strategy": None, "app_title": "Mock Paperless",
                    "app_logo": None, "barcodes_enabled": True,
                    "barcode_enable_tiff_support": None, "barcode_string": None,
                    "barcode_retain_split_pages": None, "barcode_enable_asn": True,
                    "barcode_asn_prefix": "ASN", "barcode_upscale": None, "barcode_dpi": None,
                    "barcode_max_pages": None, "barcode_enable_tag": None}])
            if head == "profile":
                me = mock.users[mock.me]
                return self._json(200, {
                    "email": me["email"], "password": "**********", "first_name": me["first_name"],
                    "last_name": me["last_name"], "auth_token": mock.token,
                    "social_accounts": [], "has_usable_password": True, "is_mfa_enabled": False})
            if head == "status":
                return self._json(200, {
                    "pngx_version": mock.server_version, "server_os": "mock", "install_type": "mock",
                    "storage": {"total": 500 * 2 ** 30, "available": 250 * 2 ** 30},
                    "database": {"type": "mock", "status": "OK", "error": None},
                    "tasks": {"redis_status": "OK", "celery_status": "OK", "index_status": "OK",
                              "classifier_status": "OK", "sanity_check_status": "OK"}})
            if head == "search" and parts[1:] == ["autocomplete"]:
                term = self.qd.get("term", "").lower()
                limit = int(self.qd.get("limit") or 10)
                words = sorted({w.lower() for k in KINDS for n in k["nouns"] for w in n.split()}
                               | {w.lower() for lang in RARE_WORDS for w in RARE_WORDS[lang]})
                return self._json(200, [w for w in words if w.startswith(term)][:limit] if term
                                  else [])
            if head == "search":
                q = [("title_content", self.qd.get("query", "")), ("ordering", "-created")]
                ids = mock.query(q)["ids"][:3] if self.qd.get("query") else []
                return self._json(200, {
                    "total": len(ids),
                    "documents": [mock.doc_json(i, truncate=True) for i in ids],
                    "saved_views": [], "tags": [], "correspondents": [], "document_types": [],
                    "storage_paths": [], "users": [], "groups": [], "mail_rules": [],
                    "mail_accounts": [], "workflows": [], "custom_fields": []})
            if head == "tasks":
                return self._tasks(parts[1:], method)
            if head == "acknowledge_tasks" and method == "POST":
                return self._ack_tasks()
            if head == "trash":
                return self._trash(method)
            if head == "share_links":
                return self._share_links(parts[1:], method)
            if head == "bulk_edit_objects" and method == "POST":
                data = self.json_body()
                kind = data.get("object_type")
                if kind in mock.resources and data.get("operation") == "delete":
                    for eid in data.get("objects") or []:
                        if eid in mock.resources[kind]:
                            mock.element_delete(kind, int(eid))
                return self._json(200, {"result": "OK"})
            if head in EMPTY_COLLECTIONS:
                return self._json(200, self.paged(0, b"[]", 1, 25, b"[]"))
        mock.stats["unknown_endpoints"]["%s /api/%s" % (self.command, "/".join(parts))] += 1
        sys.stderr.write("[mock] UNKNOWN endpoint %s %s\n" % (self.command, self.path[:200]))
        self._json(404, {"detail": "Not found."})

    # ---- element collections -------------------------------------------- #

    def _elements(self, kind, rest, method):
        mock = self.mock
        items = mock.resources[kind]
        full = truthy(self.qd.get("full_perms", "0"))
        writable = kind in mock.ELEMENT_DEFAULTS
        if not rest:
            if method == "GET":
                known = {"page", "page_size", "full_perms", "ordering", "name__icontains",
                         "name__iexact", "name__istartswith", "id__in", "id", "is_root"}
                for k, v in self.q:
                    if k not in known:
                        mock.note_unknown("/api/%s/" % kind, k, v)
                objs = list(items.values())
                namekey = "username" if kind == "users" else "name"
                if "name__icontains" in self.qd:
                    q = self.qd["name__icontains"].lower()
                    objs = [o for o in objs if q in o[namekey].lower()]
                if "name__iexact" in self.qd:
                    q = self.qd["name__iexact"].lower()
                    objs = [o for o in objs if q == o[namekey].lower()]
                if "name__istartswith" in self.qd:
                    q = self.qd["name__istartswith"].lower()
                    objs = [o for o in objs if o[namekey].lower().startswith(q)]
                if "id__in" in self.qd:
                    s = set(csv_ints(self.qd["id__in"]))
                    objs = [o for o in objs if o["id"] in s]
                ordering = self.qd.get("ordering") or namekey
                field = ordering.lstrip("-")
                if field == "document_count":
                    counts = mock.counts(kind)
                    objs.sort(key=lambda o: counts.get(o["id"], 0), reverse=ordering[0] == "-")
                else:
                    if field not in ("name", "username", "id", "pk"):
                        field = namekey
                    field = "id" if field == "pk" else field
                    objs.sort(key=lambda o: (o[field].lower() if isinstance(o[field], str)
                                             else o[field]), reverse=ordering[0] == "-")
                all_ids = J([o["id"] for o in objs])
                total = len(objs)
                if kind == "tags" and mock.nested_tags:
                    present = {o["id"] for o in objs}
                    objs = [o for o in objs if o.get("parent") not in present]
                page, size = self.page_args()
                start = (page - 1) * size
                if page < 1 or (start >= len(objs) and page != 1):
                    raise ApiError(404, {"detail": "Invalid page."})
                results = J([mock.element_json(kind, o, full) for o in objs[start:start + size]])
                shown = total if not (kind == "tags" and mock.nested_tags) else len(objs)
                body = self.paged(shown, results, page, size, all_ids)
                return self._json(200, body)
            if method == "POST" and writable:
                obj = mock.element_write(kind, None, self.json_body())
                return self._json(201, mock.element_json(kind, obj, full))
            return self._json(405, {"detail": 'Method "%s" not allowed.' % self.command})
        if not rest[0].isdigit():
            raise not_found()
        eid = int(rest[0])
        obj = items.get(eid)
        if obj is None:
            raise not_found()
        if method == "GET":
            return self._json(200, mock.element_json(kind, obj, full))
        if not writable:
            return self._json(405, {"detail": 'Method "%s" not allowed.' % self.command})
        if method in ("PATCH", "PUT"):
            obj = mock.element_write(kind, obj, self.json_body())
            return self._json(200, mock.element_json(kind, obj, full))
        if method == "DELETE":
            mock.element_delete(kind, eid)
            return self._send(204)
        self._json(405, {"detail": 'Method "%s" not allowed.' % self.command})

    # ---- documents ------------------------------------------------------ #

    def _documents(self, rest, method):
        mock = self.mock
        if not rest:
            if method != "GET":
                return self._json(405, {"detail": 'Method "%s" not allowed.' % self.command})
            with mock.lock:
                body = self._document_list()
            return self._json(200, body)
        first = rest[0]
        if not first.isdigit():
            with mock.lock:
                if first == "post_document" and method == "POST":
                    return self._upload()
                if first == "next_asn":
                    return self._json(200, mock.next_asn)
                if first == "bulk_edit" and method == "POST":
                    return self._bulk_edit()
                if first == "selection_data" and method == "POST":
                    return self._selection_data()
            raise not_found()
        i = int(first)
        action = rest[1] if len(rest) > 1 else ""
        if action in ("download", "preview", "thumb"):
            with mock.lock:
                mock.require_doc(i)
            version = self.qd.get("version")
            if version and version != str(i):
                raise not_found()
            if action == "thumb":
                return self._thumb(i)
            return self._file(i, truthy(self.qd.get("original", "0")), action == "download")
        with mock.lock:
            mock.require_doc(i)
            full = truthy(self.qd.get("full_perms", "0"))
            fields = set(self.qd["fields"].split(",")) if self.qd.get("fields") else None
            if action == "":
                if method == "GET":
                    return self._json(200, mock.doc_json(i, full, fields=fields))
                if method in ("PATCH", "PUT"):
                    mock.update_doc(i, self.json_body())
                    return self._json(200, mock.doc_json(i, full, fields=fields))
                if method == "DELETE":
                    mock.trash_docs([i])
                    return self._send(204)
            elif action == "metadata":
                return self._json(200, self._metadata(i))
            elif action == "notes":
                return self._notes(i, method)
            elif action == "suggestions":
                kind = KINDS[mock.p_kind[i]]
                tags = [t for t in kind["_tags"] if t in mock.tags]
                dates = sorted({ord_iso(mock.p_created[i]), ord_iso(mock.p_created[i] + 14)})
                return self._json(200, {
                    "correspondents": [c for c in [mock.p_corr[i]] if c in mock.correspondents],
                    "tags": tags,
                    "document_types": [t for t in [kind["_dtype"]] if t in mock.doctypes],
                    "storage_paths": [p for p in [kind["_spath"]] if p in mock.spaths],
                    "dates": dates})
            elif action == "share_links":
                return self._json(200, [s for s in mock.share_links.values() if s["document"] == i])
            elif action in ("history", "audit_trail"):
                return self._json(200, [])
            elif action == "root" and mock.v3:
                return self._json(200, {"root_id": i})
        raise not_found()

    def _document_list(self) -> bytes:
        mock = self.mock
        qd = self.qd
        entry = mock.query(self.q)
        ids = entry["ids"]
        total = len(ids)
        page, size = self.page_args()
        start = (page - 1) * size
        if page < 1 or (start >= total and page != 1):
            raise ApiError(404, {"detail": "Invalid page."})
        page_ids = ids[start:start + size]
        fields = set(qd["fields"].split(",")) if qd.get("fields") else None
        if fields == {"id"}:
            results = ("[" + ",".join(['{"id":%d}' % i for i in page_ids]) + "]").encode()
        else:
            full = truthy(qd.get("full_perms", "0"))
            truncate = truthy(qd.get("truncate_content", "0"))
            docs = [mock.doc_json(i, full, truncate, fields) for i in page_ids]
            if any(k in ("query", "more_like_id") for k, _ in self.q) and fields is None:
                for rank, d in enumerate(docs, start):
                    d["__search_hit__"] = {"score": round(1.0 / (1 + rank), 6), "highlights": "",
                                           "note_highlights": "", "rank": rank}
            results = J(docs)
        all_json = None
        if mock.args.all_ids:
            all_json = entry["all"]
            if all_json is None:
                all_json = entry["all"] = ("[" + ",".join(map(str, ids)) + "]").encode()
        return self.paged(total, results, page, size, all_json)

    def _metadata(self, i: int) -> dict:
        mock = self.mock
        osize, osum, omime = mock.file_info(i, True)
        has_archive = not mock.flags[i] & 1
        created = iso(mock.added[i])
        meta = [] if omime != "application/pdf" else [
            {"namespace": "http://ns.adobe.com/pdf/1.3/", "prefix": "pdf", "key": "Producer",
             "value": "mock-paperless scanner"},
            {"namespace": "http://ns.adobe.com/xap/1.0/", "prefix": "xmp", "key": "CreateDate",
             "value": created},
            {"namespace": "http://purl.org/dc/elements/1.1/", "prefix": "dc", "key": "format",
             "value": "application/pdf"}]
        out = {
            "original_checksum": osum, "original_size": osize, "original_mime_type": omime,
            "media_filename": "originals/%07d.%s" % (i, "png" if omime == "image/png" else "pdf"),
            "has_archive_version": has_archive, "original_metadata": meta,
            "archive_checksum": None, "archive_media_filename": None,
            "original_filename": mock.original_name(i), "archive_size": None,
            "archive_metadata": None,
            "lang": KINDS[mock.p_kind[i]]["lang"],
        }
        if has_archive:
            asize, asum, _ = mock.file_info(i, False)
            out.update({
                "archive_checksum": asum, "archive_size": asize,
                "archive_media_filename": "archive/%07d.pdf" % i,
                "archive_metadata": [
                    {"namespace": "http://ns.adobe.com/pdf/1.3/", "prefix": "pdf",
                     "key": "Producer", "value": "mock-paperless OCRmyPDF (PDF/A)"},
                    {"namespace": "http://www.aiim.org/pdfa/ns/id/", "prefix": "pdfaid",
                     "key": "part", "value": "2"}]})
        return out

    def _notes(self, i, method):
        mock = self.mock
        notes = mock.get_notes(i)
        if method in ("POST", "DELETE"):
            notes = mock.notes[i] = list(notes)
        if method == "POST":
            text = str(self.json_body().get("note") or "")
            if not text:
                raise ApiError(400, {"note": ["This field may not be blank."]})
            now = mock.now()
            notes.insert(0, {"id": mock.next_note, "note": text, "created": now, "user": mock.me})
            mock.next_note += 1
            mock.modified[i] = mock.now()
            mock.bump()
        elif method == "DELETE":
            nid = int(self.qd.get("id") or 0)
            if not any(n["id"] == nid for n in notes):
                raise not_found()
            notes[:] = [n for n in notes if n["id"] != nid]
            mock.modified[i] = mock.now()
            mock.bump()
        self._json(200, [mock.note_json(n) for n in notes])

    def _file(self, i: int, original: bool, attachment: bool):
        mock = self.mock
        no_archive = bool(mock.flags[i] & 1)
        data = None
        info = mock.finfo.get((i, original or no_archive))
        if info is None:
            data, mime = mock.file_bytes(i, original)
            info = (len(data), hashlib.new(mock.checksum_algo, data).hexdigest(), mime)
            mock.finfo[(i, original or no_archive)] = info
        size, checksum, mime = info
        # paperless-ngx sends no ETag when an archive request falls back to the original
        etag = None if (no_archive and not original) else '"%s"' % checksum
        inm = self.headers.get("If-None-Match")
        if etag and inm and checksum in [t.strip().lstrip("W/").strip('"') for t in inm.split(",")]:
            mock.stats["not_modified"] += 1
            return self._send(304, headers=[("ETag", etag)])
        if data is None:
            data, mime = mock.file_bytes(i, original)
        name = mock.archive_name(i, original=original)
        disp = "%s; filename=\"%s\"; filename*=utf-8''%s" % (
            "attachment" if attachment else "inline", ascii_name(name), quote(name))
        headers = [("Content-Disposition", disp),
                   ("Last-Modified", self.date_time_string(mock.added[i])),
                   ("Cache-Control", "no-cache")]
        if etag:
            headers.append(("ETag", etag))
        mock.stats["pdfs_served"] += 1
        mock.stats["pdf_bytes"] += len(data)
        self._send(200, data, ctype=mime, headers=headers, compress=False)

    def _thumb(self, i: int):
        mock = self.mock
        etag = '"thumb-%d-%s"' % (i, mock.seed)
        headers = [("ETag", etag), ("Cache-Control", "no-cache"),
                   ("Last-Modified", self.date_time_string(mock.added[i]))]
        if self.headers.get("If-None-Match") == etag or (
                self.headers.get("If-Modified-Since") == headers[2][1]):
            mock.stats["not_modified"] += 1
            return self._send(304, headers=headers)
        mock.stats["thumbnails_served"] += 1
        self._send(200, mock.thumbnail(i), ctype="image/png", headers=headers, compress=False)

    def _share(self, slug: str):
        mock = self.mock
        link = next((s for s in mock.share_links.values() if s["slug"] == slug), None)
        if link is None or not mock.alive[link["document"]]:
            return self._json(404, {"detail": "Not found."})
        data, mime = mock.file_bytes(link["document"], link["file_version"] == "original")
        self._send(200, data, ctype=mime, compress=False)

    def _upload(self):
        mock = self.mock
        fields, files = self.multipart()
        if "document" not in files:
            raise ApiError(400, {"document": ["No file was submitted."]})
        filename, data = files["document"]

        def one(name):
            return fields.get(name, [None])[0]

        i = mock.add_documents(1)[0]
        mock.filename_override[i] = filename
        title = one("title") or re.sub(r"\.[A-Za-z0-9]{2,5}$", "", filename)
        mock.set_title(i, title)
        mock.p_title[i] = title
        mock.content_override[i] = (
            "%s\n\nUploaded through the API as %s (%d bytes).\nmock-upload-marker-%d"
            % (title, filename, len(data), i))
        mock.corr[i] = mock.dtype[i] = mock.spath[i] = 0
        mock.tagmask[i] = mock.inbox_mask
        mock.owner[i] = mock.me
        mock.cfields[i] = []
        mock.notes[i] = []
        mock.perms[i] = None
        patch = {}
        for name in ("correspondent", "document_type", "storage_path", "archive_serial_number"):
            if one(name):
                patch[name] = int(one(name))
        if one("created"):
            patch["created"] = one("created")
        if fields.get("tags"):
            patch["tags"] = [int(t) for t in fields["tags"]] + mock.mask_ids(mock.inbox_mask)
        if one("custom_fields"):
            try:
                cf = json.loads(one("custom_fields"))
                patch["custom_fields"] = cf if isinstance(cf, dict) else [
                    {"field": int(f), "value": None} for f in cf]
            except ValueError:
                pass
        try:
            mock.update_doc(i, patch)
        except ApiError:
            mock.trash_docs([i])
            mock.trashed.pop(i, None)
            raise
        now = mock.now()
        task = {"id": len(mock.tasks) + 1, "task_id": str(uuid.uuid4()), "filename": filename,
                "created": now, "done": now + 0.5, "status": "SUCCESS", "document": i,
                "acknowledged": False, "result": "Success. New document id %d created" % i}
        mock.tasks.append(task)
        self._json(200, task["task_id"])

    def _bulk_edit(self):
        mock = self.mock
        data = self.json_body()
        ids = [int(i) for i in data.get("documents") or [] if 0 < int(i) <= mock.n and mock.alive[int(i)]]
        method = data.get("method")
        p = data.get("parameters") or {}
        if method == "delete":
            mock.trash_docs(ids)
            return self._json(200, {"result": "OK"})
        for i in ids:
            if method == "set_correspondent":
                mock.update_doc(i, {"correspondent": p.get("correspondent")})
            elif method == "set_document_type":
                mock.update_doc(i, {"document_type": p.get("document_type")})
            elif method == "set_storage_path":
                mock.update_doc(i, {"storage_path": p.get("storage_path")})
            elif method in ("add_tag", "remove_tag", "modify_tags"):
                tags = set(mock.mask_ids(mock.tagmask[i]))
                tags |= set(p.get("add_tags") or ([p["tag"]] if method == "add_tag" else []))
                tags -= set(p.get("remove_tags") or ([p["tag"]] if method == "remove_tag" else []))
                mock.update_doc(i, {"tags": sorted(tags)})
            elif method == "set_permissions":
                patch = {"set_permissions": p.get("set_permissions") or {}}
                if "owner" in p:
                    patch["owner"] = p["owner"]
                mock.update_doc(i, patch)
            elif method == "modify_custom_fields":
                cur = {e["field"]: e["value"] for e in mock.get_cf(i)}
                add = p.get("add_custom_fields") or {}
                if isinstance(add, list):
                    add = {f: None for f in add}
                cur.update({int(k): v for k, v in add.items()})
                for f in p.get("remove_custom_fields") or []:
                    cur.pop(int(f), None)
                mock.update_doc(i, {"custom_fields": [{"field": k, "value": v}
                                                      for k, v in cur.items()]})
            elif method in ("reprocess", "rotate", "merge", "split", "delete_pages"):
                mock.modified[i] = mock.now()
                mock.bump()
            else:
                raise ApiError(400, {"method": ["Unsupported bulk edit method in the mock."]})
        self._json(200, {"result": "OK"})

    def _selection_data(self):
        mock = self.mock
        ids = [int(i) for i in self.json_body().get("documents") or []
               if 0 < int(i) <= mock.n and mock.alive[int(i)]]
        tags = Counter(t for i in ids for t in mock.mask_ids(mock.tagmask[i]))

        def col(c, res):
            cnt = Counter(c[i] for i in ids)
            return [{"id": k, "document_count": cnt.get(k, 0)} for k in res]

        self._json(200, {
            "selected_correspondents": col(mock.corr, mock.correspondents),
            "selected_tags": [{"id": k, "document_count": tags.get(k, 0)} for k in mock.tags],
            "selected_document_types": col(mock.dtype, mock.doctypes),
            "selected_storage_paths": col(mock.spath, mock.spaths),
            "selected_custom_fields": []})

    # ---- tasks / trash / share links ------------------------------------ #

    def _task_json(self, t: dict) -> dict:
        mock = self.mock
        if mock.v3:
            failed = t["status"] == "FAILURE"
            return {
                "id": t["id"], "task_id": t["task_id"], "task_type": "consume_file",
                "task_type_display": "Consume File", "trigger_source": "api_upload",
                "trigger_source_display": "API Upload", "status": t["status"].lower(),
                "status_display": t["status"].title(), "date_created": iso(t["created"]),
                "date_started": iso(t["created"]), "date_done": iso(t["done"]) if t["done"] else None,
                "duration_seconds": (t["done"] - t["created"]) if t["done"] else None,
                "wait_time_seconds": 0.01,
                "input_data": {"filename": t["filename"], "mime_type": "application/pdf",
                               "overrides": {"filename": t["filename"], "owner_id": mock.me}},
                "result_data": ({"reason": t["result"], "duplicate_of": t.get("duplicate_of")}
                                if failed else {"document_id": t["document"]}),
                "related_document_ids": [t["document"]] if t["document"] else [],
                "acknowledged": t["acknowledged"], "owner": mock.me}
        return {"id": t["id"], "task_id": t["task_id"], "task_name": "consume_file",
                "task_file_name": t["filename"], "date_created": iso(t["created"]),
                "date_done": iso(t["done"]) if t["done"] else None, "type": "auto_task",
                "status": t["status"], "result": t["result"], "acknowledged": t["acknowledged"],
                "related_document": str(t["document"]) if t["document"] else None, "owner": mock.me}

    def _tasks(self, rest, method):
        mock = self.mock
        if rest == ["acknowledge"] and method == "POST":
            return self._ack_tasks()
        if rest and rest[0].isdigit():
            task = next((t for t in mock.tasks if t["id"] == int(rest[0])), None)
            if task is None:
                raise not_found()
            return self._json(200, self._task_json(task))
        if rest:
            raise not_found()
        tasks = list(reversed(mock.tasks))
        if "acknowledged" in self.qd:
            want = truthy(self.qd["acknowledged"])
            tasks = [t for t in tasks if t["acknowledged"] == want]
        if self.qd.get("task_id"):
            tasks = [t for t in tasks if t["task_id"] == self.qd["task_id"]]
        name = self.qd.get("task_type") or self.qd.get("task_name")
        if name and name != "consume_file":
            tasks = []
        if not mock.v3:
            return self._json(200, [self._task_json(t) for t in tasks])
        page, size = self.page_args()
        start = (page - 1) * size
        if page < 1 or (start >= len(tasks) and page != 1):
            raise ApiError(404, {"detail": "Invalid page."})
        results = J([self._task_json(t) for t in tasks[start:start + size]])
        self._json(200, self.paged(len(tasks), results, page, size, J([t["id"] for t in tasks])))

    def _ack_tasks(self):
        ids = set(self.json_body().get("tasks") or [])
        count = 0
        for t in self.mock.tasks:
            if t["id"] in ids and not t["acknowledged"]:
                t["acknowledged"] = True
                count += 1
        self._json(200, {"result": count})

    def _trash(self, method):
        mock = self.mock
        if method == "POST":
            data = self.json_body()
            ids = [int(i) for i in data.get("documents") or []]
            action = data.get("action")
            if action not in ("restore", "empty"):
                raise ApiError(400, {"action": ['"%s" is not a valid choice.' % action]})
            if not ids and action == "empty":
                ids = list(mock.trashed)
            done = []
            for i in ids:
                if i in mock.trashed:
                    del mock.trashed[i]
                    if action == "restore":
                        mock.alive[i] = 1
                        mock.modified[i] = mock.now()
                    done.append(i)
            mock.bump()
            return self._json(200, {"result": "OK", "doc_ids": done})
        ids = sorted(mock.trashed, key=lambda i: -mock.trashed[i])
        page, size = self.page_args()
        start = (page - 1) * size
        if page < 1 or (start >= len(ids) and page != 1):
            raise ApiError(404, {"detail": "Invalid page."})
        results = J([mock.doc_json(i, truncate=True, deleted=True) for i in ids[start:start + size]])
        self._json(200, self.paged(len(ids), results, page, size, J(ids)))

    def _share_links(self, rest, method):
        mock = self.mock
        if not rest:
            if method == "POST":
                data = self.json_body()
                doc = int(data.get("document") or 0)
                mock.require_doc(doc)
                version = data.get("file_version") or "archive"
                if version not in ("archive", "original"):
                    raise ApiError(400, {"file_version": ["Not a valid choice."]})
                sid = mock.next_share
                mock.next_share += 1
                link = {"id": sid, "created": iso(mock.now()), "expiration": data.get("expiration"),
                        "slug": hashlib.sha1(b"%d:%s" % (sid, mock.key)).hexdigest()[:40],
                        "document": doc, "file_version": version}
                mock.share_links[sid] = link
                return self._json(201, link)
            links = list(mock.share_links.values())
            return self._json(200, self.paged(len(links), J(links), 1, max(1, len(links)),
                                              J([s["id"] for s in links])))
        if rest[0].isdigit() and int(rest[0]) in mock.share_links:
            sid = int(rest[0])
            if method == "DELETE":
                del mock.share_links[sid]
                return self._send(204)
            return self._json(200, mock.share_links[sid])
        raise not_found()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--documents", type=int, default=25000, help="number of documents (ids 1..N)")
    ap.add_argument("--seed", default="1", help="seed for all generated data")
    ap.add_argument("--pdf-kb", type=float, default=40,
                    help="approximate PDF size in KiB (each file is 50%%-150%% of it)")
    ap.add_argument("--latency-ms", type=float, default=0, help="delay added to every /api/ response")
    ap.add_argument("--token", default="mock-token",
                    help="API token; empty string disables authentication")
    ap.add_argument("--server-version", default="3.5.0", help="reported as X-Version")
    ap.add_argument("--api-version", type=int, default=10, help="reported as X-Api-Version")
    ap.add_argument("--reference-date", default=DEFAULT_REFERENCE_DATE,
                    help="'today' of the generated library (YYYY-MM-DD or 'today')")
    ap.add_argument("--no-gzip", dest="gzip", action="store_false",
                    help="never gzip JSON responses")
    ap.add_argument("--gzip-level", type=int, default=1)
    ap.add_argument("--no-all-ids", dest="all_ids", action="store_false",
                    help="omit the 'all' id array real servers put into document list pages")
    ap.add_argument("--null-ordering", choices=("postgres", "sqlite"), default="postgres",
                    help="where NULLs sort: last when ascending (PostgreSQL) or first (SQLite)")
    ap.add_argument("--quiet", action="store_true", help="no per-request log lines")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    mock = Mock(args)
    Handler.mock = mock
    server = Server((args.host, args.port), Handler)
    sys.stderr.write(
        "[mock] paperless-ngx %s (API v%d) with %d documents, seed %s, generated in %.2fs\n"
        "[mock] listening on http://%s:%d  token: %s\n" % (
            args.server_version, args.api_version, mock.n, args.seed, time.perf_counter() - t0,
            args.host, server.server_address[1], args.token or "(none required)"))
    sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
