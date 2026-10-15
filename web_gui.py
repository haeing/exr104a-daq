"""Local web GUI for EXR104A acquisition. Run: python web_gui.py"""

import argparse
from array import array
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlparse

from exr_daq import Store, dependencies, utc_now, gate_waveform, storage_factor
from scope import CaptureCancelled, Preamble, Scope
from runfiles import DEFAULT_DATA_DIR, allocate_run, next_run_number
from connection import open_instrument

STATIC = Path(__file__).resolve().parent / "web"
METRICS = ("positive_height_v", "negative_height_v", "max_v", "min_v")


def number(value, name, low, high, integer=False):
    if isinstance(value, bool):
        raise ValueError(f"{name}: enter a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: enter a number") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name}: enter a value between {low} and {high}")
    if integer and result != int(result):
        raise ValueError(f"{name}: enter an integer")
    return int(result) if integer else result


def validate_config(body):
    if not isinstance(body, dict):
        raise ValueError("Settings must be a JSON object")
    config = {
        "channel": number(body.get("channel", 3), "Channel", 1, 4, True),
        "trigger_level": number(body.get("trigger_mv", 100), "Trigger (mV)", -1e6, 1e6) / 1000,
        "slope": body.get("slope", "rising"),
        "count": number(body.get("count", 1000), "Event count", 1, 1000000, True),
        "baseline_samples": None,
        "binning": number(body.get("binning", 1), "Waveform binning", 1, 1000000, True),
        "bin_width_us": number(body.get("bin_width_us", 0), "Bin width (us)", 0, 1000000),
        "gate_before_us": number(body.get("gate_before_us", 1), "Before trigger (us)", 0, 1000000),
        "gate_after_us": number(body.get("gate_after_us", 3), "After trigger (us)", 0, 1000000),
    }
    if config["slope"] not in ("rising", "falling", "either"):
        raise ValueError("Select a valid trigger edge")
    duration = config["gate_before_us"] + config["gate_after_us"]
    if duration <= 0 or config["bin_width_us"] > duration / 2:
        raise ValueError("Gate must be positive and contain at least two waveform bins")
    if body.get("baseline_samples") not in (None, ""):
        config["baseline_samples"] = number(body["baseline_samples"], "Baseline samples", 1, 100000000, True)
    if body.get("filename"):
        raise ValueError("Run filenames are assigned automatically. Refresh the page to use the new settings.")
    return config


def preview_waveform(raw, preamble, limit=1000):
    """Min/max envelope: preserve narrow peaks in a bounded web preview."""
    np, _ = dependencies()
    if len(raw) <= limit * 2:
        indices = np.arange(len(raw))
    else:
        indices = []
        for group in np.array_split(np.arange(len(raw)), limit):
            block = raw[group[0]:group[-1] + 1]
            indices.extend(sorted((int(group[0] + np.argmin(block)),
                                   int(group[0] + np.argmax(block)))))
        indices = np.unique([0, *indices, len(raw) - 1])
    voltage = (raw[indices].astype(float) - preamble.y_reference) * preamble.y_increment + preamble.y_origin
    times = (indices - preamble.x_reference) * preamble.x_increment + preamble.x_origin
    return {"time_us": (times * 1e6).tolist(), "voltage_v": voltage.tolist()}


class Analyzer:
    def __init__(self, data_dir=DEFAULT_DATA_DIR, ip="192.168.10.103", demo=False, resource=None, backend="@py"):
        self.data_dir = Path(data_dir).resolve()
        self.ip, self.demo, self.resource, self.backend = ip, demo, resource, backend
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.values = {metric: array("d") for metric in METRICS}
        self.state = {"status": "idle", "saved": 0, "target": 0, "error": "", "output": "",
                      "points": 0, "baseline_samples": 0, "elapsed_s": 0, "rate_hz": 0,
                      "bytes": 0, "waveform": None, "latest": None, "config": None,
                      "started_utc": None}
        self.state.update(stored_points=0, run_number=None, actual_bin_width_us=0, acquired_points=0)
        self.started = None

    def update(self, **kwargs):
        with self.lock:
            self.state.update(kwargs)

    def start(self, body):
        config = validate_config(body)
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("Acquisition is already running. Stop the run before changing settings.")
            try:
                output, run_number = allocate_run(self.data_dir)
            except OSError as exc:
                raise ValueError(f"Cannot write to data directory {self.data_dir}: {exc}") from exc
            config.update(filename=output.name, run_number=run_number)
            config["started_utc"] = utc_now()
            self.stop_event.clear()
            self.values = {metric: array("d") for metric in METRICS}
            self.started = time.monotonic()
            self.state.update(status="connecting", saved=0, target=config["count"], error="",
                              output=str(output), points=0, baseline_samples=0, bytes=0,
                              waveform=None, latest=None, config=config, started_utc=config["started_utc"],
                              elapsed_s=0, rate_hz=0, stored_points=0, run_number=run_number,
                              actual_bin_width_us=0, acquired_points=0)
            self.thread = threading.Thread(target=self.run, args=(config, output), daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self.stop_event.set()
                self.state["status"] = "stopping"

    def snapshot(self, metric="positive_height_v", bins=60):
        if metric not in METRICS:
            raise ValueError("Unknown histogram metric")
        bins = number(bins, "Bins", 5, 500, True)
        with self.lock:
            result = dict(self.state)
            values = array("d", self.values[metric])
            if self.started is not None and self.thread and self.thread.is_alive():
                result["elapsed_s"] = time.monotonic() - self.started
            result["rate_hz"] = result["saved"] / max(result["elapsed_s"], 0.001)
        histogram = {"edges": [], "counts": [], "mean": None, "std": None, "metric": metric}
        if values:
            np, _ = dependencies()
            data = np.frombuffer(values, dtype=np.float64)
            counts, edges = np.histogram(data, bins=bins)
            histogram.update(edges=edges.tolist(), counts=counts.tolist(),
                             mean=float(data.mean()), std=float(data.std()))
        result.update(histogram=histogram, demo=self.demo, ip=self.ip,
                      data_dir=str(self.data_dir), next_filename=f"run{next_run_number(self.data_dir):05d}.dat")
        return result

    def record(self, store, raw, preamble, config):
        acquired_points = len(raw)
        raw, preamble = gate_waveform(raw, preamble, config)
        baseline = config["baseline_samples"] or max(1, len(raw) // 10)
        baseline_v, max_v, min_v, positive, negative = store.append(raw, preamble, baseline)
        latest = dict(zip(METRICS, (positive, negative, max_v, min_v)))
        latest["baseline_v"] = baseline_v
        wave = preview_waveform(raw, preamble)
        with self.lock:
            for metric in METRICS:
                self.values[metric].append(latest[metric])
            self.state.update(saved=self.state["saved"] + 1, points=len(raw),
                              stored_points=store.last_info["stored_points"],
                              acquired_points=acquired_points,
                              actual_bin_width_us=store.last_info["actual_bin_width_us"],
                              baseline_samples=baseline, waveform=wave, latest=latest,
                              elapsed_s=time.monotonic() - self.started,
                              bytes=Path(store.file.filename).stat().st_size)

    def run(self, config, output):
        store = None
        final_status = "failed"
        try:
            np, _ = dependencies()
            with ExitStack() as stack:
                if self.demo:
                    identity = "DEMO / simulated EXR104A"
                    scope = None
                    rng = np.random.default_rng()
                else:
                    import pyvisa
                    manager = pyvisa.ResourceManager(self.backend)
                    stack.callback(manager.close)
                    instrument = open_instrument(manager, self.ip, self.resource)
                    stack.callback(instrument.close)
                    instrument.timeout = 30000  # Per-operation communication timeout, not a trigger deadline.
                    instrument.read_termination = "\n"
                    instrument.write_termination = "\n"
                    instrument.chunk_size = 1024 * 1024

                    def stop_scope():
                        try:
                            instrument.write(":STOP")
                        except Exception:
                            pass
                    stack.callback(stop_scope)
                    scope = Scope(instrument)
                    identity = scope.configure(config["channel"], config["trigger_level"], config["slope"],
                                               gate_before_us=config["gate_before_us"],
                                               gate_after_us=config["gate_after_us"])
                store = Store(output, {**config, "ip": self.ip, "demo": self.demo}, identity)
                # Keep the writer on this worker; HTTP handlers never read active HDF5.
                stack.callback(store.close)
                self.update(status="stopping" if self.stop_event.is_set() else "running")
                try:
                    for _ in range(config["count"]):
                        if self.stop_event.is_set():
                            raise CaptureCancelled()
                        if self.demo:
                            if self.stop_event.wait(0.1):
                                raise CaptureCancelled()
                            duration = (config["gate_before_us"] + config["gate_after_us"]) * 1e-6
                            dt = duration / 4096
                            preamble = Preamble.parse(f"2,1,4096,1,{dt},"
                                                       f"{-config['gate_before_us'] * 1e-6},0,0.0001,0,0")
                            x = np.arange(preamble.points)
                            amplitude = abs(config["trigger_level"]) * 1.5 + 0.05
                            center = config["gate_before_us"] * 1e-6 / dt + 150
                            pulse = rng.normal(amplitude, amplitude * 0.08) * np.exp(-0.5 * ((x - center) / 60) ** 2)
                            if config["slope"] == "falling":
                                pulse = -pulse
                            raw = np.clip((pulse + rng.normal(0, 0.002, x.size)) / preamble.y_increment,
                                          -32768, 32767).astype(np.int16)
                        else:
                            raw, preamble = scope.capture(container=np.array, cancel_event=self.stop_event)
                        self.record(store, raw, preamble, config)
                    final_status = "complete"
                except CaptureCancelled:
                    final_status = "stopped"
                except Exception as exc:
                    store.file.attrs["error"] = str(exc)
                    raise
                finally:
                    store.file.attrs["status"] = final_status
        except Exception as exc:
            self.update(error=str(exc))
        finally:
            self.update(status=final_status, elapsed_s=time.monotonic() - self.started)


def make_handler(analyzer):
    class Handler(BaseHTTPRequestHandler):
        def send_body(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def json_reply(self, code, value):
            self.send_body(code, json.dumps(value, allow_nan=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                params = parse_qs(parsed.query)
                try:
                    self.json_reply(200, analyzer.snapshot(params.get("metric", [METRICS[0]])[0],
                                                          params.get("bins", [60])[0]))
                except ValueError as exc:
                    self.json_reply(400, {"error": str(exc)})
                return
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                     "/style.css": ("style.css", "text/css")}
            if parsed.path not in files:
                self.json_reply(404, {"error": "Not found"})
                return
            name, kind = files[parsed.path]
            self.send_body(200, (STATIC / name).read_bytes(), kind + "; charset=utf-8")

        def do_POST(self):
            # Only same-origin JSON may change the local acquisition state.
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://" + self.headers.get("Host", ""),):
                self.json_reply(403, {"error": "Origin not allowed"})
                return
            if self.headers.get_content_type() != "application/json":
                self.json_reply(415, {"error": "Expected application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 8192:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/start":
                    analyzer.start(body)
                elif self.path == "/api/stop":
                    analyzer.stop()
                else:
                    self.json_reply(404, {"error": "Not found"})
                    return
                self.json_reply(202, {"ok": True})
            except (ValueError, TypeError) as exc:
                self.json_reply(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.json_reply(409, {"error": str(exc)})

        def log_message(self, format, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ip", default="192.168.10.103")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--resource")
    parser.add_argument("--visa-backend", default="@py")
    parser.add_argument("--demo", action="store_true", help="Simulated data; never connects to scope")
    args = parser.parse_args()
    analyzer = Analyzer(args.data_dir, args.ip, args.demo, args.resource, args.visa_backend)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(analyzer))
    print(f"EXR analyzer: http://{args.host}:{args.port}" + (" [DEMO]" if args.demo else ""), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        analyzer.stop()
        if analyzer.thread:
            analyzer.thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
