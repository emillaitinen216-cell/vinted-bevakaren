"""Tester för vinted_bot. Kör: python -m unittest -v"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
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
        self.assertIsNone(vb.evaluate(item(title="Nudie no. kopia"), search, self.cfg)[0])
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


if __name__ == "__main__":
    unittest.main()
