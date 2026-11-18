from datetime import datetime, timedelta, timezone
from email.message import Message
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scope import CaptureCancelled
from test_daq import FakeInstrument, HAS_STORAGE
from web_gui import Analyzer, STATIC, make_handler, validate_config


class WebValidationTests(unittest.TestCase):
    def test_channel_trigger_and_units(self):
        config = validate_config({"channel": "2", "trigger_mv": "-100", "slope": "falling"})
        self.assertEqual(config["channel"], 2)
        self.assertEqual(config["trigger_level"], -0.1)
        self.assertEqual(config["slope"], "falling")
        self.assertEqual(config["binning"], 1)
        self.assertEqual(config["bin_width_us"], 0)

    def test_invalid_settings_and_paths(self):
        for body in ({"channel": 5}, {"channel": 2.5}, {"trigger_mv": "nan"},
                     {"count": 0}, {"count": True}, {"slope": "auto"},
                     {"filename": "../outside.h5"}, {"filename": "/tmp/file.h5"},
                     {"filename": "test.txt"}, {"baseline_samples": 0},
                     {"binning": 0}, {"binning": 1.5}, {"bin_width_us": -0.1},
                     {"gate_before_us": 0, "gate_after_us": 0},
                     {"gate_before_us": -1}, {"bin_width_us": 3}, []):
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_config(body)

    def test_capture_can_be_cancelled_before_arming(self):
        from scope import Scope
        import threading
        i = FakeInstrument()
        scope = Scope(i)
        scope.configure(3, 0.1, "rising")
        event = threading.Event()
        event.set()
        with self.assertRaises(CaptureCancelled):
            scope.capture(cancel_event=event)
        self.assertNotIn(":SINGle", i.commands)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.analyzer = Analyzer(self.tmp.name)

    def request(self, method, path, body=None, origin="http://localhost:8000"):
        handler = object.__new__(make_handler(self.analyzer))
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = "localhost:8000"
        handler.headers["Origin"] = origin
        handler.headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode()
        handler.headers["Content-Length"] = str(len(raw))
        handler.rfile = BytesIO(raw)
        reply = {}
        handler.send_body = lambda code, data, kind: reply.update(code=code, data=data, kind=kind)
        getattr(handler, "do_" + method)()
        return reply

    def test_static_and_idle_state(self):
        for path in ("/", "/style.css", "/app.js"):
            self.assertEqual(self.request("GET", path)["code"], 200)
        reply = self.request("GET", "/api/state")
        self.assertEqual(reply["code"], 200)
        state = json.loads(reply["data"])
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["histogram"]["counts"], [])

    def test_bad_config_rejected_without_worker(self):
        reply = self.request("POST", "/api/start", {"channel": 7})
        self.assertEqual(reply["code"], 400)
        self.assertIsNone(self.analyzer.thread)

    def test_comment_before_first_run(self):
        reply = self.request("POST", "/api/comment", {"run_number": None, "comment": "측정 준비"})
        self.assertEqual(reply["code"], 202)
        line = (Path(self.tmp.name) / "run-log.txt").read_text(encoding="utf-8").strip()
        run_number, event, _, saved_comment = line.split("\t")
        self.assertEqual((run_number, event, json.loads(saved_comment)), ("-", "COMMENT", "측정 준비"))

    def test_foreign_origin_cannot_start(self):
        reply = self.request("POST", "/api/start", {}, origin="https://example.org")
        self.assertEqual(reply["code"], 403)
        self.assertIsNone(self.analyzer.thread)

    def test_double_start_conflict(self):
        fake = type("Worker", (), {"is_alive": lambda self: True})()
        self.analyzer.thread = fake
        reply = self.request("POST", "/api/start", {})
        self.assertEqual(reply["code"], 409)

    def test_static_has_unique_ids_and_valid_asset_references(self):
        class PageParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []
                self.assets = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.ids.append(attrs["id"])
                if tag == "script":
                    self.assets.append(attrs["src"])
                if tag == "link" and attrs.get("rel") == "stylesheet":
                    self.assets.append(attrs["href"])

        page = PageParser()
        page.feed((STATIC / "index.html").read_text())
        self.assertEqual(len(page.ids), len(set(page.ids)))
        for asset in page.assets:
            self.assertTrue((STATIC / asset.lstrip("/")).is_file())


@unittest.skipUnless(HAS_STORAGE, "numpy and h5py are not installed")
class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.analyzer = Analyzer(self.tmp.name, demo=True)

    def wait(self):
        self.analyzer.thread.join(timeout=10)
        self.assertFalse(self.analyzer.thread.is_alive())

    def test_demo_roundtrip_histogram_and_preview(self):
        import h5py
        import numpy as np
        self.analyzer.start({"channel": 3, "trigger_mv": 100, "count": 3})
        self.wait()
        state = self.analyzer.snapshot(bins=10)
        self.assertEqual(state["status"], "complete", state["error"])
        self.assertEqual(state["saved"], 3)
        self.assertEqual(sum(state["histogram"]["counts"]), 3)
        self.assertLessEqual(len(state["waveform"]["time_us"]), 2002)
        with h5py.File(state["output"], "r") as f:
            self.assertEqual(f.attrs["committed_events"], 3)
            self.assertEqual(f.attrs["run_started_utc"], state["started_utc"])
            self.assertEqual(f.attrs["status"], "complete")
            self.assertEqual(f["adc"].shape, (3, 4096))
            np.testing.assert_allclose(state["latest"]["positive_height_v"],
                                       f["events"][-1]["positive_height_v"])

    def test_stop_keeps_committed_events(self):
        import h5py
        self.analyzer.start({"count": 1000})
        deadline = time.monotonic() + 5
        while self.analyzer.snapshot()["saved"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.analyzer.add_comment("수동 종료 예정", run_number=1)
        self.analyzer.stop(run_number=1)
        self.wait()
        state = self.analyzer.snapshot()
        self.assertEqual(state["status"], "stopped", state["error"])
        self.assertGreaterEqual(state["saved"], 1)
        with h5py.File(state["output"], "r") as f:
            self.assertEqual(f.attrs["committed_events"], state["saved"])
            self.assertEqual(f.attrs["status"], "stopped")
        lines = (Path(self.tmp.name) / "run-log.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[1].split("\t")[-1], '"수동 종료 예정"')
        ended = datetime.fromisoformat(state["ended_utc"]).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(lines[2].split("\t"), ["00001", "END", ended, '""'])

    def test_comment_adds_a_line_after_auto_completion(self):
        self.analyzer.start({"count": 1, "comment": "실험 시작\nCH 3"})
        self.wait()
        state = self.analyzer.snapshot()
        self.assertEqual(state["status"], "complete", state["error"])
        self.analyzer.add_comment("자동 종료 확인", state["run_number"])
        self.analyzer.add_comment("추가 기록", state["run_number"])
        lines = (Path(self.tmp.name) / "run-log.txt").read_text(encoding="utf-8").splitlines()
        started = datetime.fromisoformat(state["started_utc"]).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M:%S")
        ended = datetime.fromisoformat(state["ended_utc"]).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(lines[:2], [
            f'00001\tSTART\t{started}\t"실험 시작\\nCH 3"',
            f'00001\tEND\t{ended}\t""',
        ])
        self.assertEqual(len(lines), 4)
        for line, value in zip(lines[2:], ("자동 종료 확인", "추가 기록")):
            run_number, event, timestamp, saved_comment = line.split("\t")
            self.assertEqual((run_number, event, json.loads(saved_comment)), ("00001", "COMMENT", value))
            self.assertRegex(timestamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
            self.assertGreaterEqual(timestamp, ended)

    def test_existing_output_never_overwritten(self):
        path = Path(self.tmp.name) / "run00001.dat"
        path.write_bytes(b"existing")
        self.analyzer.start({"count": 1})
        self.wait()
        self.assertEqual(Path(self.analyzer.snapshot()["output"]).name, "run00002.dat")
        self.assertEqual(path.read_bytes(), b"existing")

    def test_storage_failure_reported(self):
        with patch("web_gui.Store", side_effect=OSError("disk full")):
            self.analyzer.start({"count": 1, "comment": "실험 시작"})
            self.wait()
        state = self.analyzer.snapshot()
        self.assertEqual(state["status"], "failed")
        self.assertIn("disk full", state["error"])
        self.analyzer.add_comment("장비 확인", state["run_number"])
        lines = (Path(self.tmp.name) / "run-log.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [(fields[0], fields[1], json.loads(fields[3])) for fields in (line.split("\t") for line in lines)],
            [("00001", "START", "실험 시작"), ("00001", "END", "ERROR: disk full"),
             ("00001", "COMMENT", "장비 확인")],
        )

    def test_bin_width_us_gate_and_stored_time_axis(self):
        import h5py
        self.analyzer.start({"count": 1, "bin_width_us": 0.01,
                             "gate_before_us": 2, "gate_after_us": 6})
        self.wait()
        state = self.analyzer.snapshot()
        self.assertEqual(state["status"], "complete", state["error"])
        # Every event uses exactly 800 fixed bins of 0.01 us.
        self.assertEqual(state["stored_points"], 800)
        self.assertAlmostEqual(state["actual_bin_width_us"], 0.01)
        self.assertAlmostEqual(state["waveform"]["time_us"][0], -2)
        self.assertLess(state["waveform"]["time_us"][-1], 6)
        with h5py.File(state["output"], "r") as f:
            event = f["events"][0]
            self.assertAlmostEqual(event["x_increment"] * 1e6, state["actual_bin_width_us"])
            self.assertEqual(f.attrs["waveform_binning"], 5)

    def test_binned_run_and_automatic_increment(self):
        import h5py
        self.analyzer.start({"count": 2, "binning": 10})
        self.wait()
        state = self.analyzer.snapshot()
        self.assertEqual(state["status"], "complete", state["error"])
        self.assertEqual(state["points"], 4096)
        self.assertEqual(state["stored_points"], 409)
        self.assertEqual(state["run_number"], 1)
        self.assertEqual(state["next_filename"], "run00002.dat")
        with h5py.File(state["output"], "r") as f:
            self.assertEqual(f["adc"].shape, (2, 409))
            self.assertEqual(f.attrs["waveform_binning"], 10)
        restarted = Analyzer(self.tmp.name, demo=True)
        restarted.start({"count": 1})
        restarted.thread.join(timeout=5)
        self.assertEqual(restarted.snapshot()["run_number"], 2)

    def test_hardware_worker_uses_selected_channel_and_trigger(self):
        from types import SimpleNamespace
        instrument = FakeInstrument()
        instrument.close = lambda: None
        manager = SimpleNamespace(open_resource=lambda address: instrument, close=lambda: None)
        visa = SimpleNamespace(ResourceManager=lambda backend: manager)
        self.analyzer.demo = False
        with patch.dict("sys.modules", {"pyvisa": visa}):
            self.analyzer.start({"channel": 2, "trigger_mv": 100, "count": 2,
                                 "baseline_samples": 1, "gate_before_us": 0.002,
                                 "gate_after_us": 0.002})
            self.wait()
        state = self.analyzer.snapshot()
        self.assertEqual(state["status"], "complete", state["error"])
        self.assertEqual(state["saved"], 2)
        self.assertIn(":TRIGger:EDGE:SOURce CHANnel2", instrument.commands)
        trigger = next(c for c in instrument.commands if c.startswith(":TRIGger:LEVel "))
        self.assertAlmostEqual(float(trigger.split(",")[1]), 0.1)
        self.assertEqual(instrument.commands.count(":SINGle"), 2)
        self.assertEqual(instrument.commands[-1], ":STOP")


if __name__ == "__main__":
    unittest.main()
