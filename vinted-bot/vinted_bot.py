#!/usr/bin/env python3
"""
Vinted-bevakaren
================

Söker igenom Vinted efter underprisade märkesplagg (jeans, fältjackor,
dunjackor …), uppskattar vinsten vid återförsäljning och skickar en notis
(Telegram, Discord och/eller ntfy) när något ser lönsamt ut.

    python vinted_bot.py               # bevaka i en loop
    python vinted_bot.py --dry-run     # visa vad som matchar just nu, skicka inget
    python vinted_bot.py --kalibrera   # visa aktuella medianpriser per märke
    python vinted_bot.py --test-notis  # skicka en testnotis

Se README.md för installation och inställningar.
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html import escape
from pathlib import Path

import yaml

log = logging.getLogger("vinted-bot")
HERE = Path(__file__).resolve().parent

# Tyst läge (--tyst): skriv inte ut fynd, chattnamn eller chat-id i loggen. För körningar vars loggar
# är publika, t.ex. GitHub Actions i ett publikt repo – annars kan vem som helst läsa fynden i realtid.
QUIET = False


def who(name: str, chat_id: str) -> str:
    """Hur en chatt beskrivs i loggen – utan namn och id i tyst läge."""
    if QUIET:
        return "en grupp/kanal" if str(chat_id).startswith("-") else "en privat chatt"
    return f"{name} (chat-id {chat_id})"

DEFAULTS: dict = {
    "domain": "vinted.se",
    "currency": "SEK",
    "interval_seconds": 240,
    "delay_between_searches": [3, 7],
    "max_pages": 3,
    "impersonate": "chrome",
    "notify_on_first_run": False,
    "shipping_estimate": 49,
    "sell_fee_percent": 0,
    "min_profit": 100,
    "min_roi_percent": 0,
    "condition_multipliers": {
        "Ny med prislapp": 1.25,
        "Ny utan prislapp": 1.1,
        "Mycket bra": 1.0,
        "Bra": 0.85,
        "Tillfredsställande": 0.6,
    },
    "exclude_keywords": [],
    "premium_keywords": {},
    "notify": {},
}


class ConfigError(Exception):
    pass


class FetchError(Exception):
    pass


class Blocked(FetchError):
    """Vinted/Cloudflare nekade anropet (403/429) – dags att pausa."""


# --------------------------------------------------------------------------
# Textmatchning
# --------------------------------------------------------------------------

_FOLD_EXTRA = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ł": "l", "þ": "th"})


def fold(text: str) -> str:
    """Gemener utan accenter och apostrofer: "Fjällräven" -> "fjallraven", "Norrøna" -> "norrona", "Levi's" -> "levis"."""
    text = unicodedata.normalize("NFKD", text.casefold().translate(_FOLD_EXTRA))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"['’`´.]", "", text)


def words(text: str) -> list[str]:
    return re.findall(r"\w+", fold(text))


def contains_words(haystack: list[str], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return n > 0 and any(tuple(haystack[i:i + n]) == needle for i in range(len(haystack) - n + 1))


def hashtag(text: str) -> str:
    """Telegram-hashtagg av ett namn: "C.P. Company" -> "#CPCompany", "Fjällräven" -> "#Fjällräven"."""
    parts = re.findall(r"\w+", re.sub(r"['’`´.]", "", text))
    return "#" + "".join(p[:1].upper() + p[1:] for p in parts) if parts else ""


def keyword_re(keyword: str) -> re.Pattern:
    """Matchar nyckelordet som helt ord/fras, oavsett skiftläge och accenter."""
    return re.compile(r"(?<!\w)" + re.escape(fold(keyword)) + r"(?!\w)")


# --------------------------------------------------------------------------
# Datamodell
# --------------------------------------------------------------------------

@dataclass
class Item:
    id: int
    title: str
    brand: str
    size: str
    condition: str
    price: float          # säljarens pris
    total: float          # pris inkl. Vinteds köparskydd
    currency: str
    url: str
    photo: str | None = None
    is_business: bool = False


@dataclass
class BrandRule:
    label: str
    resale: float
    max_price: float
    suspicious_below: float = 0.0
    min_price: float = 0.0
    skip: bool = False  # undantaget märke, t.ex. en billig sidolinje som annars skulle matcha


@dataclass
class Search:
    name: str
    query: str
    catalog_ids: list[int]
    min_profit: float
    min_roi_percent: float
    brands: list[tuple[tuple[str, ...], BrandRule]]
    other_brands: BrandRule | None
    sizes: set[str]
    exclude: list[tuple[str, re.Pattern]]
    premium: list[tuple[str, re.Pattern, float]]
    tags: list[str] = field(default_factory=list)

    def rule_for(self, brand: str) -> BrandRule | None:
        brand_words = words(brand)
        for key, rule in self.brands:
            if contains_words(brand_words, key):
                return rule
        return self.other_brands

    def all_rules(self) -> list[BrandRule]:
        """Alla regler som kan ge fynd (undantagna märken räknas inte)."""
        return [r for _, r in self.brands if not r.skip] + ([self.other_brands] if self.other_brands else [])

    def price_cap(self) -> float:
        return max(r.max_price for r in self.all_rules())

    def price_floor(self) -> float:
        return min(r.min_price for r in self.all_rules())


@dataclass
class Deal:
    item: Item
    search: str
    resale: float
    cost: float
    profit: float
    roi: float
    bonuses: list[str] = field(default_factory=list)
    suspicious: bool = False
    price_drop_from: float | None = None
    hashtags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

def _num(value, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} måste vara ett tal, fick {value!r}") from None


def build_search(raw: dict, cfg: dict) -> Search:
    name = str(raw.get("name") or raw.get("query") or "").strip()
    if not name:
        raise ConfigError("Varje sökning behöver 'name' eller 'query'")
    catalog = raw.get("catalog") or []
    catalog = [int(c) for c in (catalog if isinstance(catalog, list) else [catalog])]
    max_price = raw.get("max_price")
    suspicious = _num(raw.get("suspicious_below", 0), f"{name}: suspicious_below")
    min_price = _num(raw.get("min_price", 0), f"{name}: min_price")

    brands = []
    for label, spec in (raw.get("brands") or {}).items():
        if spec is None or spec is False or (isinstance(spec, str) and fold(spec) in ("ignorera", "ignore")):
            # T.ex. "Thomas Burberry: ignorera" – matchar annars "Burberry" men är en billig sidolinje.
            brands.append((tuple(words(str(label))), BrandRule(str(label), 0.0, 0.0, skip=True)))
            continue
        spec = spec if isinstance(spec, dict) else {"resale": spec}
        mp = spec.get("max_price", max_price)
        if mp is None:
            raise ConfigError(f"{name}: märket '{label}' saknar max_price (sätt det på märket eller sökningen)")
        rule = BrandRule(
            label=str(label),
            resale=_num(spec.get("resale"), f"{name}/{label}: resale"),
            max_price=_num(mp, f"{name}/{label}: max_price"),
            suspicious_below=_num(spec.get("suspicious_below", suspicious), f"{name}/{label}: suspicious_below"),
            min_price=_num(spec.get("min_price", min_price), f"{name}/{label}: min_price"),
        )
        brands.append((tuple(words(str(label))), rule))
    # Längst namn först, så att t.ex. "polo ralph lauren" vinner över "ralph lauren".
    brands.sort(key=lambda kv: len(kv[0]), reverse=True)

    other = None
    if raw.get("resale") is not None:
        if max_price is None:
            raise ConfigError(f"{name}: 'resale' för övriga märken kräver 'max_price'")
        other = BrandRule("övriga märken", _num(raw["resale"], f"{name}: resale"),
                          _num(max_price, f"{name}: max_price"), suspicious, min_price)
    if not any(not r.skip for _, r in brands) and other is None:
        raise ConfigError(f"{name}: ange 'brands' och/eller 'resale'")

    premium = {**(cfg.get("premium_keywords") or {}), **(raw.get("premium_keywords") or {})}
    exclude = list(cfg.get("exclude_keywords") or []) + list(raw.get("exclude_keywords") or [])
    return Search(
        name=name,
        query=str(raw.get("query") or "").strip(),
        catalog_ids=catalog,
        min_profit=_num(raw.get("min_profit", cfg["min_profit"]), f"{name}: min_profit"),
        min_roi_percent=_num(raw.get("min_roi_percent", cfg["min_roi_percent"]), f"{name}: min_roi_percent"),
        brands=brands,
        other_brands=other,
        sizes={fold(str(s)) for s in raw.get("sizes") or []},
        exclude=[(str(k), keyword_re(str(k))) for k in exclude],
        premium=[(str(k), keyword_re(str(k)), _num(v, f"premium_keywords/{k}")) for k, v in premium.items()],
        tags=[str(tag) for tag in (raw.get("tags") or [])],
    )


def load_config(path: Path) -> tuple[dict, list[Search]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"Hittar inte konfigurationsfilen {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"Fel i {path.name}: {e}") from None
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "searches"}}
    cfg["condition_multipliers"] = {
        fold(k): float(v)
        for k, v in {**DEFAULTS["condition_multipliers"], **(raw.get("condition_multipliers") or {})}.items()
    }
    searches = [build_search(s, cfg) for s in raw.get("searches") or [] if s.get("enabled", True)]
    if not searches:
        raise ConfigError(f"{path.name} innehåller inga aktiva sökningar")
    names = [s.name for s in searches]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"Sökningarnas namn måste vara unika: {', '.join(sorted(dupes))}")
    return cfg, searches


def load_dotenv(path: Path) -> None:
    """Läser KEY=VALUE-rader från .env. Ifyllda värden gäller före miljövariabler med samma namn."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("\"'")
            if value:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)


# --------------------------------------------------------------------------
# Tolkning av Vinteds katalogsida
# --------------------------------------------------------------------------
#
# Vinteds katalogsida är en Next.js-app. Sökresultaten skickas med i sidans
# "flight data" som JSON-objekt av formen {"id":…,"productItem":{…}}. Med
# headern "RSC: 1" fås enbart den datan (utan HTML); annars ligger den
# JSON-kodad i <script>self.__next_f.push([1,"…"])</script>-taggar.

_NEXT_F = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_LIST_MARK = '"items":{"items":'
_ITEM_START = re.compile(r'\{"id":\d+,"productItem":')
KNOWN_CONDITIONS = {fold(c) for c in DEFAULTS["condition_multipliers"]} | {"godtagbar"}


def _txt(value) -> str:
    if value is None or (isinstance(value, str) and value.startswith("$undefined")):
        return ""
    return str(value)


def _amount(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def buyer_fee(price: float) -> float:
    """Vinteds köparskydd i Sverige: 5 % + 7,50 kr."""
    return round(price * 0.05 + 7.5, 2)


def split_second_line(line: str) -> tuple[str, str]:
    """"46 | W29 · Mycket bra" -> ("46 | W29", "Mycket bra")."""
    parts = [p.strip() for p in line.split("·") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return ("", parts[0]) if fold(parts[0]) in KNOWN_CONDITIONS else (parts[0], "")
    return " · ".join(parts[:-1]), parts[-1]


def _to_item(p: dict, base_url: str) -> Item | None:
    try:
        item_id = int(p.get("id"))
    except (TypeError, ValueError):
        return None
    price = _amount(p.get("price"))
    if price is None:
        return None
    total = _amount(p.get("totalItemPrice"))
    if total is None:
        fee = _amount(p.get("serviceFee"))
        total = price + (fee if fee is not None else buyer_fee(price))
    box = p.get("itemBox") if isinstance(p.get("itemBox"), dict) else {}
    size, condition = split_second_line(_txt(box.get("secondLine")))
    photos = p.get("photos") if isinstance(p.get("photos"), list) else []
    photo = next((ph["url"] for ph in photos if isinstance(ph, dict) and ph.get("url")), None)
    url = _txt(p.get("url")) or f"/items/{item_id}"
    price_obj = p.get("price")
    user = p.get("user") if isinstance(p.get("user"), dict) else {}
    return Item(
        id=item_id,
        title=_txt(p.get("title")),
        brand=_txt(box.get("firstLine")),
        size=size,
        condition=condition,
        price=price,
        total=total,
        currency=_txt(price_obj.get("currencyCode")) if isinstance(price_obj, dict) else "",
        url=base_url + url if url.startswith("/") else url,
        photo=photo or _txt(p.get("thumbnailUrl")) or None,
        is_business=bool(user.get("isBusiness")),
    )


def parse_items(body: str, base_url: str) -> list[Item] | None:
    """Plockar ut annonserna ur katalogsidan. None betyder att formatet inte känns igen."""
    if "self.__next_f.push" in body:
        body = "".join(json.loads(m.group(1)) for m in _NEXT_F.finditer(body))
    decoder = json.JSONDecoder()
    raw: list[dict] = []
    mark = body.find(_LIST_MARK)
    if mark != -1:
        try:
            entries, _ = decoder.raw_decode(body, mark + len(_LIST_MARK))
            raw = [e["productItem"] for e in entries
                   if isinstance(e, dict) and isinstance(e.get("productItem"), dict)]
        except (ValueError, TypeError):
            raw = []
    if not raw:
        for m in _ITEM_START.finditer(body):
            try:
                obj, _ = decoder.raw_decode(body, m.start())
            except ValueError:
                continue
            if isinstance(obj.get("productItem"), dict):
                raw.append(obj["productItem"])
        if not raw and mark == -1:
            return None
    items, seen = [], set()
    for p in raw:
        item = _to_item(p, base_url)
        if item and item.id not in seen:
            seen.add(item.id)
            items.append(item)
    return items


# --------------------------------------------------------------------------
# Vinted-klient
# --------------------------------------------------------------------------

class VintedClient:
    def __init__(self, domain: str, currency: str, impersonate: str = "chrome", debug_dir: Path | None = None):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            raise SystemExit("Paketet curl_cffi saknas – kör: pip install -r requirements.txt") from None
        self._http = curl_requests
        self.base = f"https://www.{domain}"
        self.currency = currency
        self.impersonate = impersonate
        self.debug_dir = debug_dir
        self.retries = 2                # nya försök med ny session när Vinted nekar
        self.retry_wait = (10.0, 20.0)  # sekunder, gånger försöksnumret
        self.timeout = 30.0
        self._new_session()

    def _new_session(self) -> None:
        # curl_cffi härmar Chromes TLS-fingeravtryck; vanliga requests stoppas av Cloudflare.
        self.session = self._http.Session(impersonate=self.impersonate)
        self.session.headers.update({"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"})

    def params(self, search: Search, page: int, price_cap: bool) -> list[tuple[str, str]]:
        params = [("search_text", search.query)] if search.query else []
        params += [("catalog[]", str(c)) for c in search.catalog_ids]
        if price_cap:
            if search.price_floor() > 0:
                params.append(("price_from", f"{search.price_floor():g}"))
            params.append(("price_to", f"{search.price_cap():g}"))
        params += [("currency", self.currency), ("order", "newest_first")]
        if page > 1:
            params.append(("page", str(page)))
        return params

    def fetch(self, search: Search, page: int = 1, price_cap: bool = True, retries: int | None = None) -> list[Item]:
        params = self.params(search, page, price_cap)
        retries = self.retries if retries is None else retries
        for attempt in range(retries + 1):
            try:
                return self._fetch_once(search, page, params)
            except Blocked as e:
                if attempt == retries:
                    raise
                wait = random.uniform(*self.retry_wait) * (attempt + 1)
                log.debug("%s: nekad (%s) – försöker igen med ny session om %.0f s", search.name, e, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _fetch_once(self, search: Search, page: int, params: list[tuple[str, str]]) -> list[Item]:
        for rsc in (True, False):
            headers = {"RSC": "1", "Accept": "*/*"} if rsc else {"Accept": "text/html,application/xhtml+xml"}
            try:
                resp = self.session.get(self.base + "/catalog", params=params, headers=headers, timeout=self.timeout)
            except Exception as e:  # nätverksfel, timeout …
                raise FetchError(f"nätverksfel: {e}") from None
            if resp.status_code in (401, 403, 429):
                self._new_session()
                raise Blocked(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise FetchError(f"HTTP {resp.status_code}")
            if self.debug_dir:
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                name = re.sub(r"\W+", "_", search.name)
                (self.debug_dir / f"{name}_p{page}_{'rsc' if rsc else 'html'}.txt").write_text(resp.text, encoding="utf-8")
            items = parse_items(resp.text, self.base)
            if items is not None:
                return items
        raise FetchError("kunde inte tolka svaret – Vinted kan ha ändrat sidan (kör med --debug-dir för att spara svaret)")


# --------------------------------------------------------------------------
# Värdering
# --------------------------------------------------------------------------

def evaluate(item: Item, search: Search, cfg: dict) -> tuple[Deal | None, str]:
    """Returnerar (Deal, "ok") om annonsen är lönsam, annars (None, skäl)."""
    rule = search.rule_for(item.brand)
    if rule is None:
        return None, f"märket '{item.brand or '?'}' bevakas inte här"
    if rule.skip:
        return None, f"märket '{item.brand}' är undantaget ({rule.label})"
    if not rule.min_price <= item.price <= rule.max_price:
        return None, f"pris {item.price:.0f} utanför {rule.min_price:.0f}–{rule.max_price:.0f}"
    text = fold(item.title)
    for keyword, pattern in search.exclude:
        if pattern.search(text):
            return None, f"innehåller '{keyword}'"
    if search.sizes and item.size and not search.sizes & set(words(item.size)):
        return None, f"storlek {item.size}"

    multiplier = cfg["condition_multipliers"].get(fold(item.condition), 1.0)
    bonuses = [(kw, amount) for kw, pattern, amount in search.premium if pattern.search(text)]
    resale = rule.resale * multiplier + sum(amount for _, amount in bonuses)
    cost = item.total + float(cfg["shipping_estimate"])
    profit = resale * (1 - float(cfg["sell_fee_percent"]) / 100) - cost
    roi = profit / cost if cost > 0 else 0.0
    if profit < search.min_profit:
        return None, f"vinst {profit:.0f} < {search.min_profit:.0f}"
    if roi * 100 < search.min_roi_percent:
        return None, f"ROI {roi:.0%} < {search.min_roi_percent:.0f} %"
    return Deal(
        item=item,
        search=search.name,
        resale=resale,
        cost=cost,
        profit=profit,
        roi=roi,
        bonuses=[f"{kw} ({amount:+.0f})" for kw, amount in bonuses],
        suspicious=item.price < rule.suspicious_below,
        hashtags=[tag for tag in dict.fromkeys(
            [hashtag(item.brand if rule is search.other_brands else rule.label)]
            + [hashtag(t) for t in search.tags]) if tag],
    ), "ok"


# --------------------------------------------------------------------------
# Notiser
# --------------------------------------------------------------------------

@dataclass
class Message:
    title: str
    lines: list[str]
    url: str
    photo: str | None = None


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def deal_message(deal: Deal, cfg: dict) -> Message:
    it = deal.item
    cur = "kr" if it.currency in ("SEK", "") else it.currency
    icon = "📉" if deal.price_drop_from else ("⚠️" if deal.suspicious else "🔥")
    title = f"{icon} {it.brand or 'Okänt märke'} – {money(it.price)} {cur} (≈ +{money(deal.profit)} {cur})"
    lines = [it.title]
    if deal.price_drop_from:
        lines.append(f"Prissänkt från {money(deal.price_drop_from)} {cur}")
    lines += [
        f"💰 {money(it.price)} {cur} · {money(it.total)} {cur} inkl. köparskydd + ~{money(cfg['shipping_estimate'])} {cur} frakt",
        f"📈 Säljvärde ≈ {money(deal.resale)} {cur} → vinst ≈ {money(deal.profit)} {cur} ({deal.roi:.0%})",
        f"📏 {it.size or '–'} · {it.condition or 'okänt skick'}",
    ]
    if deal.bonuses:
        lines.append("✨ " + ", ".join(deal.bonuses))
    if deal.suspicious:
        lines.append("⚠️ Misstänkt lågt pris för märket – kolla äktheten (etiketter, sömmar, QR-kod/hologram) innan du köper.")
    if it.is_business:
        lines.append("🏪 Företagssäljare")
    lines.append(f"🔎 {deal.search}")
    if deal.hashtags:
        lines.append("🏷 " + " ".join(deal.hashtags))  # tryck på en hashtagg i Telegram för att se alla med samma
    return Message(title, lines, it.url, it.photo)


class NotifyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def description(self) -> str:
        """Telegrams förklaring, t.ex. "Bad Request: not enough rights to send text messages to the chat"."""
        try:
            return str(json.loads(self.detail).get("description") or self.detail)
        except (ValueError, AttributeError):
            return self.detail

    @property
    def is_rights_problem(self) -> bool:
        """Boten får inte skriva i chatten (inte fel i själva meddelandet, och inte en tillfällig spärr)."""
        if self.status == 403:
            return True
        text = self.description.lower()
        return self.status == 400 and any(s in text for s in (
            "not enough rights", "need administrator rights", "have no rights", "chat_write_forbidden",
            "chat not found", "bot is not a member", "bot was kicked", "topic_closed"))

    @property
    def migrate_to_chat_id(self) -> str | None:
        """Telegram svarar så här när en grupp har gjorts om till supergrupp (nytt chat-id)."""
        try:
            new_id = json.loads(self.detail).get("parameters", {}).get("migrate_to_chat_id")
        except (ValueError, AttributeError):
            return None
        return str(new_id) if new_id else None


def http_post_json(url: str, payload: dict, timeout: float = 20):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "vinted-bevakare/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        # Felet ska inte innehålla URL:en – för Telegram står token i den.
        raise NotifyError(e.code, e.read().decode("utf-8", "replace")[:300]) from None
    return json.loads(body) if body else None


class TelegramNotifier:
    name = "Telegram"

    def __init__(self, token: str, chat_id: str, on_migrate=None, fallback_chat: str | None = None,
                 hint_gate=None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.on_migrate = on_migrate  # anropas med nytt chat-id när en grupp blivit supergrupp
        self.fallback_chat = fallback_chat  # ägarens privata chatt, om boten inte får skriva i chat_id
        self.hint_gate = hint_gate or (lambda: True)  # avgör om förklaringen till ägaren får skickas nu
        self._warned = False

    def _post(self, method: str, payload: dict, chat_id: str | None = None) -> None:
        try:
            http_post_json(f"{self.api}/{method}", {"chat_id": chat_id or self.chat_id, **payload})
        except NotifyError as e:
            if chat_id or not (new_id := e.migrate_to_chat_id):
                raise
            log.info("Telegram: gruppen har blivit en supergrupp – nytt chat-id %s", "(dolt)" if QUIET else new_id)
            self.chat_id = new_id
            if self.on_migrate:
                self.on_migrate(new_id)
            http_post_json(f"{self.api}/{method}", {"chat_id": self.chat_id, **payload})

    def _deliver(self, msg: Message, chat_id: str | None = None) -> None:
        text = f"<b>{escape(msg.title)}</b>\n" + "\n".join(escape(line) for line in msg.lines)
        markup = {"inline_keyboard": [[{"text": "Öppna på Vinted", "url": msg.url}]]}
        if msg.photo and len(text) <= 1024:  # bildtexter får vara max 1024 tecken
            try:
                self._post("sendPhoto", {"photo": msg.photo, "caption": text, "parse_mode": "HTML",
                                         "reply_markup": markup}, chat_id)
                return
            except NotifyError as e:
                if e.status in (401, 403, 404, 429):  # 429: för många meddelanden – vänta till nästa körning
                    raise
                log.debug("Telegram tog inte emot bilden (%s) – skickar som text", e)
        self._post("sendMessage", {"text": text, "parse_mode": "HTML", "reply_markup": markup}, chat_id)

    def send(self, msg: Message) -> None:
        try:
            self._deliver(msg)
        except NotifyError as e:
            # Får boten inte skriva i gruppen/kanalen: skicka till ägarens privata chatt i stället.
            if not e.is_rights_problem or not self.fallback_chat or self.fallback_chat == self.chat_id:
                raise
            if not self._warned:
                self._warned = True
                log.warning("Telegram: får inte skriva i %s (%s) – skickar till din privata chatt",
                            who("chatten", self.chat_id), e.description)
                if self.hint_gate():
                    try:
                        self._deliver(rights_hint(e.description, msg.url), self.fallback_chat)
                    except Exception as hint_error:
                        log.warning("Telegram: kunde inte skicka förklaringen: %s", hint_error)
            self._deliver(msg, self.fallback_chat)


def daily_gate(store: "Store | None", key: str, period: int = 86400):
    """Släpper igenom högst en gång per period (sparas i databasen), så att ägaren inte tjatas på."""
    def gate() -> bool:
        if not store:
            return True
        now = int(time.time())
        last = int(store.get_setting(key) or 0)
        if now - last < period:
            return False
        store.set_setting(key, str(now))
        return True
    return gate


def rights_hint(description: str, url: str) -> Message:
    return Message("⚠️ Jag kan inte skriva i gruppen", [
        f"Telegram säger: {description}",
        "Fynden skickas hit till dig tills det är fixat.",
        "Gör mig till administratör i gruppen (eller kanalen) med rätten att skicka meddelanden: "
        "tryck på gruppens namn › pennan › Administratörer › Lägg till administratör.",
    ], url)


class DiscordNotifier:
    name = "Discord"

    def __init__(self, webhook: str):
        self.webhook = webhook

    def send(self, msg: Message) -> None:
        embed = {"title": msg.title[:256], "url": msg.url, "description": "\n".join(msg.lines)[:4000], "color": 0x09B1BA}
        if msg.photo:
            embed["image"] = {"url": msg.photo}
        http_post_json(self.webhook, {"embeds": [embed]})


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.topic = topic
        self.server = server.rstrip("/")

    def send(self, msg: Message) -> None:
        payload = {"topic": self.topic, "title": msg.title, "message": "\n".join(msg.lines),
                   "click": msg.url, "priority": 4, "tags": ["moneybag"]}
        if msg.photo:
            payload["attach"] = msg.photo
        http_post_json(self.server + "/", payload)


def build_notifiers(cfg: dict, store: "Store | None" = None) -> list:
    conf = cfg.get("notify") or {}
    tg = conf.get("telegram") or {}

    def setting(env: str, value) -> str:
        return os.environ.get(env) or (str(value).strip() if value else "")

    notifiers = []
    raw_token, chat = setting("TELEGRAM_BOT_TOKEN", tg.get("bot_token")), setting("TELEGRAM_CHAT_ID", tg.get("chat_id"))
    token = clean_token(raw_token)
    if raw_token and not token:
        log.warning("TELEGRAM_BOT_TOKEN ser inte ut som en token från @BotFather – kör: python vinted_bot.py --telegram")
    elif token:
        fixed_chat = chat.strip().strip("\"'<>")
        chat = fixed_chat or discover_telegram_chat(token, store, cfg)
        if chat:
            remember, fallback = None, None
            if store and not fixed_chat:
                remember = lambda new_id: store.set_setting(telegram_chat_key(token), new_id)  # noqa: E731
                fallback = store.get_setting(telegram_chat_key(token) + ":private") or None
            notifiers.append(TelegramNotifier(token, chat, on_migrate=remember, fallback_chat=fallback,
                                              hint_gate=daily_gate(store, telegram_chat_key(token) + ":hint")))
    if webhook := setting("DISCORD_WEBHOOK_URL", conf.get("discord_webhook")):
        notifiers.append(DiscordNotifier(webhook))
    if topic := setting("NTFY_TOPIC", conf.get("ntfy_topic")):
        notifiers.append(NtfyNotifier(topic, setting("NTFY_SERVER", conf.get("ntfy_server")) or "https://ntfy.sh"))
    return notifiers


def print_message(msg: Message) -> None:
    print(f"\n{msg.title}")
    for line in msg.lines:
        print(f"   {line}")
    print(f"   {msg.url}", flush=True)


# --------------------------------------------------------------------------
# Koppla Telegram (--telegram)
# --------------------------------------------------------------------------

# En token från @BotFather: botens id, kolon, ~35 tecken. Exempel: 123456789:AAHk3…
_TOKEN_RE = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}")


def clean_token(raw: str | None) -> str | None:
    """Plockar ut token ur det som klistrats in – även med <>, citattecken, "bot" före eller hela länken."""
    m = _TOKEN_RE.search(urllib.parse.unquote(raw or ""))  # en kopierad länk kan ha %20, %3C …
    return m.group(0) if m else None


def mask_token(token: str) -> str:
    return f"{token[:6]}…{token[-4:]}"


class TelegramError(Exception):
    def __init__(self, code, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


def telegram_api(token: str, method: str, params: dict | None = None, timeout: float = 35):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "vinted-bevakare/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except ValueError:
            data = {"ok": False, "error_code": e.code, "description": e.reason}
    except (OSError, http.client.HTTPException) as e:  # URLError, timeout, avbruten anslutning …
        raise TelegramError(None, f"kunde inte nå api.telegram.org ({getattr(e, 'reason', e)})") from None
    if not data.get("ok"):
        raise TelegramError(data.get("error_code"), str(data.get("description", "")))
    return data["result"]


def chats_from_updates(updates: list) -> list[dict]:
    """Unika chattar i uppdateringarna, den senaste sist."""
    chats: dict = {}
    for update in updates:
        for key in ("message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            if key == "my_chat_member" and (update[key].get("new_chat_member") or {}).get("status") in ("kicked", "left"):
                chats.pop(chat["id"], None)  # boten är blockerad eller borttagen där
                continue
            chats.pop(chat["id"], None)
            chats[chat["id"]] = {"id": chat["id"], "name": chat_name(chat), "type": chat.get("type", "")}
    return list(chats.values())


def chat_name(chat: dict) -> str:
    return (chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("username") or str(chat.get("id", "")))


_SWITCH_COMMANDS = ("/start", "/vinted")


@dataclass
class ChatEvent:
    """En händelse i Telegram som kan flytta vart fynden skickas."""
    kind: str      # "switch" (/start), "added" (boten lades till), "removed", "migrated"
    when: tuple    # (datum, update_id) – datum först, eftersom update_id kan börja om efter en tyst vecka
    chat: dict
    user_id: int | None = None
    new_chat_id: str | None = None


def telegram_events(updates: list) -> list[ChatEvent]:
    events = []
    for update in updates:
        uid = update.get("update_id", 0)
        member = update.get("my_chat_member")
        if isinstance(member, dict) and "id" in (member.get("chat") or {}):
            new = (member.get("new_chat_member") or {}).get("status")
            old = (member.get("old_chat_member") or {}).get("status")
            user_id = (member.get("from") or {}).get("id")
            when = (member.get("date", 0), uid)
            if new in ("member", "administrator") and old in (None, "left", "kicked"):
                events.append(ChatEvent("added", when, member["chat"], user_id))
            elif new in ("left", "kicked"):
                events.append(ChatEvent("removed", when, member["chat"], user_id))
        message = update.get("message")  # inte channel_post: där syns inte vem som skrev
        if isinstance(message, dict) and "id" in (message.get("chat") or {}):
            when = (message.get("date", 0), uid)
            if message.get("migrate_to_chat_id"):
                events.append(ChatEvent("migrated", when, message["chat"],
                                        new_chat_id=str(message["migrate_to_chat_id"])))
            text = (message.get("text") or "").strip().lower()
            if any(text == c or text.startswith((c + "@", c + " ")) for c in _SWITCH_COMMANDS):
                events.append(ChatEvent("switch", when, message["chat"], (message.get("from") or {}).get("id")))
    return sorted(events, key=lambda e: e.when)


def telegram_chat_key(token: str) -> str:
    return f"telegram_chat:{token.split(':')[0]}"


def discover_telegram_chat(token: str, store: "Store | None", cfg: dict, api=None) -> str | None:
    """Vart fynden ska skickas när TELEGRAM_CHAT_ID inte är satt. Gör att det räcker att lägga in token,
    t.ex. som hemlighet i GitHub Actions.

    - Första gången: din privata chatt med boten (du blir då dess ägare).
    - Lägger du till boten i en grupp, eller skriver /start i en chatt, byter den dit. Bara ägaren
      kan byta – andra som trycker Starta eller lägger till boten i sina grupper påverkar inget.
    - Tas boten bort ur chatten den skickar till går den tillbaka till din privata chatt.
    Valet sparas i databasen och gäller tills något av ovanstående händer igen.
    """
    api = api or telegram_api
    key = telegram_chat_key(token)
    get = (lambda k: store.get_setting(key + k) or None) if store else (lambda k: None)
    saved = get("")
    owner = get(":owner")
    private = get(":private")
    if saved and not owner and not saved.startswith("-"):
        owner = private = saved  # sparad av en äldre version: privat chatt-id = ägarens användar-id
    try:
        updates = api(token, "getUpdates", {"timeout": 0}, timeout=20) or []
    except TelegramError as e:
        log.warning("Telegram: %s", e)
        return saved
    seen_raw = get(":seen")
    seen = tuple(int(x) for x in seen_raw.split(":")) if seen_raw else (0, 0)
    events = [e for e in telegram_events(updates) if e.when > seen]

    target = saved
    if not owner:
        if saved:
            # Grupp sparad av en äldre version: bara den som lade till boten i just den gruppen blir ägare.
            adders = [e for e in events if e.kind == "added" and str(e.chat["id"]) == saved and e.user_id]
            owner = str(adders[0].user_id) if adders else None
        else:
            owner, private = first_contact(updates, events)
            target = private
    if not target and not owner:
        chats = chats_from_updates(updates)  # ingen ägare att gå på, t.ex. --test-notis utan databas
        if chats:
            target = str(([c for c in chats if c["type"] == "private"] or chats)[-1]["id"])
        else:
            log.warning("Telegram: hittar inget chat-id. Skriv ett meddelande (t.ex. hej) till din bot, "
                        "så kopplas den nästa gång boten körs.")
            return None

    moved = False  # ägaren (eller en borttagning) har flyttat fynden – då skickas en välkomsthälsning
    for event in events:
        chat_id = str(event.chat["id"])
        if event.kind == "migrated" and chat_id == target:
            target = event.new_chat_id
        elif event.kind in ("switch", "added") and owner and str(event.user_id) == owner:
            moved, target = moved or target != chat_id, chat_id
            if event.chat.get("type") == "private":
                private = chat_id
        elif event.kind == "removed":
            if chat_id == private:
                private = None  # ägaren har blockerat boten privat
            if chat_id == target:
                moved, target = True, private
    if events:
        seen = max(seen, events[-1].when)

    if store:
        for suffix, value in (("", target), (":owner", owner), (":private", private)):
            store.set_setting(key + suffix, value or "")
        if seen != (0, 0):
            store.set_setting(key + ":seen", f"{seen[0]}:{seen[1]}")
    if not target:
        log.warning("Telegram: boten är borttagen ur chatten den skickade till. Skriv /start till boten "
                    "eller lägg till den i en grupp, så kopplas den nästa gång boten körs.")
        return None
    if target != saved:
        name = next((chat_name(e.chat) for e in reversed(events)
                     if target in (str(e.chat["id"]), e.new_chat_id)), target)
        if saved and not moved:
            log.info("Telegram: %s har blivit en supergrupp", who(name, target))
            return target
        log.info("Telegram: skickar fynden till %s", who(name, target))
        if store:
            url = f"https://www.{cfg['domain']}/"
            try:
                TelegramNotifier(token, target).send(Message(
                    "✅ Vinted-bevakaren är kopplad hit!", ["Här kommer fynden att dyka upp."], url))
            except NotifyError as e:
                log.warning("Telegram: kunde inte skicka välkomstmeddelandet: %s", e)
                if e.is_rights_problem and private and private != target and daily_gate(store, key + ":hint")():
                    try:
                        TelegramNotifier(token, private).send(rights_hint(e.description, url))
                    except Exception as e2:
                        log.warning("Telegram: kunde inte heller skriva i din privata chatt: %s", e2)
            except Exception as e:
                log.warning("Telegram: kunde inte skicka välkomstmeddelandet: %s", e)
    return target


def first_contact(updates: list, events: list[ChatEvent]) -> tuple[str | None, str | None]:
    """(ägare, privat chatt) vid första kopplingen: den som först skrev till boten privat eller lade till
    den i en grupp. Den som var först gäller, inte den som var senast."""
    contacts = [(e.when, str(e.user_id), None) for e in events if e.kind == "added" and e.user_id]
    for update in updates:
        message = update.get("message")
        if isinstance(message, dict) and (message.get("chat") or {}).get("type") == "private":
            chat_id = str(message["chat"]["id"])
            contacts.append(((message.get("date", 0), update.get("update_id", 0)), chat_id, chat_id))
    if not contacts:
        return None, None
    contacts.sort(key=lambda c: c[0])
    _, owner, private = contacts[0]
    if len({c[1] for c in contacts}) > 1:
        log.warning("Telegram: flera personer har kontaktat boten – den som var först (id %s) räknas som ägare.",
                    "dolt" if QUIET else owner)
    return owner, private


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Sätter KEY=VALUE i .env – ersätter befintliga rader, behåller allt annat."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in values:
            lines[i] = f"{key}={values[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask_pasted(prompt: str) -> str:
    """input() som även läser resten av en inklistring med flera rader (t.ex. hela svaret från BotFather).

    Annars blir de övriga raderna kvar och körs som kommandon i terminalen när programmet avslutas.
    """
    text = input(prompt)
    try:
        import select
        while select.select([sys.stdin], [], [], 0.3)[0]:
            line = sys.stdin.readline()
            if not line:
                break
            text += "\n" + line.rstrip("\n")
    except (ImportError, OSError, ValueError):
        pass  # t.ex. Windows, där select inte fungerar på konsolen
    return text


def explain_telegram_error(e: TelegramError) -> str:
    if e.code is None:
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in e.description and sys.platform == "darwin":
            hint = ("\nPå Mac: öppna Program › Python 3.x och dubbelklicka på \"Install Certificates.command\", "
                    "kör sedan kommandot igen.")
        return f"Kunde inte nå Telegram: {e.description}. Kontrollera internetanslutningen.{hint}"
    if e.code == 409:
        return ("Telegram lämnar inte ut meddelanden till den här boten eftersom den har en webhook eller\n"
                "redan körs i ett annat program. Skapa en ny bot hos @BotFather och kör kommandot igen.")
    return f"Telegram svarade: {e}"


def telegram_setup(cfg: dict, env_path: Path, ask=ask_pasted, api=telegram_api, wait_seconds: float = 180) -> int:
    """Kontrollerar token, hittar ditt chat-id, sparar båda i .env och skickar ett testmeddelande."""
    tg = (cfg.get("notify") or {}).get("telegram") or {}

    def check(token: str):
        try:
            return api(token, "getMe")
        except TelegramError as e:
            if e.code not in (401, 404):
                raise
            print(f"Telegram känner inte igen token {mask_token(token)} ({e.description}).\n"
                  "Kopiera den igen hos @BotFather (/mybots → din bot → API Token) – hela, utan mellanslag.")
            return None

    def ask_or_empty(prompt: str) -> str:
        try:
            return ask(prompt)
        except EOFError:
            print()
            return ""

    token = clean_token(os.environ.get("TELEGRAM_BOT_TOKEN") or str(tg.get("bot_token") or ""))
    bot = None
    try:
        if token and (bot := check(token)):
            reply = ask_or_empty(f"Hittade en sparad token för @{bot.get('username')}. "
                                 "Tryck Enter för att använda den, eller klistra in en ny: ")
            if (new := clean_token(reply)) and new != token:
                token, bot = new, check(new)
        for _ in range(5):
            if bot:
                break
            raw = ask_or_empty("Klistra in din token från @BotFather (ser ut som 123456789:AAH…): ")
            if not raw.strip():  # tomt svar eller Ctrl+D = avbryt
                return 1
            token = clean_token(raw)
            if not token:
                print("Det där ser inte ut som en token. Den består av siffror, ett kolon och ~35 tecken.\n"
                      "Hämta den igen hos @BotFather: /mybots → välj din bot → API Token.")
                continue
            bot = check(token)
    except TelegramError as e:
        print(explain_telegram_error(e))
        return 1
    if not bot:
        return 1
    username = bot.get("username", "din bot")
    print(f"✔ Token fungerar – boten heter @{username}")

    try:
        updates = api(token, "getUpdates", {"timeout": 0})
        chats = chats_from_updates(updates)
        if not chats:
            print(f"Öppna Telegram, sök upp @{username}, tryck Starta/Start och skriv ett meddelande (t.ex. hej).\n"
                  f"Jag väntar i upp till {wait_seconds / 60:.0f} minuter …")
            deadline = time.monotonic() + wait_seconds
            while not chats and time.monotonic() < deadline:
                params = {"timeout": 25}
                if updates:  # bekräfta det vi redan sett, annars kommer samma uppdateringar direkt igen
                    params["offset"] = max(u.get("update_id", 0) for u in updates) + 1
                updates = api(token, "getUpdates", params, timeout=40) or []
                chats = chats_from_updates(updates)
    except TelegramError as e:
        print(explain_telegram_error(e))
        return 1
    if not chats:
        print(f"Fick inget meddelande. Skriv något till @{username} och kör kommandot igen.")
        return 1

    chat = chats[-1]
    if len(chats) > 1:
        print("Hittade flera chattar:")
        for i, c in enumerate(chats, 1):
            print(f"  {i}. {c['name']} ({c['type']}, id {c['id']})")
        choice = ask_or_empty(f"Vilken ska notiserna gå till? [{len(chats)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(chats):
            chat = chats[int(choice) - 1]
    print(f"✔ Chat-id: {chat['id']} ({chat['name']})")

    update_env_file(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat["id"])})
    os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = token, str(chat["id"])
    print(f"✔ Sparat i {env_path}")
    try:
        TelegramNotifier(token, str(chat["id"])).send(Message(
            "✅ Vinted-bevakaren är kopplad hit!", ["Här kommer fynden att dyka upp."], f"https://www.{cfg['domain']}/"))
    except Exception as e:
        print(f"Kunde inte skicka testmeddelandet: {e}")
        return 1
    print("✔ Testmeddelande skickat – kolla Telegram. Starta bevakningen med: python vinted_bot.py")
    return 0


# --------------------------------------------------------------------------
# Lagring av sedda annonser
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path))
        upgrading = self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'seen_by'").fetchone() is None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen (
                item_id INTEGER PRIMARY KEY, price REAL, first_seen INTEGER, last_seen INTEGER,
                notified INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS searches (name TEXT PRIMARY KEY, initialized INTEGER);
            CREATE TABLE IF NOT EXISTS deals (
                item_id INTEGER, ts INTEGER, search TEXT, brand TEXT, title TEXT, price REAL,
                total REAL, resale REAL, profit REAL, url TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, message TEXT);
            -- Varje sökning bedömer en annons själv första gången den ser den, så att en bred sökning
            -- (t.ex. alla fältjackor) inte "tar" annonsen innan märkets egen sökning hunnit bedöma den.
            CREATE TABLE IF NOT EXISTS seen_by (
                search TEXT, item_id INTEGER, price REAL, last_seen INTEGER,
                PRIMARY KEY (search, item_id)) WITHOUT ROWID;
        """)
        if upgrading:  # äldre databas: där räknades allt som setts som sett av alla sökningar
            self.db.execute("INSERT OR IGNORE INTO seen_by "
                            "SELECT s.name, seen.item_id, seen.price, seen.last_seen FROM searches s CROSS JOIN seen")
            self.db.commit()

    def add_pending(self, msg: "Message") -> None:
        self.db.execute("INSERT INTO pending (ts, message) VALUES (?, ?)",
                        (int(time.time()), json.dumps(asdict(msg), ensure_ascii=False)))
        self.db.commit()

    def pending(self, max_age: int = 86400) -> list[tuple[int, "Message"]]:
        """Notiser som inte kom fram, äldst först. Äldre än max_age sekunder slängs."""
        dropped = self.db.execute("DELETE FROM pending WHERE ts < ?", (int(time.time()) - max_age,)).rowcount
        self.db.commit()
        if dropped:
            log.warning("%d fynd kunde inte skickas på ett dygn och har slängts.", dropped)
        rows = self.db.execute("SELECT id, message FROM pending ORDER BY id").fetchall()
        return [(pid, Message(**json.loads(raw))) for pid, raw in rows]

    def delete_pending(self, pid: int) -> None:
        self.db.execute("DELETE FROM pending WHERE id = ?", (pid,))
        self.db.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        self.db.commit()

    def seen_price(self, item_id: int, search: str) -> float | None:
        """Priset när sökningen senast såg annonsen (None = ny för den här sökningen)."""
        row = self.db.execute("SELECT price FROM seen_by WHERE search = ? AND item_id = ?",
                              (search, item_id)).fetchone()
        return row[0] if row else None

    def was_notified(self, item_id: int) -> bool:
        row = self.db.execute("SELECT notified FROM seen WHERE item_id = ?", (item_id,)).fetchone()
        return bool(row and row[0])

    def mark_seen(self, item_id: int, price: float, search: str) -> None:
        now = int(time.time())
        self.db.execute(
            "INSERT INTO seen (item_id, price, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET price = excluded.price, last_seen = excluded.last_seen",
            (item_id, price, now, now))
        self.db.execute("INSERT OR REPLACE INTO seen_by VALUES (?, ?, ?, ?)", (search, item_id, price, now))
        self.db.commit()

    def log_deal(self, deal: Deal) -> None:
        it = deal.item
        self.db.execute("UPDATE seen SET notified = 1 WHERE item_id = ?", (it.id,))
        self.db.execute("INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (it.id, int(time.time()), deal.search, it.brand, it.title, it.price,
                         it.total, deal.resale, deal.profit, it.url))
        self.db.commit()

    def is_initialized(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM searches WHERE name = ?", (name,)).fetchone() is not None

    def set_initialized(self, name: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO searches VALUES (?, ?)", (name, int(time.time())))
        self.db.commit()

    def prune(self, days: int = 90) -> None:
        cutoff = int(time.time()) - days * 86400
        self.db.execute("DELETE FROM seen WHERE last_seen < ?", (cutoff,))
        self.db.execute("DELETE FROM seen_by WHERE last_seen < ?", (cutoff,))
        self.db.commit()


# --------------------------------------------------------------------------
# Själva bevakningen
# --------------------------------------------------------------------------

class Bot:
    def __init__(self, cfg: dict, searches: list[Search], client, store: Store | None,
                 notifiers: list, dry_run: bool = False, time_budget: float | None = None,
                 queue_undelivered: bool = False):
        self.cfg = cfg
        self.searches = searches
        self.client = client
        self.store = store
        self.notifiers = notifiers
        self.dry_run = dry_run
        self.time_budget = time_budget
        # Köa fynd som inte kom fram även när ingen kanal gick att bygga just nu (t.ex. ingen Telegram-chatt
        # hittades), så länge någon kanal är inställd.
        self.queue_undelivered = queue_undelivered
        self.shown: set[int] = set()
        # Spärrar Vinted under en körning med tidsgräns: vänta så här länge och fortsätt (högst
        # block_pauses gånger per varv) i stället för att avsluta – om tidsgränsen räcker till det.
        self.block_cooldown = 0.0
        self.block_pauses = 2

    def _pause(self) -> None:
        low, high = self.cfg["delay_between_searches"]
        time.sleep(random.uniform(low, high))

    def run_search(self, search: Search) -> int:
        first_run = not self.dry_run and not self.store.is_initialized(search.name)
        silent = first_run and not self.cfg["notify_on_first_run"]
        found = 0
        for page in range(1, int(self.cfg["max_pages"]) + 1):
            if page > 1:
                self._pause()
            items = self.client.fetch(search, page)
            new = 0
            for item in items:
                prev = None if self.dry_run else self.store.seen_price(item.id, search.name)
                dropped = prev is not None and item.price < prev - 0.5
                if prev is not None and not dropped:
                    continue
                new += prev is None
                if not self.dry_run:
                    self.store.mark_seen(item.id, item.price, search.name)
                    if self.store.was_notified(item.id):
                        continue  # redan skickad, av den här eller en annan sökning
                if silent:
                    continue
                deal, reason = evaluate(item, search, self.cfg)
                if deal is None:
                    log.debug("  – %s | %s %.0f kr: %s", item.title[:50], item.brand, item.price, reason)
                    continue
                if self.dry_run:
                    if item.id in self.shown:
                        continue  # samma annons i två sökningar visas en gång
                    self.shown.add(item.id)
                if dropped:
                    deal.price_drop_from = prev
                self.notify(deal)
                found += 1
            # Var alla annonser på sidan nya kan fler ha tillkommit sedan förra varvet – bläddra vidare.
            if self.dry_run or silent or not items or new < len(items):
                break
        if first_run:
            self.store.set_initialized(search.name)
            if silent:
                log.info("%s: första körningen – befintliga annonser sparade, notiser kommer för nya", search.name)
        return found

    def notify(self, deal: Deal) -> None:
        msg = deal_message(deal, self.cfg)
        if not QUIET:
            print_message(msg)
        if self.dry_run:
            return
        self.store.log_deal(deal)
        if (self.notifiers or self.queue_undelivered) and not self._deliver(msg):
            self.store.add_pending(msg)  # skickas igen nästa körning

    def _deliver(self, msg: Message) -> bool:
        delivered = False
        for notifier in self.notifiers:
            try:
                notifier.send(msg)
                delivered = True
            except Exception as e:
                log.warning("Kunde inte skicka via %s: %s", notifier.name, e)
        return delivered

    def flush_pending(self) -> None:
        """Skickar notiser som inte kom fram vid en tidigare körning (högst ett dygn gamla)."""
        if self.dry_run or not self.store:
            return
        waiting = self.store.pending()
        if not self.notifiers:
            return
        if waiting:
            log.info("Skickar %d notis(er) som inte kom fram förra gången.", len(waiting))
        for pid, msg in waiting:
            if self._deliver(msg):
                self.store.delete_pending(pid)  # annars nytt försök nästa körning (högst ett dygn)

    def _run_search_or_wait(self, search: Search, started: float) -> int:
        while True:
            try:
                return self.run_search(search)
            except Blocked as e:
                left = self.time_budget - (time.monotonic() - started) if self.time_budget else 0.0
                if not self.block_cooldown or self._pauses_left <= 0 or left < self.block_cooldown + 30:
                    raise
                self._pauses_left -= 1
                log.info("Vinted nekade anropet (%s) – väntar %g s och fortsätter.", e, self.block_cooldown)
                time.sleep(self.block_cooldown)

    def run_cycle(self) -> int:
        """Ett varv genom sökningarna. Med tidsgräns avbryts varvet när tiden är slut, och nästa
        varv börjar där det här slutade – så att varje körning i molnet ryms inom en debiterad minut."""
        started = time.monotonic()  # tidsgränsen räknar även in fynd som skickas om
        self._pauses_left = self.block_pauses
        try:
            self.flush_pending()
        except Exception:
            log.exception("Kunde inte skicka om väntande fynd – fortsätter med sökningarna")
        n = len(self.searches)
        rotate = bool(self.time_budget and self.store)
        start_at = int(self.store.get_setting("next_search") or 0) % n if rotate else 0
        order = self.searches[start_at:] + self.searches[:start_at]
        total = done = 0
        try:
            for i, search in enumerate(order):
                if i:
                    if self.time_budget and time.monotonic() - started >= self.time_budget:
                        log.info("Tidsgränsen (%g s) nådd – %d sökningar tas först nästa körning.",
                                 self.time_budget, n - i)
                        break
                    self._pause()
                done = i + 1
                try:
                    found = self._run_search_or_wait(search, started)
                except Blocked:
                    done = i  # försök med samma sökning först nästa gång
                    raise
                except FetchError as e:
                    log.warning("%s: %s", search.name, e)
                    continue
                except Exception:
                    log.exception("%s: oväntat fel – hoppar över sökningen det här varvet", search.name)
                    continue
                total += found
                log.debug("%s: %d fynd", search.name, found)
        finally:
            if rotate:
                self.store.set_setting("next_search", str((start_at + done) % n))
        return total

    def run_forever(self) -> None:
        backoff = 0
        while True:
            try:
                found = self.run_cycle()
                backoff = 0
            except Blocked as e:
                backoff = min(max(backoff * 2, 60), 900)
                log.warning("Vinted nekade anropet (%s) – pausar %d s och försöker igen", e, backoff)
                time.sleep(backoff)
                continue
            wait = float(self.cfg["interval_seconds"]) * random.uniform(0.8, 1.2)
            log.info("Varv klart: %d fynd. Nästa sökning om %d s.", found, wait)
            time.sleep(wait)


def calibrate(searches: list[Search], client: VintedClient, cfg: dict) -> None:
    """Visar medianpriset på aktuella annonser per märke, som stöd för 'resale'-värdena."""
    print("Annonspriser just nu (utan maxpris). Faktiska försäljningspriser ligger ofta 10–20 % lägre.\n")
    for i, search in enumerate(searches):
        prices: dict[str, list[float]] = {}
        rules = {r.label: r for r in search.all_rules()}
        for page in (1, 2):
            if i or page > 1:
                time.sleep(random.uniform(*cfg["delay_between_searches"]))
            try:
                items = client.fetch(search, page, price_cap=False)
            except FetchError as e:
                print(f"{search.name}: {e}")
                break
            for item in items:
                rule = search.rule_for(item.brand)
                if rule:
                    prices.setdefault(rule.label, []).append(item.price)
        print(search.name)
        for label, rule in rules.items():
            ps = prices.get(label, [])
            if len(ps) < 5:
                print(f"   {label:<22} för få annonser ({len(ps)})")
                continue
            q1, med, q3 = statistics.quantiles(ps, n=4)
            print(f"   {label:<22} n={len(ps):<3} median {money(med):>6} kr  (mitten-50 %: {money(q1)}–{money(q3)})"
                  f"   resale i config: {money(rule.resale)}  max_price: {money(rule.max_price)}")
        print()


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Bevakar Vinted efter underprisade märkesplagg och skickar notiser.")
    ap.add_argument("-c", "--config", default=str(HERE / "config.yaml"), help="konfigurationsfil (standard: config.yaml)")
    ap.add_argument("--db", help="SQLite-fil för sedda annonser (standard: vinted_bot.db bredvid configen)")
    ap.add_argument("--once", action="store_true", help="kör ett varv och avsluta (t.ex. från cron)")
    ap.add_argument("--dry-run", action="store_true", help="visa vad som matchar just nu – skickar och sparar inget")
    ap.add_argument("--kalibrera", "--calibrate", dest="calibrate", action="store_true",
                    help="visa aktuella medianpriser per märke")
    ap.add_argument("--test-notis", "--test-notify", dest="test_notify", action="store_true",
                    help="skicka en testnotis till alla inställda kanaler")
    ap.add_argument("--telegram", action="store_true",
                    help="koppla Telegram: kollar token, hittar ditt chat-id och sparar båda i .env")
    ap.add_argument("--bara", "--only", dest="only", metavar="TEXT", help="kör bara sökningar vars namn innehåller TEXT")
    ap.add_argument("--paus", "--delay", dest="pause", nargs=2, type=float, metavar=("MIN", "MAX"),
                    help="sekunder att vänta mellan anropen till Vinted (ersätter delay_between_searches)")
    ap.add_argument("--max-tid", "--time-budget", dest="time_budget", type=float, metavar="SEK",
                    help="påbörja inga nya sökningar efter SEK sekunder; nästa körning fortsätter där den slutade")
    ap.add_argument("--debug-dir", help="spara Vinteds råa svar i den här mappen (felsökning)")
    ap.add_argument("-v", "--verbose", action="store_true", help="visa varför annonser sorteras bort")
    ap.add_argument("--tyst", "--quiet", dest="quiet", action="store_true",
                    help="skriv inte ut fynd, chattnamn eller chat-id (för publika loggar, t.ex. GitHub Actions)")
    args = ap.parse_args(argv)
    global QUIET
    QUIET = args.quiet

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    config_path = Path(args.config).resolve()
    load_dotenv(config_path.parent / ".env")
    try:
        cfg, searches = load_config(config_path)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    if args.pause:
        if not (all(math.isfinite(x) for x in args.pause) and 0 <= args.pause[0] <= args.pause[1]):
            log.error("--paus MIN MAX: ange två tal där 0 ≤ MIN ≤ MAX, t.ex. --paus 0.5 1.5")
            return 2
        cfg["delay_between_searches"] = list(args.pause)
    if args.time_budget is not None and not (math.isfinite(args.time_budget) and args.time_budget > 0):
        log.error("--max-tid SEK: ange ett positivt antal sekunder, t.ex. --max-tid 30")
        return 2
    if args.only:
        searches = [s for s in searches if fold(args.only) in fold(s.name)]
        if not searches:
            log.error("Ingen sökning matchar '%s'", args.only)
            return 2

    if args.telegram:
        try:
            return telegram_setup(cfg, config_path.parent / ".env")
        except KeyboardInterrupt:
            print("\nAvbrutet.")
            return 1

    if args.test_notify:
        notifiers = build_notifiers(cfg)
        if not notifiers:
            log.error("Inga notiskanaler inställda – se README.md (Telegram, Discord eller ntfy).")
            return 1
        msg = Message("✅ Testnotis från Vinted-bevakaren", ["Fungerar! Här kommer fynden att dyka upp."],
                      f"https://www.{cfg['domain']}/")
        ok = True
        for n in notifiers:
            try:
                n.send(msg)
                log.info("Skickade testnotis via %s", n.name)
            except Exception as e:
                ok = False
                log.error("%s: %s", n.name, e)
                if isinstance(n, TelegramNotifier) and isinstance(e, NotifyError) and e.status in (400, 401, 403, 404):
                    log.error("Kontrollera token och chat-id med: python vinted_bot.py --telegram")
        return 0 if ok else 1

    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    client = VintedClient(cfg["domain"], cfg["currency"], cfg["impersonate"], debug_dir)
    if args.calibrate:
        calibrate(searches, client, cfg)
        return 0

    if args.dry_run:
        bot = Bot(cfg, searches, client, None, [], dry_run=True, time_budget=args.time_budget)
        try:
            found = bot.run_cycle()
        except Blocked as e:
            log.error("Vinted nekade anropet (%s). Vänta en stund och försök igen.", e)
            return 1
        log.info("Torrkörning klar: %d annonser skulle ha notifierats.", found)
        return 0

    store = Store(Path(args.db) if args.db else config_path.parent / "vinted_bot.db")
    store.prune()
    notifiers = build_notifiers(cfg, store)
    if not notifiers:
        log.warning("Inga notiskanaler inställda – fynd skrivs bara ut här. Se README.md.")
    else:
        log.info("Notiser skickas via: %s", ", ".join(n.name for n in notifiers))
    log.info("Bevakar %d sökningar på %s.", len(searches), cfg["domain"])
    if args.once:
        # Schemalagd körning: hellre hoppa över en spärrad sökning än att vänta länge.
        client.retries, client.retry_wait, client.timeout = 1, (2.0, 4.0), 20.0
    configured = any(os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "DISCORD_WEBHOOK_URL", "NTFY_TOPIC")) or any(
        [((cfg.get("notify") or {}).get("telegram") or {}).get("bot_token"),
         (cfg.get("notify") or {}).get("discord_webhook"), (cfg.get("notify") or {}).get("ntfy_topic")])
    bot = Bot(cfg, searches, client, store, notifiers, time_budget=args.time_budget, queue_undelivered=configured)
    if args.once:
        bot.block_cooldown = 75.0  # bara om tidsgränsen räcker, se Bot._run_search_or_wait
    try:
        if args.once:
            log.info("Klart: %d fynd.", bot.run_cycle())
        else:
            bot.run_forever()
    except Blocked as e:
        if args.once:  # t.ex. schemalagd körning – nästa körning försöker igen
            log.warning("Vinted nekade anropet (%s) – försöker igen nästa körning.", e)
            return 0
        log.error("Vinted nekade anropet (%s).", e)
        return 1
    except KeyboardInterrupt:
        log.info("Avslutar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
