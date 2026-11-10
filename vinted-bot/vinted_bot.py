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
import hashlib
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
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import yaml

try:
    from zoneinfo import ZoneInfo
    STOCKHOLM = ZoneInfo("Europe/Stockholm")
except Exception:  # saknas tidszonsdata: nära nog
    STOCKHOLM = timezone(timedelta(hours=1))

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
    "exclude_exceptions": [],           # fraser som aldrig räknas som exclude-ord ("barn coat")
    "womens_keywords": [],              # damplagg – hoppas över om titeln inte också säger unisex/herr
    "unisex_keywords": [],
    "premium_keywords": {},
    "notify": {},
    # Ämnen i Telegram-gruppen: ämnets namn -> sökningarnas taggar som hör dit (första träffen gäller).
    "topics": {
        "Dunjackor": ["Dunjacka"],
        "Jackor": ["Jacka", "Fältjacka"],
        "Jeans": ["Jeans", "Bootcut"],
        "Tröjor & skjortor": ["Tröja", "Skjorta", "Hoodie"],
        "Byxor & shorts": ["Byxor", "Shorts", "Mjukisbyxor"],
    },
    "quiet_hours": [23, 7],             # fynd kommer utan ljud mellan de här timmarna (svensk tid); false = av
    "deal_risk": True,
    "mens_catalog": 5,                  # Vinteds kategori "Herr": annonser utanför den skickas inte; false = av                  # hämta annonssidan för varje fynd och visa kopierisken i fyndet
    "daily_summary_hour": 20,           # "Dagens 5 bästa fynd" efter den här timmen (svensk tid); false = av
    "summary_topic": "⭐ Dagens bästa",
    "weekly_report": True,
    "info_topics": True,
    "chat_topic": "💬 Snack",
    "stats_topic": "📊 Statistik",
    "price_topic": "💰 Prischeck",
    "resell_topic": "📈 Resell Guide",      # låst flik: bevaka vilka plagg som helst + hur man resellar bäst         # flik där man skriver ett plagg och får vad det kostar på Vinted
    "remove_unwatched_deals": True,     # ta bort inlägg med märken som tagits bort ur config.yaml
    "owner_alarms": True,               # privat larm till ägaren när boten stått still, spärras eller inte hittar fynd
    "join_requests": True,              # ansökningslänk: den som ansöker skickar skärmdump på betalningen, ägaren släpper in
    "clean_deal_topics": True,          # fynd-flikarna är bara för fynd: annat tas bort och hänvisas till General/Snack
    "auto_tune": True,                  # boten justerar varje sökings vinstgräns efter hur fynden säljs             # öppen flik där medlemmarna kan prata; false = ingen                # flikarna "👋 Välkommen" och "📖 Start här", som boten skriver och låser              # veckorapport på söndagar, i samma ämne som dagens bästa
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


# Inte "utan" – "inte äkta utan kopia" betyder att det ÄR en kopia.
_NEGATIONS = ("ingen", "inga", "inget", "inte", "ej", "not", "non", "no")
_FILLER = ("en", "ett", "a", "an", "nan", "nagon", "nagot", "nagra", "some", "any")
# "inte för herr", "not suitable for men", "nie męska", "nicht Herren", "geen heren", "pas homme"
_UNISEX_NEGATED = re.compile(r"(?<!\w)(?:%s)(?:\W+\w+){0,2}\W*$" % "|".join(
    ("inte", "ej", "ingen", "inga", "not", "no", "non", "nie", "nicht", "kein", "keine", "geen", "niet", "pas")))
_NEGATED_BEFORE = re.compile(r"(?<!\w)(?:%s)\s+(?:(?:%s)\s+)?$" % ("|".join(_NEGATIONS), "|".join(_FILLER)))


def negated(text: str, pos: int, extra: tuple[str, ...] = ()) -> bool:
    """Står ett nekande ord före positionen, med högst ett småord emellan? ("äkta, inte en kopia")
    Texten ska vara fold():ad. extra = fler nekande ord, t.ex. "utan" ("utan kvitto")."""
    if _NEGATED_BEFORE.search(text[:pos]):
        return True
    return bool(extra) and bool(re.search(r"(?<!\w)(?:%s)\s+$" % "|".join(map(re.escape, extra)), text[:pos]))


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
    exact: bool = False  # märket måste heta exakt så (t.ex. "Essentials", inte "Nike Sport Essentials")


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
    base_min_profit: float | None = None  # min_profit i config.yaml; min_profit kan justeras av boten
    womens: list[tuple[str, re.Pattern]] = field(default_factory=list)
    unisex: list[re.Pattern] = field(default_factory=list)
    exclude_exceptions: list[re.Pattern] = field(default_factory=list)
    hot: bool = False                   # toppmärke: körs en extra gång mitt i varvet

    def rule_for(self, brand: str) -> BrandRule | None:
        brand_words = words(brand)
        matches = [rule for key, rule in self.brands
                   if (tuple(brand_words) == key if rule.exact else contains_words(brand_words, key))]
        # Ett undantag gäller alltid, även när ett längre märkesnamn också matchar
        # ("Ralph Lauren Chaps" ska undantas av "Chaps" trots att "Ralph Lauren" är längre).
        return next((r for r in matches if r.skip), None) or (matches[0] if matches else self.other_brands)

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
    risk: str | None = None             # kort legitcheck av annonsen, t.ex. "🟢 Säljare & annons: låg risk …"


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
            exact=bool(spec.get("exact", False)),
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
    womens = list(cfg.get("womens_keywords") or []) + list(raw.get("womens_keywords") or [])
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
        womens=[(str(k), keyword_re(str(k))) for k in womens],
        unisex=[keyword_re(str(k)) for k in cfg.get("unisex_keywords") or []],
        exclude_exceptions=[keyword_re(str(k)) for k in cfg.get("exclude_exceptions") or []],
        hot=bool(raw.get("hot", False)),
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
    quiet = cfg.get("quiet_hours")
    if quiet not in (False, None) and not (isinstance(quiet, list) and len(quiet) == 2 and
                                           all(isinstance(h, int) and 0 <= h <= 23 for h in quiet)):
        raise ConfigError(f"{path.name}: quiet_hours ska vara två timmar, t.ex. [23, 7], eller false")
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

    def fetch_listing_page(self, item_id: int) -> str | None:
        """Annonssidan, för legitcheck. None om annonsen inte finns (såld och borttagen)."""
        body = None
        for rsc in (True, False):
            headers = {"RSC": "1", "Accept": "*/*"} if rsc else {"Accept": "text/html,application/xhtml+xml"}
            try:
                resp = self.session.get(f"{self.base}/items/{item_id}", headers=headers, timeout=self.timeout)
            except Exception as e:
                raise FetchError(f"nätverksfel: {e}") from None
            if resp.status_code in (401, 403, 429):
                self._new_session()
                raise Blocked(f"HTTP {resp.status_code}")
            if resp.status_code in (404, 410):
                return None
            if resp.status_code != 200:
                raise FetchError(f"HTTP {resp.status_code}")
            body = resp.text
            if '"user_info_header"' in body or '"attributes"' in body:
                return body
        return body

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
    def mentions(pattern: re.Pattern, text: str = text) -> bool:
        return any(not negated(text, m.start()) for m in pattern.finditer(text))

    def for_men() -> bool:  # "herr/dam", "unisex" – men inte "dam, inte för herr"
        return any(not _UNISEX_NEGATED.search(text[:m.start()]) for u in search.unisex for m in u.finditer(text))

    checked = text
    for exception in search.exclude_exceptions:  # "barn coat" är ett herrplagg, inte barnkläder
        checked = exception.sub(" ", checked)
    for keyword, pattern in search.exclude:
        if mentions(pattern, checked):
            return None, f"innehåller '{keyword}'"
    for keyword, pattern in search.womens:
        if mentions(pattern) and not for_men():
            return None, f"damplagg ('{keyword}')"
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
    item_id: int | None = None          # satt för fynd – då kan inlägget markeras som sålt senare
    topic: str | None = None            # ämnet i gruppen, om gruppen har Ämnen påslaget
    buttons: list | None = None         # egna knappar i stället för "Öppna på Vinted"


def topic_for(tags: list[str], cfg: dict) -> str | None:
    """Ämnet ett fynd hör till, utifrån sökningens taggar (t.ex. [Dunjacka] -> "Dunjackor")."""
    topics = cfg.get("topics") or {}
    for tag in tags:
        for name, topic_tags in topics.items():
            if fold(tag) in {fold(str(t)) for t in topic_tags or []}:
                return str(name)
    return None


def within(seconds: float) -> str:
    """Hur snabbt något hände, avrundat uppåt: "inom 15 min" (hela 5-tal), "inom 3 tim", "inom 2 dygn"."""
    minutes = max(1, math.ceil(seconds / 60))
    if minutes <= 55:
        return f"inom {math.ceil(minutes / 5) * 5} min"
    if minutes <= 23 * 60:
        return f"inom {math.ceil(minutes / 60)} tim"
    return f"inom {math.ceil(minutes / (24 * 60))} dygn"


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
    if deal.risk:
        lines.append(deal.risk)
    if deal.bonuses:
        lines.append("✨ " + ", ".join(deal.bonuses))
    if deal.suspicious:
        lines.append("⚠️ Misstänkt lågt pris för märket – kolla äktheten (etiketter, sömmar, QR-kod/hologram) innan du köper.")
    if it.is_business:
        lines.append("🏪 Företagssäljare")
    lines.append(f"🔎 {deal.search}")
    if deal.hashtags:
        lines.append("🏷 " + " ".join(deal.hashtags))  # tryck på en hashtagg i Telegram för att se alla med samma
    return Message(title, lines, it.url, it.photo, item_id=it.id)


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


def member_status_ok(info: dict) -> bool:
    """getChatMember: med i gruppen? "restricted" räknas bara om personen inte har lämnat."""
    status = info.get("status") if isinstance(info, dict) else None
    return status in ("creator", "administrator", "member") or (status == "restricted" and bool(info.get("is_member")))


def temporary_error(e: Exception) -> bool:
    """Nätverksfel, för många anrop eller fel hos Telegram – värt att försöka igen senare."""
    return isinstance(e, (OSError, http.client.HTTPException)) or (
        isinstance(e, NotifyError) and (e.status == 429 or e.status >= 500))


class TelegramNotifier:
    name = "Telegram"

    def __init__(self, token: str, chat_id: str, on_migrate=None, fallback_chat: str | None = None,
                 hint_gate=None, legit_button: bool = False, store: "Store | None" = None, quiet=None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.legit_button = legit_button  # bara när LegitListener lyssnar, annars gör knapparna inget
        self.store = store                # ämnenas id:n och skickade fynd (för "❌ SÅLD")
        self.quiet = quiet or (lambda: False)  # tyst natt: meddelanden utan ljud
        self._forum: bool | None = None   # har gruppen Ämnen påslaget? Kollas en gång per varv
        self._no_topic: set[str] = set()  # ämnen som inte gick att skapa det här varvet
        self._forum_retry = 0.0            # Telegram svarade inte: fråga inte igen förrän då (monotonic)
        self._temporary = False            # senaste _thread stötte på ett tillfälligt fel
        self._members: dict = {}           # person -> med i gruppen (bevakningar), kollas per varv
        self._topic_warned = False
        self.chat_id = chat_id
        self.on_migrate = on_migrate  # anropas med nytt chat-id när en grupp blivit supergrupp
        self.fallback_chat = fallback_chat  # ägarens privata chatt, om boten inte får skriva i chat_id
        self.hint_gate = hint_gate or (lambda: True)  # avgör om förklaringen till ägaren får skickas nu
        self._warned = False

    def _post(self, method: str, payload: dict, chat_id: str | None = None) -> dict:
        """Anropar Telegram och returnerar det skickade meddelandet."""
        try:
            response = http_post_json(f"{self.api}/{method}", {"chat_id": chat_id or self.chat_id, **payload})
        except NotifyError as e:
            if chat_id or not (new_id := e.migrate_to_chat_id):
                raise
            log.info("Telegram: gruppen har blivit en supergrupp – nytt chat-id %s", "(dolt)" if QUIET else new_id)
            self.chat_id, self._forum = new_id, None
            if self.on_migrate:
                self.on_migrate(new_id)
            payload = {k: v for k, v in payload.items() if k != "message_thread_id"}  # ämnena hörde till gamla gruppen
            response = http_post_json(f"{self.api}/{method}", {"chat_id": self.chat_id, **payload})
        result = (response or {}).get("result") if isinstance(response, dict) else None
        return result if isinstance(result, dict) else {}

    def new_run(self) -> None:
        """Nytt varv: kolla igen om gruppen har Ämnen (ägaren kan ha slagit på dem eller gett rätten)."""
        self._forum, self._no_topic, self._forum_retry = None, set(), 0.0
        self._members = {}

    def prepare_topics(self, topics: list[str]) -> None:
        """Skapar gruppens ämnen direkt, så att de syns redan innan första fyndet, och tar bort
        ämnen som boten skapat men som inte längre finns i config.yaml."""
        for topic in topics:
            self._thread(str(self.chat_id), topic)
            if self._temporary or not self._forum:
                return  # Telegram svarar inte (nytt försök nästa varv), eller gruppen saknar Ämnen
        if self.store:
            self._remove_old_topics(set(topics))

    def post_info(self, pages: dict[str, str], locked: set[str] | None = None) -> None:
        """Skriver flikarnas infotext (en gång och fäst; låst om fliken finns i locked, None = alla).
        Ändras texten redigeras inlägget."""
        if not self.store or not self._forum:
            return
        chat = str(self.chat_id)
        for topic, html in pages.items():
            thread = self._thread(chat, topic)
            if not thread:
                continue
            key = f"info:{chat}:{topic}"
            digest = hashlib.sha1(html.encode("utf-8")).hexdigest()[:12]
            saved = (self.store.get_setting(key) or "").split(":")
            if len(saved) == 2 and saved[1] == digest:
                continue  # redan skickad, oförändrad
            body = {"text": html[:4096], "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
            if len(saved) == 2 and saved[0].isdigit():
                try:
                    http_post_json(f"{self.api}/editMessageText", {"chat_id": chat, "message_id": int(saved[0]), **body})
                    self.store.set_setting(key, f"{saved[0]}:{digest}")
                    continue
                except NotifyError as e:
                    if "not modified" in e.description.lower():
                        self.store.set_setting(key, f"{saved[0]}:{digest}")
                        continue
                    # inlägget har tagits bort – skicka ett nytt
            sent = self._post("sendMessage", {**body, "message_thread_id": thread, "disable_notification": True}, chat)
            if not sent.get("message_id"):
                continue
            self.store.set_setting(key, f"{sent['message_id']}:{digest}")
            steps = [("pinChatMessage", {"message_id": sent["message_id"], "disable_notification": True})]
            if locked is None or topic in locked:
                steps.append(("closeForumTopic", {"message_thread_id": thread}))  # bara boten skriver här
            for method, payload in steps:
                try:
                    http_post_json(f"{self.api}/{method}", {"chat_id": chat, **payload})
                except (NotifyError, OSError, ValueError, http.client.HTTPException):
                    pass
            log.info("Telegram: skrev infotexten i ämnet '%s'.", topic)

    def _remove_old_topics(self, keep: set[str]) -> None:
        prefix = f"topic:{self.chat_id}:"
        for key, thread in self.store.settings_starting_with(prefix):
            name = key[len(prefix):]
            if name in keep:
                continue
            payload = {"chat_id": self.chat_id, "message_thread_id": int(thread)}
            try:
                http_post_json(f"{self.api}/deleteForumTopic", payload)
                log.info("Telegram: tog bort ämnet '%s', som inte längre används.", name)
            except NotifyError as e:
                if temporary_error(e):
                    continue  # nytt försök nästa varv
                if "not found" not in e.description.lower() and "invalid" not in e.description.lower():
                    # Saknar boten rätten "Ta bort meddelanden" går ämnet åtminstone att stänga
                    # (boten skapade det), så att inget mer hamnar där.
                    try:
                        http_post_json(f"{self.api}/closeForumTopic", payload)
                    except (NotifyError, OSError, ValueError, http.client.HTTPException):
                        pass
                    log.warning("Telegram: kunde inte ta bort ämnet '%s' (%s) – stängde det i stället. Ta bort det i "
                                "Telegram, eller ge boten rätten 'Ta bort meddelanden'.", name, e.description)
            except (OSError, ValueError, http.client.HTTPException):
                continue
            self.store.set_setting(key, "")  # klart – försök inte igen

    def _thread(self, chat_id: str, topic: str | None, fresh: bool = False) -> int | None:
        """Ämnet (tråden) i gruppen för meddelandet – skapas första gången. None om gruppen saknar Ämnen."""
        self._temporary = False
        if not topic or not self.store or not str(chat_id).startswith("-") or topic in self._no_topic:
            return None
        if self._forum is None:
            if time.monotonic() < self._forum_retry:
                return None
            try:
                info = http_post_json(f"{self.api}/getChat", {"chat_id": chat_id}) or {}
                self._forum = bool((info.get("result") or {}).get("is_forum"))
            except (NotifyError, OSError, ValueError, http.client.HTTPException) as e:
                if not temporary_error(e):
                    self._forum = False
                self._temporary = temporary_error(e)
                self._forum_retry = time.monotonic() + 60  # Telegram hänger: vänta en minut med nästa fråga
                return None
        if not self._forum:
            return None
        key = f"topic:{chat_id}:{topic}"
        if not fresh and (saved := self.store.get_setting(key)):
            return int(saved)
        try:
            created = http_post_json(f"{self.api}/createForumTopic", {"chat_id": chat_id, "name": topic[:128]}) or {}
            thread = int(created["result"]["message_thread_id"])
        except (NotifyError, OSError, KeyError, TypeError, ValueError, http.client.HTTPException) as e:
            if temporary_error(e):
                self._temporary = True
                return None  # försök igen med nästa fynd
            self._no_topic.add(topic)  # ämnen som redan finns fungerar fortfarande
            if not self._topic_warned:
                self._topic_warned = True
                why = e.description if isinstance(e, NotifyError) else type(e).__name__
                log.warning("Telegram: kunde inte skapa ämnet '%s' (%s) – ge boten rätten 'Hantera ämnen'. "
                            "Fynden skickas utan ämnen så länge.", topic, why)
            return None
        self.store.set_setting(key, str(thread))
        return thread

    def _deliver(self, msg: Message, chat_id: str | None = None) -> None:
        text = f"<b>{escape(msg.title)}</b>\n" + "\n".join(escape(line) for line in msg.lines)
        target = str(chat_id or self.chat_id)
        if msg.buttons is not None:
            rows = msg.buttons
        else:
            rows = [[{"text": "Öppna på Vinted", "url": msg.url}]]
            item_id = msg.item_id or (int(m.group(1)) if (m := _ITEM_URL.search(msg.url)) else None)
            if self.legit_button and item_id:
                rows[0].append({"text": "🔍 Legitcheck", "callback_data": f"legit:{item_id}"})
                if msg.item_id and target.startswith("-"):  # i en grupp: så att inte två köper samma plagg
                    rows.append([{"text": "🙋 Jag tar den", "callback_data": f"claim:{item_id}"},
                                 {"text": "📝 Säljtext", "callback_data": f"sell:{item_id}"}])
                elif msg.item_id:
                    rows.append([{"text": "📝 Säljtext", "callback_data": f"sell:{item_id}"}])
        payload: dict = {"parse_mode": "HTML"}
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}
        if self.quiet():
            payload["disable_notification"] = True
        topic_key = None
        if thread := self._thread(target, msg.topic):
            payload["message_thread_id"], topic_key = thread, f"topic:{target}:{msg.topic}"

        def send(method: str, body: dict) -> dict:
            try:
                return self._post(method, {**payload, **body}, chat_id)
            except NotifyError as e:
                if not topic_key or "thread not found" not in e.description.lower():
                    raise
                self.store.set_setting(topic_key, "")  # ämnet har tagits bort – skapa det igen
                # Ändra payload på plats, så att även reservförsöket som text hamnar i det nya ämnet.
                payload.pop("message_thread_id", None)
                if new_thread := self._thread(target, msg.topic, fresh=True):
                    payload["message_thread_id"] = new_thread
                return self._post(method, {**payload, **body}, chat_id)

        sent, kind = None, "text"
        if msg.photo and len(text) <= 1024:  # bildtexter får vara max 1024 tecken
            try:
                sent, kind = send("sendPhoto", {"photo": msg.photo, "caption": text}), "photo"
            except NotifyError as e:
                if e.status in (401, 403, 404, 429):  # 429: för många meddelanden – vänta till nästa körning
                    raise
                log.debug("Telegram tog inte emot bilden (%s) – skickar som text", e)
        if sent is None:
            sent = send("sendMessage", {"text": text})
        if self.store and msg.item_id and sent.get("message_id"):
            chat = str((sent.get("chat") or {}).get("id") or target)
            self.store.record_sent(chat, int(sent["message_id"]), msg.item_id, kind, text, msg.url)

    def is_member(self, user_id: int) -> bool:
        """Är personen fortfarande med i gruppen fynden går till? Kollas en gång per varv och person."""
        if not str(self.chat_id).startswith("-"):
            return False  # fynden går till en privat chatt – då finns ingen grupp att vara med i
        if user_id not in self._members:
            try:
                info = (http_post_json(f"{self.api}/getChatMember", {"chat_id": self.chat_id, "user_id": user_id})
                        or {}).get("result") or {}
                self._members[user_id] = member_status_ok(info)
            except NotifyError as e:
                self._members[user_id] = temporary_error(e) or e.status >= 500  # osäkert: skicka hellre
            except (OSError, ValueError, http.client.HTTPException):
                self._members[user_id] = True
        return self._members[user_id]

    def delete_message(self, chat_id: str, message_id: int) -> str | None:
        """Tar bort ett inlägg. None om det gick (eller redan var borttaget), annars Telegrams förklaring."""
        try:
            http_post_json(f"{self.api}/deleteMessage", {"chat_id": chat_id, "message_id": message_id})
            return None
        except NotifyError as e:
            if temporary_error(e):
                raise
            return None if "not found" in e.description.lower() else e.description

    def send_private(self, user_id: str, msg: Message) -> None:
        """Ett fynd till en medlems privata chatt (bevakningar). NotifyError om personen inte startat boten."""
        self._deliver(msg, str(user_id))

    def mark_sold(self, chat_id: str, message_id: int, kind: str, html: str, url: str, status: str) -> None:
        """Ändrar ett fynd i gruppen till "❌ SÅLD" (eller "❌ BORTTAGEN")."""
        limit = 1024 if kind == "photo" else 4096
        text = f"❌ <b>{escape(status)}</b>\n{html}"
        if len(text) > limit:
            text = re.sub(r"&[^;\s]*$", "", text[:limit])  # klipp inte mitt i "&amp;"
        field_name, method = ("caption", "editMessageCaption") if kind == "photo" else ("text", "editMessageText")
        try:
            http_post_json(f"{self.api}/{method}", {
                "chat_id": chat_id, "message_id": message_id, field_name: text, "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": [[{"text": "Öppna på Vinted", "url": url}]]}})
        except NotifyError as e:
            if "not modified" not in e.description.lower() and "not found" not in e.description.lower():
                raise

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


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    """Är det tyst natt nu? quiet_hours: [23, 7] = från kl. 23 till kl. 7 (svensk tid)."""
    hours = cfg.get("quiet_hours")
    if not isinstance(hours, (list, tuple)) or len(hours) != 2:
        return False
    try:
        start, end = (int(h) for h in hours)
    except (TypeError, ValueError):
        return False
    hour = (now or datetime.now(STOCKHOLM)).hour
    return (start <= hour or hour < end) if start > end else (start <= hour < end)


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


def telegram_settings(cfg: dict) -> tuple[str | None, str]:
    """(token, fast chat-id) från miljön/.env eller config.yaml."""
    tg = (cfg.get("notify") or {}).get("telegram") or {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or str(tg.get("bot_token") or "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID") or str(tg.get("chat_id") or "").strip()
    return clean_token(token), chat.strip().strip("\"'<>")


def build_notifiers(cfg: dict, store: "Store | None" = None, legit_button: bool = False) -> list:
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
                                              hint_gate=daily_gate(store, telegram_chat_key(token) + ":hint"),
                                              legit_button=legit_button, store=store,
                                              quiet=lambda: in_quiet_hours(cfg)))
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
            if any(text == c or text.startswith((c + "@", c + " ")) for c in _SWITCH_COMMANDS) \
                    and not text.startswith("/start "):  # "/start bevaka" = knappen "Starta boten", inget byte
                events.append(ChatEvent("switch", when, message["chat"], (message.get("from") or {}).get("id")))
    return sorted(events, key=lambda e: e.when)


def telegram_chat_key(token: str) -> str:
    return f"telegram_chat:{token.split(':')[0]}"


def discover_telegram_chat(token: str, store: "Store | None", cfg: dict, api=None,
                           updates: list | None = None) -> str | None:
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
    if updates is None:  # annars kommer de från legitcheck-lyssnaren, som redan hämtat dem
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
            -- Fynd som skickats till Telegram, så att de kan markeras "❌ SÅLD" när plagget sålts.
            -- Medlemmarnas egna bevakningar (/bevaka Moncler L): fynden skickas privat till dem.
            -- Medlemmarnas egna försäljningar (/sald 1200) – för månadens topplista.
            -- Gruppens medlemmar som boten sett (skrivit, gått med, tryckt på en knapp) – för /alla.
            CREATE TABLE IF NOT EXISTS members (
                chat_id TEXT, user_id INTEGER, name TEXT, ts INTEGER, PRIMARY KEY (chat_id, user_id));
            CREATE TABLE IF NOT EXISTS sales (
                user_id INTEGER, name TEXT, item_id INTEGER, sold REAL, bought REAL, profit REAL, ts INTEGER,
                PRIMARY KEY (user_id, item_id));
            -- Annonser som medlemmar följer (/följ): meddelande vid prissänkning eller när den säljs.
            CREATE TABLE IF NOT EXISTS follows (
                user_id INTEGER, item_id INTEGER, title TEXT, price REAL, created INTEGER, checked INTEGER,
                PRIMARY KEY (user_id, item_id));
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, query TEXT, sizes TEXT,
                created INTEGER, active INTEGER NOT NULL DEFAULT 1);
            -- Ansökningar via ansökningslänken (/lank): väntar -> insläppt / nekad.
            CREATE TABLE IF NOT EXISTS join_requests (
                user_id INTEGER, chat_id TEXT, name TEXT, ts INTEGER, status TEXT,
                PRIMARY KEY (user_id, chat_id));
            CREATE TABLE IF NOT EXISTS sent (
                chat_id TEXT, message_id INTEGER, item_id INTEGER, kind TEXT, html TEXT, url TEXT,
                ts INTEGER, checks INTEGER NOT NULL DEFAULT 0, status TEXT,
                PRIMARY KEY (chat_id, message_id));
        """)
        if "max_price" not in {row[1] for row in self.db.execute("PRAGMA table_info(watches)")}:
            self.db.execute("ALTER TABLE watches ADD COLUMN max_price REAL")  # /bevaka … max 800
            self.db.commit()
        if "sold_after" not in {row[1] for row in self.db.execute("PRAGMA table_info(sent)")}:
            self.db.execute("ALTER TABLE sent ADD COLUMN sold_after INTEGER")  # sekunder från fynd till såld
            # Förr kollades fynden efter 1, 6 och 24 timmar – samma antal gjorda kollar i det nya schemat,
            # annars kollas (och märks) alla fynd från de senaste två veckorna om på en gång.
            self.db.execute("UPDATE sent SET checks = CASE checks WHEN 1 THEN 3 WHEN 2 THEN 5 WHEN 3 THEN 6 "
                            "ELSE checks END")
            self.db.commit()
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

    def settings_starting_with(self, prefix: str) -> list[tuple[str, str]]:
        return self.db.execute("SELECT key, value FROM settings WHERE substr(key, 1, ?) = ? AND value != ''",
                               (len(prefix), prefix)).fetchall()

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

    def touch(self, search: str, item_ids: list[int]) -> None:
        """Annonserna syns fortfarande: förnya last_seen så att prune() inte glömmer dem (och skickar dem igen)."""
        now = int(time.time())
        self.db.executemany("UPDATE seen_by SET last_seen = ? WHERE search = ? AND item_id = ?",
                            [(now, search, i) for i in item_ids])
        self.db.executemany("UPDATE seen SET last_seen = ? WHERE item_id = ?", [(now, i) for i in item_ids])
        self.db.commit()

    def record_sent(self, chat_id: str, message_id: int, item_id: int, kind: str, html: str, url: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO sent (chat_id, message_id, item_id, kind, html, url, ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (chat_id, message_id, item_id, kind, html, url, int(time.time())))
        self.db.commit()

    # Efter hur lång tid ett skickat fynd kollas igen (1:a, 2:a, … gången), i sekunder. Tätt i början,
    # så att "❌ SÅLD inom 20 min" blir rätt – de bästa fynden säljs ofta på minuter.
    SOLD_CHECKS = (10 * 60, 30 * 60, 3600, 3 * 3600, 6 * 3600, 24 * 3600)

    def due_sold_checks(self, limit: int) -> list[tuple]:
        """Skickade fynd som är dags att kolla igen, nyaste först (där spelar tiden störst roll):
        (chat, meddelande, annons, typ, html, url, skickat). Alla meddelanden för högst `limit` annonser –
        samma fynd kan ha skickats både till gruppen och privat till dem som bevakar märket."""
        now = int(time.time())
        rows = self.db.execute(
            "SELECT chat_id, message_id, item_id, kind, html, url, ts, checks FROM sent "
            "WHERE status IS NULL AND checks < ? ORDER BY ts DESC", (len(self.SOLD_CHECKS),)).fetchall()
        due = [r for r in rows if now - r[6] >= self.SOLD_CHECKS[r[7]]]
        items = list(dict.fromkeys(r[2] for r in due))[:limit]
        return [r[:7] for r in rows if r[2] in items]

    def sold_checked(self, item_id: int, status: str | None) -> None:
        """status: t.ex. "SÅLD" när plagget är sålt, None om det fortfarande är till salu."""
        if status:
            self.db.execute("UPDATE sent SET status = ?, sold_after = ? - ts WHERE item_id = ? AND status IS NULL",
                            (status, int(time.time()), item_id))
        else:
            self.db.execute("UPDATE sent SET checks = checks + 1 WHERE item_id = ? AND status IS NULL", (item_id,))
        self.db.commit()

    def top_deals(self, since: int, limit: int = 5) -> list[tuple]:
        """Dygnets bästa fynd som inte sålts: (märke, titel, pris, vinst, länk, sökning)."""
        return self.db.execute(
            "SELECT d.brand, d.title, d.price, MAX(d.profit), d.url, d.search FROM deals d "
            "WHERE d.ts >= ? AND NOT EXISTS (SELECT 1 FROM sent s WHERE s.item_id = d.item_id AND s.status IS NOT NULL) "
            "GROUP BY d.item_id ORDER BY MAX(d.profit) DESC LIMIT ?", (since, limit)).fetchall()

    def remember_member(self, chat_id: str, user: dict) -> None:
        if not isinstance(user, dict) or user.get("is_bot") or not isinstance(user.get("id"), int):
            return
        name = str(user.get("first_name") or user.get("username") or "Medlem")[:30]
        self.db.execute("INSERT OR REPLACE INTO members VALUES (?, ?, ?, ?)", (chat_id, user["id"], name, int(time.time())))
        self.db.commit()

    def forget_member(self, chat_id: str, user_id) -> None:
        self.db.execute("DELETE FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        self.db.commit()

    def add_join_request(self, user_id: int, chat_id: str, name: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO join_requests VALUES (?, ?, ?, ?, 'väntar')",
                        (user_id, chat_id, name, int(time.time())))
        self.db.commit()

    def join_request(self, user_id) -> tuple[str, str, str] | None:
        """(grupp, namn, status) för personens senaste ansökan."""
        return self.db.execute("SELECT chat_id, name, status FROM join_requests WHERE user_id = ? ORDER BY ts DESC LIMIT 1",
                               (user_id,)).fetchone()

    def set_join_status(self, user_id, chat_id: str, status: str) -> None:
        self.db.execute("UPDATE join_requests SET status = ? WHERE user_id = ? AND chat_id = ?", (status, user_id, chat_id))
        self.db.commit()

    def members(self, chat_id: str) -> list[tuple[int, str]]:
        return self.db.execute("SELECT user_id, name FROM members WHERE chat_id = ? ORDER BY ts DESC", (chat_id,)).fetchall()

    def deal_info(self, item_id: int) -> tuple | None:
        """(märke, titel, pris, pris inkl. köparskydd, säljvärde) för ett fynd boten skickat."""
        return self.db.execute("SELECT brand, title, price, total, resale FROM deals WHERE item_id = ? "
                               "ORDER BY ts DESC LIMIT 1", (item_id,)).fetchone()

    def record_sale(self, user_id: int, name: str, item_id: int, sold: float, bought: float) -> float:
        profit = sold - bought
        self.db.execute("INSERT OR REPLACE INTO sales VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (user_id, name, item_id, sold, bought, profit, int(time.time())))
        self.db.commit()
        return profit

    def leaderboard(self, since: int, until: int | None = None, limit: int = 5) -> list[tuple]:
        """(person, namn, vinst, antal) – mest vinst först."""
        return self.db.execute(
            "SELECT user_id, (SELECT name FROM sales s2 WHERE s2.user_id = s.user_id ORDER BY ts DESC LIMIT 1), "
            "SUM(profit), COUNT(*) FROM sales s WHERE ts >= ? AND ts < ? GROUP BY user_id ORDER BY SUM(profit) DESC "
            "LIMIT ?", (since, until or 2 ** 62, limit)).fetchall()

    def sell_speed(self, brand: str, since: int) -> float | None:
        """Hur snabbt märkets fynd brukar säljas (median i sekunder), om minst 3 har sålts."""
        rows = sorted(r[0] for r in self.db.execute(
            "SELECT MIN(s.sold_after) FROM sent s WHERE s.status = 'SÅLD' AND s.sold_after IS NOT NULL AND s.ts >= ? "
            "AND s.item_id IN (SELECT item_id FROM deals WHERE brand = ?) GROUP BY s.item_id", (since, brand)))
        return rows[(len(rows) - 1) // 2] if len(rows) >= 3 else None

    MAX_FOLLOWS = 20  # per person

    def add_follow(self, user_id: int, item_id: int, title: str, price: float | None) -> None:
        now = int(time.time())
        self.db.execute("INSERT OR REPLACE INTO follows VALUES (?, ?, ?, ?, ?, ?)", (user_id, item_id, title, price, now, now))
        self.db.commit()

    def follows(self, user_id: int) -> list[tuple]:
        """(annons, titel, pris) – personens följda annonser."""
        return self.db.execute("SELECT item_id, title, price FROM follows WHERE user_id = ? ORDER BY created",
                               (user_id,)).fetchall()

    def remove_follows(self, user_id: int, item_ids: list[int] | None = None) -> int:
        if item_ids is None:
            cur = self.db.execute("DELETE FROM follows WHERE user_id = ?", (user_id,))
        else:
            cur = self.db.executemany("DELETE FROM follows WHERE user_id = ? AND item_id = ?", [(user_id, i) for i in item_ids])
        self.db.commit()
        return cur.rowcount

    def due_follows(self, limit: int, every: int = 1800) -> dict[int, list[tuple]]:
        """Följda annonser som är dags att kolla (äldst kollade först): annons -> [(person, titel, pris)]."""
        rows = self.db.execute("SELECT item_id, MIN(checked) FROM follows WHERE checked <= ? GROUP BY item_id "
                               "ORDER BY MIN(checked) LIMIT ?", (int(time.time()) - every, limit)).fetchall()
        return {item_id: self.db.execute("SELECT user_id, title, price FROM follows WHERE item_id = ?",
                                         (item_id,)).fetchall() for item_id, _ in rows}

    def follow_checked(self, item_id: int, price: float | None = None, gone: bool = False) -> None:
        if gone:
            self.db.execute("DELETE FROM follows WHERE item_id = ?", (item_id,))
        elif price is not None:
            self.db.execute("UPDATE follows SET checked = ?, price = ? WHERE item_id = ?", (int(time.time()), price, item_id))
        else:
            self.db.execute("UPDATE follows SET checked = ? WHERE item_id = ?", (int(time.time()), item_id))
        self.db.commit()

    def sent_with_brand(self) -> list[tuple]:
        """(chat, meddelande, märke, skickat) för skickade fynd som inte redan raderats, nyaste först."""
        return self.db.execute(
            "SELECT s.chat_id, s.message_id, (SELECT d.brand FROM deals d WHERE d.item_id = s.item_id LIMIT 1), s.ts "
            "FROM sent s WHERE COALESCE(s.status, '') != 'RADERAD' ORDER BY s.ts DESC").fetchall()

    def mark_deleted(self, chat_id: str, message_id: int) -> None:
        self.db.execute("UPDATE sent SET status = 'RADERAD' WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
        self.db.commit()

    def deal_outcomes(self, since: int) -> list[tuple]:
        """(sökning, märke, vinst, status, sekunder till såld) per fynd sedan since."""
        return self.db.execute(
            "SELECT d.search, d.brand, MAX(d.profit), "
            "(SELECT s.status FROM sent s WHERE s.item_id = d.item_id AND s.status IS NOT NULL LIMIT 1), "
            "(SELECT MIN(s.sold_after) FROM sent s WHERE s.item_id = d.item_id AND s.status = 'SÅLD') "
            "FROM deals d WHERE d.ts >= ? GROUP BY d.item_id", (since,)).fetchall()

    def search_outcomes(self, search: str, since: int, until: int) -> tuple[int, int, float | None]:
        """Sökningens fynd mellan since och until: (antal, sålda inom ett dygn, median till såld i sekunder)."""
        items = [r[0] for r in self.db.execute("SELECT DISTINCT item_id FROM deals WHERE search = ? AND ts >= ? AND ts < ?",
                                               (search, since, until))]
        sold = sorted(r[0] for r in self.db.execute(
            f"SELECT MIN(sold_after) FROM sent WHERE status = 'SÅLD' AND sold_after IS NOT NULL AND sold_after <= 86400 "
            f"AND item_id IN ({','.join('?' * len(items))}) GROUP BY item_id", items)) if items else []
        return len(items), len(sold), (sold[(len(sold) - 1) // 2] if sold else None)

    MAX_WATCHES = 10  # per person

    def add_watch(self, user_id: int, query: str, sizes: list[str], max_price: float | None = None) -> None:
        self.db.execute("INSERT INTO watches (user_id, query, sizes, created, max_price) VALUES (?, ?, ?, ?, ?)",
                        (user_id, query, ",".join(sizes), int(time.time()), max_price))
        self.db.execute("UPDATE watches SET active = 1 WHERE user_id = ?", (user_id,))  # boten får skriva igen
        self.db.commit()

    def watches(self, user_id: int | None = None) -> list[tuple]:
        """(id, person, sökord, storlekar) – en persons bevakningar, eller alla aktiva."""
        if user_id is None:
            rows = self.db.execute("SELECT id, user_id, query, sizes FROM watches WHERE active = 1 ORDER BY id")
        else:
            rows = self.db.execute("SELECT id, user_id, query, sizes FROM watches WHERE user_id = ? ORDER BY id", (user_id,))
        return [(i, u, q, [x for x in (z or "").split(",") if x]) for i, u, q, z in rows]

    def watch_max_prices(self, user_id: int | None = None) -> dict[int, float]:
        """Bevakning -> maxpris, för de bevakningar som har ett."""
        sql = "SELECT id, max_price FROM watches WHERE max_price IS NOT NULL" + (" AND user_id = ?" if user_id else "")
        return dict(self.db.execute(sql, (user_id,) if user_id else ()).fetchall())

    def watches_paused(self, user_id: int) -> bool:
        return bool(self.db.execute("SELECT 1 FROM watches WHERE user_id = ? AND active = 0 LIMIT 1", (user_id,)).fetchone())

    def resume_watches(self, user_id: int) -> None:
        """Personen skriver till boten igen (och är med i gruppen): skicka fynden igen."""
        self.db.execute("UPDATE watches SET active = 1 WHERE user_id = ? AND active = 0", (user_id,))
        self.db.commit()

    def remove_watches(self, user_id: int, watch_ids: list[int] | None = None) -> int:
        """Tar bort personens bevakningar (alla, eller de med de här id:na). Returnerar antalet."""
        if watch_ids is None:
            cur = self.db.execute("DELETE FROM watches WHERE user_id = ?", (user_id,))
        else:
            cur = self.db.executemany("DELETE FROM watches WHERE user_id = ? AND id = ?", [(user_id, i) for i in watch_ids])
        self.db.commit()
        return cur.rowcount

    def pause_watches(self, user_id: int) -> None:
        """Personen har stoppat boten: skicka inget förrän hen skriver /bevaka igen."""
        self.db.execute("UPDATE watches SET active = 0 WHERE user_id = ?", (user_id,))
        self.db.commit()

    def week_stats(self, since: int) -> dict:
        """Veckans siffror för veckorapporten."""
        q = self.db.execute
        best_per_item = "SELECT item_id, brand, title, price, url, MAX(profit) AS profit FROM deals WHERE ts >= ? GROUP BY item_id"
        deals = q(f"SELECT COUNT(*) FROM ({best_per_item})", (since,)).fetchone()[0]
        brand = q(f"SELECT brand, COUNT(*), SUM(profit) FROM ({best_per_item}) WHERE brand != '' "
                  "GROUP BY brand ORDER BY COUNT(*) DESC, SUM(profit) DESC LIMIT 1", (since,)).fetchone()
        best = q(f"SELECT brand, title, price, profit, url FROM ({best_per_item}) ORDER BY profit DESC LIMIT 1",
                 (since,)).fetchone()
        sent = q("SELECT COUNT(DISTINCT item_id) FROM sent WHERE ts >= ?", (since,)).fetchone()[0]
        sold = [r[0] for r in q("SELECT MIN(sold_after) FROM sent WHERE ts >= ? AND status = 'SÅLD' "
                                "GROUP BY item_id", (since,))]
        claims: dict = {}
        for (value,) in q("SELECT st.value FROM settings st JOIN sent s ON st.key = "
                          "'claim:' || s.chat_id || ':' || s.message_id WHERE s.ts >= ? AND st.value != ''", (since,)):
            who = json.loads(value)
            name, count = claims.get(who.get("id"), (who.get("name") or "Någon", 0))
            claims[who.get("id")] = (name, count + 1)
        timed = sorted(t for t in sold if t is not None)
        return {"deals": deals, "brand": brand, "best": best, "sent": sent, "sold": len(sold),
                "median_sold_after": timed[(len(timed) - 1) // 2] if timed else None,
                "top_claimer": max(claims.values(), key=lambda c: c[1]) if claims else None}

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
        self.db.execute("DELETE FROM sent WHERE ts < ?", (int(time.time()) - 14 * 86400,))
        # "🙋 Jag tar den" på fynd som inte längre finns kvar i sent.
        self.db.execute("DELETE FROM settings WHERE key LIKE 'claim:%' AND NOT EXISTS (SELECT 1 FROM sent "
                        "WHERE settings.key = 'claim:' || sent.chat_id || ':' || sent.message_id)")
        self.db.commit()


# --------------------------------------------------------------------------
# Legitcheck – riskbedömning av en annons ("är den äkta?")
# --------------------------------------------------------------------------
#
# Ingen bot kan se på bilder om ett plagg är äkta. Legitcheck väger ihop varningssignaler – priset jämfört
# med vad märket brukar kosta, säljarens omdömen, ord i annonsen och antal bilder – och lägger till
# märkets egen checklista ur legit_tips.yaml. Svarar på /legit och knappen "🔍 Legitcheck" i fynd-chatten.

_ITEM_URL = re.compile(r"/items/(\d+)")
_FAKE_WORDS = ["replica", "replika", "fake", "kopia", "dupe", "1:1", "aaa", "look alike", "podróbka", "nachbau"]
_WEAK_FAKE_WORDS = ["copy", "mirror", "inspired", "inspirerad"]  # ofta något annat: "inspired by the 90s"
# Fraser som innehåller kopie-ord utan att handla om plagget.
_HARMLESS = re.compile(r"(?<!\w)(?:fake fur|fake leather|fake suede|faux \w+|kopia (?:pa|av) (?:kvitto\w*|kvittot)"
                       r"|copy of (?:the |my )?receipt)(?!\w)")
_UNSURE_WORDS = ["vet ej om äkta", "vet inte om äkta", "vet inte om den är äkta", "okänd äkthet",
                 "not sure if real", "not sure if authentic", "no idea if real", "kan inte garantera äkthet"]
_PROOF_WORDS = ["kvitto", "kvittot", "receipt", "paragon", "certilogo", "qr", "qr-kod", "nfc",
                "äkthetsbevis", "äkthetscertifikat"]


@dataclass
class Listing:
    """Det legitcheck behöver från en annonssida."""
    id: int
    url: str
    title: str = ""
    brand: str = ""
    size: str = ""
    condition: str = ""
    description: str = ""
    photos: int = 0
    price: float | None = None          # säljarens pris i kronor (Vinted räknar om utländska priser)
    seller_reviews: int | None = None
    seller_rating: float | None = None  # 0–1
    business: bool = False
    catalog_ids: list[int] = field(default_factory=list)  # kategorierna från Herr ner till annonsens
    verification_offered: bool = False  # Vinted erbjuder äkthetskontroll för annonsen
    reserved: bool = False
    status: str = ""                    # Vinteds statusrad, t.ex. "Såld"

    @property
    def sold(self) -> bool:
        return fold(self.status) in ("sald", "sold")


def parse_listing(body: str, base_url: str, item_id: int) -> Listing | None:
    """Läser en annonssida (RSC eller HTML). None om sidan inte känns igen."""
    if "self.__next_f.push" in body:
        body = "".join(json.loads(m.group(1)) for m in _NEXT_F.finditer(body))
    decoder = json.JSONDecoder()
    plugins: dict[str, dict] = {}  # sidans delar: "attributes", "description", "user_info_header" …
    for m in re.finditer(r'\{"data":', body):
        try:
            obj, _ = decoder.raw_decode(body, m.start())
        except ValueError:
            continue
        if isinstance(obj.get("name"), str) and isinstance(obj.get("data"), dict):
            plugins.setdefault(obj["name"], obj["data"])
    if "attributes" not in plugins and "user_info_header" not in plugins:
        return None
    pricing: dict = {}
    for m in re.finditer(r'\{"pricingServices":', body):
        try:
            obj, _ = decoder.raw_decode(body, m.start())
        except ValueError:
            continue
        if isinstance(obj.get("pricingServices"), dict) and "originalAskingAmount" in obj["pricingServices"]:
            pricing = obj["pricingServices"]
            break

    attrs = {a.get("code"): _txt((a.get("data") or {}).get("value"))
             for a in (plugins.get("attributes") or {}).get("attributes") or [] if isinstance(a, dict)}
    seller = plugins.get("user_info_header") or {}
    summary = (plugins.get("summary") or {}).get("lines") or []
    title = ""
    if summary and isinstance(summary[0], dict):
        title = _txt(next((e.get("value") for e in summary[0].get("elements") or [] if isinstance(e, dict)), ""))
    asking = pricing.get("originalAskingAmount") or {}
    crumbs = (plugins.get("breadcrumbs") or {}).get("breadcrumbs") or []
    rating = _amount(seller.get("feedback_reputation"))
    reviews = seller.get("feedback_count")
    return Listing(
        id=item_id,
        url=f"{base_url}/items/{item_id}",
        title=title or _txt((plugins.get("gallery") or {}).get("accessibility_label")),
        brand=attrs.get("brand", ""),
        size=attrs.get("size", ""),
        condition=attrs.get("status", ""),
        description=_txt((plugins.get("description") or {}).get("description")),
        photos=len((plugins.get("gallery") or {}).get("photos") or []),
        price=_amount(asking) if asking.get("currencyCode") in ("SEK", None) else None,
        seller_reviews=int(reviews) if isinstance(reviews, (int, float)) else None,
        seller_rating=rating,
        business=bool(seller.get("business")),
        catalog_ids=list(dict.fromkeys(int(c) for crumb in crumbs if isinstance(crumb, dict)
                                       for c in re.findall(r"/catalog/(\d+)", str(crumb.get("url") or "")))),
        verification_offered=bool((pricing.get("services") or {}).get("itemVerification")),
        reserved=bool((plugins.get("ask_seller") or {}).get("is_reserved")),
        status=_txt((plugins.get("buyer_item_status") or {}).get("title")),
    )


def load_legit_tips(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def brand_tips(brand: str, tips: dict) -> dict:
    """Märkets checklista ur legit_tips.yaml (längsta matchande namn vinner), eller {}."""
    brand_words = words(brand)
    best: tuple[int, dict] = (0, {})
    for key, entry in (tips.get("brands") or {}).items():
        if not isinstance(entry, dict):
            continue
        for name in [key, *(entry.get("aliases") or [])]:
            key_words = tuple(words(str(name)))
            if len(key_words) > best[0] and contains_words(brand_words, key_words):
                best = (len(key_words), entry)
    return best[1]


_MISSING_AFTER = re.compile(r"^\s+(?:saknas|finns (?:ej|inte)|missing|not included)(?!\w)")


def _found(words_list: list[str], text: str, proof: bool = False) -> list[str]:
    """Orden som står i texten utan att vara nekade. proof: även "utan kvitto" och "kvitto saknas" räknas bort."""
    hits = []
    for word in words_list:
        for m in keyword_re(word).finditer(text):
            if negated(text, m.start(), ("utan", "without") if proof else ()):
                continue
            if proof and _MISSING_AFTER.search(text[m.end():]):
                continue
            hits.append(word)
            break
    return hits


def _listing_signals(listing: Listing) -> tuple[list[tuple[int, str]], list[str], list[str]]:
    """Säljaren, ord i annonsen och bilderna: (varningar med poäng, det som talar för, fake-ord)."""
    bad: list[tuple[int, str]] = []
    good: list[str] = []
    # Säljaren.
    reviews, rating = listing.seller_reviews, listing.seller_rating
    if reviews == 0:
        bad.append((2, "Säljaren har inga omdömen"))
    elif reviews is not None and reviews < 5:
        bad.append((1, f"Säljaren har bara {reviews} omdömen"))
    if reviews and reviews >= 5 and rating is not None and rating < 0.8:
        bad.append((2, f"Säljaren har lågt betyg ({rating * 5:.1f}/5)"))
    elif reviews and reviews >= 20 and rating is not None and rating >= 0.9:
        good.append(f"Säljaren har {reviews} omdömen, snitt {rating * 5:.1f}/5")

    # Ord i titel och beskrivning.
    # Meningsslut behålls (fold tar bort punkter): "Kvitto finns. Inte fake." är två påståenden.
    text = _HARMLESS.sub(" ", fold(re.sub(r"[.!?]+", " ; ", f"{listing.title} ; {listing.description}")))
    fake = _found(_FAKE_WORDS, text)
    if fake:
        bad.append((4, "Annonsen innehåller " + ", ".join(f"'{w}'" for w in fake)))
    elif weak := _found(_WEAK_FAKE_WORDS, text):
        bad.append((2, "Annonsen innehåller " + ", ".join(f"'{w}'" for w in weak) + " – kolla vad som menas"))
    if _found(_UNSURE_WORDS, text):
        bad.append((2, "Säljaren skriver att hen inte vet om plagget är äkta"))
    if not fake and (proof := _found(_PROOF_WORDS, text, proof=True)):
        bad.append((-1, ""))
        good.append("Säljaren nämner " + ", ".join(proof))

    # Bilder.
    if listing.photos < 3:
        bad.append((1, f"Bara {listing.photos} bild{'er' if listing.photos != 1 else ''}"))
    elif listing.photos >= 8:
        good.append(f"{listing.photos} bilder")

    return bad, good, fake


def is_mens(listing: "Listing", cfg: dict) -> bool:
    """Ligger annonsen under Vinteds kategori Herr? (Okänd kategori räknas som ja.)"""
    root = cfg.get("mens_catalog")
    return not root or not listing.catalog_ids or int(root) in listing.catalog_ids


def risk_line(listing: Listing) -> str:
    """Kort kopierisk för ett fynd: säljaren, orden i annonsen och bilderna. Priset räknas inte här –
    att det är lågt vet man redan, och märkets kopiegräns har en egen varning i fyndet."""
    bad, good, fake = _listing_signals(listing)
    score = max(sum(points for points, _ in bad), 4 if fake else 0)
    level = "🔴 Säljare & annons: hög risk" if score >= 4 else (
        "🟡 Säljare & annons: medel risk" if score >= 2 else "🟢 Säljare & annons: låg risk")
    reviews, rating = listing.seller_reviews, listing.seller_rating
    facts = []
    if reviews == 0:
        facts.append("inga omdömen")
    elif reviews is not None:
        stars = f" ({rating * 5:.1f}/5)".replace(".", ",") if rating is not None and reviews >= 5 else ""
        facts.append(f"{reviews} omdöme{'n' if reviews != 1 else ''}{stars}")
    facts.append(f"{listing.photos} bild{'er' if listing.photos != 1 else ''}")
    facts += [why[0].lower() + why[1:] for points, why in bad if points > 0 and why.startswith(("Annonsen", "Säljaren skriver"))]
    facts += [g[0].lower() + g[1:] for g in good if g.startswith("Säljaren nämner")]
    line = f"{level} – " + ", ".join(facts)
    if listing.verification_offered:
        line += " · 🛡 äkthetskontroll finns"
    if listing.reserved:
        line += " · ⏳ reserverad"
    return line


def legit_check(listing: Listing, searches: list[Search], tips: dict) -> Message:
    """Riskbedömning av annonsen: varningssignaler, det som talar för, och märkets checklista."""
    bad: list[tuple[int, str]] = []
    good: list[str] = []
    brand = listing.brand or "märket"

    # Priset jämfört med vad märket brukar säljas för i samma kategori (säljvärdet i config.yaml).
    # Den sökning vars kategori ligger djupast i annonsens kategoriväg gäller, och märkets egen regel
    # går före "övriga märken".
    rule, best = None, (-1, False)
    for s in searches:
        r = s.rule_for(listing.brand)
        depth = max((listing.catalog_ids.index(c) for c in s.catalog_ids if c in listing.catalog_ids), default=-1)
        if r and not r.skip and depth >= 0 and (depth, r is not s.other_brands) > best:
            rule, best = r, (depth, r is not s.other_brands)
    if rule and listing.price:
        ratio = listing.price / rule.resale
        if rule.suspicious_below and listing.price < rule.suspicious_below:
            bad.append((3, f"Priset ({money(listing.price)} kr) är under gränsen där kopior är vanliga för "
                           f"{brand} ({money(rule.suspicious_below)} kr)"))
        elif ratio < 0.4:
            bad.append((2, f"Priset är {1 - ratio:.0%} under vad {brand} brukar säljas för (~{money(rule.resale)} kr)"))
        elif ratio < 0.6:
            bad.append((1, f"Priset är {1 - ratio:.0%} under vad {brand} brukar säljas för (~{money(rule.resale)} kr)"))
        elif ratio >= 0.9:
            good.append(f"Priset ligger i nivå med vad {brand} brukar säljas för (~{money(rule.resale)} kr)")
        if ratio < 0.6 and fold(listing.condition) == fold("Ny med prislapp"):
            bad.append((1, "Ny med prislapp men långt under normalpris"))

    more_bad, more_good, fake = _listing_signals(listing)
    bad += more_bad
    good += more_good

    score = max(sum(points for points, _ in bad), 4 if fake else 0)  # säger annonsen själv "replica" är risken hög
    level = "🔴 Hög" if score >= 4 else ("🟡 Medel" if score >= 2 else "🟢 Låg")
    price = f" – {money(listing.price)} kr" if listing.price else ""
    lines = [" · ".join(x for x in (listing.title, listing.size, listing.condition) if x),
             f"Risk för kopia: {level}", ""]
    warnings = [why for points, why in bad if points > 0]
    if warnings:
        lines += ["⚠️ Varningssignaler"] + [f"• {w}" for w in warnings]
    if good:
        lines += ["✅ Talar för"] + [f"• {g}" for g in good]
    if listing.verification_offered:
        lines.append("🛡 Vinted erbjuder äkthetskontroll för den här annonsen – välj den när du köper.")
    if listing.sold:
        lines.append("❌ Annonsen är redan såld.")
    elif listing.reserved:
        lines.append("⏳ Annonsen är reserverad.")

    entry = brand_tips(listing.brand, tips)
    checklist = list(entry.get("tips") or [])[:5] or list(tips.get("general") or [])[:4]
    if checklist:
        lines += ["", f"📋 Be säljaren om bilder och kolla ({brand if entry else 'allmänt'})"]
        lines += [f"• {t}" for t in checklist]
    official = entry.get("official") or {}
    if official.get("url"):
        lines.append(f"🔗 {official.get('name') or 'Officiell kontroll'}: {official['url']}"
                     + (f" – {official['how']}" if official.get("how") else ""))
    if entry and tips.get("general"):
        lines += ["", "ℹ️ " + str(tips["general"][0])]
    lines += ["", "Boten kan inte se på bilderna om plagget är äkta. Det här är en riskbedömning, "
                  "inte en garanti."]
    return Message(f"🔍 Legitcheck: {listing.brand or 'okänt märke'}{price}", lines, listing.url)


_WATCH_SIZE = re.compile(r"x{0,3}s|m|x{0,4}l|[2-5]xl|\d{1,2}|w\d{2}(?:l\d{2})?|l\d{2}", re.I)


def _watch_size(part: str) -> str | None:
    """Storleken som Vinted skriver den: "W32L32" -> "w32", "3XL" -> "xxxl", "L32" (längd) -> None."""
    part = part.lower()
    if m := re.fullmatch(r"(w\d{2})l\d{2}", part):
        return m.group(1)
    if m := re.fullmatch(r"([2-5])xl", part):
        return "x" * int(m.group(1)) + "l"
    return None if re.fullmatch(r"l\d{2}", part) else part


def parse_watch(arg: str) -> tuple[str, list[str]] | None:
    """"Moncler L" -> ("Moncler", ["l"]), "Stone Island M/L" -> ("Stone Island", ["m", "l"]). None om sökord saknas."""
    tokens = arg.split()
    sizes: list[str] = []
    if len(tokens) > 1 and (parts := [x for x in re.split(r"[,/]", tokens[-1]) if x]) and \
            all(_WATCH_SIZE.fullmatch(x) for x in parts):
        sizes = [z for z in dict.fromkeys(_watch_size(x) for x in parts) if z]
        if not sizes:
            return None  # bara längd ("L32") – säg till hellre än att spara något som aldrig matchar
        tokens.pop()
        if len(tokens) > 1 and fold(tokens[-1]) in ("stl", "storlek", "size", "str"):
            tokens.pop()
    query = " ".join(tokens).strip()
    if not words(query) or len(query) > 40 or _WATCH_SIZE.fullmatch(query):  # "/bevaka L" saknar märke
        return None
    return query, sizes


def split_max_price(arg: str) -> tuple[str, float | None]:
    """"Colmar L max 800" -> ("Colmar L", 800). Även "under 800" och "<800"."""
    m = re.search(r"(?:^|\s)(?:max|maxpris|under|<)\s*(\d{2,6})\s*(?:kr|sek)?\s*$", arg, re.I)
    if not m:
        return arg, None
    return arg[:m.start()].strip(), float(m.group(1))


def watch_matches(query: str, sizes: list[str], item: Item) -> bool:
    """Alla sökorden finns i märket eller titeln, och (om storlek angetts) storleken stämmer."""
    found = set(words(f"{item.brand} {item.title}"))
    if not all(w in found for w in words(query)):
        return False
    if not sizes:
        return True
    have = set(words(item.size))
    have |= {t[1:] for t in have if re.fullmatch(r"w\d+", t)}  # "W32" räknas som 32 (men inte längden L32)
    return any(fold(x) in have for x in sizes)


def watch_label(query: str, sizes: list[str], max_price: float | None = None) -> str:
    return (query + (f" ({', '.join(x.upper() for x in sizes)})" if sizes else "")
            + (f", max {money(max_price)} kr" if max_price else ""))


def covered_by_group(query: str, searches: list[Search]) -> bool:
    """Bevakar gruppen redan märket? Annars söker boten efter det själv (privat bevakning)."""
    return any((r := s.rule_for(query)) and not r.skip and r is not s.other_brands for s in searches)


def watch_help(username: str = "") -> Message:
    bot = f"@{username}" if username else "boten"
    return Message("🔔 Egna bevakningar", [
        "Få fynd i ett visst märke (och storlek) privat, direkt när de kommer:",
        "/bevaka Moncler – alla Moncler-fynd",
        "/bevaka Stone Island L – bara storlek L (flera: M/L)",
        "/bevaka dunjacka – ord i titeln går också",
        "/bevaka Colmar L max 800 – vilket märke som helst, med maxpris",
        "Märken som inte finns i gruppen söker jag efter själv och skickar bara till dig.",
        "/bevakningar – visa dina bevakningar",
        "/sluta 2 – ta bort nummer 2 (eller /sluta alla)",
        "",
        f"Du behöver ha tryckt Starta hos {bot} en gång, så att den får skriva till dig.",
    ], "", buttons=[])


def _thread_of(msg: dict) -> int | None:
    """Ämnet (forumtråden) ett meddelande ligger i, om gruppen har Ämnen."""
    thread = msg.get("message_thread_id") if isinstance(msg, dict) and msg.get("is_topic_message") else None
    return int(thread) if isinstance(thread, int) else None


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def leaderboard_message(store: "Store", cfg: dict, now: datetime | None = None) -> Message:
    now = now or datetime.now(STOCKHOLM)
    rows = store.leaderboard(int(month_start(now).timestamp()))
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = [f"{medals[i]} {name} – {'+' if profit >= 0 else ''}{money(profit)} kr ({count} sålda)"
             for i, (_, name, profit, count) in enumerate(rows)] or ["Ingen har rapporterat något än den här månaden."]
    return Message("🏆 Månadens topplista", lines + ["", "Sålt vidare? Svara på fyndet med /sald och priset, t.ex. /sald 1200."],
                   "", buttons=[])


def sell_text_message(listing: "Listing | None", info: tuple | None, url: str) -> Message:
    """Färdig annonstext för att sälja ett fynd vidare."""
    brand = (listing.brand if listing else "") or (info[0] if info else "") or ""
    title = (listing.title if listing else "") or (info[1] if info else "") or ""
    size = listing.size if listing else ""
    condition = listing.condition if listing else ""
    head = title if fold(brand) and fold(brand) in fold(title) else f"{brand} {title}".strip()
    head = head[:60] + (f" – stl {size}" if size else "")
    lines = ["Titel:", head, "", "Beskrivning:",
             f"{title}{'.' if title and not title.endswith('.') else ''}"]
    if size or condition:
        lines.append(" · ".join(x for x in (f"Storlek {size}" if size else "", f"Skick: {condition}" if condition else "") if x))
    lines += ["Fler bilder eller mått? Skriv gärna!", "Skickas inom 1–2 dagar."]
    if info and info[4]:
        lines += ["", f"Prisförslag: {money(round(float(info[4]), -1))} kr (vad liknande brukar säljas för)."]
    lines += ["", "Ta egna bilder i dagsljus – kopiera inte säljarens bilder."]
    return Message("📝 Säljtext", lines, url, buttons=[[{"text": "Öppna fyndet", "url": url}]])


def chat_text() -> str:
    return "\n".join([
        "<b>💬 Snack</b>",
        "",
        "Här får alla skriva: fråga om äkthet, visa vad du köpt, tipsa om fynd eller sälj vidare till varandra.",
        "Håll fynden i sina flikar och var schysst mot varandra. 🙌",
    ])


def price_text() -> str:
    return "\n".join([
        "<b>💰 Prischeck</b>",
        "",
        "Skriv märke och modell här, så svarar boten vad det kostar på Vinted just nu – median, vanligt "
        "prisintervall och vad du bör sätta för att sälja snabbt.",
        "",
        "<b>Exempel</b>",
        "<code>Moncler Maya</code>",
        "<code>Stone Island tröja</code>",
        "<code>Nudie Grim Tim</code>",
        "",
        "Tips: färre ord ger fler annonser att jämföra med. Inget kommando behövs här – skriv bara plagget.",
    ])


def resell_text() -> str:
    return "\n".join([
        "<b>📈 Resell Guide</b>",
        "",
        "<b>🔔 Bevaka vilka plagg som helst</b>",
        "Du är inte begränsad till märkena i gruppen. Skriv <b>privat till boten</b> (tryck på botens namn → "
        "<b>Starta</b> första gången):",
        "<code>/bevaka Moncler L</code> – ett märke i din storlek (flera: <code>M/L</code>)",
        "<code>/bevaka Rick Owens max 1500</code> – vilket märke eller plagg som helst, upp till ett maxpris",
        "<code>/bevaka Salomon XT-6</code> – utan maxpris får du annonser som ligger klart under vanligt pris",
        "<code>/bevakningar</code> – dina bevakningar · <code>/sluta 2</code> – ta bort nr 2",
        "Fynden kommer bara till dig, aldrig i gruppen. Du kan ha 10 bevakningar.",
        "",
        "<b>🛒 Köp rätt</b>",
        "• Var snabb – de bästa fynden säljs på minuter. Slå på ljud för flikarna du gillar.",
        "• Kolla äktheten först: tryck <b>🔍 Legitcheck</b> och be säljaren om bilder på etiketter och tvättlapp.",
        "• Räkna med frakt och köparskydd – vinsten i fynden är redan räknad efter dem.",
        "• Storlek M och L säljs snabbast. XS och XXL tar ofta längre tid.",
        "• Kolla <b>📊 Statistik</b> för vilka märken som går snabbast, och <b>💰 Prischeck</b> innan du köper "
        "något som inte kommit i gruppen.",
        "• Lägg ett bud 10–15 % under – många säljare tackar ja, särskilt på äldre annonser.",
        "",
        "<b>📸 Sälj bra</b>",
        "• Bilder i dagsljus mot en ljus vägg: fram, bak, etiketter, tvättlapp, detaljer och eventuella fel.",
        "• Titel: märke + modell + plagg + storlek, t.ex. <i>Moncler Maya dunjacka herr L</i>.",
        "• Skriv mått (axlar, bröst, längd) och ärligt skick – det ger färre frågor och returer.",
        "• Sätt pris med <b>💰 Prischeck</b>: strax under medianen säljer snabbt. Knappen <b>📝 Säljtext</b> under "
        "varje fynd ger en färdig annonstext.",
        "• Har det inte sålts på en vecka: sänk 5–10 % – de som sparat annonsen får ett meddelande.",
        "• Söndag kväll är det flest köpare på Vinted. Skicka paketet inom ett dygn efter köpet.",
        "",
        "<b>🍂 Tänk på säsongen</b>",
        "Dunjackor och tjocka jackor säljs dyrast oktober–januari – köp gärna på våren och sommaren när de är "
        "billiga. Jeans, tröjor och skjortor säljs hela året.",
        "",
        "<b>⚠️ Undvik</b>",
        "• Betala aldrig utanför Vinted (Swish, länkar i chatten) – då försvinner köparskyddet.",
        "• Fynd med 🔴 kopierisk eller ⚠️ misstänkt lågt pris utan bra bilder på etiketterna.",
        "• Tryck <i>Jag har ett problem</i> på Vinted inom 2 dagar efter leveransen om något inte stämmer.",
        "",
        "<b>💸 Bra att veta om skatt</b>",
        "Vinted rapporterar till Skatteverket den som säljer minst 30 saker eller för minst 2 000 euro per år. "
        "Säljer du regelbundet med vinst kan det räknas som näringsverksamhet – läs mer hos Skatteverket.",
    ])


def welcome_text() -> str:
    return "\n".join([
        "<b>👋 Välkommen till gruppen!</b>",
        "",
        "Här dyker underprisade märkesplagg från Vinted upp automatiskt, dygnet runt – plagg som går att "
        "köpa billigt och sälja vidare med vinst.",
        "",
        "<b>Kom igång på en minut</b>",
        "1. Läs fliken <b>📖 Start här</b> – där står hur allt fungerar.",
        "2. Tryck på botens namn och sedan <b>Starta</b>, så att den får skicka fynd privat till dig.",
        "3. Skriv t.ex. <code>/bevaka Moncler L</code> <b>privat till boten</b> för att få dina märken direkt i "
        "mobilen. Övriga kommandon, som <code>/pris</code> och <code>/legit</code>, skrivs i fliken <b>General</b>.",
        "4. Stäng av ljudet för flikarna du inte bryr dig om (håll inne på fliken → Stäng av).",
        "",
        "<b>Var schysst</b>",
        "• Tryck <b>🙋 Jag tar den</b> innan du köper, så att inte två köper samma plagg.",
        "• Köp alltid med <b>Köp nu</b> på Vinted – aldrig Swish eller annan betalning utanför Vinted.",
        "• Kolla äktheten innan du köper (knappen 🔍 Legitcheck).",
        "",
        "Lycka till med fynden! 🔥",
    ])


# Hur märkena visas i listor: sökningarnas märkesnycklar -> namn (None = alias som inte visas för sig).
BRAND_NAMES = {"Abercrombie": "Abercrombie & Fitch", "Nudie": "Nudie Jeans", "Burberrys": None, "AMI": None}


def stats_text(rows: list[tuple], searches: list[Search], cfg: dict, today: str) -> str:
    """Flikens statistik: vilka märken bland gruppens fynd som säljs snabbast, mest och med störst vinst.
    rows: (sökning, märke, vinst, status, sekunder till såld) per fynd de senaste 30 dagarna."""
    by_name = {s.name: s for s in searches}
    brands: dict[str, dict] = {}
    for search, brand, profit, status, after in rows:
        rule = by_name[search].rule_for(brand) if search in by_name else None
        label = rule.label if rule and rule.label != "övriga märken" else brand
        name = {"Burberrys": "Burberry", "AMI": "Ami Paris"}.get(label) or BRAND_NAMES.get(label) or label
        b = brands.setdefault(name, {"deals": 0, "sold": [], "profit": []})
        b["deals"] += 1
        if status == "SÅLD" and after is not None:
            b["sold"].append(after)
            b["profit"].append(profit)

    def median(xs):
        xs = sorted(xs)
        return xs[(len(xs) - 1) // 2]

    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    sold_some = {n: b for n, b in brands.items() if len(b["sold"]) >= 2}
    fastest = sorted(sold_some.items(), key=lambda x: median(x[1]["sold"]))[:5]
    most = sorted(((n, b) for n, b in brands.items() if b["sold"]), key=lambda x: (-len(x[1]["sold"]), -len(x[1]["sold"]) / x[1]["deals"], x[0]))[:5]
    richest = sorted(sold_some.items(), key=lambda x: -sum(x[1]["profit"]) / len(x[1]["profit"]))[:5]
    slow = sorted(((n, b) for n, b in brands.items() if b["deals"] >= 4),
                  key=lambda x: (len(x[1]["sold"]) / x[1]["deals"], -x[1]["deals"]))[:3]
    e = lambda t: escape(t, quote=False)
    lines = ["<b>📊 Det här säljs bäst</b>",
             "Bland gruppens fynd de senaste 30 dagarna – uppdateras varje dag.", ""]
    total = sum(b["deals"] for b in brands.values())
    sold = sum(len(b["sold"]) for b in brands.values())
    if not total:
        return "\n".join(lines + ["Inga fynd än – statistiken kommer när de första fynden har hunnit säljas."])
    lines.append(f"{total} fynd, varav {sold} sålda ({sold * 100 // total} %).")
    sections = [
        ("⚡ Säljs snabbast", [f"{medals[i]} {e(n)} – hälften {within(median(b['sold']))}" for i, (n, b) in enumerate(fastest)]),
        ("🔥 Flest sålda", [f"{medals[i]} {e(n)} – {len(b['sold'])} av {b['deals']} fynd sålda"
                           for i, (n, b) in enumerate(most)]),
        ("💰 Störst vinst per sålt fynd", [f"{medals[i]} {e(n)} – ≈ +{money(sum(b['profit']) / len(b['profit']))} kr"
                                         for i, (n, b) in enumerate(richest)]),
        ("🐢 Blir oftast liggande", [f"• {e(n)} – {len(b['sold'])} av {b['deals']} sålda" for n, b in slow]),
    ]
    for title, items in sections:
        if items:
            lines += ["", f"<b>{title}</b>"] + items
    lines += ["", f"<i>Senast uppdaterad {today}. \"Såld\" = annonsen försvann från Vinted efter att fyndet "
                  "postades här.</i>"]
    return "\n".join(lines)


def guide_text(searches: list[Search], cfg: dict) -> str:
    """Hur gruppen fungerar, med märkeslistan direkt ur config.yaml (uppdateras när den ändras)."""
    shown = BRAND_NAMES
    topics: dict[str, dict[str, bool]] = {}  # flik -> märke -> söks extra ofta
    for search in searches:
        brands = topics.setdefault(topic_for(search.tags, cfg) or "Övrigt", {})
        for rule in search.all_rules():
            name = shown.get(rule.label, rule.label)
            if name and rule.label != "övriga märken":
                brands[name] = brands.get(name, False) or search.hot
    brands = [f"<b>{escape(t, quote=False)}:</b> " + escape(", ".join(n + (" ⚡" if hot else "") for n, hot in
                                                                 sorted(b.items(), key=lambda x: fold(x[0]))), quote=False)
              for t, b in topics.items()]
    summary = cfg.get("summary_topic") or "⭐ Dagens bästa"
    quiet = cfg.get("quiet_hours")
    lines = [
        "<b>📖 Så fungerar gruppen</b>",
        "",
        "<b>Flikarna</b>",
        "Fynden sorteras i flikar efter typ av plagg. I <b>" + escape(summary) + "</b> kommer dygnets fem bästa "
        "fynd varje kväll kl. " + str(cfg.get("daily_summary_hour") or 20) + ", och på söndagar en veckorapport.",
        "",
        "<b>Ett fynd</b>",
        "🔥 Märke – pris (≈ +vinst). Vinsten är en uppskattning: vad plagget brukar säljas för, minus pris, "
        "köparskydd och frakt.",
        "🟢🟡🔴 Kopierisk utifrån säljarens omdömen, bilderna och orden i annonsen.",
        "⚠️ Misstänkt lågt pris – märket kopieras ofta, kolla extra noga.",
        "⚡ Märket brukar säljas snabbt – slå till direkt om du vill ha det.",
        "Fynd-flikarna är bara för fynd: annat som skrivs där tas bort. Prata i General eller 💬 Snack.",
        "📉 Prissänkt – annonsen har blivit billigare.",
        "",
        "<b>Knapparna</b>",
        "• <b>Öppna på Vinted</b> – gå direkt till annonsen.",
        "• <b>🔍 Legitcheck</b> – riskbedömning och vad du ska be säljaren fota.",
        "• <b>🙋 Jag tar den</b> – visar att du köper. Tryck igen om du ångrar dig.",
        "• <b>📝 Säljtext</b> – en färdig annonstext med prisförslag, skickad privat till dig.",
        "När plagget är sålt ändras inlägget till <b>❌ SÅLD inom …</b>.",
        "",
        "<b>Bevakningar – skriv privat till boten</b>",
        "<code>/bevaka Moncler L</code> – få Moncler i storlek L privat (flera: M/L, bara märke går också)",
        "<code>/bevaka Rick Owens max 1500</code> – vilket märke som helst: finns det inte i gruppen söker boten "
        "efter det själv och skickar bara till dig (utan maxpris: annonser klart under vanligt pris)",
        "<code>/bevakningar</code> – visa dina bevakningar · <code>/sluta 2</code> – ta bort nr 2 (eller "
        "<code>/sluta alla</code>)",
        "<code>/följ</code> + länk – följ en annons: du får besked när priset sänks eller när den blir såld",
        "<code>/följer</code> – visa annonserna du följer · <code>/avfölj 2</code> – sluta följa nr 2",
        "",
        "<b>Övriga kommandon – skriv i fliken General</b>",
        "<code>/legit</code> + länk – kolla vilken Vinted-annons som helst",
        "<code>/pris Moncler Maya</code> – vad det kostar på Vinted just nu" + (
            f" (eller skriv bara plagget i fliken <b>{escape(str(cfg['price_topic']), quote=False)}</b>)"
            if cfg.get("price_topic") else ""),
        "<code>/sald 1200</code> – svara på fyndet när du sålt det vidare (eller <code>/sald 1200 400</code>: "
        "såld för, köpt för)",
        "<code>/topplista</code> – månadens vinsttopplista (den 1:a varje månad kommer förra månadens)",
    ]
    if isinstance(quiet, list) and len(quiet) == 2:
        lines += ["", f"🌙 Mellan kl. {quiet[0]} och {quiet[1]} kommer fynden utan ljud."]
    if cfg.get("stats_topic"):
        lines += ["", f"<b>{escape(str(cfg['stats_topic']), quote=False)}</b> visar varje dag vilka märken som säljs "
                      "snabbast, mest och med störst vinst bland fynden här."]
    if cfg.get("auto_tune", True):
        lines += ["", "🎯 Boten lär sig: märken vars fynd blir liggande kräver större vinst, och märken där allt "
                      "säljs på minuter får lite fler fynd. Det justeras av sig självt varje dygn."]
    lines += ["", "<b>Märken som bevakas</b> (⚡ = söks extra ofta)"] + brands + [
        "",
        "<b>Bra att veta</b>",
        "• Du har 2 dagar efter leveransen att trycka <i>Jag har ett problem</i> på Vinted om något inte stämmer.",
        "• Bästa fynden säljs på minuter – var snabb, men kolla alltid äktheten först.",
    ]
    return "\n".join(lines)


def legit_help() -> Message:
    return Message("🔍 Legitcheck", [
        "Skriv /legit följt av en länk till en Vinted-annons, eller svara på ett fynd med /legit.",
        "Du kan också trycka på knappen 🔍 Legitcheck under ett fynd.",
        "Boten väger ihop pris, säljarens omdömen, ord i annonsen och antal bilder, och visar vad du ska "
        "kolla för just det märket. Den kan inte se på bilderna om plagget är äkta.",
    ], "https://www.vinted.se/")


def item_id_in(value) -> int | None:
    """Annons-id ur en länk, ett meddelande eller ett knappsvar."""
    if isinstance(value, str):
        if m := _ITEM_URL.search(value) or re.fullmatch(r"\s*(?:legit:)?(\d{6,})\s*", value):
            return int(m.group(1))
        return None
    if not isinstance(value, dict):
        return None
    texts = [value.get("text"), value.get("caption")]
    texts += [e.get("url") for e in (value.get("entities") or []) + (value.get("caption_entities") or [])
              if isinstance(e, dict)]
    for row in ((value.get("reply_markup") or {}).get("inline_keyboard") or []):
        texts += [b.get("url") or b.get("callback_data") for b in row if isinstance(b, dict)]
    for text in texts:
        if isinstance(text, str) and (m := _ITEM_URL.search(text) or re.fullmatch(r"legit:(\d+)", text)):
            return int(m.group(1))
    return None


class LegitListener:
    """Lyssnar på Telegram (long polling) och svarar på /legit och knappen "🔍 Legitcheck".

    Körs i en egen tråd medan boten söker. Bara fynd-chatten, ägarens privata chatt och medlemmarnas privata chattar får svar.
    """

    def __init__(self, token: str, db_path: Path, cfg: dict, searches: list[Search], tips: dict,
                 fixed_chat: str | None = None, api=None, client=None):
        self.token, self.db_path, self.cfg = token, db_path, cfg
        self.searches, self.tips, self.fixed_chat = searches, tips, fixed_chat
        self.api = api or telegram_api
        self.client = client or VintedClient(cfg["domain"], cfg["currency"], cfg["impersonate"])
        self.client.retries, self.client.timeout = 0, 20.0
        self.offset: int | None = None
        self.recent: list[float] = []            # när kontrollerna gjordes – skydd mot spam
        self.last_by_user: dict = {}
        self.username = ""                       # bottens användarnamn, för /legit@annanbot
        self.errors = 0                          # fel i rad från Telegram – väntar längre och längre
        self.members: dict = {}                  # person -> (är med i gruppen, när det kollades)
        self.last_command: dict = {}             # person -> senaste /bevaka m.fl. (spamskydd)
        self.admins: dict = {}                   # person -> (är admin i gruppen, när det kollades)
        self.clean_warned = False

    def run(self, until: float | None = None) -> None:
        """Lyssnar tills time.monotonic() når until (None = för alltid)."""
        store = Store(self.db_path)  # egen anslutning, eftersom lyssnaren har en egen tråd
        try:
            try:
                me = self.api(self.token, "getMe", None, timeout=10)
                self.username = str(me.get("username") or "") if isinstance(me, dict) else ""
            except TelegramError:
                pass
            try:  # visas i Telegrams kommandomeny när man skriver /
                self.api(self.token, "setMyCommands", {"commands": json.dumps([
                    {"command": "legit", "description": "Riskbedömning av en Vinted-annons (skicka länken)"},
                    {"command": "bevaka", "description": "Få fynd i ett märke privat, t.ex. /bevaka Moncler L"},
                    {"command": "bevakningar", "description": "Visa dina bevakningar"},
                    {"command": "sluta", "description": "Ta bort en bevakning, t.ex. /sluta 2"},
                    {"command": "pris", "description": "Vad kostar det på Vinted? T.ex. /pris Moncler Maya"},
                    {"command": "sald", "description": "Sålt vidare? Svara på fyndet med /sald 1200"},
                    {"command": "topplista", "description": "Månadens vinsttopplista"},
                    {"command": "folj", "description": "Följ en annons: meddelande vid prissänkning eller när den säljs"},
                    {"command": "foljer", "description": "Visa annonserna du följer"},
                    {"command": "avfolj", "description": "Sluta följa, t.ex. /avfolj 2 eller /avfolj alla"},
                    {"command": "hjalp", "description": "Hur legitcheck fungerar"}])}, timeout=10)
            except TelegramError:
                pass
            while True:
                left = None if until is None else until - time.monotonic() - 5
                if left is not None and left < 1:
                    break  # (timeout 0 skulle bli en snabb loop mot Telegram)
                self.poll(store, 25 if left is None else int(min(25, left)))
            self.confirm()
        except Exception:
            log.exception("Legitcheck: oväntat fel – slutar lyssna")
        finally:
            store.db.close()

    def poll(self, store: "Store", wait: int = 0) -> None:
        params: dict = {"timeout": max(0, wait)}
        if self.offset is not None:
            params["offset"] = self.offset  # bekräftar det som redan hanterats
        try:
            updates = self.api(self.token, "getUpdates", params, timeout=max(0, wait) + 15) or []
        except TelegramError as e:
            # 409: någon annan lyssnar på samma bot just nu (t.ex. boten körs även på en egen dator).
            self.errors += 1
            if self.errors == 1:
                log.warning("Legitcheck: Telegram svarade inte (%s) – försöker igen", e.description or e.code)
            time.sleep(30 if e.code == 409 else min(60, 5 * 2 ** (self.errors - 1)))
            return
        if self.errors:
            log.info("Legitcheck: kontakt med Telegram igen")
            self.errors = 0
        if not updates:
            return
        self.offset = max(int(u.get("update_id", 0)) for u in updates) + 1
        if not self.fixed_chat:
            discover_telegram_chat(self.token, store, self.cfg, self.api, updates=updates)  # /vinted, ny grupp …
        handled = json.loads(store.get_setting("legit:handled") or "[]")
        for update in updates:
            if update.get("update_id") in handled:
                continue  # redan besvarad i en tidigare körning
            try:
                self.handle(update, store)
            except Exception:
                log.exception("Legitcheck: kunde inte svara")
            handled = (handled + [update.get("update_id")])[-300:]
            store.set_setting("legit:handled", json.dumps(handled))  # direkt, ifall körningen tar slut mitt i

    def confirm(self) -> None:
        """Säger till Telegram att allt hittills är hanterat, så att nästa körning inte svarar igen."""
        if self.offset is not None:
            try:
                self.api(self.token, "getUpdates", {"offset": self.offset, "timeout": 0}, timeout=20)
            except TelegramError:
                pass

    def allowed_chats(self, store: "Store") -> set[str]:
        key = telegram_chat_key(self.token)
        return {c for c in (self.fixed_chat, store.get_setting(key), store.get_setting(key + ":private")) if c}

    def group_chat(self, store: "Store") -> str | None:
        return next((str(c) for c in (self.fixed_chat, store.get_setting(telegram_chat_key(self.token)))
                     if c and str(c).startswith("-")), None)

    def owner(self, store: "Store") -> str:
        """Ägarens användar-id (den som kopplade boten), eller "" om det inte är känt."""
        key = telegram_chat_key(self.token)
        return store.get_setting(key + ":owner") or store.get_setting(key + ":private") or ""  # privat chatt-id = användar-id

    def is_member(self, store: "Store", user_id) -> bool:
        """Är personen med i fynd-gruppen? Då får hen använda boten även i sin privata chatt."""
        group = self.group_chat(store)
        if not group or not isinstance(user_id, int):
            return False
        known, when = self.members.get(user_id, (False, -1e9))
        if time.monotonic() - when < 600:
            return known
        try:
            info = self.api(self.token, "getChatMember", {"chat_id": group, "user_id": user_id}, timeout=10)
            known = member_status_ok(info)
        except TelegramError:
            known = False
        self.members[user_id] = (known, time.monotonic())
        return known

    def handle(self, update: dict, store: "Store") -> None:
        allowed = self.allowed_chats(store)
        if isinstance(update.get("chat_join_request"), dict):  # någon tryckte på ansökningslänken
            self.join_request(update["chat_join_request"], store)
            return
        query = update.get("callback_query")
        if isinstance(query, dict):  # knapparna under ett fynd
            msg = query.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            data = str(query.get("data") or "")
            if data.startswith("join:"):  # ✅ Släpp in / ❌ Neka under en ansökan
                self.join_decision(query, data, store)
                return
            if chat in allowed and chat.startswith("-"):
                store.remember_member(chat, query.get("from"))
            if data.startswith("claim:") and chat in allowed:
                self.claim(query, msg, chat, store)
                return
            m = re.fullmatch(r"(legit|sell):(\d+)", data)
            item_id = int(m.group(2)) if m else None
            private = (msg.get("chat") or {}).get("type") == "private"
            ok = item_id and (chat in allowed or (private and self.is_member(store, (query.get("from") or {}).get("id"))))
            if ok and m.group(1) == "sell":
                self.sell_text(item_id, query, chat, store, private)
                return
            self.answer(query, "Kollar annonsen …" if ok else "")
            if ok:
                self.check(item_id, chat, msg.get("message_id"), (query.get("from") or {}).get("id"), _thread_of(msg))
            return
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        text = str(msg.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        command, _, addressee = (parts[0].lower().partition("@") if text.startswith("/") else ("", "", ""))
        chat = str((msg.get("chat") or {}).get("id", ""))
        if chat in allowed and chat.startswith("-"):  # vilka som finns i gruppen, för /alla
            store.remember_member(chat, msg.get("from"))
            for joined in msg.get("new_chat_members") or []:
                store.remember_member(chat, joined)
                if isinstance(joined, dict) and (store.join_request(joined.get("id")) or ("", "", ""))[2] == "väntar":
                    store.set_join_status(joined.get("id"), chat, "insläppt")  # släppt in direkt i Telegram
            if isinstance(left := msg.get("left_chat_member"), dict):
                store.forget_member(chat, left.get("id"))
        if addressee and self.username and addressee != self.username.lower():
            return  # kommandot gäller en annan bot i gruppen
        if command == "/help" and not addressee:
            return  # vanligt kommando för andra botar – svara bara på /help@dennabot
        private = (msg.get("chat") or {}).get("type") == "private"
        user_id = (msg.get("from") or {}).get("id")
        if private and command in ("/lank", "/länk", "/betalinfo") and str(user_id) == self.owner(store):
            self.owner_command(command, parts[1] if len(parts) > 1 else "", chat, store)
            return
        if private and self.applicant_message(msg, chat, user_id, store):
            return
        if command in ("/alla", "/everyone", "/all") and chat in allowed and chat.startswith("-"):
            self.tag_everyone(msg, chat, store, parts[1] if len(parts) > 1 else "")
            return
        command = {"/sålt": "/sald", "/salt": "/sald", "/sålde": "/sald", "/följ": "/folj", "/följer": "/foljer",
                   "/avfölj": "/avfolj"}.get(command, command)
        known = ("/legit", "/hjalp", "/help", "/bevaka", "/bevakningar", "/sluta", "/start",
                 "/pris", "/sald", "/topplista", "/folj", "/foljer", "/avfolj")
        if not private and chat in allowed and self.in_deal_topic(msg, chat, store) and command not in ("/legit", "/sald"):
            # Fynd-flikarna är bara för fynd (svar på ett fynd med /legit eller /sald hör hemma där).
            if not self.remove_from_deal_topic(msg, chat, store, is_command=command in known):
                return
            msg = {k: v for k, v in msg.items() if k not in ("message_thread_id", "is_topic_message")}  # svara i General
        if not command and not private and chat in allowed and self.in_price_topic(msg, chat, store):
            now = time.monotonic()
            if now - self.last_command.get(user_id, -1e9) >= 3:  # skydd mot spam
                self.last_command[user_id] = now
                self.price_check(text, chat, msg.get("message_id"), user_id, _thread_of(msg))
            return
        if command not in known:
            return
        if command == "/start" and (not private or chat in allowed):
            return  # ägarens /start sköts av discover_telegram_chat
        own_only = private and command in ("/bevakningar", "/sluta", "/foljer", "/avfolj")  # rör bara personens egna bevakningar
        if chat not in allowed and not own_only and not (private and self.is_member(store, user_id)):
            return
        thread = _thread_of(msg)
        if not private and command in ("/bevaka", "/bevakningar", "/sluta", "/folj", "/foljer", "/avfolj"):
            now = time.monotonic()
            if now - self.last_command.get(user_id, -1e9) < 3:
                return
            self.last_command[user_id] = now
            start = f"https://t.me/{self.username}?start=bevaka" if self.username else None
            self.reply(chat, msg.get("message_id"), Message("🔔 Bevakningar skrivs privat", [
                f"Skriv {command} privat till {'@' + self.username if self.username else 'boten'} i stället – "
                "då får du fynden där och gruppen hålls ren."], "",
                buttons=[[{"text": "Öppna boten", "url": start}]] if start else []), thread)
            return
        if command in ("/bevaka", "/bevakningar", "/sluta", "/start"):
            now = time.monotonic()
            if now - self.last_command.get(user_id, -1e9) < 3:
                return  # samma person skickade nyss ett kommando – skydd mot spam i gruppen
            self.last_command[user_id] = now
            self.watch_command(command, parts[1] if len(parts) > 1 else "", msg, chat, store, thread, private)
            return
        if command in ("/folj", "/foljer", "/avfolj"):
            now = time.monotonic()
            if now - self.last_command.get(user_id, -1e9) < 3:
                return
            self.last_command[user_id] = now
            self.follow_command(command, parts[1] if len(parts) > 1 else "", msg, chat, store, thread, private)
            return
        if command in ("/pris", "/sald", "/topplista"):
            now = time.monotonic()
            if now - self.last_command.get(user_id, -1e9) < 3:
                return
            self.last_command[user_id] = now
            arg = parts[1] if len(parts) > 1 else ""
            if command == "/pris":
                self.price_check(arg, chat, msg.get("message_id"), user_id, thread)
            elif command == "/sald":
                self.report_sale(arg, msg, chat, store, thread)
            else:
                self.reply(chat, msg.get("message_id"), leaderboard_message(store, self.cfg), thread)
            return
        if command != "/legit":
            self.reply(chat, msg.get("message_id"), legit_help(), thread)
            return
        item_id = item_id_in(parts[1] if len(parts) > 1 else "") or item_id_in(msg.get("reply_to_message"))
        if not item_id:
            self.reply(chat, msg.get("message_id"), legit_help(), thread)
            return
        self.check(item_id, chat, msg.get("message_id"), (msg.get("from") or {}).get("id"), thread)

    # ---- Ansökningar: länken -> skärmdump på betalningen -> ägaren släpper in ----

    def tg(self, method: str, payload: dict):
        """Anropar Telegram. Kastar NotifyError (med Telegrams förklaring) eller OSError."""
        res = http_post_json(f"https://api.telegram.org/bot{self.token}/{method}", payload)
        return res.get("result") if isinstance(res, dict) else res

    def send_html(self, chat, text: str, buttons: list | None = None) -> bool:
        payload: dict = {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self.tg("sendMessage", payload)
            return True
        except (NotifyError, OSError):
            return False

    @staticmethod
    def join_buttons(user_id: int) -> list:
        return [[{"text": "✅ Släpp in", "callback_data": f"join:ok:{user_id}"},
                 {"text": "❌ Neka", "callback_data": f"join:no:{user_id}"}]]

    @staticmethod
    def mention(user_id, name: str, username: str = "") -> str:
        link = f'<a href="tg://user?id={user_id}">{escape(name, quote=False)}</a>'
        return link + (f" (@{escape(username, quote=False)})" if username else "")

    def pay_steps(self, store: "Store") -> list[str]:
        pay = (store.get_setting("join:payinfo") or "").strip()
        first = f"1. Betala: {escape(pay, quote=False)}" if pay else "1. Betala enligt instruktionerna du fått från ägaren."
        return [first, "2. Skicka en <b>skärmdump på betalningen</b> här i chatten.",
                "När betalningen är kollad släpps du in direkt och får ett meddelande här."]

    def join_request(self, req: dict, store: "Store") -> None:
        """Någon har ansökt via länken: be om skärmdump på betalningen och säg till ägaren."""
        chat = str((req.get("chat") or {}).get("id", ""))
        user = req.get("from") or {}
        user_id = user.get("id")
        if not self.cfg.get("join_requests", True) or chat != self.group_chat(store) or not isinstance(user_id, int) \
                or user.get("is_bot"):
            return
        name = str(user.get("first_name") or user.get("username") or "Någon")[:30]
        store.add_join_request(user_id, chat, name)
        title = escape(str((req.get("chat") or {}).get("title") or "gruppen"), quote=False)
        # Telegram låter boten skriva till den som ansöker (via user_chat_id) en kort stund efter ansökan.
        reached = self.send_html(req.get("user_chat_id") or user_id, "\n".join(
            [f"<b>👋 Tack för din ansökan till {title}!</b>", "", "Så här går du med:"] + self.pay_steps(store)))
        log.info("Ny ansökan till gruppen%s", "" if reached else " (kunde inte skriva till personen)")
        owner = self.owner(store)
        if owner:
            lines = [f"<b>🆕 Ny ansökan</b>", self.mention(user_id, name, str(user.get("username") or ""))]
            lines.append("Jag har bett hen skicka en skärmdump på betalningen – den kommer hit." if reached else
                         "⚠️ Jag kunde inte skriva till hen – skriv själv och be om betalning.")
            self.send_html(owner, "\n".join(lines), self.join_buttons(user_id))

    def applicant_message(self, msg: dict, chat: str, user_id, store: "Store") -> bool:
        """Privat meddelande från någon som väntar på att släppas in. True = hanterat här."""
        row = store.join_request(user_id) if isinstance(user_id, int) else None
        if not row or row[2] != "väntar" or str(user_id) == self.owner(store):
            return False
        group, name, _ = row
        if self.is_member(store, user_id):
            store.set_join_status(user_id, group, "insläppt")  # redan med (insläppt på annat sätt)
            return False
        now = time.monotonic()
        if now - self.last_command.get(user_id, -1e9) < 3:
            return True  # spamskydd
        self.last_command[user_id] = now
        image = "photo" in msg or str((msg.get("document") or {}).get("mime_type") or "").startswith(("image/", "application/pdf"))
        if not image:
            self.send_html(chat, "\n".join(["<b>📸 Skärmdump på betalningen</b>"] + self.pay_steps(store)))
            return True
        owner = self.owner(store)
        user = msg.get("from") or {}
        caption = "\n".join([f"<b>💸 Betalning från</b> {self.mention(user_id, name, str(user.get('username') or ''))}",
                             "Släpp in?"])
        try:
            if not owner:
                raise NotifyError(0, "ägaren okänd")
            self.tg("copyMessage", {"chat_id": owner, "from_chat_id": chat, "message_id": msg.get("message_id"),
                                    "caption": caption, "parse_mode": "HTML",
                                    "reply_markup": {"inline_keyboard": self.join_buttons(user_id)}})
        except (NotifyError, OSError) as e:
            log.warning("Ansökan: kunde inte skicka skärmdumpen till ägaren (%s)",
                        getattr(e, "description", type(e).__name__) if not QUIET else getattr(e, "status", "fel"))
            self.send_html(chat, "Något gick fel när skärmdumpen skulle skickas – försök igen om en stund.")
            return True
        self.send_html(chat, "<b>✅ Tack!</b>\nSkärmdumpen är skickad. Du får ett meddelande här när du är insläppt.")
        return True

    def join_decision(self, query: dict, data: str, store: "Store") -> None:
        """Ägaren tryckte ✅ Släpp in eller ❌ Neka."""
        m = re.fullmatch(r"join:(ok|no):(\d+)", data)
        if not m:
            self.answer(query, "")
            return
        if str((query.get("from") or {}).get("id")) != self.owner(store):
            self.answer(query, "Bara ägaren kan släppa in.")
            return
        user_id, approve = int(m.group(2)), m.group(1) == "ok"
        row = store.join_request(user_id)
        if not row:
            self.answer(query, "Hittar inte ansökan.")
            return
        group, name, status = row
        if status == "väntar":
            try:
                self.tg("approveChatJoinRequest" if approve else "declineChatJoinRequest",
                        {"chat_id": group, "user_id": user_id})
                status = "insläppt" if approve else "nekad"
            except NotifyError as e:
                why = e.description or ""
                if "USER_ALREADY_PARTICIPANT" in why:
                    status = "insläppt"
                elif "HIDE_REQUESTER_MISSING" in why:
                    status = "borta"  # ansökan är återkallad eller redan hanterad i Telegram
                else:
                    self.answer(query, "Telegram sa nej – boten behöver rätten 'Bjuda in användare' i gruppen.")
                    return
            except OSError:
                self.answer(query, "Kunde inte nå Telegram – försök igen.")
                return
            store.set_join_status(user_id, group, status)
            if status in ("insläppt", "nekad"):
                self.send_html(user_id, "<b>✅ Välkommen!</b>\nDu är nu med i gruppen – fynden kommer i flikarna. "
                               "Läs gärna fliken Start här först." if status == "insläppt" else
                               "<b>Din ansökan godkändes inte.</b>\nHör av dig till ägaren om du tror att det är fel.")
        label = {"insläppt": f"✅ {name} är insläppt", "nekad": f"❌ {name} är nekad",
                 "borta": f"Ansökan från {name} finns inte längre"}.get(status, status)
        self.answer(query, label)
        msg = query.get("message") or {}
        try:  # byt knapparna mot hur det gick, så att samma ansökan inte hanteras två gånger
            self.tg("editMessageReplyMarkup", {"chat_id": (msg.get("chat") or {}).get("id"),
                                               "message_id": msg.get("message_id"), "reply_markup": {
                                                   "inline_keyboard": [[{"text": label, "callback_data": "join:klar"}]]}})
        except (NotifyError, OSError):
            pass

    def owner_command(self, command: str, arg: str, chat: str, store: "Store") -> None:
        """/lank – ansökningslänken. /betalinfo text – vad den som ansöker ska betala och hur."""
        if command == "/betalinfo":
            arg = arg.strip()[:500]
            if arg:
                store.set_setting("join:payinfo", arg)
            current = store.get_setting("join:payinfo") or ""
            if not current:
                self.send_html(chat, "<b>💳 Betalinfo</b>\nSkriv vad den som ansöker ska betala och hur, t.ex.\n"
                               "/betalinfo Swisha 99 kr till 070-123 45 67 och skriv ditt Telegram-namn")
                return
            self.send_html(chat, "\n".join(["<b>💳 Betalinfo sparad</b>" if arg else "<b>💳 Betalinfo</b>",
                                            "Så här ser det ut för den som ansöker:", ""] + self.pay_steps(store) +
                                           ["", "Ändra med /betalinfo och ny text."]))
            return
        group = self.group_chat(store)
        if not group:
            self.send_html(chat, "Boten är inte kopplad till någon grupp än.")
            return
        key = f"join:link:{group}"
        link = store.get_setting(key) or ""
        if not link or arg.strip().lower() == "ny":
            try:
                res = self.tg("createChatInviteLink", {"chat_id": group, "name": "Ansökan", "creates_join_request": True})
                link = str((res or {}).get("invite_link") or "")
            except (NotifyError, OSError):
                link = ""
            if not link:
                self.send_html(chat, "<b>🔗 Kunde inte skapa länken</b>\nGör boten till admin i gruppen med rätten "
                               "<b>Bjuda in användare via länk</b> och skriv /lank igen.")
                return
            store.set_setting(key, link)
        pay = store.get_setting("join:payinfo")
        self.send_html(chat, "\n".join([
            "<b>🔗 Ansökningslänk</b>", escape(link, quote=False), "",
            "Dela länken. Den som trycker på den skickar en ansökan, och jag ber hen om en skärmdump på betalningen. "
            "Skärmdumpen kommer hit med knapparna ✅ Släpp in och ❌ Neka.",
            "", f"💳 Betalinfo: {escape(pay, quote=False)}" if pay else
            "💳 Betalinfo saknas – skriv t.ex. /betalinfo Swisha 99 kr till 070-123 45 67",
            "", "Viktigt: ta bort gruppens gamla länkar (och sätt på <i>Godkänn nya medlemmar</i> om gruppen är "
            "offentlig), annars kan folk gå med utan att betala. /lank ny gör en ny ansökningslänk."]))

    def is_admin(self, chat: str, user_id) -> bool:
        known, when = self.admins.get(user_id, (False, -1e9))
        if time.monotonic() - when < 600:
            return known
        try:
            info = self.api(self.token, "getChatMember", {"chat_id": chat, "user_id": user_id}, timeout=10)
            known = isinstance(info, dict) and info.get("status") in ("creator", "administrator")
        except TelegramError:
            known = True  # osäkert: ta hellre inte bort något
        self.admins[user_id] = (known, time.monotonic())
        return known

    def in_deal_topic(self, msg: dict, chat: str, store: "Store") -> bool:
        """Skrev en vanlig medlem något i en av fynd-flikarna?"""
        thread, user = _thread_of(msg), msg.get("from") or {}
        if not self.cfg.get("clean_deal_topics", True) or not thread or user.get("is_bot") or msg.get("sender_chat"):
            return False
        if not any(k in msg for k in ("text", "caption", "photo", "video", "sticker", "animation", "document", "voice")):
            return False  # händelser som "gick med i gruppen"
        names = [str(n) for n in self.cfg.get("topics") or {}]
        if self.cfg.get("summary_topic"):
            names.append(str(self.cfg["summary_topic"]))
        threads = {int(v) for n in names if (v := store.get_setting(f"topic:{chat}:{n}")) and v.isdigit()}
        return thread in threads and not self.is_admin(chat, user.get("id"))

    def in_price_topic(self, msg: dict, chat: str, store: "Store") -> bool:
        """Ett vanligt textmeddelande i fliken 💰 Prischeck (inte ett svar till någon annan)?"""
        name, thread = self.cfg.get("price_topic"), _thread_of(msg)
        if not name or not thread or not str(msg.get("text") or "").strip() or (msg.get("from") or {}).get("is_bot"):
            return False
        reply = msg.get("reply_to_message") or {}
        if reply and "forum_topic_created" not in reply and not (reply.get("from") or {}).get("is_bot"):
            return False  # svar till en annan medlem – ett samtal, ingen fråga till boten
        saved = store.get_setting(f"topic:{chat}:{name}") or ""
        return saved.isdigit() and int(saved) == thread

    def remove_from_deal_topic(self, msg: dict, chat: str, store: "Store", is_command: bool) -> bool:
        """Tar bort meddelandet ur fynd-fliken. True = det var ett kommando som ska besvaras (i General)."""
        api = f"https://api.telegram.org/bot{self.token}"
        try:
            http_post_json(f"{api}/deleteMessage", {"chat_id": chat, "message_id": msg.get("message_id")})
        except (NotifyError, OSError) as e:
            if not self.clean_warned:
                self.clean_warned = True
                log.warning("Kunde inte ta bort ett meddelande i en fynd-flik (%s) – boten behöver rätten "
                            "'Ta bort meddelanden'.", e.description if isinstance(e, NotifyError) else type(e).__name__)
            return False
        if is_command:
            return True
        user = msg.get("from") or {}
        key = f"hint:{user.get('id')}"
        if time.time() - float(store.get_setting(key) or 0) < 3600:
            return False  # nyss tillsagd – tjata inte
        store.set_setting(key, str(int(time.time())))
        name = escape(str(user.get("first_name") or "Hej")[:20], quote=False)
        snack = store.get_setting(f"topic:{chat}:{self.cfg.get('chat_topic')}") if self.cfg.get("chat_topic") else None
        payload = {"chat_id": chat, "parse_mode": "HTML", "text":
                   f'<a href="tg://user?id={user.get("id")}">{name}</a>: fynd-flikarna är bara för fynd, så jag tog bort '
                   "ditt meddelande där. Skriv här i stället – kommandon som /pris i General, och bevakningar privat till mig."}
        if snack and snack.isdigit():
            payload["message_thread_id"] = int(snack)
        try:
            http_post_json(f"{api}/sendMessage", payload)
        except (NotifyError, OSError):
            pass
        return False

    def tag_everyone(self, msg: dict, chat: str, store: "Store", text: str) -> None:
        """/alla text – bara för gruppens admins: skickar texten och nämner alla medlemmar boten känner till."""
        user_id = (msg.get("from") or {}).get("id")
        try:
            info = self.api(self.token, "getChatMember", {"chat_id": chat, "user_id": user_id}, timeout=10)
        except TelegramError:
            return
        if not isinstance(info, dict) or info.get("status") not in ("creator", "administrator"):
            return  # bara admins får tagga alla
        reply_to = msg.get("reply_to_message") or {}
        text = text.strip() or str(reply_to.get("text") or reply_to.get("caption") or "").strip()
        thread = _thread_of(msg)
        if not text:
            self.reply(chat, msg.get("message_id"), Message("📣 Tagga alla", [
                "Skriv /alla och ditt meddelande, t.ex. /alla Nytt i gruppen ikväll!",
                "Eller svara på ett meddelande med /alla."], "", buttons=[]), thread)
            return
        people = [(uid, name) for uid, name in store.members(chat) if uid != user_id]
        mentions = [f'<a href="tg://user?id={uid}">{escape(name, quote=False)}</a>' for uid, name in people]
        chunks = [mentions[i:i + 40] for i in range(0, len(mentions), 40)] or [[]]  # Telegram tar högst ~50 per meddelande
        for n, chunk in enumerate(chunks):
            body = (f"📣 {escape(text, quote=False)}\n\n" if n == 0 else "") + " ".join(chunk)
            payload = {"chat_id": chat, "text": body[:4096], "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
            if thread:
                payload["message_thread_id"] = thread
            try:
                http_post_json(f"https://api.telegram.org/bot{self.token}/sendMessage", payload)
            except (NotifyError, OSError) as e:
                log.warning("Kunde inte tagga alla (%s)", getattr(e, "status", type(e).__name__))
                return
        log.info("Taggade %d medlemmar", len(people))

    def _budget(self, user) -> bool:
        """Delad spärr för allt som anropar Vinted: högst 30 per 10 minuter, en per person och 5 s."""
        now = time.monotonic()
        if user is not None and now - self.last_by_user.get(user, -1e9) < 5:
            return False
        self.recent = [t for t in self.recent if now - t < 600]
        if len(self.recent) >= 30:
            return False
        self.last_by_user[user] = now
        self.recent.append(now)
        return True

    def price_check(self, query: str, chat: str, reply_to, user, thread: int | None) -> None:
        """/pris Moncler Maya – vad liknande plagg kostar på Vinted just nu."""
        query = query.strip()[:60]
        search_url = f"https://www.{self.cfg['domain']}/catalog?search_text={urllib.parse.quote(query)}"
        if not words(query):
            self.reply(chat, reply_to, Message("💰 Pris", [
                "Skriv /pris och vad du letar efter, t.ex. /pris Moncler Maya eller /pris Stone Island tröja."],
                "", buttons=[]), thread)
            return
        if not self._budget(user):
            self.reply(chat, reply_to, Message("💰 Pris", ["Många frågor just nu – vänta en minut."], "", buttons=[]), thread)
            return
        catalogs = sorted({c for s in self.searches for c in s.catalog_ids})
        search = build_search({"name": "pris", "query": query, "catalog": catalogs, "resale": 1,
                               "max_price": 1_000_000, "min_profit": 0}, self.cfg)
        wanted = words(query)
        prices: list[float] = []
        try:
            for page in (1, 2):
                if page > 1:
                    time.sleep(random.uniform(1.5, 3))
                items = self.client.fetch(search, page, price_cap=False, retries=1)
                found = set()
                prices += [i.price for i in items
                           if (found := set(words(f"{i.brand} {i.title}"))) and all(w in found for w in wanted)]
                if len(items) < 50:
                    break
        except FetchError:
            self.reply(chat, reply_to, Message("💰 Pris", ["Vinted svarar inte just nu – försök igen om en stund."],
                                               search_url), thread)
            return
        if len(prices) < 5:
            lines = [f"Hittade för få annonser för \"{query}\" ({len(prices)} st) för att säga något säkert.",
                     "Prova med färre ord, t.ex. bara märket och modellen."]
        else:
            q1, med, q3 = statistics.quantiles(prices, n=4)
            lines = [f"\"{query}\" just nu på Vinted: {len(prices)} annonser",
                     f"Median {money(med)} kr – hälften ligger mellan {money(q1)} och {money(q3)} kr.",
                     f"Vill du sälja snabbt: sätt runt {money(round(med * 0.9, -1))} kr.",
                     "Det här är annonspriser – det som faktiskt säljs går ofta 10–20 % lägre."]
        self.reply(chat, reply_to, Message("💰 Pris", lines, search_url,
                                           buttons=[[{"text": "Se annonserna på Vinted", "url": search_url}]]), thread)
        log.info("Pris: svarade på en fråga")

    def sell_text(self, item_id: int, query: dict, chat: str, store: "Store", private: bool) -> None:
        """Knappen "📝 Säljtext": en färdig annonstext för att sälja plagget vidare, privat till den som trycker."""
        user = query.get("from") or {}
        info = store.deal_info(item_id)
        listing = None
        if self._budget(user.get("id")):
            try:
                body = self.client.fetch_listing_page(item_id)
                listing = parse_listing(body, self.client.base, item_id) if body else None
            except FetchError:
                pass
        if not info and not listing:
            self.answer(query, "Hittade inte fyndet – det kan vara borttaget.")
            return
        msg = sell_text_message(listing, info, f"https://www.{self.cfg['domain']}/items/{item_id}")
        target = chat if private else str(user.get("id"))
        if self.reply(target, None, msg):
            self.answer(query, "" if private else "Skickade säljtexten privat ✉️")
        else:
            self.answer(query, f"Tryck Starta hos @{self.username or 'boten'} först, så skickar jag texten privat.")

    def report_sale(self, arg: str, msg: dict, chat: str, store: "Store", thread: int | None) -> None:
        """/sald 1200 (svar på fyndet) eller /sald 1200 400 (såld för, köpt för)."""
        user = msg.get("from") or {}
        numbers = [float(n) for n in re.findall(r"(?<![\w/.-])\d{1,6}(?![\w/.-])", arg)]
        item_id = item_id_in(arg) or item_id_in(msg.get("reply_to_message")) or 0
        info = store.deal_info(item_id) if item_id else None
        help_msg = Message("🏆 Sålt vidare?", [
            "Svara på fyndet med /sald och priset du sålde för, t.ex. /sald 1200.",
            "Eller skriv vad du sålde och köpte för: /sald 1200 400."], "", buttons=[])
        if not numbers or not isinstance(user.get("id"), int):
            self.reply(chat, msg.get("message_id"), help_msg, thread)
            return
        sold = numbers[0]
        if len(numbers) > 1:
            bought = numbers[1]
        elif info:
            bought = float(info[3]) + float(self.cfg["shipping_estimate"])  # pris inkl. köparskydd + frakt
        else:
            self.reply(chat, msg.get("message_id"), help_msg, thread)
            return
        name = str(user.get("first_name") or user.get("username") or "Någon")[:20]
        profit = store.record_sale(user["id"], name, item_id or -int(time.time()), sold, bought)
        start = month_start(datetime.now(STOCKHOLM))
        board = store.leaderboard(int(start.timestamp()), limit=100)
        place = next((n for n, row in enumerate(board, 1) if row[0] == user["id"]), None)
        total = next((row[2] for row in board if row[0] == user["id"]), profit)
        what = f" på {info[0]}" if info and info[0] else ""
        lines = [f"{'Snyggt' if profit > 0 else 'Noterat'}, {name}! {'+' if profit >= 0 else ''}{money(profit)} kr{what}.",
                 f"Din vinst den här månaden: {money(total)} kr" + (f" – plats {place} i /topplista." if place else ".")]
        self.reply(chat, msg.get("message_id"), Message("🏆 Sålt!", lines, "", buttons=[]), thread)

    def follow_command(self, command: str, arg: str, msg: dict, chat: str, store: "Store",
                       thread: int | None, private: bool) -> None:
        """/följ länk, /följer och /avfölj 2 (eller alla)."""
        user = msg.get("from") or {}
        user_id, reply_to = user.get("id"), msg.get("message_id")
        if not isinstance(user_id, int):
            return
        mine = store.follows(user_id)
        if command == "/foljer":
            lines = [f"{n}. {title or 'Annons'} – {money(price) + ' kr' if price else 'pris okänt'}"
                     for n, (_, title, price) in enumerate(mine, 1)] or ["Du följer inga annonser än."]
            self.reply(chat, reply_to, Message("👀 Annonser du följer", lines + ["", "Sluta följa: /avfolj 2 eller /avfolj alla"],
                                               "", buttons=[]), thread)
            return
        if command == "/avfolj":
            if fold(arg.strip()) in ("alla", "all"):
                removed = store.remove_follows(user_id)
            else:
                numbers = {int(x) for x in re.findall(r"\d+", arg)}
                removed = store.remove_follows(user_id, [f[0] for n, f in enumerate(mine, 1) if n in numbers]) if numbers else 0
            text = f"Slutade följa {removed} annons{'er' if removed != 1 else ''}." if removed else \
                "Skriv /avfolj och numret från /foljer, t.ex. /avfolj 2 (eller /avfolj alla)."
            self.reply(chat, reply_to, Message("👀 Följda annonser", [text], "", buttons=[]), thread)
            return
        item_id = item_id_in(arg) or item_id_in(msg.get("reply_to_message"))
        if not item_id:
            self.reply(chat, reply_to, Message("👀 Följ en annons", [
                "Skriv /följ och klistra in länken till en Vinted-annons (eller svara på ett fynd med /följ).",
                "Du får ett privat meddelande när priset sänks eller när den blir såld."], "", buttons=[]), thread)
            return
        if len(mine) >= store.MAX_FOLLOWS and item_id not in {f[0] for f in mine}:
            self.reply(chat, reply_to, Message("👀 Följda annonser", [
                f"Du följer redan {store.MAX_FOLLOWS} annonser – sluta följa någon med /avfolj först."], "", buttons=[]), thread)
            return
        title, price = "", None
        if self._budget(user_id):
            try:
                body = self.client.fetch_listing_page(item_id)
                listing = parse_listing(body, self.client.base, item_id) if body else None
            except FetchError:
                body, listing = "", None
            if body is None or (listing and listing.sold):
                self.reply(chat, reply_to, Message("👀 Följ en annons", ["Annonsen är redan såld eller borttagen."],
                                                   "", buttons=[]), thread)
                return
            if listing:
                title, price = " ".join(x for x in (listing.brand, listing.title) if x)[:80], listing.price
        url = f"https://www.{self.cfg['domain']}/items/{item_id}"
        confirm = Message("👀 Du följer annonsen", [title or url, (f"Pris nu: {money(price)} kr. " if price else "") +
                                                     "Du får ett meddelande här när priset sänks eller när den blir såld.",
                                                     "Visa alla: /foljer · Sluta: /avfolj"], url)
        if not private and not self.reply(str(user_id), None, confirm):
            start = f"https://t.me/{self.username}?start=folj" if self.username else None
            self.reply(chat, reply_to, Message("👀 Följ en annons", [
                f"Tryck Starta hos {'@' + self.username if self.username else 'boten'} först, så att den får skriva "
                "till dig privat – skriv sedan /följ igen."], "",
                buttons=[[{"text": "Starta boten", "url": start}]] if start else []), thread)
            return
        store.add_follow(user_id, item_id, title, price)
        if private:
            self.reply(chat, reply_to, confirm, thread)
        else:
            name = str(user.get("first_name") or "")[:20]
            self.reply(chat, reply_to, Message("👀 Följer", [f"Klart{', ' + name if name else ''}! Du får besked privat."],
                                               "", buttons=[]), thread)

    def watch_command(self, command: str, arg: str, msg: dict, chat: str, store: "Store",
                      thread: int | None, private: bool) -> None:
        """/bevaka, /bevakningar och /sluta. Svaren innehåller aldrig någon annans bevakningar."""
        user = msg.get("from") or {}
        user_id, reply_to = user.get("id"), msg.get("message_id")
        if not isinstance(user_id, int) or command == "/start":
            self.reply(chat, reply_to, watch_help(self.username), thread)
            return
        mine = store.watches(user_id)
        if private and store.watches_paused(user_id) and self.is_member(store, user_id):
            store.resume_watches(user_id)  # skriver till boten igen – då får den skriva tillbaka
        if command == "/bevakningar":
            maxes = store.watch_max_prices(user_id)
            lines = [f"{n}. {watch_label(q, z, maxes.get(i))}" + ("" if covered_by_group(q, self.searches) else " (privat sökning)")
                     for n, (i, _, q, z) in enumerate(mine, 1)] or ["Du har inga bevakningar än."]
            if store.watches_paused(user_id):
                lines.append("⏸ Pausade – skriv till mig privat för att få fynden igen.")
            self.reply(chat, reply_to, Message("🔔 Dina bevakningar", lines + ["", "Ta bort: /sluta 2 eller /sluta alla"],
                                               "", buttons=[]), thread)
            return
        if command == "/sluta":
            if fold(arg.strip()) in ("alla", "all"):
                removed = store.remove_watches(user_id)
            else:
                numbers = {int(x) for x in re.findall(r"\d+", arg)}
                removed = store.remove_watches(user_id, [w[0] for n, w in enumerate(mine, 1) if n in numbers]) if numbers else 0
            text = f"Tog bort {removed} bevakning{'ar' if removed != 1 else ''}." if removed else \
                "Skriv /sluta och numret från /bevakningar, t.ex. /sluta 2 (eller /sluta alla)."
            self.reply(chat, reply_to, Message("🔔 Bevakningar", [text], "", buttons=[]), thread)
            return
        arg, max_price = split_max_price(arg)
        parsed = parse_watch(arg)
        if not parsed:
            self.reply(chat, reply_to, watch_help(self.username), thread)
            return
        if len(mine) >= store.MAX_WATCHES:
            self.reply(chat, reply_to, Message("🔔 Bevakningar", [
                f"Du har redan {store.MAX_WATCHES} bevakningar – ta bort en med /sluta först."], "", buttons=[]), thread)
            return
        query, sizes = parsed
        label = watch_label(query, sizes, max_price)
        how = ("du får gruppens fynd här, privat, direkt när de kommer." if covered_by_group(query, self.searches) else
               "märket finns inte i gruppen, så jag söker efter det själv och skickar annonser hit – bara till dig – "
               + ("som kostar högst så mycket." if max_price else "som ligger klart under vanligt pris."))
        confirm = Message("🔔 Bevakning sparad", [f"{label}: {how}",
                                                  "Visa alla: /bevakningar · Ta bort: /sluta"], "", buttons=[])
        if private:
            store.add_watch(user_id, query, sizes, max_price)
            self.reply(chat, reply_to, confirm, thread)
            return
        # I gruppen: se först till att boten får skriva privat till personen.
        if not self.reply(str(user_id), None, confirm):
            start = f"https://t.me/{self.username}?start=bevaka" if self.username else None
            self.reply(chat, reply_to, Message("🔔 Bevakningar", [
                f"Tryck Starta hos {'@' + self.username if self.username else 'boten'} först, så att den får skriva "
                "till dig privat – skriv sedan /bevaka igen."], "",
                buttons=[[{"text": "Starta boten", "url": start}]] if start else []), thread)
            return
        store.add_watch(user_id, query, sizes, max_price)
        name = str(user.get("first_name") or "")[:20]
        self.reply(chat, reply_to, Message("🔔 Bevakning sparad", [
            f"Klart{', ' + name if name else ''}! Du får {label} privat."], "", buttons=[]), thread)

    def claim(self, query: dict, msg: dict, chat: str, store: "Store") -> None:
        """"🙋 Jag tar den": första som trycker får fyndet, samma person kan släppa det igen."""
        user = query.get("from") or {}
        name = str(user.get("first_name") or user.get("username") or "Någon")[:20]
        key = f"claim:{chat}:{msg.get('message_id')}"
        current = json.loads(store.get_setting(key) or "null")
        if current and current.get("id") != user.get("id"):
            self.answer(query, f"{current.get('name', 'Någon')} har redan tagit den.")
            return
        if current:
            store.set_setting(key, "")
            label, note = "🙋 Jag tar den", "Du släppte den."
        else:
            store.set_setting(key, json.dumps({"id": user.get("id"), "name": name}, ensure_ascii=False))
            label, note = f"🙋 {name} tar den", "Den är din – köp den på Vinted innan någon annan gör det!"
        self.answer(query, note)
        rows = [[dict(b, text=label) if str(b.get("callback_data", "")).startswith("claim:") else b for b in row]
                for row in ((msg.get("reply_markup") or {}).get("inline_keyboard") or [])]
        try:
            http_post_json(f"https://api.telegram.org/bot{self.token}/editMessageReplyMarkup", {
                "chat_id": chat, "message_id": msg.get("message_id"), "reply_markup": {"inline_keyboard": rows}})
        except NotifyError as e:
            log.warning("Kunde inte uppdatera knappen (%s)", e.description if not QUIET else e.status)
        except OSError as e:
            log.warning("Kunde inte nå Telegram (%s)", e)

    def check(self, item_id: int, chat: str, reply_to, user, thread: int | None = None) -> None:
        now = time.monotonic()
        if user is not None and now - self.last_by_user.get(user, -1e9) < 5:
            return  # samma person tryckte flera gånger
        self.recent = [t for t in self.recent if now - t < 600]
        url = f"https://www.{self.cfg['domain']}/items/{item_id}"
        if len(self.recent) >= 30:
            self.reply(chat, reply_to, Message("🔍 Legitcheck", ["Många kontroller just nu – vänta några minuter."], url), thread)
            return
        self.last_by_user[user] = now
        self.recent.append(now)
        try:
            body = self.client.fetch_listing_page(item_id)
        except Blocked:
            self.reply(chat, reply_to, Message("🔍 Legitcheck", ["Vinted svarar inte just nu – försök igen om en stund."], url), thread)
            return
        except FetchError as e:
            self.reply(chat, reply_to, Message("🔍 Legitcheck", [f"Kunde inte hämta annonsen ({e})."], url), thread)
            return
        listing = parse_listing(body, self.client.base, item_id) if body else None
        if listing is None:
            self.reply(chat, reply_to, Message("🔍 Legitcheck", ["Hittade inte annonsen – den kan vara såld eller borttagen."], url), thread)
            return
        self.reply(chat, reply_to, legit_check(listing, self.searches, self.tips), thread)
        log.info("Legitcheck: svarade på en kontroll")

    def reply(self, chat: str, reply_to, msg: Message, thread: int | None = None) -> bool:
        text = f"<b>{escape(msg.title)}</b>\n" + "\n".join(escape(line) for line in msg.lines)
        buttons = msg.buttons if msg.buttons is not None else [[{"text": "Öppna på Vinted", "url": msg.url}]]
        payload: dict = {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if thread:
            payload["message_thread_id"] = thread  # svara i samma ämne
        try:
            http_post_json(f"https://api.telegram.org/bot{self.token}/sendMessage", payload)
            return True
        except NotifyError as e:  # bara Telegrams förklaring – hela svaret kan innehålla chatt-id
            if e.status != 403:  # 403: personen har inte startat boten – vanligt, och sköts av den som frågar
                log.warning("Legitcheck: kunde inte svara i Telegram (%s)", e.description if not QUIET else e.status)
        except OSError as e:
            log.warning("Legitcheck: kunde inte nå Telegram (%s)", e)
        return False

    def answer(self, query: dict, text: str) -> None:
        try:
            self.api(self.token, "answerCallbackQuery", {"callback_query_id": query.get("id"), "text": text}, timeout=10)
        except TelegramError:
            pass  # för gammal knapptryckning – svaret kommer ändå som meddelande


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
        self._cycle_started, self._inspect_off = time.monotonic(), False  # för kopierisk-kollen
        self.spare_end: float | None = None  # i molnet: sök toppmärkena igen fram till dess (monotonic)
        self.cycle_blocked: bool | None = None  # spärrade Vinted det senaste varvet?

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
            still_there = []
            for item in items:
                prev = None if self.dry_run else self.store.seen_price(item.id, search.name)
                dropped = prev is not None and item.price < prev - 0.5
                if prev is not None and not dropped:
                    still_there.append(item.id)
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
                listing, gone = self.inspect(deal)
                if gone:
                    log.debug("  – %s: redan såld eller borttagen", item.title[:50])
                    continue
                if listing:
                    deal.risk = risk_line(listing)
                self.notify(deal, topic_for(search.tags, self.cfg))
                found += 1
            if still_there:
                self.store.touch(search.name, still_there)
            # Var alla annonser på sidan nya kan fler ha tillkommit sedan förra varvet – bläddra vidare.
            if self.dry_run or silent or not items or new < len(items):
                break
        if first_run:
            self.store.set_initialized(search.name)
            if silent:
                log.info("%s: första körningen – befintliga annonser sparade, notiser kommer för nya", search.name)
        return found

    def inspect(self, deal: Deal) -> tuple["Listing | None", bool]:
        """Hämtar fyndets annonssida för kopierisken: (annonsen, redan såld/borttagen)."""
        if self.dry_run or not self.cfg.get("deal_risk", True) or not hasattr(self.client, "fetch_listing_page"):
            return None, False
        if self._inspect_off or (self.time_budget and time.monotonic() - self._cycle_started >= self.time_budget):
            return None, False
        self._pause()  # ett anrop till hos Vinted – samma lugna tempo som mellan sökningarna
        try:
            body = self.client.fetch_listing_page(deal.item.id)
            if body is None:
                return None, True
            listing = parse_listing(body, self.client.base, deal.item.id)
        except Exception as e:  # även Blocked: fyndet skickas ändå, utan kopierisk
            if isinstance(e, Blocked):
                self._inspect_off = True  # Vinted spärrar – inga fler annonssidor det här varvet
            log.debug("Kunde inte hämta annonsen för kopierisken (%s)", type(e).__name__)
            return None, False
        if listing and not is_mens(listing, self.cfg):
            log.debug("  – %s: inte ett herrplagg (kategori %s)", deal.item.title[:50], listing.catalog_ids[:2])
            return listing, True  # dam- eller barnplagg som hamnat i sökningen – skicka inte
        return listing, bool(listing and listing.sold)

    def notify(self, deal: Deal, topic: str | None = None) -> None:
        msg = deal_message(deal, self.cfg)
        msg.topic = topic
        if self.store and deal.item.brand:
            speed = self.store.sell_speed(deal.item.brand, int(time.time()) - 30 * 86400)
            if speed is not None and speed <= 3600:  # bara när det verkligen går fort
                at = next((i for i, line in enumerate(msg.lines) if line.startswith("🔎")), len(msg.lines))
                msg.lines.insert(at, f"⚡ {deal.item.brand}-fynd brukar säljas {within(speed)} – var snabb")
        if not QUIET:
            print_message(msg)
        if self.dry_run:
            return
        self.store.log_deal(deal)
        if (self.notifiers or self.queue_undelivered) and not self._deliver(msg):
            self.store.add_pending(msg)  # skickas igen nästa körning
        try:
            self.notify_watchers(deal, msg)
        except Exception as e:  # bevakningarna får aldrig stoppa sökningarna
            log.warning("Kunde inte skicka bevakningar (%s)", type(e).__name__)

    def notify_watchers(self, deal: Deal, msg: Message) -> int:
        """Skickar fyndet privat till dem som bevakar märket/storleken (/bevaka)."""
        tg = next((n for n in self.notifiers if isinstance(n, TelegramNotifier)), None)
        if not tg or not self.store:
            return 0
        matches: dict[int, list[str]] = {}
        for _, user_id, query, sizes in self.store.watches():
            if watch_matches(query, sizes, deal.item) and str(user_id) != str(tg.chat_id):  # ägaren får det redan
                matches.setdefault(user_id, []).append(watch_label(query, sizes))
        sent = 0
        for user_id, labels in matches.items():
            try:
                if not tg.is_member(user_id):
                    self.store.pause_watches(user_id)  # har lämnat gruppen
                    continue
                private = Message(f"🔔 {msg.title}", [f"🔔 Din bevakning: {', '.join(labels)}"] + msg.lines,
                                  msg.url, msg.photo, item_id=msg.item_id)
                tg.send_private(str(user_id), private)
                sent += 1
            except NotifyError as e:
                if e.status == 403 or "chat not found" in e.description.lower():
                    self.store.pause_watches(user_id)  # har stoppat boten – inga fler försök
                log.warning("Kunde inte skicka en bevakning (%s)", e.status)
            except Exception as e:  # ett konstigt fel för en person får inte stoppa de andra
                log.warning("Kunde inte skicka en bevakning (%s)", type(e).__name__)
        return sent

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
        """Ett varv, och kom ihåg om Vinted spärrade det (för larmen till ägaren)."""
        try:
            total = self._run_cycle()
        except Blocked:
            self.cycle_blocked = True
            raise
        self.cycle_blocked = False
        return total

    def _run_cycle(self) -> int:
        """Ett varv genom sökningarna. Med tidsgräns avbryts varvet när tiden är slut, och nästa
        varv börjar där det här slutade – så att varje körning i molnet ryms inom en debiterad minut."""
        started = time.monotonic()  # tidsgränsen räknar även in fynd som skickas om
        self._cycle_started, self._inspect_off = started, False
        self.apply_tuning()
        self._pauses_left = self.block_pauses
        for notifier in self.notifiers:
            if isinstance(notifier, TelegramNotifier):
                notifier.new_run()
                if not self.dry_run:
                    try:
                        self.remove_unwatched_deals(notifier)
                        notifier.prepare_topics(self.topic_names())
                        notifier.post_info(self.info_pages(), locked={"👋 Välkommen", "📖 Start här",
                                                                      str(self.cfg.get("stats_topic") or ""),
                                                                      str(self.cfg.get("resell_topic") or "")})
                    except Exception as e:  # ämnena får aldrig stoppa sökningarna
                        log.warning("Telegram: kunde inte förbereda ämnena (%s) – fortsätter", type(e).__name__)
        try:
            self.flush_pending()
        except Exception:
            log.exception("Kunde inte skicka om väntande fynd – fortsätter med sökningarna")
        n = len(self.searches)
        rotate = bool(self.time_budget and self.store)
        start_at = int(self.store.get_setting("next_search") or 0) % n if rotate else 0
        order = [(s, False) for s in self.searches[start_at:] + self.searches[:start_at]]
        # Toppmärkena (hot: true) körs en extra gång, ett halvt varv från den vanliga sökningen: de bästa
        # fynden säljs på minuter. (Ligger den vanliga sent i varvet kommer den extra före.)
        half = n // 2
        extra = [(pos + half if pos < half else pos - half, s) for pos, (s, _) in enumerate(order) if s.hot]
        for at, search in sorted(extra, key=lambda e: e[0], reverse=True):  # bakifrån, så att platserna står kvar
            order.insert(at, (search, True))
        total = done = 0
        try:
            for i, (search, extra) in enumerate(order):
                if i:
                    if self.time_budget and time.monotonic() - started >= self.time_budget:
                        log.info("Tidsgränsen (%g s) nådd – %d sökningar tas först nästa körning.",
                                 self.time_budget, n - done)
                        break
                    self._pause()
                if not extra:
                    done += 1
                try:
                    found = self._run_search_or_wait(search, started)
                except Blocked:
                    done -= not extra  # försök med samma sökning först nästa gång
                    raise
                except FetchError as e:
                    log.warning("%s: %s", search.name, e)
                    continue
                except Exception:
                    log.exception("%s: oväntat fel – hoppar över sökningen det här varvet", search.name)
                    continue
                total += found
                if found:  # bara antal – inga annonser, så det går bra även i publika loggar
                    log.info("%s: %d fynd", search.name, found)
        finally:
            if rotate:
                self.store.set_setting("next_search", str((start_at + done) % n))
        # Sålt-kollen får köra en minut efter tidsgränsen: i molnet lyssnar boten ändå på
        # Telegram ett par minuter till, så det tar ingen tid från sökningarna.
        deadline = started + self.time_budget + 60 if self.time_budget else None
        for check, what in ((self.check_sold, "om fynden är sålda"), (self.check_follows, "de följda annonserna"),
                            (self.check_private_watches, "de privata bevakningarna")):
            try:
                check(deadline=deadline)
            except Blocked:
                break  # Vinted spärrar just nu – kollas nästa varv
            except Exception:
                log.exception("Kunde inte kolla %s", what)
        try:
            self.use_spare_time()
        except Blocked:
            pass  # Vinted spärrar – nästa körning tar vid
        except Exception:
            log.exception("Kunde inte använda den lediga tiden")
        return total

    def use_spare_time(self) -> int:
        """Tiden som blir över innan körningen slutar (i molnet lyssnar boten ändå på Telegram):
        toppmärkena (hot: true) söks igen och de privata bevakningarna kollas. Samma lugna tempo mot Vinted."""
        hot = [s for s in self.searches if s.hot]
        if not self.spare_end or not hot or self.dry_run:
            return 0
        rounds = found = 0
        while time.monotonic() + 30 < self.spare_end:
            for search in hot:
                if time.monotonic() + 15 >= self.spare_end:
                    break
                self._pause()
                try:
                    found += self.run_search(search)
                except Blocked:
                    raise
                except FetchError:
                    continue
            rounds += 1
            self.check_private_watches(deadline=self.spare_end)
        if rounds:
            log.info("Sökte toppmärkena %d gånger till i den lediga tiden (%d fynd).", rounds, found)
        return found

    def topic_names(self) -> list[str]:
        names = list(self.info_pages()) + [str(name) for name in self.cfg.get("topics") or {}]
        hour = self.cfg.get("daily_summary_hour")
        if hour is not None and hour is not False and self.cfg.get("summary_topic"):
            names.append(str(self.cfg["summary_topic"]))
        return names

    def remove_unwatched_deals(self, tg: "TelegramNotifier", limit: int = 100) -> int:
        """Tar bort inlägg med fynd i märken som inte bevakas längre (märket borttaget ur config.yaml)."""
        if not self.store or not self.cfg.get("remove_unwatched_deals", True):
            return 0

        def watched(brand: str) -> bool:
            return any((r := s.rule_for(brand)) and not r.skip for s in self.searches)

        removed = failed = too_old = 0
        reason = ""
        for chat, message_id, brand, ts in self.store.sent_with_brand():
            if brand is None or watched(brand):
                continue
            if time.time() - (ts or 0) > 47 * 3600:  # Telegram låter bara boten ta bort inlägg yngre än 48 timmar
                too_old += 1
                self.store.mark_deleted(chat, message_id)
                continue
            if removed + failed >= limit:
                break
            try:
                why = tg.delete_message(chat, message_id)
            except (NotifyError, OSError, http.client.HTTPException):
                continue  # tillfälligt fel – nytt försök nästa varv
            if why:
                failed, reason = failed + 1, reason or why
            else:
                removed += 1
            self.store.mark_deleted(chat, message_id)  # försök inte igen
        if removed or failed or too_old:
            log.info("Tog bort %d inlägg med märken som inte bevakas längre; %d var för gamla (över 48 timmar)%s.",
                     removed, too_old, f", {failed} nekades av Telegram ({reason})" if failed else "")
        return removed

    def info_pages(self) -> dict[str, str]:
        """Text (HTML) för flikarna "👋 Välkommen" och "📖 Start här"."""
        if not self.cfg.get("info_topics", True):
            return {}
        pages = {"👋 Välkommen": welcome_text(), "📖 Start här": guide_text(self.searches, self.cfg)}
        if chat := self.cfg.get("chat_topic"):
            pages[str(chat)] = chat_text()
        if (stats := self.cfg.get("stats_topic")) and self.store:
            pages[str(stats)] = self.stats_page()
        if price := self.cfg.get("price_topic"):
            pages[str(price)] = price_text()
        if resell := self.cfg.get("resell_topic"):
            pages[str(resell)] = resell_text()
        return pages

    def stats_page(self, now: datetime | None = None) -> str:
        """Statistikfliken räknas om en gång per dag (så att inlägget bara redigeras då)."""
        now = now or datetime.now(STOCKHOLM)
        today = now.date().isoformat()
        cached = self.store.get_setting("stats:text")
        if cached and self.store.get_setting("stats:day") == today:
            return cached
        rows = self.store.deal_outcomes(int(now.timestamp()) - 30 * 86400)
        text = stats_text(rows, self.searches, self.cfg, now.strftime("%-d/%-m"))
        self.store.set_setting("stats:text", text)
        self.store.set_setting("stats:day", today)
        return text

    def check_sold(self, max_checks: int = 8, deadline: float | None = None) -> int:
        """Kollar äldre fynd igen och ändrar dem till "❌ SÅLD" i Telegram när plaggen är sålda."""
        tg = [n for n in self.notifiers if isinstance(n, TelegramNotifier)]
        if self.dry_run or not self.store or not tg or not hasattr(self.client, "fetch_listing_page"):
            return 0
        rows = self.store.due_sold_checks(limit=max_checks)
        items: dict[int, list[tuple]] = {}
        for row in rows:
            items.setdefault(row[2], []).append(row)
        marked = 0
        for i, (item_id, messages) in enumerate(list(items.items())[:max_checks]):
            if i:
                if deadline and time.monotonic() >= deadline:
                    break
                self._pause()
            try:
                body = self.client.fetch_listing_page(item_id)
            except Blocked:
                raise  # Vinted spärrar – sluta kolla det här varvet
            except FetchError as e:
                log.debug("Kunde inte kolla om ett fynd är sålt: %s", e)
                continue  # nytt försök nästa varv
            done = True
            try:
                listing = parse_listing(body, self.client.base, item_id) if body else None
                status = "BORTTAGEN" if body is None else ("SÅLD" if listing and listing.sold else None)
                for chat, message_id, _, kind, html, url, sent_at in (messages if status else []):
                    label = f"{status} {within(time.time() - sent_at)}" if status == "SÅLD" else status
                    try:
                        tg[0].mark_sold(chat, message_id, kind, html, url, label)
                    except Exception as e:  # bara Telegrams förklaring – hela svaret kan innehålla chatt-id
                        done = done and not temporary_error(e)  # tillfälligt fel: försök igen nästa varv
                        log.warning("Kunde inte markera ett fynd som sålt: %s",
                                    e.description if isinstance(e, NotifyError) else type(e).__name__)
            except Exception as e:  # oväntad annonssida: räkna kollen, så att den inte stoppar de andra
                log.warning("Kunde inte kolla om ett fynd är sålt (%s)", type(e).__name__)
                status = None
            if not done:
                continue
            marked += bool(status)
            self.store.sold_checked(item_id, status)
        if marked:
            log.info("Markerade %d fynd som sålda.", marked)
        return marked

    def daily_summary(self, now: datetime | None = None) -> bool:
        """Skickar "Dagens 5 bästa fynd" en gång per dag, efter daily_summary_hour (svensk tid)."""
        hour = self.cfg.get("daily_summary_hour")
        if hour is None or hour is False or self.dry_run or not self.store or not self.notifiers:
            return False
        now = now or datetime.now(STOCKHOLM)
        today = now.date().isoformat()
        if now.hour < int(hour) or self.store.get_setting("summary:last") == today:
            return False
        rows = self.store.top_deals(int(now.timestamp()) - 86400)
        self.store.set_setting("summary:last", today)
        if not rows:
            return False
        lines, buttons = [], []
        for i, (brand, title, price, profit, url, search) in enumerate(rows, 1):
            lines.append(f"{i}. {brand or 'Okänt märke'} – {money(price)} kr (≈ +{money(profit)} kr) · {title[:60]}")
            buttons.append([{"text": f"{i}. {brand or 'Fynd'} – {money(price)} kr", "url": url}])
        msg = Message(f"⭐ Dagens {len(rows)} bästa fynd", lines + ["", "Tryck på en knapp för att öppna annonsen."],
                      f"https://www.{self.cfg['domain']}/", topic=self.cfg.get("summary_topic") or None, buttons=buttons)
        if not self._deliver(msg):
            self.store.set_setting("summary:last", "")  # försök igen nästa varv
            return False
        log.info("Skickade dagens bästa fynd.")
        return True

    def weekly_report(self, now: datetime | None = None) -> bool:
        """Veckorapport på söndagar efter daily_summary_hour (svensk tid), en gång per vecka."""
        hour = self.cfg.get("daily_summary_hour")
        if not self.cfg.get("weekly_report", True) or self.dry_run or not self.store or not self.notifiers:
            return False
        hour = 20 if hour is None or hour is False else int(hour)
        now = now or datetime.now(STOCKHOLM)
        year, week, _ = now.isocalendar()
        if now.weekday() != 6 or now.hour < hour or self.store.get_setting("weekly:last") == f"{year}-{week}":
            return False
        stats = self.store.week_stats(int(now.timestamp()) - 7 * 86400)
        self.store.set_setting("weekly:last", f"{year}-{week}")
        if not stats["deals"]:
            return False
        lines = [f"🔥 {stats['deals']} fynd den här veckan"]
        if stats["sent"]:
            line = f"❌ {stats['sold']} av {stats['sent']} såldes"
            if stats["median_sold_after"] is not None:
                line += f" – hälften av dem {within(stats['median_sold_after'])}"
            lines.append(line)
        if stats["brand"]:
            brand, count, profit = stats["brand"]
            lines.append(f"🏆 Flest fynd: {brand} ({count} st, ≈ +{money(profit)} kr sammanlagt)")
        buttons = None
        if stats["best"]:
            brand, title, price, profit, url = stats["best"]
            lines.append(f"💎 Största fyndet: {brand or 'Okänt märke'} – {money(price)} kr (≈ +{money(profit)} kr) · {title[:50]}")
            buttons = [[{"text": f"💎 {brand or 'Fynd'} – {money(price)} kr", "url": url}]]
        if stats["top_claimer"]:
            name, count = stats["top_claimer"]
            lines.append(f"🙋 Flest tagna fynd: {name} ({count} st)")
        msg = Message("📊 Veckans fynd", lines, f"https://www.{self.cfg['domain']}/",
                      topic=self.cfg.get("summary_topic") or None,
                      buttons=buttons or [[{"text": "Öppna Vinted", "url": f"https://www.{self.cfg['domain']}/"}]])
        if not self._deliver(msg):
            self.store.set_setting("weekly:last", "")  # försök igen nästa varv
            return False
        log.info("Skickade veckorapporten.")
        return True

    def monthly_leaderboard(self, now: datetime | None = None) -> bool:
        """Förra månadens vinsttopplista, den 1:a i månaden efter daily_summary_hour."""
        hour = self.cfg.get("daily_summary_hour")
        if self.dry_run or not self.store or not self.notifiers or hour is None or hour is False:
            return False
        now = now or datetime.now(STOCKHOLM)
        last_month = month_start(month_start(now) - timedelta(days=1))
        key = last_month.strftime("%Y-%m")
        if now.day != 1 or now.hour < int(hour) or self.store.get_setting("monthly:last") == key:
            return False
        self.store.set_setting("monthly:last", key)
        msg = leaderboard_message(self.store, self.cfg, last_month)
        if not self.store.leaderboard(int(last_month.timestamp()), int(month_start(now).timestamp())):
            return False
        msg.title = "🏆 Förra månadens topplista"
        msg.url, msg.topic = f"https://www.{self.cfg['domain']}/", self.cfg.get("summary_topic") or None
        msg.buttons = [[{"text": "Öppna Vinted", "url": msg.url}]]
        if not self._deliver(msg):
            self.store.set_setting("monthly:last", "")
            return False
        return True

    TUNE_MIN, TUNE_MAX = 0.8, 1.5  # vinstgränsen kan bli 80–150 % av den i config.yaml

    def apply_tuning(self) -> None:
        """Sätter varje sökings vinstgräns till config.yaml-värdet gånger den sparade justeringen."""
        for search in self.searches:
            if search.base_min_profit is None:
                search.base_min_profit = search.min_profit
            factor = 1.0
            if self.store and self.cfg.get("auto_tune", True):
                try:
                    factor = float(self.store.get_setting(f"tune:{search.name}") or 1)
                except ValueError:
                    pass
            search.min_profit = round(search.base_min_profit * min(self.TUNE_MAX, max(self.TUNE_MIN, factor)))

    def auto_tune(self, now: datetime | None = None) -> list[str]:
        """En gång per dygn: fynd som sällan säljs inom ett dygn höjer sökningens vinstgräns (färre, bättre fynd);
        fynd som säljs på minuter sänker den lite (fler fynd i det som går åt). Bara sökningar med nog många fynd."""
        if not self.cfg.get("auto_tune", True) or self.dry_run or not self.store:
            return []
        now = now or datetime.now(STOCKHOLM)
        if self.store.get_setting("tune:last") == now.date().isoformat():
            return []
        self.store.set_setting("tune:last", now.date().isoformat())
        until = int(now.timestamp()) - 86400                 # fynd som hunnit få ett dygn på sig
        changed = []
        for search in self.searches:
            deals, sold, median = self.store.search_outcomes(search.name, until - 14 * 86400, until)
            key = f"tune:{search.name}"
            factor = float(self.store.get_setting(key) or 1)
            if deals >= 5 and sold / deals < 0.2:
                new = min(self.TUNE_MAX, factor * 1.1)       # fynden blir liggande: kräv mer vinst
            elif sold >= 3 and sold / deals >= 0.6 and median is not None and median <= 1800:
                new = max(self.TUNE_MIN, factor * 0.95)      # allt går åt på minuter: släpp fram lite fler
            else:
                continue
            if abs(new - factor) > 1e-9:
                self.store.set_setting(key, f"{new:.3f}")
                changed.append(search.name)
        self.apply_tuning()
        if changed:
            log.info("Justerade vinstgränsen för %d sökningar efter hur fynden säljs: %s", len(changed), ", ".join(changed))
        return changed

    PRIVATE_EVERY = 540    # varje privat bevakning söks ungefär var 10:e minut
    PRIVATE_DEAL = 0.6     # utan maxpris: högst 60 % av vad sökordet brukar kosta just nu

    def check_private_watches(self, max_queries: int = 4, deadline: float | None = None) -> int:
        """/bevaka för märken som gruppen inte bevakar: boten söker själv och skickar bra annonser privat.
        Inget av det postas i gruppen."""
        tg = next((n for n in self.notifiers if isinstance(n, TelegramNotifier)), None)
        if self.dry_run or not self.store or not tg or not hasattr(self.client, "fetch"):
            return 0
        maxes = self.store.watch_max_prices()
        groups: dict[str, list[tuple]] = {}
        for watch_id, user_id, query, sizes in self.store.watches():
            if not covered_by_group(query, self.searches):
                groups.setdefault(" ".join(words(query)), []).append((watch_id, user_id, query, sizes, maxes.get(watch_id)))
        now = time.time()
        due = sorted((float(self.store.get_setting(f"pw:last:{k}") or 0), k) for k in groups)
        due = [k for last, k in due if now - last >= self.PRIVATE_EVERY][:max_queries]
        catalogs = sorted({c for s in self.searches for c in s.catalog_ids})
        sent = 0
        for i, key in enumerate(due):
            if deadline and time.monotonic() >= deadline:
                break
            if i or self.searches:
                self._pause()
            name = f"privat:{key}"
            search = build_search({"name": name, "query": groups[key][0][2], "catalog": catalogs, "resale": 1,
                                   "max_price": 1_000_000, "min_profit": 0}, self.cfg)
            try:
                items = self.client.fetch(search, 1, price_cap=False)
            except Blocked:
                raise
            except FetchError:
                continue
            self.store.set_setting(f"pw:last:{key}", str(int(now)))
            wanted = words(key)
            items = [it for it in items if all(w in set(words(f"{it.brand} {it.title}")) for w in wanted)]
            prices = sorted(it.price for it in items)
            if len(prices) >= 8:
                self.store.set_setting(f"pw:median:{key}", str(prices[(len(prices) - 1) // 2]))
            median = float(self.store.get_setting(f"pw:median:{key}") or 0) or None
            first = not self.store.is_initialized(name)
            for it in items:
                if self.store.seen_price(it.id, name) is not None:
                    continue
                self.store.mark_seen(it.id, it.price, name)
                if first:
                    continue  # första gången: bara spara det som redan finns
                for watch_id, user_id, query, sizes, max_price in groups[key]:
                    limit = max_price or (median * self.PRIVATE_DEAL if median else None)
                    if not limit:
                        continue
                    check = build_search({"name": name, "query": query, "catalog": catalogs, "resale": max(limit, median or 0) + 1,
                                          "max_price": limit, "min_profit": -1e9, "min_roi_percent": -1e9,
                                          "sizes": sizes}, self.cfg)
                    deal, _ = evaluate(it, check, self.cfg)
                    if not deal:
                        continue
                    lines = [it.title, f"📏 {it.size or '–'} · {it.condition or 'okänt skick'}"]
                    if median:
                        lines.append(f"📊 \"{query}\" kostar runt {money(median)} kr på Vinted just nu "
                                     f"({max(0, round((1 - it.price / median) * 100))} % under)")
                    lines.append(f"🔔 Din privata bevakning: {watch_label(query, sizes, max_price)}")
                    msg = Message(f"🔔 {it.brand or query} – {money(it.price)} kr", lines, it.url, it.photo)
                    try:
                        tg.send_private(str(user_id), msg)
                        sent += 1
                    except NotifyError as e:
                        if e.status == 403 or "chat not found" in e.description.lower():
                            self.store.pause_watches(user_id)
                        log.warning("Kunde inte skicka en privat bevakning (%s)", e.status)
                    except Exception as e:
                        log.warning("Kunde inte skicka en privat bevakning (%s)", type(e).__name__)
            if first:
                self.store.set_initialized(name)
        return sent

    def check_follows(self, max_checks: int = 5, deadline: float | None = None) -> int:
        """Kollar annonser som medlemmar följer (/följ): prissänkt eller såld -> privat meddelande."""
        tg = next((n for n in self.notifiers if isinstance(n, TelegramNotifier)), None)
        if self.dry_run or not self.store or not tg or not hasattr(self.client, "fetch_listing_page"):
            return 0
        told = 0
        for i, (item_id, people) in enumerate(self.store.due_follows(max_checks).items()):
            if i:
                if deadline and time.monotonic() >= deadline:
                    break
                self._pause()
            try:
                body = self.client.fetch_listing_page(item_id)
                listing = parse_listing(body, self.client.base, item_id) if body else None
            except Blocked:
                raise
            except Exception:
                continue  # nytt försök nästa varv
            url = f"https://www.{self.cfg['domain']}/items/{item_id}"
            gone = body is None or bool(listing and listing.sold)
            price = listing.price if listing else None
            for user_id, title, old in people:
                if gone:
                    msg = Message("❌ Annonsen du följer är såld", [title or "", "Den är borttagen från dina följda annonser."], url)
                elif price is not None and old is not None and price < old - 0.5:
                    msg = Message(f"📉 Prissänkt: {money(old)} → {money(price)} kr", [title or "",
                                  f"Sänkt med {money(old - price)} kr. Skynda dig om du vill ha den!"], url)
                else:
                    continue
                try:
                    tg.send_private(str(user_id), msg)
                    told += 1
                except Exception as e:  # personen har stoppat boten e.d.
                    log.warning("Kunde inte meddela en följare (%s)", getattr(e, "status", type(e).__name__))
            self.store.follow_checked(item_id, price, gone)
        return told

    ALARM_GAP = 40 * 60        # boten har stått still så här länge
    ALARM_BLOCKED = 3          # varv i rad som Vinted spärrat
    ALARM_NO_DEALS = 12 * 3600

    def owner_alarms(self, now: float | None = None) -> list[str]:
        """Privat meddelande till ägaren när något är fel: boten har stått still, Vinted spärrar den
        flera varv i rad, eller inga fynd har kommit på länge. Körs efter varje varv."""
        tg = next((n for n in self.notifiers if isinstance(n, TelegramNotifier)), None)
        if not self.cfg.get("owner_alarms", True) or self.dry_run or not self.store or not tg:
            return []
        owner = tg.fallback_chat or (None if str(tg.chat_id).startswith("-") else str(tg.chat_id))
        now = time.time() if now is None else now
        get, put = self.store.get_setting, self.store.set_setting
        clock = lambda ts: datetime.fromtimestamp(ts, STOCKHOLM).strftime("%H:%M")  # noqa: E731
        alarms: list[tuple[str, list[str]]] = []

        last_run = float(get("health:last_run") or 0)
        put("health:last_run", str(int(now)))
        if last_run and now - last_run > self.ALARM_GAP:
            alarms.append(("🚨 Boten stod still", [
                f"Ingen körning mellan kl. {clock(last_run)} och {clock(now)} ({within(now - last_run)[5:]}).",
                "Den är igång igen nu. Fynd som kom under tiden kan ha missats."]))

        if self.cycle_blocked is not None:
            blocked = int(get("health:blocked") or 0)
            if self.cycle_blocked:
                blocked += 1
                if blocked == self.ALARM_BLOCKED:
                    alarms.append(("🚨 Vinted spärrar boten", [
                        f"Vinted har nekat boten {blocked} varv i rad, så inga nya fynd kommer just nu.",
                        "Den försöker igen av sig själv – du får besked när det släpper."]))
            else:
                if blocked >= self.ALARM_BLOCKED:
                    alarms.append(("✅ Vinted släpper igenom boten igen", ["Sökningarna fungerar som vanligt."]))
                blocked = 0
            put("health:blocked", str(blocked))

        last_deal = self.store.db.execute("SELECT MAX(ts) FROM deals").fetchone()[0]
        if last_deal and now - last_deal > self.ALARM_NO_DEALS and now - float(get("health:no_deals") or 0) > 86400:
            put("health:no_deals", str(int(now)))
            alarms.append(("🚨 Inga fynd på länge", [
                f"Senaste fyndet kom för {within(now - last_deal)[5:]} sedan.",
                "Sökningarna körs, men inget går igenom filtren – vinstgränserna kan vara för höga."]))

        repo = os.environ.get("GITHUB_REPOSITORY")
        url = f"https://github.com/{repo}/actions" if repo else f"https://www.{self.cfg['domain']}/"
        sent = []
        for title, lines in alarms if owner else []:
            try:
                tg.send_private(owner, Message(title, lines, url, buttons=[[
                    {"text": "Öppna körningarna" if repo else "Öppna Vinted", "url": url}]]))
                sent.append(title)
            except Exception as e:
                log.warning("Kunde inte skicka larmet till ägaren (%s)", getattr(e, "status", type(e).__name__))
        if sent:
            log.info("Skickade %d larm till ägaren.", len(sent))
        return sent

    def try_daily_summary(self) -> None:
        """Dagens bästa och (på söndagar) veckorapporten – fel här får aldrig stoppa boten."""
        for report, what in ((self.daily_summary, "dagens bästa fynd"), (self.weekly_report, "veckorapporten"),
                             (self.monthly_leaderboard, "månadens topplista"), (self.auto_tune, "självjusteringen"),
                             (self.owner_alarms, "larmen till ägaren")):
            try:
                report()
            except Exception:
                log.exception("Kunde inte skicka %s", what)

    def run_forever(self) -> None:
        backoff = 0
        while True:
            try:
                found = self.run_cycle()
                backoff = 0
            except Blocked as e:
                backoff = min(max(backoff * 2, 60), 900)
                log.warning("Vinted nekade anropet (%s) – pausar %d s och försöker igen", e, backoff)
                self.try_daily_summary()  # behöver inte Vinted
                time.sleep(backoff)
                continue
            self.try_daily_summary()
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
    started = time.monotonic()
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
    ap.add_argument("--legit", metavar="LÄNK", help="legitcheck av en Vinted-annons: skriver ut riskbedömningen")
    ap.add_argument("--lyssna", "--listen", dest="listen", type=float, metavar="SEK",
                    help="med --once: svara på /legit i Telegram i högst SEK sekunder från start (utan --once: hela tiden)")
    ap.add_argument("--meddela", dest="say", metavar="TEXT",
                    help="skicka TEXT till notiskanalerna, t.ex. ett larm från GitHub Actions")
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
    if args.listen is not None and not (math.isfinite(args.listen) and args.listen > 0):
        log.error("--lyssna SEK: ange ett positivt antal sekunder, t.ex. --lyssna 540")
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

    if args.say:
        # Använder databasen, så att meddelandet hamnar i samma chatt som fynden.
        store = Store(Path(args.db) if args.db else config_path.parent / "vinted_bot.db")
        server, repo = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY")
        url = f"{server}/{repo}/actions" if server and repo else f"https://www.{cfg['domain']}/"
        sent = False
        for n in build_notifiers(cfg, store):
            try:
                n.send(Message(args.say, [], url))
                sent = True
            except Exception as e:
                log.error("%s: %s", n.name, e)
        return 0 if sent else 1

    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    client = VintedClient(cfg["domain"], cfg["currency"], cfg["impersonate"], debug_dir)
    if args.legit:
        item_id = item_id_in(args.legit)
        if not item_id:
            log.error("--legit: ange en länk till en Vinted-annons (…/items/123456789-…)")
            return 2
        try:
            body = client.fetch_listing_page(item_id)
        except FetchError as e:
            log.error("Kunde inte hämta annonsen: %s", e)
            return 1
        listing = parse_listing(body, client.base, item_id) if body else None
        if listing is None:
            log.error("Hittade inte annonsen – den kan vara såld eller borttagen.")
            return 1
        print_message(legit_check(listing, searches, load_legit_tips(HERE / "legit_tips.yaml")))
        return 0
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

    db_path = Path(args.db) if args.db else config_path.parent / "vinted_bot.db"
    store = Store(db_path)
    store.prune()
    token, fixed_chat = telegram_settings(cfg)
    # Legitcheck: lyssna på Telegram medan boten kör – hela tiden i loop-läge, med --once bara med --lyssna.
    listen = bool(token) and (not args.once or args.listen is not None)
    notifiers = build_notifiers(cfg, store, legit_button=listen)
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
    listener = None
    until = started + args.listen if args.once and args.listen else None
    if until:
        bot.spare_end = until - 45  # lämna tid till dagens bästa och att spara databasen
    if listen:
        listener = threading.Thread(
            target=LegitListener(token, db_path, cfg, searches, load_legit_tips(HERE / "legit_tips.yaml"),
                                 fixed_chat=fixed_chat or None).run,
            args=(until,), name="legitcheck", daemon=True)
        listener.start()
        log.info("Legitcheck: svarar på /legit i Telegram%s.", f" i {args.listen:g} s" if until else "")
    try:
        if args.once:
            try:
                log.info("Klart: %d fynd.", bot.run_cycle())
            finally:
                bot.try_daily_summary()  # även när Vinted spärrar – sammanfattningen behöver inte Vinted
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
    finally:
        if listener and until:  # --once --lyssna: fortsätt svara på /legit tills tiden är slut
            listener.join(max(0.0, until - time.monotonic()) + 30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
