import datetime as dt
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fieldcard as fc  # noqa: E402

DEMO = os.path.join(os.path.dirname(__file__), "..", "examples", "demo_places_nyc.json")
NYC = (40.7678, -73.9718)


class Geometry(unittest.TestCase):
    def test_haversine_one_degree_lat(self):
        self.assertAlmostEqual(fc.haversine_km(0, 0, 1, 0), 111.19, delta=0.2)

    def test_bearing_and_compass(self):
        self.assertAlmostEqual(fc.bearing_deg(0, 0, 1, 0), 0, delta=0.1)
        self.assertEqual(fc.compass(90), "E")
        self.assertEqual(fc.compass(359), "N")


class SunAndSeason(unittest.TestCase):
    def test_nyc_oct_10_2026(self):
        s = fc.sun_events(dt.date(2026, 10, 10), *NYC, tz_hours=-4)
        # reference values cross-checked with the `astral` library (EDT)
        self.assertLess(abs((s["sunrise"] - dt.datetime(2026, 10, 10, 7, 2)).total_seconds()), 240)
        self.assertLess(abs((s["sunset"] - dt.datetime(2026, 10, 10, 18, 23)).total_seconds()), 240)
        self.assertGreater(s["dusk"], s["sunset"])

    def test_polar_night_has_no_sunrise(self):
        s = fc.sun_events(dt.date(2026, 12, 21), 78.2, 15.6, 1)
        self.assertIsNone(s["sunrise"])

    def test_seasons(self):
        self.assertEqual(fc.season_for(40, 10), "autumn")
        self.assertEqual(fc.season_for(-33, 10), "spring")
        self.assertEqual(fc.season_for(-33, 12), "summer")
        self.assertEqual(fc.season_for(10, 7), "tropical")


class Places(unittest.TestCase):
    def test_reach_scales_with_time_and_speed(self):
        self.assertGreater(fc.reach_km("run", 60), fc.reach_km("walk", 60))
        self.assertEqual(fc.reach_km("garden", 60), 0)

    def test_pick_places_respects_reach_and_variety(self):
        with open(DEMO) as f:
            raw = json.load(f)
        short = fc.pick_places(raw, *NYC, "walk", 30)
        longer = fc.pick_places(raw, *NYC, "walk", 120)
        self.assertLessEqual(max(p["dist"] for p in short), fc.reach_km("walk", 30))
        self.assertGreater(len(longer), len(short))
        self.assertEqual(longer, sorted(longer, key=lambda p: p["dist"]))

    def test_parse_overpass_dedups_and_uses_center(self):
        data = {"elements": [
            {"tags": {"leisure": "park", "name": "A"}, "center": {"lat": 1, "lon": 2}},
            {"tags": {"leisure": "park", "name": "A"}, "lat": 1, "lon": 2},
            {"tags": {"tourism": "viewpoint"}, "lat": 3, "lon": 4},
            {"tags": {"leisure": "park"}, "lat": 5, "lon": 6}]}
        got = fc.parse_overpass(data, 0, 0)
        self.assertEqual([p["name"] for p in got], ["A", "Viewpoint"])


class Copy(unittest.TestCase):
    facts = {"season": "autumn", "activity": "walk", "minutes": 90, "date": "x"}

    def setUp(self):
        import random
        self.fb = fc.fallback_copy(self.facts, random.Random(1))

    def test_fallback_has_nine_distinct_items(self):
        self.assertEqual(len(set(self.fb["notice"])), 9)

    def test_parse_rejects_garbage_and_clips(self):
        self.assertIsNone(fc.parse_llm_json("not json", self.fb))
        long = {"title": "word " * 30, "mission": "ok", "notice": ["a b c"] * 5, "send_off": "bye"}
        got = fc.parse_llm_json(json.dumps(long), self.fb)
        self.assertLessEqual(len(got["title"].split()), 7)
        self.assertEqual(len(got["notice"]), 9)
        self.assertEqual(got["source"], "local model")


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        payload = {"title": "Autumn Light Hunt", "mission": "Walk to <b>The Pond</b> and listen.",
                   "notice": [f"thing {i}" for i in range(9)], "send_off": "Go slowly."}
        body = json.dumps({"response": json.dumps(payload)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class EndToEnd(unittest.TestCase):
    def run_cli(self, *extra):
        out = os.path.join(tempfile.mkdtemp(), "card.html")
        fc.main(["--lat", str(NYC[0]), "--lon", str(NYC[1]), "--date", "2026-10-10",
                 "--tz", "-4", "--out", out, *extra])
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_offline_card(self):
        h = self.run_cli("--places", DEMO, "--no-llm", "--start", "15:30")
        self.assertIn("The Pond", h)
        self.assertIn("<svg", h)
        self.assertNotIn("Daylight check", h)

    def test_late_start_warns_with_latest_start(self):
        h = self.run_cli("--places", DEMO, "--no-llm", "--start", "17:30")
        self.assertIn("Daylight check", h)

    def test_no_places_gives_turnaround_rule(self):
        h = self.run_cli("--no-net", "--no-llm", "--start", "15:00")
        self.assertIn("Turn around at", h)

    def test_garden_mode_has_no_map_or_turnaround(self):
        h = self.run_cli("--activity", "garden", "--no-llm", "--start", "09:00")
        self.assertNotIn("<svg", h)
        self.assertNotIn("Turn around</b>", h)

    def test_local_model_path_and_html_escaping(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_port}"
        try:
            h = self.run_cli("--places", DEMO, "--start", "15:30")
        finally:
            srv.shutdown()
            srv.server_close()
            del os.environ["OLLAMA_HOST"]
        self.assertIn("Autumn Light Hunt", h)
        self.assertIn("&lt;b&gt;The Pond&lt;/b&gt;", h)  # model output is escaped
        self.assertIn("local model", h)

    def test_unreachable_model_falls_back(self):
        os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
        try:
            h = self.run_cli("--places", DEMO, "--start", "15:30")
        finally:
            del os.environ["OLLAMA_HOST"]
        self.assertIn("built-in prompts", h)


class WebServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fc.WebHandler.places_override = DEMO
        cls.server = HTTPServer(("127.0.0.1", 0), fc.WebHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_form_200(self):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=3) as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8")
            self.assertIn("Fieldcard", body)
            self.assertIn('name="lat"', body)
            self.assertIn('name="minutes"', body)
            self.assertIn("Use my location", body)
            self.assertIn("NYC Central Park", body)
            self.assertIn("Ollama local model", body)

    def test_post_valid_card_200(self):
        import urllib.request
        import urllib.parse
        url = f"http://127.0.0.1:{self.port}/card"
        data = urllib.parse.urlencode({
            "lat": "40.7678",
            "lon": "-73.9718",
            "minutes": "90",
            "activity": "walk",
            "model": "llama3.2:3b"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8")
            self.assertIn("The Pond", body)
            self.assertIn("Print Field Card (A5)", body)
            self.assertIn("Make another card", body)

    def test_post_invalid_latitude_400(self):
        import urllib.request
        import urllib.parse
        url = f"http://127.0.0.1:{self.port}/card"
        data = urllib.parse.urlencode({"lat": "999", "lon": "0", "minutes": "90", "activity": "walk"}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                self.fail("Expected 400 Bad Request")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            body = e.read().decode("utf-8")
            self.assertIn("Invalid Request", body)
            self.assertNotIn("Traceback", body)

    def test_post_invalid_activity_400(self):
        import urllib.request
        import urllib.parse
        url = f"http://127.0.0.1:{self.port}/card"
        data = urllib.parse.urlencode({"lat": "40.7", "lon": "-73.9", "minutes": "90", "activity": "skydiving"}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                self.fail("Expected 400 Bad Request")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_post_oversized_payload_413(self):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/card"
        data = b"x" * 70000
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                self.fail("Expected 413 Payload Too Large")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)


class FeaturesTests(unittest.TestCase):
    def test_qr_code_svg(self):
        import qr_code
        svg = qr_code.qr_svg("https://fieldcard.local/test", size_px=120)
        self.assertIn("<svg", svg)
        self.assertIn('viewBox="0 0', svg)
        self.assertIn("<rect", svg)

    def test_garden_advice_calculation(self):
        advice = fc.garden_advice("2026-10-15", today=dt.date(2026, 10, 10))
        self.assertGreater(len(advice), 0)
        crops = [a["crop"] for a in advice]
        self.assertTrue(any("Garlic" in c or "Kale" in c or "Spinach" in c for c in crops))

    def test_run_club_group_mode(self):
        out = os.path.join(tempfile.mkdtemp(), "card.html")
        fc.main(["--lat", str(NYC[0]), "--lon", str(NYC[1]), "--group", "--no-llm",
                 "--places", DEMO, "--out", out])
        with open(out, encoding="utf-8") as f:
            h = f.read()
        self.assertIn("Pace options", h)
        self.assertIn("Conversational", h)

    def test_post_walk_journal_log(self):
        tmp_log = os.path.join(tempfile.mkdtemp(), "test_notes.md")
        code = fc.run_log(["--found", "Oak leaf, spider web, moss", "--notes", "Cold breeze from south", "--file", tmp_log])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(tmp_log))
        with open(tmp_log, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Oak leaf, spider web, moss", content)
        self.assertIn("Cold breeze from south", content)

    def test_banned_words_filtered(self):
        fallback = {"title": "Safe Title", "mission": "Safe mission", "notice": ["Safe 1"] * 9, "send_off": "Safe bye"}
        bad_json = json.dumps({
            "title": "Embrace the glorious tapestry",
            "mission": "Immerse yourself and unplug from screens in nature's beauty",
            "notice": ["Item one", "Item two"],
            "send_off": "Go forth"
        })
        parsed = fc.parse_llm_json(bad_json, fallback)
        self.assertEqual(parsed["title"], "Safe Title")
        self.assertEqual(parsed["mission"], "Safe mission")


if __name__ == "__main__":
    unittest.main()

