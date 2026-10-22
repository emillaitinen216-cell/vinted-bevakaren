"""Tester för vinted_bot. Kör: python -m unittest -v"""
import http.client
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from pathlib import Path

import vinted_bot as vb

BASE = "https://www.vinted.se"


def product(item_id, title, brand, second_line, price, total=None, fee=None):
    p = {
        "id": item_id,
        "title": title,
        "url": f"/items/{item_id}-x",
        "price": {"amount": f"{price:.2f}", "currencyCode": "SEK"},
        "photos": [{"url": f"https://images1.vinted.net/{item_id}.webp"}],
        "user": {"id": 1, "photo": None, "isBusiness": False},
        "itemBox": {"firstLine": brand, "secondLine": second_line, "badge": "$undefined"},
    }
    if fee is not None:
        p["serviceFee"] = {"amount": f"{fee:.2f}", "currencyCode": "SEK"}
    if total is not None:
        p["totalItemPrice"] = {"amount": f"{total:.2f}", "currencyCode": "SEK"}
    return p


def rsc_payload(products):
    """Efterliknar Vinteds RSC-svar för katalogsidan."""
    entries = [{"id": p["id"], "productItem": p, "catalogTracking": {"contentSource": "search"}} for p in products]
    return ('0:["$","$L1",null,{}]\n'
            '5:["$","$L2",null,{"filters":{"catalogIds":[257]},"items":{"items":'
            + json.dumps(entries, ensure_ascii=False) + '},"general":{}}]\n')


def html_page(products):
    """Samma data inbäddad i HTML, uppdelad på två __next_f-chunkar."""
    text = rsc_payload(products)
    half = len(text) // 2
    chunks = [text[:half], text[half:]]
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks)
    return f"<!DOCTYPE html><html><body><script>self.__next_f.push([0])</script>{scripts}</body></html>"


SAMPLE = [
    product(101, "Nudie Grim Tim selvage", "Nudie Jeans", "46 | W29 · Mycket bra", 150, total=165),
    product(102, "Diesel Zatiny bootcut", "Diesel", "50 | W34 · Bra", 120, fee=13.5),
    product(103, "Jeans utan märke", "$undefined", "M", 50),
]


def make_cfg(**overrides):
    cfg = dict(vb.DEFAULTS)
    cfg["condition_multipliers"] = {vb.fold(k): v for k, v in vb.DEFAULTS["condition_multipliers"].items()}
    cfg["delay_between_searches"] = [0, 0]
    cfg["min_roi_percent"] = 0
    cfg.update(overrides)
    return cfg


def make_search(cfg, **raw):
    base = {"name": "Test", "query": "jeans", "catalog": [257], "max_price": 220, "min_profit": 100,
            "brands": {"nudie": 400}}
    base.update(raw)
    return vb.build_search(base, cfg)


def item(**kw):
    defaults = dict(id=1, title="Nudie jeans", brand="Nudie Jeans", size="46 | W30", condition="Mycket bra",
                    price=150, total=165, currency="SEK", url=f"{BASE}/items/1")
    defaults.update(kw)
    return vb.Item(**defaults)


class ParseTests(unittest.TestCase):
    def check_sample(self, items):
        self.assertEqual([i.id for i in items], [101, 102, 103])
        a, b, c = items
        self.assertEqual(a.brand, "Nudie Jeans")
        self.assertEqual((a.size, a.condition), ("46 | W29", "Mycket bra"))
        self.assertEqual((a.price, a.total, a.currency), (150.0, 165.0, "SEK"))
        self.assertEqual(a.url, f"{BASE}/items/101-x")
        self.assertEqual(a.photo, "https://images1.vinted.net/101.webp")
        self.assertEqual(b.total, 133.5)          # pris + serviceFee när totalpris saknas
        self.assertEqual(c.brand, "")             # "$undefined" blir tom sträng
        self.assertEqual((c.size, c.condition), ("M", ""))
        self.assertEqual(c.total, 50 + vb.buyer_fee(50))

    def test_rsc(self):
        self.check_sample(vb.parse_items(rsc_payload(SAMPLE), BASE))

    def test_html(self):
        self.check_sample(vb.parse_items(html_page(SAMPLE), BASE))

    def test_empty_result_and_unknown_format(self):
        self.assertEqual(vb.parse_items(rsc_payload([]), BASE), [])
        self.assertIsNone(vb.parse_items("<html><body>Vänta...</body></html>", BASE))

    def test_fallback_without_list_marker(self):
        text = "".join(json.dumps({"id": p["id"], "productItem": p}, separators=(",", ":")) for p in SAMPLE[:2])
        self.assertEqual([i.id for i in vb.parse_items(text, BASE)], [101, 102])

    def test_split_second_line(self):
        self.assertEqual(vb.split_second_line("46 | W29 · Mycket bra"), ("46 | W29", "Mycket bra"))
        self.assertEqual(vb.split_second_line("Ny med prislapp"), ("", "Ny med prislapp"))
        self.assertEqual(vb.split_second_line("XL"), ("XL", ""))
        self.assertEqual(vb.split_second_line(""), ("", ""))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_skip_wins_over_longer_brand_name(self):
        s = make_search(self.cfg, brands={"Ralph Lauren": 1000, "Chaps": "ignorera", "Burberry": 900,
                                          "Thomas Burberry": None})
        self.assertTrue(s.rule_for("Ralph Lauren Chaps").skip)
        self.assertTrue(s.rule_for("Thomas Burberry").skip)
        self.assertFalse(s.rule_for("Polo Ralph Lauren").skip)

    def test_exact_brand(self):
        s = make_search(self.cfg, brands={"Essentials": {"resale": 560, "exact": True}, "Fear of God": 560})
        self.assertEqual(s.rule_for("ESSENTIALS").label, "Essentials")
        self.assertIsNone(s.rule_for("Nike Sport Essentials"))
        self.assertIsNone(s.rule_for("Amazon Essentials"))
        self.assertEqual(s.rule_for("Fear of God Essentials").label, "Fear of God")

    def test_brand_matching(self):
        s = make_search(self.cfg, brands={"levis": 400, "cp company": 1800, "ralph lauren": 1000,
                                          "polo ralph lauren": 1100, "lee": 300, "fjallraven": 900, "nudie": 400})
        self.assertEqual(s.rule_for("Levi's").label, "levis")
        self.assertEqual(s.rule_for("C.P. Company").label, "cp company")
        self.assertEqual(s.rule_for("Polo Ralph Lauren").label, "polo ralph lauren")
        self.assertEqual(s.rule_for("Lauren Ralph Lauren").label, "ralph lauren")
        self.assertEqual(s.rule_for("Fjällräven").label, "fjallraven")
        self.assertEqual(s.rule_for("Nudie Jeans").label, "nudie")
        self.assertIsNone(s.rule_for("Fleece Factory"))   # "lee" ska inte matcha mitt i ett ord
        self.assertIsNone(s.rule_for(""))

    def test_nordic_letters_and_ignored_brands(self):
        s = make_search(self.cfg, brands={"Norrøna": 900, "Burberry": 1500, "Thomas Burberry": "ignorera",
                                          "Ralph Lauren": 800, "Lauren Ralph Lauren": None})
        self.assertEqual(s.rule_for("Norrona").label, "Norrøna")
        self.assertEqual(s.rule_for("NORRØNA").label, "Norrøna")
        self.assertTrue(s.rule_for("Thomas Burberry").skip)
        self.assertFalse(s.rule_for("Burberry").skip)
        self.assertTrue(s.rule_for("Lauren Ralph Lauren").skip)
        self.assertEqual(s.price_cap(), 220)                                   # undantagna räknas inte
        deal, reason = vb.evaluate(item(brand="Thomas Burberry", title="Burberry jacka"), s, self.cfg)
        self.assertIsNone(deal)
        self.assertIn("undantaget", reason)
        with self.assertRaises(vb.ConfigError):
            make_search(self.cfg, brands={"Chaps": "ignorera"})                 # bara undantag = fel

    def test_other_brands(self):
        s = make_search(self.cfg, resale=300)
        self.assertEqual(s.rule_for("H&M").label, "övriga märken")

    def test_config_errors(self):
        with self.assertRaises(vb.ConfigError):
            make_search(self.cfg, brands={})
        with self.assertRaises(vb.ConfigError):
            make_search(self.cfg, max_price=None)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(shipping_estimate=49, sell_fee_percent=0)
        self.search = make_search(self.cfg, premium_keywords={"selvage": 100}, exclude_keywords=["replica"],
                                  suspicious_below=100)

    def test_negated_exclude_word_is_ignored(self):
        search = make_search(self.cfg, exclude_keywords=["kopia", "defekt", "fake"])
        self.assertIsNotNone(vb.evaluate(item(title="Nudie jeans äkta, ingen kopia"), search, self.cfg)[0])
        self.assertIsNotNone(vb.evaluate(item(title="Nudie not fake"), search, self.cfg)[0])
        self.assertIsNone(vb.evaluate(item(title="Nudie kopia"), search, self.cfg)[0])
        self.assertIsNone(vb.evaluate(item(title="Nudie defekt dragkedja"), search, self.cfg)[0])
        self.assertIsNone(vb.evaluate(item(title="ingen kopia? jo, kopia"), search, self.cfg)[0])
        self.assertIsNone(vb.evaluate(item(title="Nudie inte äkta utan kopia"), search, self.cfg)[0])
        self.assertIsNotNone(vb.evaluate(item(title="Nudie inte en kopia"), search, self.cfg)[0])
        self.assertIsNone(vb.evaluate(item(title="Nudie ingen, kopia"), search, self.cfg)[0])

    def test_profit_with_bonus(self):
        deal, reason = vb.evaluate(item(title="Nudie Grim Tim Selvage"), self.search, self.cfg)
        self.assertEqual(reason, "ok")
        self.assertEqual(deal.resale, 500)              # 400 + 100 för selvage
        self.assertEqual(deal.cost, 165 + 49)
        self.assertEqual(deal.profit, 500 - 214)
        self.assertAlmostEqual(deal.roi, 286 / 214)
        self.assertFalse(deal.suspicious)

    def test_condition_multiplier(self):
        deal, _ = vb.evaluate(item(condition="Ny med prislapp"), self.search, self.cfg)
        self.assertEqual(deal.resale, 500)              # 400 × 1.25
        deal, reason = vb.evaluate(item(condition="Tillfredsställande"), self.search, self.cfg)
        self.assertIsNone(deal)                         # 240 − 214 = 26 < 100
        self.assertIn("vinst", reason)

    def test_rejections(self):
        cases = {
            "pris": item(price=250, total=270),
            "bevakas inte": item(brand="H&M"),
            "replica": item(title="Nudie replica"),
        }
        for expected, it in cases.items():
            deal, reason = vb.evaluate(it, self.search, self.cfg)
            self.assertIsNone(deal)
            self.assertIn(expected, reason)

    def test_min_price(self):
        search = make_search(self.cfg, min_price=100, brands={"nudie": 400, "dondup": {"resale": 500, "min_price": 50}})
        self.assertEqual(search.price_floor(), 50)
        deal, reason = vb.evaluate(item(price=80, total=91.5), search, self.cfg)
        self.assertIsNone(deal)
        self.assertIn("utanför", reason)
        self.assertIsNotNone(vb.evaluate(item(brand="Dondup", price=80, total=91.5), search, self.cfg)[0])

    def test_size_filter(self):
        search = make_search(self.cfg, sizes=["W32", "M"])
        self.assertIsNone(vb.evaluate(item(size="46 | W30"), search, self.cfg)[0])
        self.assertIsNotNone(vb.evaluate(item(size="48 | W32"), search, self.cfg)[0])
        self.assertIsNotNone(vb.evaluate(item(size=""), search, self.cfg)[0])

    def test_suspicious_and_min_roi(self):
        deal, _ = vb.evaluate(item(price=60, total=70.5), self.search, self.cfg)
        self.assertTrue(deal.suspicious)
        cfg = make_cfg(min_roi_percent=500)
        search = make_search(cfg)
        deal, reason = vb.evaluate(item(), search, cfg)
        self.assertIsNone(deal)
        self.assertIn("ROI", reason)

    def test_message(self):
        deal, _ = vb.evaluate(item(price=60, total=70.5, title="Nudie selvage"), self.search, self.cfg)
        msg = vb.deal_message(deal, self.cfg)
        self.assertIn("Nudie Jeans", msg.title)
        self.assertIn("60 kr", msg.title)
        self.assertTrue(any("Misstänkt" in line for line in msg.lines))
        self.assertTrue(any("selvage (+100)" in line for line in msg.lines))
        self.assertEqual(msg.url, f"{BASE}/items/1")


class FakeClient:
    def __init__(self):
        self.pages = {}
        self.calls = []

    def fetch(self, search, page=1, price_cap=True):
        self.calls.append(page)
        return self.pages.get(page, [])


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.search = make_search(self.cfg)
        self.client = FakeClient()
        self.notifier = FakeNotifier()
        self.store = vb.Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.bot = vb.Bot(self.cfg, [self.search], self.client, self.store, [self.notifier])

    def test_first_run_is_silent_then_new_items_notify(self):
        self.client.pages = {1: [item(id=1), item(id=2)]}
        self.assertEqual(self.bot.run_search(self.search), 0)
        self.assertEqual(self.notifier.sent, [])

        self.client.pages = {1: [item(id=3), item(id=1), item(id=2)]}
        self.assertEqual(self.bot.run_search(self.search), 1)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.client.calls[-1], 1)      # inte alla nya → ingen sida 2

    def test_price_drop_renotifies_once(self):
        self.client.pages = {1: [item(id=1, price=210, total=228)]}
        self.bot.run_search(self.search)                # första körningen, tyst
        # Prissänkning så att den blir lönsam → notis med "Prissänkt".
        self.client.pages = {1: [item(id=1, price=150, total=165)]}
        self.assertEqual(self.bot.run_search(self.search), 1)
        self.assertTrue(any("Prissänkt" in line for line in self.notifier.sent[-1].lines))
        # Ytterligare sänkning av redan notifierad annons ger ingen ny notis.
        self.client.pages = {1: [item(id=1, price=140, total=154.5)]}
        self.assertEqual(self.bot.run_search(self.search), 0)

    def test_pages_further_when_whole_page_is_new(self):
        self.client.pages = {1: [item(id=1)]}
        self.bot.run_search(self.search)
        self.client.pages = {1: [item(id=10), item(id=11)], 2: [item(id=12), item(id=1)]}
        self.assertEqual(self.bot.run_search(self.search), 3)
        self.assertEqual(self.client.calls[-2:], [1, 2])

    def test_dry_run_reports_everything_without_store(self):
        bot = vb.Bot(self.cfg, [self.search], self.client, None, [self.notifier], dry_run=True)
        self.client.pages = {1: [item(id=1), item(id=2, brand="H&M")]}
        self.assertEqual(bot.run_search(self.search), 1)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(bot.run_search(make_search(self.cfg, name="Annan")), 0)  # samma annons visas en gång

    def test_each_search_judges_an_item_itself(self):
        broad = make_search(self.cfg, name="Bred", brands={"replay": 400})
        self.bot.searches = [broad, self.search]
        for s in (broad, self.search):
            self.store.set_initialized(s.name)
        self.client.pages = {1: [item(id=7)]}
        self.assertEqual(self.bot.run_search(broad), 0)        # inget Nudie-pris i den breda sökningen
        self.assertEqual(self.bot.run_search(self.search), 1)  # men märkets egen sökning hittar fyndet
        twin = make_search(self.cfg, name="Tvilling")
        self.store.set_initialized(twin.name)
        self.assertEqual(self.bot.run_search(twin), 0)         # och samma annons skickas inte två gånger
        self.assertEqual(len(self.notifier.sent), 1)

    def test_listing_still_up_after_90_days_is_not_sent_again(self):
        now = [1_000_000_000.0]
        with mock.patch.object(vb.time, "time", side_effect=lambda: now[0]):
            self.client.pages = {1: [item(id=5)]}
            self.bot.run_search(self.search)                     # första körningen, tyst
            self.client.pages = {1: [item(id=6), item(id=5)]}
            self.assertEqual(self.bot.run_search(self.search), 1)
            for _ in range(100):                                 # ligger kvar i 100 dagar
                now[0] += 86400
                self.store.prune()
                self.assertEqual(self.bot.run_search(self.search), 0)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_old_database_keeps_what_it_had_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gammal.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE seen (item_id INTEGER PRIMARY KEY, price REAL, first_seen INTEGER, last_seen INTEGER,
                                   notified INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE searches (name TEXT PRIMARY KEY, initialized INTEGER);
                INSERT INTO seen VALUES (1, 150, 0, 0, 0);
                INSERT INTO searches VALUES ('Test', 0);
            """)
            db.commit()
            db.close()
            store = vb.Store(path)
            self.addCleanup(store.db.close)
            bot = vb.Bot(self.cfg, [self.search], self.client, store, [self.notifier])
            self.client.pages = {1: [item(id=1), item(id=2)]}
            self.assertEqual(bot.run_search(self.search), 1)   # bara den nya annonsen
            self.assertEqual(store.seen_price(1, "Test"), 150)



class HashtagTests(unittest.TestCase):
    def test_hashtag(self):
        cases = {"Nudie": "#Nudie", "C.P. Company": "#CPCompany", "Levi's": "#Levis", "Fjällräven": "#Fjällräven",
                 "north face": "#NorthFace", "Arc'teryx": "#Arcteryx", "H&M": "#HM", "": "", "  ": ""}
        for text, expected in cases.items():
            self.assertEqual(vb.hashtag(text), expected, text)

    def test_deal_hashtags(self):
        cfg = make_cfg()
        search = make_search(cfg, brands={"Nudie": 400}, resale=200, tags=["Jeans", "jeans"])
        deal, _ = vb.evaluate(item(), search, cfg)
        self.assertEqual(deal.hashtags, ["#Nudie", "#Jeans"])          # dubbletter bort
        self.assertEqual(vb.deal_message(deal, cfg).lines[-1], "🏷 #Nudie #Jeans")
        other, _ = vb.evaluate(item(brand="Weekday Studio", price=10, total=18), search, cfg)
        self.assertEqual(other.hashtags, ["#WeekdayStudio", "#Jeans"])  # övriga märken: Vinteds märkesnamn


class NamedClient(FakeClient):
    """Returnerar en ny annons per anrop och kan nekas för en viss sökning."""

    def __init__(self, blocked=None, block_times=None):
        super().__init__()
        self.searched, self.blocked, self.next_id = [], blocked, 1000
        self.block_times = block_times  # None = alltid

    def fetch(self, search, page=1, price_cap=True):
        if page > 1:
            return []   # bara en sida per sökning
        self.searched.append(search.name)
        if search.name == self.blocked and self.block_times != 0:
            if self.block_times:
                self.block_times -= 1
            raise vb.Blocked("HTTP 403")
        self.next_id += 1
        return [item(id=self.next_id)]


class TimeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.searches = [make_search(self.cfg, name=n) for n in ("A", "B", "C", "D")]
        self.store = vb.Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.clock = [0.0]
        patcher = mock.patch.object(vb.time, "monotonic", side_effect=self.tick)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self):
        self.clock[0] += 10   # varje sökning "tar" 10 s
        return self.clock[0]

    def test_stops_at_budget_and_continues_next_run(self):
        client = NamedClient()
        bot = vb.Bot(self.cfg, self.searches, client, self.store, [], time_budget=25)
        with self.assertLogs("vinted-bot", "INFO") as logs:
            bot.run_cycle()
        self.assertEqual(client.searched, ["A", "B", "C"])
        self.assertIn("Tidsgränsen", "\n".join(logs.output))
        self.assertEqual(self.store.get_setting("next_search"), "3")
        client.searched.clear()
        with self.assertLogs("vinted-bot", "INFO"):
            bot.run_cycle()
        self.assertEqual(client.searched, ["D", "A", "B"])           # fortsätter där förra slutade
        self.assertEqual(self.store.get_setting("next_search"), "2")

    def test_blocked_search_is_first_next_time(self):
        client = NamedClient(blocked="B")
        bot = vb.Bot(self.cfg, self.searches, client, self.store, [], time_budget=100)
        with self.assertRaises(vb.Blocked):
            bot.run_cycle()
        self.assertEqual(self.store.get_setting("next_search"), "1")

    def test_waits_once_when_blocked_and_budget_allows(self):
        client = NamedClient(blocked="B", block_times=1)
        bot = vb.Bot(self.cfg, self.searches, client, self.store, [], time_budget=400)
        bot.block_cooldown = 75
        with mock.patch.object(vb.time, "sleep") as sleep, self.assertLogs("vinted-bot", "INFO") as logs:
            bot.run_cycle()
        self.assertEqual(client.searched, ["A", "B", "B", "C", "D"])   # B igen efter pausen
        self.assertIn(mock.call(75), sleep.call_args_list)
        self.assertIn("väntar 75 s", "\n".join(logs.output))

    def test_gives_up_when_blocked_without_time_to_wait(self):
        client = NamedClient(blocked="B")
        bot = vb.Bot(self.cfg, self.searches, client, self.store, [], time_budget=100)
        bot.block_cooldown = 75
        with mock.patch.object(vb.time, "sleep"), self.assertRaises(vb.Blocked):
            bot.run_cycle()
        self.assertEqual(client.searched, ["A", "B"])
        self.assertEqual(self.store.get_setting("next_search"), "1")

    def test_waits_at_most_twice_per_run(self):
        client = NamedClient(blocked="B")
        bot = vb.Bot(self.cfg, self.searches, client, self.store, [], time_budget=1000)
        bot.block_cooldown = 75
        with mock.patch.object(vb.time, "sleep") as sleep, self.assertRaises(vb.Blocked), \
                self.assertLogs("vinted-bot", "INFO"):
            bot.run_cycle()
        self.assertEqual(client.searched, ["A", "B", "B", "B"])
        self.assertEqual(sleep.call_args_list.count(mock.call(75)), 2)

    def test_without_budget_runs_everything_in_order(self):
        client = NamedClient()
        vb.Bot(self.cfg, self.searches, client, self.store, []).run_cycle()
        self.assertEqual(client.searched, ["A", "B", "C", "D"])
        self.assertIsNone(self.store.get_setting("next_search"))

    def test_say_sends_text_to_saved_chat(self):
        notifier = FakeNotifier()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(vb, "build_notifiers", return_value=[notifier]) as build, \
                mock.patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "a/b"}):
            self.assertEqual(vb.main(["--meddela", "Boten har stannat", "--db", str(Path(d) / "x.db")]), 0)
            self.assertIsInstance(build.call_args.args[1], vb.Store)   # samma chatt som fynden
            build.call_args.args[1].db.close()
        self.assertEqual(notifier.sent[0].title, "Boten har stannat")
        self.assertEqual(notifier.sent[0].url, "https://github.com/a/b/actions")

    def test_time_budget_option_is_validated(self):
        for bad in ("0", "-5", "inf", "nan"):
            with self.assertLogs("vinted-bot", "ERROR"):
                self.assertEqual(vb.main(["--max-tid", bad, "--dry-run"]), 2, bad)


NO_RIGHTS = json.dumps({"ok": False, "error_code": 400,
                        "description": "Bad Request: not enough rights to send text messages to the chat"})


class FallbackTests(unittest.TestCase):
    """Får boten inte skriva i gruppen ska fynden gå till ägarens privata chatt, och inget får försvinna."""

    def setUp(self):
        self.cfg = make_cfg()
        self.calls = []
        patcher = mock.patch.object(vb, "http_post_json", side_effect=self.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.blocked = {"-100"}

    def post(self, url, payload, timeout=20):
        self.calls.append((payload["chat_id"], payload.get("text") or payload.get("caption")))
        if payload["chat_id"] in self.blocked:
            raise vb.NotifyError(400, NO_RIGHTS)

    def msg(self, n):
        return vb.Message(f"Fynd {n}", ["rad"], f"https://www.vinted.se/items/{n}")

    def test_falls_back_to_private_chat_and_warns_once(self):
        n = vb.TelegramNotifier(TOKEN, "-100", fallback_chat="77")
        with self.assertLogs("vinted-bot", "WARNING"):
            n.send(self.msg(1))
            n.send(self.msg(2))
        to_private = [text for chat, text in self.calls if chat == "77"]
        self.assertEqual(len(to_private), 3)                                    # en förklaring + två fynd
        self.assertIn("Jag kan inte skriva i gruppen", to_private[0])
        self.assertIn("not enough rights", to_private[0])
        self.assertIn("Fynd 1", to_private[1])
        self.assertIn("Fynd 2", to_private[2])

    def test_no_fallback_raises(self):
        with self.assertRaises(vb.NotifyError):
            vb.TelegramNotifier(TOKEN, "-100").send(self.msg(1))
        self.blocked = set()
        bad_token = vb.TelegramNotifier(TOKEN, "-100", fallback_chat="77")
        with mock.patch.object(vb, "http_post_json", side_effect=vb.NotifyError(401, "Unauthorized")):
            with self.assertRaises(vb.NotifyError):
                bad_token.send(self.msg(1))                                     # fel token: ingen reserv-chatt

    def test_hint_at_most_once_per_day_across_runs(self):
        store = vb.Store(":memory:")
        self.addCleanup(store.db.close)
        with self.assertLogs("vinted-bot", "WARNING"):
            for run in range(3):                                                # tre körningar i rad
                gate = vb.daily_gate(store, "hint")
                vb.TelegramNotifier(TOKEN, "-100", fallback_chat="77", hint_gate=gate).send(self.msg(run))
        hints = [text for chat, text in self.calls if chat == "77" and "kan inte skriva" in text]
        self.assertEqual(len(hints), 1)
        self.assertEqual(len([1 for chat, text in self.calls if chat == "77" and text.startswith("<b>Fynd ")]), 3)

    def test_content_errors_do_not_use_fallback(self):
        bad_html = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})
        with mock.patch.object(vb, "http_post_json", side_effect=vb.NotifyError(400, bad_html)):
            with self.assertRaises(vb.NotifyError):
                vb.TelegramNotifier(TOKEN, "-100", fallback_chat="77").send(self.msg(1))
        self.assertTrue(vb.NotifyError(403, "Forbidden: bot was kicked").is_rights_problem)
        self.assertFalse(vb.NotifyError(400, bad_html).is_rights_problem)

    def test_too_many_requests_is_not_retried_as_text(self):
        busy = vb.NotifyError(429, json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests"}))
        calls = []

        def post(url, payload, timeout=20):
            calls.append(url.rsplit("/", 1)[-1])
            raise busy
        with mock.patch.object(vb, "http_post_json", side_effect=post), self.assertRaises(vb.NotifyError):
            vb.TelegramNotifier(TOKEN, "-100", fallback_chat="77").send(
                vb.Message("t", ["r"], "https://www.vinted.se/", photo="https://img/1.webp"))
        self.assertEqual(calls, ["sendPhoto"])                                  # inget direkt nytt försök som text

    def test_network_error_on_hint_still_delivers_deal(self):
        def post(url, payload, timeout=20):
            self.calls.append((payload["chat_id"], payload.get("text")))
            if payload["chat_id"] == "-100":
                raise vb.NotifyError(400, NO_RIGHTS)
            if "kan inte skriva" in (payload.get("text") or ""):
                raise OSError("connection reset")
        with mock.patch.object(vb, "http_post_json", side_effect=post), self.assertLogs("vinted-bot", "WARNING"):
            vb.TelegramNotifier(TOKEN, "-100", fallback_chat="77").send(self.msg(1))
        self.assertIn("Fynd 1", self.calls[-1][1])

    def test_description(self):
        self.assertEqual(vb.NotifyError(400, NO_RIGHTS).description,
                         "Bad Request: not enough rights to send text messages to the chat")
        self.assertEqual(vb.NotifyError(500, "Server error").description, "Server error")

    def test_failed_welcome_tells_owner_privately(self):
        store = vb.Store(":memory:")
        self.addCleanup(store.db.close)
        updates = [tg_msg(1, 77, "/start", 77, 100), tg_member(5, -100, "member", 77, 200, chat_type="supergroup")]
        with self.assertLogs("vinted-bot", "INFO"):
            target = vb.discover_telegram_chat(TOKEN, store, self.cfg, api=FakeTelegram(updates=[updates]))
        self.assertEqual(target, "-100")
        self.assertEqual([chat for chat, _ in self.calls], ["-100", "77"])
        self.assertIn("Jag kan inte skriva i gruppen", self.calls[-1][1])


class QuietModeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, vb, "QUIET", vb.QUIET)

    def test_quiet_hides_chat_details_and_deals(self):
        vb.QUIET = True
        self.assertEqual(vb.who("VintedSnipes", "-1004392185496"), "en grupp/kanal")
        self.assertEqual(vb.who("Emil", "6980573621"), "en privat chatt")
        cfg = make_cfg()
        bot = vb.Bot(cfg, [make_search(cfg)], FakeClient(), None, [], dry_run=True)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            deal, _ = vb.evaluate(item(), make_search(cfg), cfg)
            bot.notify(deal)
        self.assertEqual(out.getvalue(), "")                                    # inga fynd i loggen
        vb.QUIET = False
        self.assertIn("6980573621", vb.who("Emil", "6980573621"))

    def test_quiet_flag_sets_mode(self):
        with mock.patch.object(vb, "VintedClient", side_effect=RuntimeError("stopp")), self.assertRaises(RuntimeError):
            vb.main(["--tyst", "--dry-run"])
        self.assertTrue(vb.QUIET)


class FailingNotifier:
    name = "trasig"

    def __init__(self):
        self.ok = False
        self.sent = []

    def send(self, msg):
        if not self.ok:
            raise vb.NotifyError(400, NO_RIGHTS)
        self.sent.append(msg.title)


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.search = make_search(self.cfg)
        self.store = vb.Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.store.set_setting("dummy", "")
        self.client = FakeClient()
        self.notifier = FailingNotifier()
        self.bot = vb.Bot(self.cfg, [self.search], self.client, self.store, [self.notifier])
        self.store.set_initialized(self.search.name)

    def test_undelivered_deal_is_sent_next_run(self):
        self.client.pages = {1: [item(id=1)]}
        with self.assertLogs("vinted-bot", "WARNING"):
            self.assertEqual(self.bot.run_cycle(), 1)
        self.assertEqual(len(self.store.pending()), 1)
        self.notifier.ok = True
        self.client.pages = {}
        with self.assertLogs("vinted-bot", "INFO"):
            self.bot.run_cycle()
        self.assertEqual(self.notifier.sent, ["🔥 Nudie Jeans – 150 kr (≈ +186 kr)"])
        self.assertEqual(self.store.pending(), [])

    def test_one_bad_pending_does_not_block_the_rest(self):
        self.store.add_pending(vb.Message("trasig", [], "https://www.vinted.se/"))
        self.store.add_pending(vb.Message("bra", [], "https://www.vinted.se/"))

        class Picky:
            name, sent = "picky", []

            def send(self, msg):
                if msg.title == "trasig":
                    raise vb.NotifyError(400, "can't parse entities")
                self.sent.append(msg.title)
        picky = Picky()
        bot = vb.Bot(self.cfg, [self.search], self.client, self.store, [picky])
        with self.assertLogs("vinted-bot", "INFO"):
            bot.flush_pending()
        self.assertEqual(picky.sent, ["bra"])
        self.assertEqual([m.title for _, m in self.store.pending()], ["trasig"])

    def test_old_pending_is_dropped(self):
        self.store.add_pending(vb.Message("gammal", [], "https://www.vinted.se/"))
        self.store.db.execute("UPDATE pending SET ts = ts - 2 * 86400")
        self.assertEqual(self.store.pending(), [])

    def test_queued_when_channel_configured_but_unavailable(self):
        bot = vb.Bot(self.cfg, [self.search], self.client, self.store, [], queue_undelivered=True)
        self.client.pages = {1: [item(id=3)]}
        bot.run_cycle()
        self.assertEqual(len(self.store.pending()), 1)                         # sparas tills en chatt finns

    def test_flush_errors_do_not_stop_the_run(self):
        self.client.pages = {1: [item(id=4)]}
        with mock.patch.object(self.store, "pending", side_effect=sqlite3.OperationalError("låst")), \
                self.assertLogs("vinted-bot", "ERROR"):
            self.bot.run_cycle()
        self.assertIn(1, self.client.calls)                                     # sökningen kördes ändå

    def test_expired_pending_is_logged(self):
        self.store.add_pending(vb.Message("gammal", [], "https://www.vinted.se/"))
        self.store.db.execute("UPDATE pending SET ts = ts - 2 * 86400")
        with self.assertLogs("vinted-bot", "WARNING") as logs:
            self.store.pending()
        self.assertIn("slängts", "\n".join(logs.output))

    def test_no_pending_without_notifiers(self):
        bot = vb.Bot(self.cfg, [self.search], self.client, self.store, [])
        self.client.pages = {1: [item(id=2)]}
        bot.run_cycle()
        self.assertEqual(self.store.pending(), [])

class ConfigFileTests(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        cfg, searches = vb.load_config(Path(vb.HERE / "config.yaml"))
        self.assertGreater(len(searches), 5)
        for s in searches:
            self.assertTrue(s.catalog_ids, s.name)
            self.assertGreater(s.price_cap(), 0, s.name)
            for rule in s.all_rules():
                self.assertGreater(rule.resale, rule.max_price, f"{s.name}/{rule.label}")

    def test_dotenv_overrides_old_environment_but_not_with_empty(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"VB_A": "gammal", "VB_B": "behåll"}):
            env = Path(d) / ".env"
            env.write_text("VB_A=ny\nVB_B=\n", encoding="utf-8")
            vb.load_dotenv(env)
            self.assertEqual((os.environ["VB_A"], os.environ["VB_B"]), ("ny", "behåll"))

    def test_pause_option_is_validated(self):
        with self.assertLogs("vinted-bot", "ERROR"):
            self.assertEqual(vb.main(["--paus", "2", "1", "--dry-run"]), 2)

    def test_pause_option_overrides_config(self):
        seen = {}

        class StopHere(Exception):
            pass

        def fake_client(*args, **kwargs):
            raise StopHere

        real_load = vb.load_config

        def load(path):
            cfg, searches = real_load(path)
            seen["cfg"] = cfg
            return cfg, searches

        with mock.patch.object(vb, "load_config", side_effect=load), \
                mock.patch.object(vb, "VintedClient", side_effect=fake_client), self.assertRaises(StopHere):
            vb.main(["--paus", "0.5", "1.5", "--dry-run"])
        self.assertEqual(seen["cfg"]["delay_between_searches"], [0.5, 1.5])

    def test_dotenv(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("# kommentar\nVB_TEST_A=hej\nVB_TEST_B=\"citat\"\n", encoding="utf-8")
            vb.load_dotenv(env)
        self.assertEqual(os.environ.pop("VB_TEST_A"), "hej")
        self.assertEqual(os.environ.pop("VB_TEST_B"), "citat")



TOKEN = "123456789:AAHk3vZx9QeR7tYuIoPaSdFgHjKlZxCvBn0"


class FakeTelegram:
    """Låtsas-Telegram för telegram_setup: getMe svarar 401 för fel token, getUpdates spelar upp svar i tur och ordning."""

    def __init__(self, good_token=TOKEN, updates=None):
        self.good_token = good_token
        self.updates = list(updates or [])
        self.calls = []

    def __call__(self, token, method, params=None, timeout=35):
        self.calls.append((token, method, params))
        if token != self.good_token:
            raise vb.TelegramError(401, "Unauthorized")
        if method == "getMe":
            return {"id": 123456789, "is_bot": True, "username": "fyndbot"}
        if method == "getUpdates":
            return self.updates.pop(0) if self.updates else []
        raise AssertionError(method)


def isolate_notify_env(test):
    """Testet får en egen kopia av miljön utan notisinställningar; allt återställs efteråt."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    test.addCleanup(patcher.stop)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL", "NTFY_TOPIC", "NTFY_SERVER"):
        os.environ.pop(key, None)


def tg_msg(update_id, chat_id, text, from_id, date, chat_type="private"):
    chat = {"id": chat_id, "type": chat_type, ("first_name" if chat_type == "private" else "title"): f"chatt {chat_id}"}
    return {"update_id": update_id, "message": {"date": date, "text": text, "chat": chat, "from": {"id": from_id}}}


def tg_member(update_id, chat_id, new_status, from_id, date, old="left", chat_type="group", title="Fynd"):
    return {"update_id": update_id, "my_chat_member": {
        "date": date, "chat": {"id": chat_id, "type": chat_type, "title": title}, "from": {"id": from_id},
        "old_chat_member": {"status": old}, "new_chat_member": {"status": new_status}}}


def private_message(chat_id, first_name):
    return {"update_id": chat_id, "message": {"message_id": 1, "text": "hej",
            "chat": {"id": chat_id, "first_name": first_name, "type": "private"}}}


class TelegramSetupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(notify={})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / ".env"
        isolate_notify_env(self)
        self.sent = []
        patcher = mock.patch.object(vb, "http_post_json", side_effect=lambda url, payload, timeout=20: self.sent.append((url, payload)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = io.StringIO()
        stdout = mock.patch("sys.stdout", self.out)
        stdout.start()
        self.addCleanup(stdout.stop)

    def answers(self, *replies):
        """Låtsas-input: svarar i tur och ordning, sedan EOF (Ctrl+D). Frågorna sparas i self.prompts."""
        replies, self.prompts = list(replies), []

        def ask(prompt=""):
            self.prompts.append(prompt)
            if not replies:
                raise EOFError
            return replies.pop(0)
        return ask

    def test_clean_token(self):
        for raw in (TOKEN, f"<{TOKEN}>", f" {TOKEN}\n", f'"{TOKEN}"', f"bot{TOKEN}",
                    f"https://api.telegram.org/bot<{TOKEN}>/getUpdates", f"TELEGRAM_BOT_TOKEN={TOKEN}",
                    f"https://api.telegram.org/bot%20{TOKEN}/getUpdates", f"https://api.telegram.org/bot%3C{TOKEN}%3E/getMe"):
            self.assertEqual(vb.clean_token(raw), TOKEN, raw)
        for raw in ("", None, "hej", "123456789:kort", "AAHk3vZx9QeR7tYuIoPaSdFgHjKlZxCvBn0"):
            self.assertIsNone(vb.clean_token(raw), raw)
        self.assertNotIn(TOKEN[10:30], vb.mask_token(TOKEN))

    def test_chats_from_updates(self):
        updates = [
            private_message(11, "Emil"),
            {"update_id": 2, "my_chat_member": {"chat": {"id": -100, "title": "Fynd", "type": "group"}}},
            private_message(11, "Emil"),
            {"update_id": 4, "callback_query": {"id": "x"}},
            {"update_id": 5, "my_chat_member": {"chat": {"id": 99, "first_name": "Blockerad", "type": "private"},
                                                "new_chat_member": {"status": "kicked"}}},
        ]
        chats = vb.chats_from_updates(updates)
        self.assertEqual([c["id"] for c in chats], [-100, 11])      # senaste sist, inga dubbletter
        self.assertEqual(chats[0]["name"], "Fynd")
        self.assertEqual(chats[1], {"id": 11, "name": "Emil", "type": "private"})

    def test_update_env_file_keeps_other_lines(self):
        self.env_path.write_text("# kommentar\nTELEGRAM_BOT_TOKEN=\nNTFY_TOPIC=abc\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
        vb.update_env_file(self.env_path, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})
        self.assertEqual(self.env_path.read_text(encoding="utf-8"),
                         f"# kommentar\nTELEGRAM_BOT_TOKEN={TOKEN}\nNTFY_TOPIC=abc\nTELEGRAM_CHAT_ID=42\n")
        new = Path(self.tmp.name) / "ny.env"
        vb.update_env_file(new, {"A": "1"})
        self.assertEqual(new.read_text(encoding="utf-8"), "A=1\n")

    def test_wrong_saved_token_then_pasted_with_brackets(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "987654321:" + "x" * 35        # fel token i .env
        api = FakeTelegram(updates=[[], [private_message(555, "Emil")]])
        rc = vb.telegram_setup(self.cfg, self.env_path, ask=self.answers(f"<{TOKEN}>"), api=api, wait_seconds=5)
        self.assertEqual(rc, 0, self.out.getvalue())
        env = self.env_path.read_text(encoding="utf-8")
        self.assertIn(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", env)
        self.assertIn("TELEGRAM_CHAT_ID=555\n", env)
        self.assertIn("känner inte igen", self.out.getvalue())
        self.assertNotIn(TOKEN, self.out.getvalue())                     # token skrivs aldrig ut i klartext
        url, payload = self.sent[-1]
        self.assertIn(TOKEN, url)
        self.assertEqual(payload["chat_id"], "555")

    def test_gives_up_on_garbage_and_eof(self):
        rc = vb.telegram_setup(self.cfg, self.env_path, ask=self.answers("hej", "<DIN_TOKEN>"), api=FakeTelegram())
        self.assertEqual(rc, 1)
        self.assertIn("ser inte ut som en token", self.out.getvalue())
        self.assertFalse(self.env_path.exists())

    def test_no_message_within_wait(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        rc = vb.telegram_setup(self.cfg, self.env_path, ask=self.answers(), api=FakeTelegram(), wait_seconds=0)
        self.assertEqual(rc, 1)
        self.assertIn("@fyndbot", self.out.getvalue())
        self.assertFalse(self.env_path.exists())

    def test_waiting_acknowledges_updates_without_chat(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        noise = [{"update_id": 40, "callback_query": {"id": "x"}}]
        api = FakeTelegram(updates=[noise, [private_message(7, "Emil")]])
        self.assertEqual(vb.telegram_setup(self.cfg, self.env_path, ask=self.answers(), api=api, wait_seconds=5), 0)
        polls = [params for _, method, params in api.calls if method == "getUpdates"]
        self.assertEqual(polls[1]["offset"], 41)

    def test_saved_token_can_be_replaced(self):
        old_token = "987654321:" + "y" * 35
        os.environ["TELEGRAM_BOT_TOKEN"] = old_token
        new_bot = FakeTelegram(updates=[[private_message(3, "Emil")]])

        def api(token, method, params=None, timeout=35):
            if token == old_token and method == "getMe":
                return {"username": "gamlabot"}
            return new_bot(token, method, params, timeout)

        rc = vb.telegram_setup(self.cfg, self.env_path, ask=self.answers(TOKEN), api=api)
        self.assertEqual(rc, 0, self.out.getvalue())
        self.assertIn("@gamlabot", self.prompts[0])
        self.assertIn("@fyndbot", self.out.getvalue())
        self.assertIn(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", self.env_path.read_text(encoding="utf-8"))

    def test_network_error_is_explained(self):
        def offline(token, method, params=None, timeout=35):
            raise vb.TelegramError(None, "kunde inte nå api.telegram.org ([Errno -3] Temporary failure)")
        rc = vb.telegram_setup(self.cfg, self.env_path, ask=self.answers(TOKEN), api=offline)
        self.assertEqual(rc, 1)
        self.assertIn("Kontrollera internetanslutningen", self.out.getvalue())

    def test_choose_between_several_chats(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        api = FakeTelegram(updates=[[private_message(1, "Ett"), private_message(2, "Två")]])
        rc = vb.telegram_setup(self.cfg, self.env_path, ask=self.answers("", "1"), api=api)
        self.assertEqual(rc, 0)
        self.assertIn("TELEGRAM_CHAT_ID=1\n", self.env_path.read_text(encoding="utf-8"))

    def test_build_notifiers_cleans_token(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = f"<{TOKEN}>"
        os.environ["TELEGRAM_CHAT_ID"] = " 555 "
        notifiers = vb.build_notifiers(self.cfg)
        self.assertEqual(len(notifiers), 1)
        self.assertTrue(notifiers[0].api.endswith(f"/bot{TOKEN}"))
        self.assertEqual(notifiers[0].chat_id, "555")
        os.environ["TELEGRAM_BOT_TOKEN"] = "DIN_TOKEN"
        with self.assertLogs("vinted-bot", "WARNING"):
            self.assertEqual(vb.build_notifiers(self.cfg), [])



class DiscoverChatTests(unittest.TestCase):
    """Chat-id hittas automatiskt när bara token är satt (t.ex. i GitHub Actions)."""

    def setUp(self):
        self.cfg = make_cfg()
        self.store = vb.Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.sent = []
        patcher = mock.patch.object(vb, "http_post_json", side_effect=lambda url, payload, timeout=20: self.sent.append(payload))
        patcher.start()
        self.addCleanup(patcher.stop)
        isolate_notify_env(self)

    def test_discovers_saves_and_welcomes_once(self):
        group = {"update_id": 1, "my_chat_member": {"chat": {"id": -100, "title": "Fynd", "type": "group"}}}
        api = FakeTelegram(updates=[[private_message(77, "Emil"), group]])
        with self.assertLogs("vinted-bot", "INFO"):
            self.assertEqual(vb.discover_telegram_chat(TOKEN, self.store, self.cfg, api=api), "77")  # privat chatt före grupp
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["chat_id"], "77")
        again = FakeTelegram(updates=[[private_message(77, "Emil"), group]])
        self.assertEqual(vb.discover_telegram_chat(TOKEN, self.store, self.cfg, api=again), "77")
        self.assertEqual(len(self.sent), 1)                                   # ingen ny välkomsthälsning
        offline = FakeTelegram(good_token="1:x")
        with self.assertLogs("vinted-bot", "WARNING"):
            self.assertEqual(vb.discover_telegram_chat(TOKEN, self.store, self.cfg, api=offline), "77")

    def test_no_message_yet(self):
        with self.assertLogs("vinted-bot", "WARNING") as logs:
            self.assertIsNone(vb.discover_telegram_chat(TOKEN, self.store, self.cfg, api=FakeTelegram()))
        self.assertIn("Skriv ett meddelande", "\n".join(logs.output))
        self.assertIsNone(self.store.get_setting(f"telegram_chat:{TOKEN.split(':')[0]}"))
        self.assertEqual(self.sent, [])

    def test_wrong_token_is_a_warning_not_a_crash(self):
        with self.assertLogs("vinted-bot", "WARNING"):
            self.assertIsNone(vb.discover_telegram_chat(TOKEN, self.store, self.cfg, api=FakeTelegram(good_token="1:x")))

    def test_build_notifiers_uses_saved_chat(self):
        self.store.set_setting(f"telegram_chat:{TOKEN.split(':')[0]}", "88")
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        with mock.patch.object(vb, "telegram_api", FakeTelegram(updates=[[]])):
            notifiers = vb.build_notifiers(self.cfg, self.store)
        self.assertEqual([n.chat_id for n in notifiers], ["88"])

    def discover(self, *updates):
        return vb.discover_telegram_chat(TOKEN, self.store, self.cfg, api=FakeTelegram(updates=[list(updates)]))

    def welcomed(self):
        return [m["chat_id"] for m in self.sent]

    def test_switches_to_group_when_owner_adds_bot(self):
        start, added = tg_msg(1, 77, "/start", 77, 100), tg_member(5, -400, "member", 77, 200, title="Vinted-fynd")
        with self.assertLogs("vinted-bot", "INFO"):
            self.assertEqual(self.discover(start), "77")
        with self.assertLogs("vinted-bot", "INFO") as logs:
            self.assertEqual(self.discover(start, added), "-400")
        self.assertIn("Vinted-fynd", "\n".join(logs.output))
        self.assertEqual(self.welcomed(), ["77", "-400"])                       # välkomsthälsning i gruppen
        self.assertEqual(self.discover(start, added), "-400")                  # samma händelser igen: inget nytt
        self.assertEqual(len(self.sent), 2)
        back = tg_msg(9, 77, "/start@fyndbot", 77, 300)
        with self.assertLogs("vinted-bot", "INFO"):
            self.assertEqual(self.discover(start, added, back), "77")         # /start i privat chatt byter tillbaka

    def test_others_cannot_take_over(self):
        with self.assertLogs("vinted-bot", "INFO"):
            self.discover(tg_msg(1, 77, "/start", 77, 100), tg_member(5, -400, "member", 77, 200))
        friend_start = tg_msg(6, 88, "/start", 88, 300)                        # en vän trycker Starta på boten
        stranger_group = tg_member(7, -555, "member", 99, 310)                 # någon lägger till boten i sin grupp
        group_chatter = tg_msg(8, -400, "/start", 88, 320, chat_type="group")  # en vän skriver /start i gruppen
        self.assertEqual(self.discover(friend_start, stranger_group, group_chatter), "-400")
        self.assertEqual(self.welcomed(), ["-400"])

    def test_add_remove_readd(self):
        start = tg_msg(1, 77, "/start", 77, 100)
        add1, left = tg_member(2, -400, "member", 77, 200), tg_member(3, -400, "left", 77, 210, old="member")
        add2 = tg_member(4, -400, "member", 77, 220)
        with self.assertLogs("vinted-bot", "INFO"):
            self.assertEqual(self.discover(start, add1, left, add2), "-400")
        for _ in range(2):
            self.assertEqual(self.discover(start, add1, left, add2), "-400")
        self.assertEqual(self.welcomed(), ["-400"])                             # en enda välkomsthälsning

    def test_add_then_remove_in_same_batch_stays_private(self):
        self.store.set_setting(vb.telegram_chat_key(TOKEN), "77")              # sparad av äldre version
        added, left = tg_member(2, -400, "member", 77, 200), tg_member(3, -400, "left", 77, 210, old="member")
        self.assertEqual(self.discover(added, left), "77")
        self.assertEqual(self.sent, [])

    def test_falls_back_to_stored_private_chat_when_removed(self):
        with self.assertLogs("vinted-bot", "INFO"):
            self.discover(tg_msg(1, 77, "/start", 77, 100), tg_member(5, -400, "member", 77, 200))
        removed = tg_member(9, -400, "kicked", 55, 90_000, old="member")       # dagar senare, inget privat meddelande kvar
        with self.assertLogs("vinted-bot", "INFO"):
            self.assertEqual(self.discover(removed), "77")

    def test_new_events_after_a_quiet_week_are_not_ignored(self):
        with self.assertLogs("vinted-bot", "INFO"):
            self.discover(tg_msg(512340000, 77, "/start", 77, 1_000_000))
        later = tg_member(90001, -400, "member", 77, 1_000_000 + 10 * 86400)  # Telegram har börjat om på ett lägre id
        with self.assertLogs("vinted-bot", "INFO"):
            self.assertEqual(self.discover(later), "-400")

    def test_promotion_is_not_an_add(self):
        self.store.set_setting(vb.telegram_chat_key(TOKEN), "77")
        promoted = tg_member(4, -400, "administrator", 77, 200, old="member")
        self.assertEqual(self.discover(promoted), "77")

    def test_follows_migration_message(self):
        with self.assertLogs("vinted-bot", "INFO"):
            self.discover(tg_msg(1, 77, "/start", 77, 100), tg_member(5, -400, "member", 77, 200))
        migrate = {"update_id": 6, "message": {"date": 300, "chat": {"id": -400, "type": "group"},
                                               "migrate_to_chat_id": -1009}}
        with self.assertLogs("vinted-bot", "INFO") as logs:
            self.assertEqual(self.discover(migrate, tg_member(7, -400, "left", 77, 301, old="member")), "-1009")
        self.assertIn("supergrupp", "\n".join(logs.output))
        self.assertEqual(self.welcomed(), ["-400"])                            # ingen ny välkomsthälsning

    def test_first_writer_becomes_owner(self):
        early, late = tg_msg(1, 77, "hej", 77, 100), tg_msg(2, 88, "/start", 88, 700)
        with self.assertLogs("vinted-bot", "INFO") as logs:
            self.assertEqual(self.discover(early, late), "77")
        self.assertIn("flera personer", "\n".join(logs.output))
        self.assertEqual(self.store.get_setting(vb.telegram_chat_key(TOKEN) + ":owner"), "77")

    def test_blocked_private_chat_is_not_a_fallback(self):
        with self.assertLogs("vinted-bot", "INFO"):
            self.discover(tg_msg(1, 77, "/start", 77, 100), tg_member(5, -400, "member", 77, 200))
        blocked = tg_member(8, 77, "kicked", 77, 300, old="member", chat_type="private")
        self.assertEqual(self.discover(blocked), "-400")                       # gruppen gäller fortfarande
        with self.assertLogs("vinted-bot", "WARNING"):
            self.assertIsNone(self.discover(tg_member(9, -400, "left", 55, 400, old="member")))

    def test_old_saved_group_without_owner_is_not_taken_over(self):
        self.store.set_setting(vb.telegram_chat_key(TOKEN), "-400")
        self.assertEqual(self.discover(tg_msg(6, 88, "/start", 88, 300)), "-400")
        self.assertEqual(self.sent, [])

    def test_without_store_uses_group_activity(self):
        api = FakeTelegram(updates=[[tg_member(5, -400, "member", 77, 200)]])
        self.assertEqual(vb.discover_telegram_chat(TOKEN, None, self.cfg, api=api), "-400")
        api = FakeTelegram(updates=[[tg_msg(3, -400, "hej", 77, 100, chat_type="group")]])
        self.assertEqual(vb.discover_telegram_chat(TOKEN, None, self.cfg, api=api), "-400")
        self.assertEqual(self.sent, [])                                         # utan databas: ingen välkomsthälsning

    def test_first_time_via_group_only(self):
        added, left = tg_member(5, -400, "member", 77, 200), tg_member(6, -400, "left", 77, 300, old="member")
        with self.assertLogs("vinted-bot", "INFO"):
            self.assertEqual(self.discover(added), "-400")
        with self.assertLogs("vinted-bot", "WARNING"):
            self.assertIsNone(self.discover(added, left))                       # ingen privat chatt att falla tillbaka på
        self.assertEqual(self.store.get_setting(vb.telegram_chat_key(TOKEN)), "")

    def test_plain_message_in_group_does_not_switch(self):
        self.store.set_setting(vb.telegram_chat_key(TOKEN), "77")
        chatter = tg_msg(3, -400, "hej allihop", 77, 100, chat_type="group")
        self.assertEqual(self.discover(chatter), "77")
        self.assertEqual(self.sent, [])

    def test_follows_group_upgraded_to_supergroup(self):
        upgraded = vb.NotifyError(400, json.dumps({
            "ok": False, "error_code": 400, "description": "Bad Request: group chat was upgraded to a supergroup chat",
            "parameters": {"migrate_to_chat_id": -1001234}}))
        calls, remembered = [], []

        def post(url, payload, timeout=20):
            calls.append(payload["chat_id"])
            if payload["chat_id"] == "-400":
                raise upgraded

        with mock.patch.object(vb, "http_post_json", side_effect=post), self.assertLogs("vinted-bot", "INFO"):
            n = vb.TelegramNotifier(TOKEN, "-400", on_migrate=remembered.append)
            n.send(vb.Message("t", ["rad"], "https://www.vinted.se/items/1"))
        self.assertEqual(calls, ["-400", "-1001234"])
        self.assertEqual((n.chat_id, remembered), ("-1001234", ["-1001234"]))
        self.assertIsNone(vb.NotifyError(400, "inte json").migrate_to_chat_id)



# --------------------------------------------------------------------------
# Legitcheck
# --------------------------------------------------------------------------

def plugin(name, data):
    return '["$","$L8a",null,' + json.dumps({"data": data, "exposures": [], "name": name, "section": "sidebar",
                                              "type": name}, ensure_ascii=False) + "]"


def listing_page(item_id=42, brand="Moncler", price="1200.00", reviews=0, rating=0, photos=2, title="Moncler jacka",
                 description="Fin jacka", condition="Mycket bra", catalog=(5, 1206, 2052, 2536), verification=None):
    """En annonssida i samma format som Vinteds (RSC), med bara de delar legitcheck läser."""
    parts = [
        plugin("summary", {"item_id": str(item_id), "lines": [{"elements": [{"style": "title", "type": "text",
                                                                             "value": title}]}]}),
        plugin("gallery", {"item_id": str(item_id), "photos": [{"id": str(i)} for i in range(photos)]}),
        plugin("attributes", {"item_id": str(item_id), "attributes": [
            {"code": "brand", "data": {"value": brand}}, {"code": "size", "data": {"value": "L"}},
            {"code": "status", "data": {"value": condition}}]}),
        plugin("description", {"item_id": str(item_id), "description": description}),
        plugin("user_info_header", {"item_id": str(item_id), "name": "säljare", "feedback_count": reviews,
                                    "feedback_reputation": rating, "business": False}),
        plugin("breadcrumbs", {"item_id": str(item_id), "breadcrumbs": [{"url": f"/catalog/{c}-x"} for c in catalog]}),
        plugin("ask_seller", {"item_id": str(item_id), "is_reserved": False}),
        json.dumps({"pricingServices": {"services": {"itemVerification": verification},
                                        "originalAskingAmount": {"amount": price, "currencyCode": "SEK"},
                                        "totalAmount": {"amount": "1300.00", "currencyCode": "SEK"}}}),
    ]
    return "\n".join(f"{i:x}:{p}" for i, p in enumerate(parts))


LEGIT_TIPS = {
    "general": ["Bekräfta inte att allt är OK förrän du har kollat plagget."],
    "brands": {
        "Moncler": {"official": {"name": "Moncler", "url": "https://code.moncler.com", "how": "skanna QR-koden"},
                    "tips": ["Be om bild på QR/NFC-etiketten."]},
        "Fear of God Essentials": {"aliases": ["Essentials"], "tips": ["Kolla gummiloggan."]},
    },
}


def legit_searches(cfg):
    return [make_search(cfg, name="Moncler", catalog=[2536], max_price=2400,
                        brands={"Moncler": {"resale": 3200, "suspicious_below": 1500}})]


class LegitCheckTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.searches = legit_searches(self.cfg)

    def check(self, **kw):
        listing = vb.parse_listing(listing_page(**kw), BASE, kw.get("item_id", 42))
        return listing, vb.legit_check(listing, self.searches, LEGIT_TIPS)

    def test_parse_listing(self):
        listing, _ = self.check(photos=5, reviews=31, rating=0.97, verification={"price": 99})
        self.assertEqual((listing.brand, listing.size, listing.condition, listing.title), ("Moncler", "L", "Mycket bra", "Moncler jacka"))
        self.assertEqual((listing.price, listing.photos, listing.seller_reviews), (1200.0, 5, 31))
        self.assertEqual(listing.catalog_ids, [5, 1206, 2052, 2536])
        self.assertTrue(listing.verification_offered)
        self.assertEqual(listing.url, f"{BASE}/items/42")

    def test_parse_listing_from_html(self):
        rsc = listing_page()
        html = '<script>self.__next_f.push([1,' + json.dumps(rsc) + '])</script>'
        self.assertEqual(vb.parse_listing(html, BASE, 42).brand, "Moncler")
        self.assertIsNone(vb.parse_listing("<html>inget här</html>", BASE, 42))

    def test_high_risk(self):
        _, msg = self.check(price="900", reviews=0, description="Moncler 1:1")
        text = "\n".join(msg.lines)
        self.assertIn("🔴 Hög", text)
        self.assertIn("under gränsen där kopior är vanliga", text)
        self.assertIn("inga omdömen", text)
        self.assertIn("'1:1'", text)
        self.assertIn("code.moncler.com", text)
        self.assertTrue(msg.title.startswith("🔍 Legitcheck: Moncler – 900 kr"))

    def test_low_risk(self):
        _, msg = self.check(price="3100", reviews=40, rating=0.98, photos=10, description="Kvitto finns. Inte fake.")
        text = "\n".join(msg.lines)
        self.assertIn("🟢 Låg", text)
        self.assertIn("40 omdömen", text)
        self.assertIn("Säljaren nämner kvitto", text)
        self.assertNotIn("'fake'", text)                  # "inte fake" räknas inte

    def test_unknown_brand_gets_general_tips(self):
        _, msg = self.check(brand="Okänt AB", reviews=12, rating=1, photos=5)
        text = "\n".join(msg.lines)
        self.assertNotIn("Priset", text)                  # inget att jämföra med
        self.assertIn("(allmänt)", text)
        self.assertIn("Bekräfta inte att allt är OK", text)

    def text_of(self, **kw):
        return "\n".join(self.check(**kw)[1].lines)

    def test_denials_are_not_fake_words(self):
        for desc in ("100% äkta, inte en kopia", "Not a fake, bought in store", "This is not a replica",
                     "Inte någon fake!", "Fake fur i huvan", "Kopia på kvittot finns"):
            text = self.text_of(price="3100", reviews=40, rating=0.98, photos=10, description=desc)
            self.assertNotIn("Annonsen innehåller", text, desc)
            self.assertIn("🟢 Låg", text, desc)
        self.assertIn("'kopia'", self.text_of(description="Inte äkta utan kopia"))

    def test_fake_word_wins_over_proof(self):
        text = self.text_of(price="3100", reviews=40, rating=0.98, photos=10,
                            description="1:1 replica, QR-kod och NFC funkar, kvitto medföljer")
        self.assertIn("🔴 Hög", text)
        self.assertNotIn("Säljaren nämner", text)

    def test_missing_receipt_is_not_proof(self):
        for desc in ("Inget kvitto finns tyvärr", "Kvitto saknas", "Säljs utan kvitto", "No receipt", "Kvitto finns inte"):
            self.assertNotIn("Säljaren nämner", self.text_of(description=desc), desc)
        self.assertIn("Säljaren nämner kvitto", self.text_of(description="Kvitto finns. Inte fake."))

    def test_ambiguous_words_only_raise_a_flag(self):
        text = self.text_of(price="3100", reviews=40, rating=0.98, photos=10, description="Inspired by the 90s")
        self.assertIn("'inspired' – kolla vad som menas", text)
        self.assertIn("🟡 Medel", text)

    def test_most_specific_search_sets_the_price(self):
        broad = make_search(self.cfg, name="Jackor", catalog=[1206], max_price=900, brands={"Moncler": 1000})
        listing = vb.parse_listing(listing_page(price="1200"), BASE, 42)
        text = "\n".join(vb.legit_check(listing, [broad] + self.searches, LEGIT_TIPS).lines)
        self.assertIn("under gränsen där kopior är vanliga för Moncler (1 500 kr)", text)

    def test_shipped_tips_file(self):
        tips = vb.load_legit_tips(vb.HERE / "legit_tips.yaml")
        self.assertGreaterEqual(len(tips["general"]), 3)
        for brand, entry in tips["brands"].items():
            self.assertTrue(entry["tips"], brand)
            self.assertTrue(all(isinstance(x, str) and len(x) <= 160 for x in entry["tips"]), brand)
        self.assertEqual(vb.brand_tips("Polo Ralph Lauren", tips), tips["brands"]["Ralph Lauren"])
        self.assertEqual(vb.brand_tips("Essentials", tips), tips["brands"]["Fear of God Essentials"])
        self.assertIn("code.moncler.com", vb.brand_tips("Moncler", tips)["official"]["url"])

    def test_brand_alias(self):
        self.assertEqual(vb.brand_tips("Essentials", LEGIT_TIPS)["tips"], ["Kolla gummiloggan."])
        self.assertEqual(vb.brand_tips("Fear of God Essentials", LEGIT_TIPS)["tips"], ["Kolla gummiloggan."])
        self.assertEqual(vb.brand_tips("Nike", LEGIT_TIPS), {})

    def test_item_id_in(self):
        self.assertEqual(vb.item_id_in("https://www.vinted.fr/items/1234567-jacka?x=1"), 1234567)
        self.assertEqual(vb.item_id_in(" 9876543 "), 9876543)
        self.assertEqual(vb.item_id_in("legit:555555"), 555555)
        self.assertIsNone(vb.item_id_in("hej"))
        deal = {"caption": "🔥 Moncler – 900 kr", "reply_markup": {"inline_keyboard": [[
            {"text": "Öppna på Vinted", "url": "https://www.vinted.se/items/7777777-x"}]]}}
        self.assertEqual(vb.item_id_in(deal), 7777777)


class FakeListingClient:
    base = BASE

    def __init__(self, body=None, error=None):
        self.body, self.error, self.calls = body, error, []
        self.retries, self.timeout = 2, 30.0

    def fetch_listing_page(self, item_id):
        self.calls.append(item_id)
        if self.error:
            raise self.error
        return self.body


class LegitTelegram(FakeTelegram):
    def __call__(self, token, method, params=None, timeout=35):
        if method == "answerCallbackQuery":
            self.calls.append((token, method, params))
            return True
        return super().__call__(token, method, params, timeout)


class LegitListenerTests(unittest.TestCase):
    GROUP = "-1001"

    def setUp(self):
        self.cfg = make_cfg()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bot.db"
        self.store = vb.Store(self.db)
        self.addCleanup(self.store.db.close)
        self.api = LegitTelegram()
        self.sent = []
        patcher = mock.patch.object(vb, "http_post_json", side_effect=lambda url, payload, timeout=20: self.sent.append((url, payload)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = FakeListingClient(listing_page(price="900"))
        self.listener = vb.LegitListener(TOKEN, self.db, self.cfg, legit_searches(self.cfg), LEGIT_TIPS,
                                         fixed_chat=self.GROUP, api=self.api, client=self.client)

    def poll(self, *updates):
        self.api.updates = [list(updates)]
        self.listener.poll(self.store)

    def answered(self):
        return [m for m in self.api.calls if m[1] == "answerCallbackQuery"]

    def test_legit_command_with_link(self):
        msg = tg_msg(10, int(self.GROUP), "/legit https://www.vinted.se/items/42-jacka", 7, 100, chat_type="supergroup")
        msg["message"]["message_id"] = 55
        self.poll(msg)
        self.assertEqual(self.client.calls, [42])
        (url, payload), = self.sent
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertEqual(payload["chat_id"], self.GROUP)
        self.assertIn("Legitcheck: Moncler – 900 kr", payload["text"])
        self.assertEqual(payload["reply_parameters"]["message_id"], 55)
        self.assertEqual(self.listener.offset, 11)

    def test_only_own_chats_get_answers(self):
        self.poll(tg_msg(10, -999, "/legit https://www.vinted.se/items/42", 7, 100, chat_type="group"))
        self.assertEqual((self.client.calls, self.sent), ([], []))

    def test_button_and_no_double_answer(self):
        query = {"update_id": 20, "callback_query": {"id": "q1", "from": {"id": 7}, "data": "legit:42",
                 "message": {"message_id": 9, "chat": {"id": int(self.GROUP), "type": "supergroup"}}}}
        self.poll(query)
        self.assertEqual(len(self.answered()), 1)
        self.assertEqual(len(self.sent), 1)
        self.poll(query)                                  # samma uppdatering igen (t.ex. nästa körning)
        self.assertEqual(len(self.sent), 1)

    def test_reply_to_deal_with_legit(self):
        msg = tg_msg(11, int(self.GROUP), "/legit@fyndbot", 7, 100, chat_type="supergroup")
        msg["message"]["reply_to_message"] = {"caption": "🔥", "reply_markup": {"inline_keyboard": [[
            {"text": "Öppna på Vinted", "url": "https://www.vinted.se/items/4242424-x"}]]}}
        self.poll(msg)
        self.assertEqual(self.client.calls, [4242424])

    def test_link_on_next_line(self):
        self.poll(tg_msg(18, int(self.GROUP), "/legit\nhttps://www.vinted.se/items/42-jacka", 7, 100, chat_type="supergroup"))
        self.assertEqual(self.client.calls, [42])

    def test_commands_for_other_bots_are_ignored(self):
        self.listener.username = "fyndbot"
        self.poll(tg_msg(19, int(self.GROUP), "/legit@annanbot https://www.vinted.se/items/42", 7, 100, chat_type="supergroup"),
                  tg_msg(20, int(self.GROUP), "/help", 8, 100, chat_type="supergroup"))
        self.assertEqual((self.client.calls, self.sent), ([], []))
        self.poll(tg_msg(21, int(self.GROUP), "/help@fyndbot", 8, 100, chat_type="supergroup"))
        self.assertEqual(len(self.sent), 1)

    def test_help_and_missing_link(self):
        self.poll(tg_msg(12, int(self.GROUP), "/hjalp", 7, 100, chat_type="supergroup"),
                  tg_msg(13, int(self.GROUP), "/legit", 8, 100, chat_type="supergroup"))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all("Skriv /legit följt av en länk" in p["text"] for _, p in self.sent))
        self.assertEqual(self.client.calls, [])

    def test_vinted_problems_are_explained(self):
        self.client.error = vb.Blocked("HTTP 429")
        self.poll(tg_msg(14, int(self.GROUP), "/legit 42424242", 7, 100, chat_type="supergroup"))
        self.client.error, self.client.body = None, None
        self.poll(tg_msg(15, int(self.GROUP), "/legit 42424243", 8, 100, chat_type="supergroup"))
        texts = [p["text"] for _, p in self.sent]
        self.assertIn("Vinted svarar inte just nu", texts[0])
        self.assertIn("Hittade inte annonsen", texts[1])

    def test_same_person_cannot_spam(self):
        self.poll(tg_msg(16, int(self.GROUP), "/legit 42424242", 7, 100, chat_type="supergroup"),
                  tg_msg(17, int(self.GROUP), "/legit 42424242", 7, 101, chat_type="supergroup"))
        self.assertEqual(len(self.client.calls), 1)

    def test_confirm_acknowledges_handled_updates(self):
        self.poll(tg_msg(30, int(self.GROUP), "hej", 7, 100, chat_type="supergroup"))
        self.listener.confirm()
        self.assertEqual(self.api.calls[-1][1:], ("getUpdates", {"offset": 31, "timeout": 0}))

    def test_owner_private_chat_allowed_via_discovery(self):
        key = vb.telegram_chat_key(TOKEN)
        for suffix, value in (("", self.GROUP), (":owner", "7"), (":private", "7")):
            self.store.set_setting(key + suffix, value)
        listener = vb.LegitListener(TOKEN, self.db, self.cfg, legit_searches(self.cfg), LEGIT_TIPS,
                                    api=self.api, client=self.client)
        self.api.updates = [[tg_msg(40, 7, "/legit 42424242", 7, 100)]]
        listener.poll(self.store)
        self.assertEqual(self.sent[-1][1]["chat_id"], "7")

    def test_deal_button_only_when_listening(self):
        msg = vb.Message("🔥 Moncler", ["x"], f"{BASE}/items/123-jacka")
        vb.TelegramNotifier(TOKEN, self.GROUP, legit_button=True).send(msg)
        vb.TelegramNotifier(TOKEN, self.GROUP).send(msg)
        with_button, without = (p["reply_markup"]["inline_keyboard"][0] for _, p in self.sent)
        self.assertEqual(with_button[1], {"text": "🔍 Legitcheck", "callback_data": "legit:123"})
        self.assertEqual(len(without), 1)

    def test_run_stops_in_time_without_busy_looping(self):
        clock = [0.0]

        def api(token, method, params=None, timeout=35):
            self.api.calls.append((token, method, params))
            clock[0] += (params or {}).get("timeout", 0) or 0.01   # long polling tar sin tid
            return [] if method == "getUpdates" else True

        listener = vb.LegitListener(TOKEN, self.db, self.cfg, [], {}, fixed_chat=self.GROUP, api=api, client=self.client)
        with mock.patch.object(vb.time, "monotonic", side_effect=lambda: clock[0]):
            listener.run(until=60)
        polls = [c[2]["timeout"] for c in self.api.calls if c[1] == "getUpdates"]
        self.assertEqual(polls, [25, 25, 4])                # aldrig timeout 0 i en loop
        self.assertLessEqual(clock[0], 60)

    def test_discovery_uses_given_updates(self):
        api = mock.Mock(side_effect=AssertionError("ska inte hämta själv"))
        self.assertIsNone(vb.discover_telegram_chat(TOKEN, None, self.cfg, api, updates=[]))

    def test_listen_option_is_validated(self):
        for bad in ("0", "-1", "nan"):
            with self.assertLogs("vinted-bot", "ERROR"):
                self.assertEqual(vb.main(["--lyssna", bad, "--dry-run"]), 2, bad)


class FakeBotApi:
    """Låtsas-Telegram för notiserna: svarar som Bot API och sparar alla anrop."""

    def __init__(self, forum=True, topic_error=None, lost_thread=False):
        self.calls, self.forum, self.topic_error, self.lost_thread = [], forum, topic_error, lost_thread
        self.message_ids, self.thread_ids = iter(range(100, 200)), iter(range(500, 600))

    def __call__(self, url, payload, timeout=20):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, payload))
        if method == "getChat":
            return {"ok": True, "result": {"id": int(payload["chat_id"]), "is_forum": self.forum}}
        if method == "createForumTopic":
            if self.topic_error:
                raise vb.NotifyError(400, json.dumps({"description": self.topic_error}))
            return {"ok": True, "result": {"message_thread_id": next(self.thread_ids), "name": payload["name"]}}
        if method in ("sendMessage", "sendPhoto"):
            if self.lost_thread and payload.get("message_thread_id"):
                self.lost_thread = False
                raise vb.NotifyError(400, json.dumps({"description": "Bad Request: message thread not found"}))
            return {"ok": True, "result": {"message_id": next(self.message_ids), "chat": {"id": int(payload["chat_id"])}}}
        return {"ok": True, "result": True}

    def made(self, method):
        return [payload for m, payload in self.calls if m == method]


def sold_page(item_id=42):
    return listing_page(item_id) + "\n99:" + plugin("buyer_item_status", {"item_id": str(item_id), "title": "Såld"})


class GroupFeatureTests(unittest.TestCase):
    """Ämnen, "🙋 Jag tar den", "❌ SÅLD" och dagens bästa fynd."""
    GROUP = "-1001"

    def setUp(self):
        self.cfg = make_cfg()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bot.db"
        self.store = vb.Store(self.db)
        self.addCleanup(self.store.db.close)

    def notifier(self, api, **kw):
        patcher = mock.patch.object(vb, "http_post_json", side_effect=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        return vb.TelegramNotifier(TOKEN, self.GROUP, legit_button=True, store=self.store, **kw)

    def deal_msg(self, item_id=42, topic="Dunjackor"):
        return vb.Message("🔥 Moncler", ["Pris: 900 kr"], f"{BASE}/items/{item_id}-jacka", f"{BASE}/p.jpg",
                          item_id=item_id, topic=topic)

    # --- ämnen ---

    def test_topic_for_uses_search_tags(self):
        cfg = make_cfg(topics={"Dunjackor": ["Dunjacka"], "Jeans": ["Jeans", "Bootcut"]})
        self.assertEqual(vb.topic_for(["Herr", "dunjacka"], cfg), "Dunjackor")
        self.assertEqual(vb.topic_for(["Bootcut"], cfg), "Jeans")
        self.assertIsNone(vb.topic_for(["Skor"], cfg))

    def test_shipped_config_puts_every_search_in_a_topic(self):
        cfg, searches = vb.load_config(Path(vb.HERE / "config.yaml"))
        self.assertEqual([s.name for s in searches if not vb.topic_for(s.tags, cfg)], [])

    def test_deals_go_to_their_topic_which_is_created_once(self):
        api = FakeBotApi()
        tg = self.notifier(api)
        tg.send(self.deal_msg(1))
        tg.send(self.deal_msg(2))
        tg.send(self.deal_msg(3, topic="Jeans"))
        self.assertEqual([p["name"] for p in api.made("createForumTopic")], ["Dunjackor", "Jeans"])
        self.assertEqual([p["message_thread_id"] for p in api.made("sendPhoto")], [500, 500, 501])
        self.assertEqual(len(api.made("getChat")), 1)
        # Nästa körning (ny notifierare) minns ämnena.
        api2 = FakeBotApi()
        self.notifier(api2).send(self.deal_msg(4))
        self.assertEqual((api2.made("createForumTopic"), api2.made("sendPhoto")[0]["message_thread_id"]), ([], 500))

    def test_topics_are_created_at_start_of_run(self):
        api = FakeBotApi()
        bot = vb.Bot(self.cfg, [], FakeClient(), self.store, [self.notifier(api)])
        bot.run_cycle()
        bot.run_cycle()
        self.assertEqual([p["name"] for p in api.made("createForumTopic")],
                         ["Dunjackor", "Jackor", "Jeans", "Tröjor & skjortor", "Byxor & shorts", "⭐ Dagens bästa"])
        self.assertEqual(len(api.made("getChat")), 2)            # en gång per varv
        self.assertEqual(api.made("sendPhoto") + api.made("sendMessage"), [])

    def test_group_without_topics_gets_plain_messages(self):
        api = FakeBotApi(forum=False)
        self.notifier(api).send(self.deal_msg())
        self.assertNotIn("message_thread_id", api.made("sendPhoto")[0])
        self.assertEqual(api.made("createForumTopic"), [])

    def test_missing_topic_right_warns_once_and_still_sends(self):
        api = FakeBotApi(topic_error="Bad Request: not enough rights to create a topic")
        tg = self.notifier(api)
        with self.assertLogs("vinted-bot", "WARNING") as logs:
            tg.send(self.deal_msg(1))
            tg.send(self.deal_msg(2, topic="Jeans"))
        self.assertEqual(len(api.made("sendPhoto")), 2)
        self.assertEqual(len(api.made("createForumTopic")), 2)  # ett försök per ämne och varv
        self.assertEqual(sum("Hantera ämnen" in line for line in logs.output), 1)

    def test_deleted_topic_and_rejected_photo_create_only_one_new_topic(self):
        self.store.set_setting(f"topic:{self.GROUP}:Dunjackor", "77")
        api = FakeBotApi(lost_thread=True)
        real_call = api.__call__

        def photo_rejected(url, payload, timeout=20):
            if url.endswith("/sendPhoto") and payload.get("message_thread_id") == 500:
                api.calls.append(("sendPhoto", payload))
                raise vb.NotifyError(400, json.dumps({"description": "Bad Request: wrong file identifier"}))
            return real_call(url, payload, timeout)

        self.notifier(photo_rejected).send(self.deal_msg())
        self.assertEqual(len(api.made("createForumTopic")), 1)
        self.assertEqual(api.made("sendMessage")[0]["message_thread_id"], 500)

    def test_temporary_telegram_trouble_does_not_turn_topics_off(self):
        api = FakeBotApi()
        real_call = api.__call__
        failures = {"getChat": 1, "createForumTopic": 1}

        def flaky(url, payload, timeout=20):
            method = url.rsplit("/", 1)[1]
            if failures.get(method):
                failures[method] -= 1
                api.calls.append((method, payload))
                raise OSError("timed out")
            return real_call(url, payload, timeout)

        tg = self.notifier(flaky)
        clock = [1000.0]
        with mock.patch.object(vb.time, "monotonic", side_effect=lambda: clock[0]):
            for item_id in (1, 2, 3, 4):
                tg.send(self.deal_msg(item_id))
                clock[0] += 61 if item_id == 2 else 1       # efter en minut frågar boten igen
        self.assertEqual([p.get("message_thread_id") for p in api.made("sendPhoto")], [None, None, None, 500])
        self.assertEqual(len(api.made("getChat")), 2)

    def test_telegram_down_costs_one_question_per_minute(self):
        calls = []

        def down(url, payload, timeout=20):
            calls.append(url.rsplit("/", 1)[1])
            raise http.client.IncompleteRead(b"")

        tg = self.notifier(down)
        bot = vb.Bot(self.cfg, [], FakeClient(), self.store, [tg])
        bot.run_cycle()                                         # ämnena: ett försök, sedan vidare
        self.assertEqual(calls, ["getChat"])
        with self.assertRaises(http.client.IncompleteRead):
            tg.send(self.deal_msg())                            # skickas utan ämne – frågar inte igen
        self.assertEqual(calls, ["getChat", "sendPhoto"])

    def test_unexpected_error_while_preparing_topics_does_not_stop_the_run(self):
        notifier = self.notifier(FakeBotApi())
        bot = vb.Bot(self.cfg, [make_search(self.cfg)], FakeClient(), self.store, [notifier])
        with mock.patch.object(notifier, "prepare_topics", side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs("vinted-bot", "WARNING"):
            self.assertEqual(bot.run_cycle(), 0)
        self.assertEqual(bot.client.calls, [1])                 # sökningen kördes ändå

    def test_missing_right_for_one_topic_keeps_the_others(self):
        self.store.set_setting(f"topic:{self.GROUP}:Dunjackor", "77")
        api = FakeBotApi(topic_error="Bad Request: not enough rights to create a topic")
        tg = self.notifier(api)
        with self.assertLogs("vinted-bot", "WARNING"):
            tg.send(self.deal_msg(1, topic="Jeans"))
        tg.send(self.deal_msg(2))
        tg.send(self.deal_msg(3, topic="Jeans"))
        self.assertEqual([p.get("message_thread_id") for p in api.made("sendPhoto")], [None, 77, None])
        self.assertEqual(len(api.made("createForumTopic")), 1)  # försöker inte igen samma varv
        tg.new_run()                                                # men nästa varv
        tg.send(self.deal_msg(4, topic="Jeans"))
        self.assertEqual(len(api.made("createForumTopic")), 2)

    def test_deleted_topic_is_created_again(self):
        self.store.set_setting(f"topic:{self.GROUP}:Dunjackor", "77")
        api = FakeBotApi(lost_thread=True)
        self.notifier(api).send(self.deal_msg())
        self.assertEqual([p.get("message_thread_id") for p in api.made("sendPhoto")], [77, 500])
        self.assertEqual(self.store.get_setting(f"topic:{self.GROUP}:Dunjackor"), "500")

    # --- knapparna ---

    def test_group_deal_has_claim_button_private_chat_not(self):
        api = FakeBotApi()
        self.notifier(api).send(self.deal_msg())
        keyboard = api.made("sendPhoto")[0]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b.get("callback_data") for row in keyboard for b in row], [None, "legit:42", "claim:42"])
        api2 = FakeBotApi()
        patcher = mock.patch.object(vb, "http_post_json", side_effect=api2)
        patcher.start()
        self.addCleanup(patcher.stop)
        vb.TelegramNotifier(TOKEN, "555", legit_button=True, store=self.store).send(self.deal_msg())
        keyboard = api2.made("sendPhoto")[0]["reply_markup"]["inline_keyboard"]
        self.assertNotIn("claim:42", [b.get("callback_data") for row in keyboard for b in row])

    def claim_query(self, update_id, user_id, name, keyboard):
        return {"update_id": update_id, "callback_query": {
            "id": f"q{update_id}", "from": {"id": user_id, "first_name": name}, "data": "claim:42",
            "message": {"message_id": 9, "chat": {"id": int(self.GROUP), "type": "supergroup"},
                        "reply_markup": {"inline_keyboard": keyboard}}}}

    def test_claim_button_first_click_wins_and_can_be_released(self):
        telegram = LegitTelegram()
        sent = []
        patcher = mock.patch.object(vb, "http_post_json",
                                    side_effect=lambda url, payload, timeout=20: sent.append((url, payload)))
        patcher.start()
        self.addCleanup(patcher.stop)
        listener = vb.LegitListener(TOKEN, self.db, self.cfg, [], LEGIT_TIPS, fixed_chat=self.GROUP,
                                    api=telegram, client=FakeListingClient())
        keyboard = [[{"text": "Öppna på Vinted", "url": f"{BASE}/items/42"},
                     {"text": "🔍 Legitcheck", "callback_data": "legit:42"}],
                    [{"text": "🙋 Jag tar den", "callback_data": "claim:42"}]]

        def click(update_id, user_id, name):
            telegram.updates = [[self.claim_query(update_id, user_id, name, keyboard)]]
            listener.poll(self.store)
            return telegram.calls[-1][2]["text"]

        self.assertIn("Den är din", click(1, 7, "Emil"))
        url, payload = sent[-1]
        self.assertTrue(url.endswith("/editMessageReplyMarkup"))
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][1][0],
                         {"text": "🙋 Emil tar den", "callback_data": "claim:42"})
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0], keyboard[0])
        self.assertEqual(click(2, 8, "Kalle"), "Emil har redan tagit den.")
        self.assertEqual(len(sent), 1)                         # knappen ändras inte
        self.assertEqual(click(3, 7, "Emil"), "Du släppte den.")
        self.assertEqual(sent[-1][1]["reply_markup"]["inline_keyboard"][1][0]["text"], "🙋 Jag tar den")
        self.assertIn("Den är din", click(4, 8, "Kalle"))

    def test_claim_in_other_chat_is_ignored(self):
        telegram = LegitTelegram()
        with mock.patch.object(vb, "http_post_json") as post:
            listener = vb.LegitListener(TOKEN, self.db, self.cfg, [], LEGIT_TIPS, fixed_chat=self.GROUP,
                                        api=telegram, client=FakeListingClient())
            query = self.claim_query(1, 7, "Emil", [])
            query["callback_query"]["message"]["chat"]["id"] = -999
            telegram.updates = [[query]]
            listener.poll(self.store)
        post.assert_not_called()

    def test_legit_reply_lands_in_same_topic(self):
        telegram = LegitTelegram()
        sent = []
        with mock.patch.object(vb, "http_post_json", side_effect=lambda url, payload, timeout=20: sent.append(payload)):
            listener = vb.LegitListener(TOKEN, self.db, self.cfg, legit_searches(self.cfg), LEGIT_TIPS,
                                        fixed_chat=self.GROUP, api=telegram, client=FakeListingClient(listing_page()))
            msg = tg_msg(5, int(self.GROUP), "/legit 42424242", 7, 100, chat_type="supergroup")
            msg["message"].update(message_thread_id=500, is_topic_message=True)
            telegram.updates = [[msg]]
            listener.poll(self.store)
        self.assertEqual(sent[-1]["message_thread_id"], 500)

    # --- sålda fynd ---

    def sold_bot(self, client):
        return vb.Bot(self.cfg, [], client, self.store, [self.notifier(self.api)])

    def sent_ago(self, seconds, item_id=42, message_id=9):
        self.store.record_sent(self.GROUP, message_id, item_id, "photo", "<b>🔥 Moncler</b>\nPris: 900 kr",
                               f"{BASE}/items/{item_id}")
        self.store.db.execute("UPDATE sent SET ts = ts - ? WHERE message_id = ?", (seconds, message_id))

    def test_sold_listing_parsed(self):
        self.assertTrue(vb.parse_listing(sold_page(), BASE, 42).sold)
        self.assertFalse(vb.parse_listing(listing_page(), BASE, 42).sold)
        lines = vb.legit_check(vb.parse_listing(sold_page(), BASE, 42), [], LEGIT_TIPS).lines
        self.assertIn("❌ Annonsen är redan såld.", lines)

    def test_sold_deal_is_marked_in_the_group(self):
        self.api = FakeBotApi()
        self.sent_ago(2 * 3600)
        client = FakeListingClient(sold_page())
        self.assertEqual(self.sold_bot(client).check_sold(), 1)
        (edit,) = self.api.made("editMessageCaption")
        self.assertEqual((edit["chat_id"], edit["message_id"]), (self.GROUP, 9))
        self.assertTrue(edit["caption"].startswith("❌ <b>SÅLD</b>\n<b>🔥 Moncler</b>"))
        self.assertEqual(edit["reply_markup"]["inline_keyboard"], [[{"text": "Öppna på Vinted", "url": f"{BASE}/items/42"}]])
        self.assertEqual(self.store.due_sold_checks(10), [])   # kollas inte igen
        self.assertEqual(self.store.top_deals(0), [])

    def test_unsold_deal_is_checked_after_1_6_and_24_hours(self):
        self.api = FakeBotApi()
        self.sent_ago(30 * 60)
        client = FakeListingClient(listing_page())
        bot = self.sold_bot(client)
        self.assertEqual(bot.check_sold(), 0)
        self.assertEqual(client.calls, [])                    # för tidigt
        for hours, expected_calls in ((1, 1), (2, 1), (6, 2), (24, 3), (48, 3)):
            self.store.db.execute("UPDATE sent SET ts = ?", (int(vb.time.time()) - hours * 3600 - 1,))
            bot.check_sold()
            self.assertEqual(len(client.calls), expected_calls, hours)
        self.assertEqual(self.api.made("editMessageCaption"), [])

    def test_removed_listing_is_marked_removed(self):
        self.api = FakeBotApi()
        self.sent_ago(7 * 3600)
        self.sold_bot(FakeListingClient(None)).check_sold()
        self.assertTrue(self.api.made("editMessageCaption")[0]["caption"].startswith("❌ <b>BORTTAGEN</b>"))

    def test_vinted_trouble_does_not_count_as_a_check(self):
        self.api = FakeBotApi()
        self.sent_ago(2 * 3600)
        bot = self.sold_bot(FakeListingClient(error=vb.FetchError("HTTP 500")))
        self.assertEqual(bot.check_sold(), 0)
        self.assertEqual(len(self.store.due_sold_checks(10)), 1)
        with self.assertRaises(vb.Blocked):
            self.sold_bot(FakeListingClient(error=vb.Blocked("HTTP 429"))).check_sold()

    def test_failed_edit_is_tried_again_next_run(self):
        self.api = FakeBotApi()
        real_call = self.api.__call__
        failures = [vb.NotifyError(429, json.dumps({"description": "Too Many Requests: retry after 30"}))]

        def busy(url, payload, timeout=20):
            if url.endswith("/editMessageCaption") and failures:
                self.api.calls.append(("editMessageCaption", payload))
                raise failures.pop()
            return real_call(url, payload, timeout)

        self.sent_ago(2 * 3600)
        bot = vb.Bot(self.cfg, [], FakeListingClient(sold_page()), self.store, [self.notifier(busy)])
        with self.assertLogs("vinted-bot", "WARNING"):
            self.assertEqual(bot.check_sold(), 0)
        self.assertEqual(len(self.store.due_sold_checks(10)), 1)   # inte klar – nytt försök
        self.assertEqual(bot.check_sold(), 1)
        self.assertEqual(len(self.api.made("editMessageCaption")), 2)
        self.assertEqual(self.store.due_sold_checks(10), [])

    def test_strange_listing_page_does_not_block_the_other_checks(self):
        self.api = FakeBotApi()
        self.sent_ago(3 * 3600, item_id=111, message_id=1)
        self.sent_ago(2 * 3600, item_id=222, message_id=2)
        client = FakeListingClient(sold_page(222))
        bot = self.sold_bot(client)
        real_parse = vb.parse_listing

        def parse(body, base, item_id):
            if item_id == 111:
                raise KeyError(0)
            return real_parse(body, base, item_id)

        with mock.patch.object(vb, "parse_listing", side_effect=parse), self.assertLogs("vinted-bot", "WARNING"):
            self.assertEqual(bot.check_sold(), 1)
        self.assertEqual(client.calls, [111, 222])
        self.assertEqual(self.api.made("editMessageCaption")[0]["message_id"], 2)

    def test_deals_are_recorded_for_sold_check(self):
        api = FakeBotApi()
        self.notifier(api).send(self.deal_msg())
        (row,) = self.store.db.execute("SELECT chat_id, message_id, item_id, kind FROM sent").fetchall()
        self.assertEqual(row, (self.GROUP, 100, 42, "photo"))

    def test_prune_forgets_old_claims(self):
        self.sent_ago(15 * 86400, message_id=1)
        self.sent_ago(3600, message_id=2)
        for message_id in (1, 2, 3):
            self.store.set_setting(f"claim:{self.GROUP}:{message_id}", "{}")
        self.store.set_setting(f"topic:{self.GROUP}:Jeans", "500")
        self.store.prune()
        keys = sorted(k for (k,) in self.store.db.execute("SELECT key FROM settings"))
        self.assertEqual(keys, [f"claim:{self.GROUP}:2", f"topic:{self.GROUP}:Jeans"])

    # --- dagens bästa fynd ---

    def log_deal(self, item_id, profit, brand="Moncler"):
        it = item(id=item_id, brand=brand, title=f"{brand} jacka", price=900, total=950, url=f"{BASE}/items/{item_id}")
        self.store.log_deal(vb.Deal(it, "Dunjackor", resale=900 + profit, cost=950, profit=profit, roi=50))

    def test_daily_summary_after_eight_once_a_day(self):
        notifier = FakeNotifier()
        bot = vb.Bot(self.cfg, [], FakeClient(), self.store, [notifier])
        for item_id, profit in ((1, 300), (2, 900), (3, 500), (4, 100), (5, 700), (6, 200), (2, 950)):
            self.log_deal(item_id, profit)
        self.store.record_sent(self.GROUP, 1, 5, "photo", "x", "u")
        self.store.sold_checked(5, "SÅLD")                    # sålda räknas inte
        evening = datetime(2026, 10, 22, 20, 5, tzinfo=vb.STOCKHOLM)
        self.assertFalse(bot.daily_summary(evening.replace(hour=19)))
        self.assertTrue(bot.daily_summary(evening))
        self.assertFalse(bot.daily_summary(evening.replace(hour=23)))
        (msg,) = notifier.sent
        self.assertEqual(msg.title, "⭐ Dagens 5 bästa fynd")
        self.assertEqual(msg.topic, "⭐ Dagens bästa")
        self.assertEqual([row[0]["url"] for row in msg.buttons], [f"{BASE}/items/{i}" for i in (2, 3, 1, 6, 4)])
        self.assertIn("+950 kr", msg.lines[0])
        self.assertTrue(bot.daily_summary(evening + vb.timedelta(days=1)) is False)  # inga nya fynd

    def test_daily_summary_is_sent_even_when_vinted_blocks(self):
        isolate_notify_env(self)
        summaries = []
        with mock.patch.object(vb.Bot, "run_cycle", side_effect=vb.Blocked("HTTP 429")), \
                mock.patch.object(vb.Bot, "try_daily_summary", lambda bot: summaries.append(1)), \
                self.assertLogs("vinted-bot", "WARNING"):
            vb.main(["--once", "--db", str(self.db)])
        self.assertEqual(summaries, [1])

    def test_daily_summary_can_be_turned_off(self):
        notifier = FakeNotifier()
        self.log_deal(1, 500)
        bot = vb.Bot(make_cfg(daily_summary_hour=None), [], FakeClient(), self.store, [notifier])
        self.assertFalse(bot.daily_summary(datetime(2026, 10, 22, 21, tzinfo=vb.STOCKHOLM)))
        self.assertEqual(notifier.sent, [])

    def test_shipped_config_skips_womens_and_kids_items(self):
        cfg, searches = vb.load_config(Path(vb.HERE / "config.yaml"))
        by_name = {s.name: s for s in searches}

        def verdict(search, title, brand, price):
            deal, why = vb.evaluate(item(title=title, brand=brand, price=price, total=price + 20), by_name[search], cfg)
            return "fynd" if deal else why

        for title in ("Moncler dunjacka dam", "Moncler Kids 14 år", "Moncler women jacket", "Moncler damjacka",
                      "Moncler dunjacka dam, inte herr", "Moncler jacka för barn", "Moncler dunjacka barn 140",
                      "Moncler kid jacket 10y", "Moncler dam inte för herr", "Women's Moncler, not suitable for men",
                      "Kurtka damska Moncler, nie męska", "Moncler Damen Daunenjacke, nicht Herren",
                      "Doudoune Moncler femme, pas homme", "Moncler dam men liten i storleken"):
            self.assertNotEqual(verdict("Moncler", title, "Moncler", 1000), "fynd", title)
        for title in ("Moncler Maya herr", "Moncler jacka Amsterdam", "Moncler damaged zip", "Moncler herr/dam",
                      "Moncler unisex dam", "Moncler veste homme femme", "Moncler men's women's",
                      "Moncler men/women", "Moncler jacket men & women", "Moncler men women size 3",
                      "Doudoune Moncler mixte femme", "Kurtka Moncler uniseks damska"):
            self.assertEqual(verdict("Moncler", title, "Moncler", 1000), "fynd", title)
        # "barn coat" och "kid mohair" är herrplagg, inte barnkläder – men "barn 140" är det.
        self.assertEqual(verdict("Carhartt jackor", "Carhartt Barn Coat brun", "Carhartt", 100), "fynd")
        self.assertNotEqual(verdict("Carhartt jackor", "Carhartt barn coat barn 140", "Carhartt", 100), "fynd")
        self.assertEqual(verdict("Acne Studios tröjor", "Acne Studios kid mohair cardigan", "Acne Studios", 200), "fynd")


if __name__ == "__main__":
    unittest.main()
