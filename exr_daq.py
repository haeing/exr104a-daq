"""Triggered EXR104A acquisition, HDF5 storage and pulse-height histograms."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import time

from scope import Scope
from runfiles import DEFAULT_DATA_DIR, allocate_run
from connection import open_instrument


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def dependencies():
    try:
        import numpy as np
        import h5py
    except ModuleNotFoundError as exc:
        raise RuntimeError("Install dependencies: python -m pip install -r requirements.txt") from exc
    return np, h5py


def features(raw, preamble, baseline_samples):
    np, _ = dependencies()
    if not 1 <= baseline_samples < len(raw):
        raise ValueError("baseline-samples must be between 1 and waveform points - 1")
    # Compute in float64; original signed ADC words are saved without conversion.
    voltage = (np.asarray(raw, dtype=np.float64) - preamble.y_reference)
    voltage = voltage * preamble.y_increment + preamble.y_origin
    baseline = float(np.mean(voltage[:baseline_samples]))
    maximum, minimum = float(np.max(voltage)), float(np.min(voltage))
    return baseline, maximum, minimum, maximum - baseline, baseline - minimum


def gate_waveform(raw, preamble, config):
    """Select [-before, +after) using the instrument's actual sample times."""
    before, after = config.get("gate_before_us"), config.get("gate_after_us")
    if before is None and after is None:
        return raw, preamble
    if before is None or after is None or before < 0 or after < 0 or before + after <= 0:
        raise ValueError("Both gate durations are required; their sum must be positive")
    dt = preamble.x_increment
    first = preamble.x_origin - preamble.x_reference * dt
    low, high = -before * 1e-6, after * 1e-6
    if first > low + dt * 1.01 or first + len(raw) * dt < high - dt * 1.01:
        raise ValueError("Acquired waveform does not cover the requested trigger gate; check scope timebase")
    start = max(0, int(math.ceil((low - first) / dt - 1e-7)))
    end = min(len(raw), int(math.ceil((high - first) / dt - 1e-7)))
    if end - start < 2:
        raise ValueError("Gate must contain at least two acquired samples")
    return raw[start:end], replace(preamble, points=end - start,
                                   x_origin=first + start * dt, x_reference=0)


def storage_factor(preamble, config):
    width = config.get("bin_width_us", 0)
    if width:
        if not math.isfinite(width) or width < 0:
            raise ValueError("Bin width must be nonnegative")
        return max(1, int(math.floor(width * 1e-6 / preamble.x_increment + 0.5)))
    return config.get("binning", 1)


class Store:
    """One extendible waveform matrix; no per-event files or groups."""

    def __init__(self, path, config, identity):
        np, h5py = dependencies()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.binning = config.get("binning", 1)
        self.config = dict(config)
        self.time_grid = None
        self.last_info = None
        if isinstance(self.binning, bool) or not isinstance(self.binning, int) or self.binning < 1:
            raise ValueError("Waveform binning must be a positive integer")
        self.file = h5py.File(path, "x")  # Never overwrite a previous run.
        self.file.attrs.update(
            schema_version=3, created_utc=utc_now(), instrument=identity,
            run_started_utc=config.get("started_utc") or utc_now(),
            config_json=json.dumps(config), committed_events=0, status="running",
            voltage_formula="(adc - y_reference) * y_increment + y_origin",
            time_formula="(sample_index - x_reference) * x_increment + x_origin",
            timestamp_note="Host receive time, not hardware trigger timestamp",
            waveform_binning=self.binning,
            binning_method="mean of consecutive ADC samples, rounded to nearest int16",
            feature_source="unbinned waveform within the selected trigger gate",
            adc_layout="Fixed-length rows on a single time grid for the entire run; no padding",
            metadata_note="Per-event lengths and binning are authoritative; file attributes describe the first event",
        )
        self.summary_dtype = np.dtype([
            ("received_unix_ns", "<i8"), ("x_increment", "<f8"),
            ("x_origin", "<f8"), ("x_reference", "<f8"),
            ("y_increment", "<f8"), ("y_origin", "<f8"), ("y_reference", "<f8"),
            ("baseline_v", "<f8"), ("max_v", "<f8"), ("min_v", "<f8"),
            ("positive_height_v", "<f8"), ("negative_height_v", "<f8"),
            ("original_points", "<u8"), ("stored_points", "<u8"),
            ("baseline_samples", "<u8"), ("binning", "<u8"),
            ("discarded_tail_samples", "<u8"),
        ])

    def append(self, raw, preamble, baseline_samples):
        np, _ = dependencies()
        f = self.file
        values = features(raw, preamble, baseline_samples)
        factor = storage_factor(preamble, self.config)
        original_points = len(raw)
        fixed_gate = self.config.get("gate_before_us") is not None and self.config.get("gate_after_us") is not None
        if fixed_gate:
            # Anchor bins to the requested trigger gate, not the first returned
            # sample. A fractional-sample trigger shift must not resize the row.
            native_grid = not self.config.get("bin_width_us", 0) and self.binning == 1
            first = preamble.x_origin - preamble.x_reference * preamble.x_increment
            if self.time_grid is None:
                low = -self.config["gate_before_us"] * 1e-6
                high = self.config["gate_after_us"] * 1e-6
                width = self.config.get("bin_width_us", 0) * 1e-6 or preamble.x_increment * factor
                count = int(math.floor((high - low) / width + 1e-7))
                if native_grid:
                    low = first
                    count = int(math.floor((high - low) / width - 1e-7)) + 1
                self.time_grid = (low, width, count)
            low, width, stored_points = self.time_grid
            if not native_grid and width < preamble.x_increment * (1 - 1e-7):
                raise ValueError("Fixed bin width is smaller than the native sample interval; increase bin width")
            if stored_points < 2:
                raise ValueError("Waveform binning must leave at least two stored samples")
            if native_grid:
                # Lock the first native time grid. Later fractional-sample
                # shifts are aligned by linear interpolation, never by padding.
                times = first + np.arange(original_points) * preamble.x_increment
                centers = low + np.arange(stored_points) * width
                tolerance = preamble.x_increment * 1.01
                if centers[0] < times[0] - tolerance or centers[-1] > times[-1] + tolerance:
                    raise ValueError("Acquisition does not cover the fixed native time grid")
                raw = np.rint(np.interp(centers, times, raw)).astype(np.int16)
                saved_origin, discarded_tail = low, 0
            else:
                edges = low + np.arange(stored_points + 1) * width
                boundaries = np.clip(np.ceil((edges - first) / preamble.x_increment - 1e-7),
                                     0, original_points).astype(np.int64)
                counts = np.diff(boundaries)
                if np.any(counts == 0):
                    raise ValueError("Acquisition has no samples in a fixed gate bin; check scope timebase or increase bin width")
                sums = np.concatenate(([0.0], np.cumsum(raw, dtype=np.float64)))
                raw = np.rint(np.diff(sums[boundaries]) / counts).astype(np.int16)
                saved_origin = low + width / 2
                discarded_tail = original_points - int(boundaries[-1])
            saved_dt, saved_reference = width, 0
        else:
            stored_points = original_points // factor
            saved_dt = preamble.x_increment * factor
            saved_origin = preamble.x_origin + (factor - 1) * preamble.x_increment / 2
            saved_reference = preamble.x_reference / factor
            discarded_tail = original_points % factor
        if stored_points < 2:
            raise ValueError("Waveform binning must leave at least two stored samples")
        if not fixed_gate and factor > 1:
            # A uniform time axis requires full bins; discard only the trailing
            # incomplete bin. Features above still include every original sample.
            raw = np.rint(np.asarray(raw)[:stored_points * factor].reshape(-1, factor)
                          .mean(axis=1)).astype(np.int16)
        if "adc" not in f:
            f.create_dataset(
                "adc", shape=(0, len(raw)), maxshape=(None, len(raw)), dtype="<i2",
                chunks=(1, min(len(raw), 262144)), compression="lzf", shuffle=True,
            )
            f.create_dataset("events", shape=(0,), maxshape=(None,),
                             dtype=self.summary_dtype, chunks=(1024,))
            f.attrs["first_preamble"] = preamble.text
            f.attrs["baseline_samples"] = baseline_samples
            f.attrs["waveform_binning"] = factor
            f.attrs["actual_bin_width_us"] = saved_dt * 1e6
            f.attrs["fixed_time_grid"] = fixed_gate
            if fixed_gate:
                f.attrs["binning_method"] = ("linear interpolation onto the first native time grid; nearest sample at boundaries within one native interval"
                                             if native_grid else "mean of samples within fixed trigger-aligned time bins, rounded to nearest int16")
                f.attrs["grid_origin_s"] = saved_origin
                f.attrs["grid_interval_s"] = saved_dt
            f.attrs["original_points"] = original_points
            f.attrs["stored_points"] = len(raw)
            f.attrs["discarded_tail_samples"] = discarded_tail
            f.attrs["baseline_note"] = "baseline_samples counts original acquisition samples"
        n = int(f.attrs["committed_events"])
        if len(raw) != f["adc"].shape[1]:
            raise ValueError(f"Stored waveform length changed from {f['adc'].shape[1]} to {len(raw)}; use a fixed gate and bin width")
        f["adc"].resize(n + 1, axis=0)
        f["events"].resize(n + 1, axis=0)
        f["adc"][n, :len(raw)] = raw
        f["events"][n] = (
            time.time_ns(), saved_dt, saved_origin, saved_reference,
            preamble.y_increment, preamble.y_origin, preamble.y_reference, *values,
            original_points, len(raw), baseline_samples, factor, discarded_tail,
        )
        # Commit only after both waveform and features have been written.
        f.flush()
        f.attrs.modify("committed_events", n + 1)
        f.flush()
        self.last_info = {"stored_points": len(raw), "actual_bin_width_us": saved_dt * 1e6}
        return values

    def close(self):
        self.file.attrs["closed_utc"] = utc_now()
        self.file.close()


def acquire(args):
    args.started_utc = utc_now()
    np, _ = dependencies()
    import pyvisa

    if args.output is None:
        path, run_number = allocate_run(args.data_dir)
        args.output = str(path)
        args.run_number = run_number
    if Path(args.output).exists():
        raise FileExistsError("Output already exists: " + args.output)
    with ExitStack() as stack:
        manager = pyvisa.ResourceManager(args.visa_backend)
        stack.callback(manager.close)
        instrument = open_instrument(manager, args.ip, args.resource)
        stack.callback(instrument.close)
        instrument.timeout = int(args.timeout * 1000)
        instrument.read_termination = "\n"
        instrument.write_termination = "\n"
        instrument.chunk_size = 1024 * 1024
        scope = Scope(instrument)
        # Best-effort stop on completion, Ctrl+C, or communication failure.
        def stop():
            try:
                instrument.write(":STOP")
            except Exception:
                pass
        stack.callback(stop)
        identity = scope.configure(args.channel, args.trigger_level, args.slope,
                                   args.points, args.time_range,
                                   args.gate_before_us, args.gate_after_us)
        config = {k: v for k, v in vars(args).items() if k != "func"}
        store = Store(args.output, config, identity)
        stack.callback(store.close)
        print(identity, flush=True)
        print(f"CH{args.channel}: {args.slope}, {args.trigger_level:g} V; "
              f"waiting for {args.count} triggered waveforms", flush=True)
        try:
            for index in range(args.count):
                raw, preamble = scope.capture(container=np.array)
                raw, preamble = gate_waveform(raw, preamble, config)
                baseline = args.baseline_samples or max(1, preamble.points // 10)
                store.append(raw, preamble, baseline)
                if index == 0:
                    stored_points = store.last_info["stored_points"]
                    print(f"{preamble.points} acquired, {stored_points} stored points/event; "
                          f"uncompressed ADC estimate: "
                          f"{2 * stored_points * args.count / 1e9:.3f} GB; "
                          f"baseline: first {baseline} samples", flush=True)
                if (index + 1) % 100 == 0 or index + 1 == args.count:
                    print(f"Saved {index + 1}/{args.count}", flush=True)
        except KeyboardInterrupt:
            store.file.attrs["status"] = "interrupted"
            print(f"Stopped. Saved {store.file.attrs['committed_events']} events.")
            return
        except BaseException as exc:
            store.file.attrs["status"] = "failed"
            store.file.attrs["error"] = str(exc)
            raise
        else:
            store.file.attrs["status"] = "complete"
    print("Saved: " + args.output)


def identify(args):
    """Read identification only, without changing acquisition settings."""
    import pyvisa
    with ExitStack() as stack:
        manager = pyvisa.ResourceManager(args.visa_backend)
        stack.callback(manager.close)
        instrument = open_instrument(manager, args.ip, args.resource)
        stack.callback(instrument.close)
        instrument.timeout = int(args.timeout * 1000)
        instrument.read_termination = "\n"
        instrument.write_termination = "\n"
        print(instrument.query("*IDN?").strip())


def histogram(args):
    np, h5py = dependencies()
    import matplotlib
    if not args.show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with h5py.File(args.input, "r") as f:
        n = int(f.attrs["committed_events"])
        if n == 0:
            raise ValueError("No completed events in file")
        # Read only the selected feature, never the waveform matrix.
        values = f["events"].fields(args.metric)[:n]
    fig, ax = plt.subplots()
    ax.hist(values[np.isfinite(values)], bins=args.bins)
    ax.set_xlabel(args.metric + " (V)")
    ax.set_ylabel("Events")
    ax.set_title(f"{n} waveforms")
    fig.tight_layout()
    output = Path(args.output)
    if output.resolve() == Path(args.input).resolve():
        raise ValueError("Histogram output must differ from input")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150)
    print("Histogram: " + str(output))
    if args.show:
        plt.show()
    plt.close(fig)


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be finite")
    return value


def positive_float(text):
    value = finite_float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    a = commands.add_parser("acquire", help="Capture triggered waveforms into HDF5")
    a.add_argument("--ip", default="192.168.10.103")
    a.add_argument("--resource", help="Override full VISA resource (e.g. TCPIP0::IP::5025::SOCKET)")
    a.add_argument("--visa-backend", default="@py", help="@py or a native VISA library path")
    a.add_argument("--channel", type=int, choices=range(1, 5), default=3, help="Default: CH3")
    a.add_argument("--trigger-level", type=finite_float, default=0.1, help="Volts (default: 100 mV)")
    a.add_argument("--slope", choices=("rising", "falling", "either"), default="rising")
    a.add_argument("--count", type=positive_int, default=1000)
    a.add_argument("--timeout", type=positive_float, default=30, help="Seconds per VISA operation; trigger wait is unlimited")
    a.add_argument("--points", type=positive_int, help="Requested acquisition points; scope may adjust")
    a.add_argument("--time-range", type=positive_float, help="Full waveform duration in seconds")
    a.add_argument("--baseline-samples", type=positive_int, help="Default: first 10%% of samples")
    a.add_argument("--binning", type=positive_int, default=1, help="Average N samples per stored sample; analysis uses original data")
    a.add_argument("--bin-width-us", type=finite_float, default=0, help="Stored sample width in microseconds; 0 keeps native samples")
    a.add_argument("--gate-before-us", type=finite_float)
    a.add_argument("--gate-after-us", type=finite_float)
    a.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    a.add_argument("--output", help="Explicit output path; otherwise allocate runNNNNN.dat in data-dir")
    a.set_defaults(func=acquire)
    h = commands.add_parser("histogram", help="Plot saved per-waveform features")
    h.add_argument("input")
    h.add_argument("--metric", choices=("positive_height_v", "negative_height_v", "max_v", "min_v"),
                   default="positive_height_v")
    h.add_argument("--bins", type=positive_int, default=100)
    h.add_argument("--output", default="data/histogram.png")
    h.add_argument("--show", action="store_true")
    h.set_defaults(func=histogram)
    i = commands.add_parser("identify", help="Read instrument identity without acquiring")
    i.add_argument("--ip", default="192.168.10.103")
    i.add_argument("--resource")
    i.add_argument("--visa-backend", default="@py")
    i.add_argument("--timeout", type=positive_float, default=5)
    i.set_defaults(func=identify)
    return p


def main():
    args = parser().parse_args()
    try:
        args.func(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
