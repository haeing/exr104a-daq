"""Convert EXR HDF5 .dat/.h5 runs to ROOT TTrees with time in us and voltage in mV."""

import argparse
import json
import os
import re
from pathlib import Path
import sys
import tempfile

from exr_daq import dependencies

REQUIRED_FIELDS = ("received_unix_ns", "x_increment", "x_origin", "x_reference",
                   "y_increment", "y_origin", "y_reference")


def validate_run(source):
    """Return committed event count and fixed stored waveform length."""
    if source.attrs.get("status") == "running":
        raise ValueError("Run is marked as running. Stop acquisition before unpacking.")
    count = source.attrs.get("committed_events")
    if count is None or int(count) != count or count < 0:
        raise ValueError("Missing or invalid committed_events attribute")
    count = int(count)
    if count == 0:
        raise ValueError("Run contains no committed events")
    if "adc" not in source or "events" not in source:
        raise ValueError("Missing adc or events dataset")
    adc, events = source["adc"], source["events"]
    if adc.ndim != 2 or events.ndim != 1 or adc.shape[1] < 1:
        raise ValueError("Expected a fixed-length 2D ADC dataset and a 1D event dataset")
    if adc.shape[0] < count or events.shape[0] < count:
        raise ValueError("Dataset is shorter than committed_events")
    missing = set(REQUIRED_FIELDS) - set(events.dtype.names or ())
    if missing:
        raise ValueError("Missing event fields: " + ", ".join(sorted(missing)))
    return count, adc.shape[1]


def reconstruct_batch(source, start, stop, points):
    """Use per-event scale/reference values, including stored bin centers."""
    np, _ = dependencies()
    event = source["events"][start:stop]
    if "stored_points" in event.dtype.names and np.any(event["stored_points"] != points):
        raise ValueError("Run contains variable stored lengths; a fixed waveform length is required")
    for field in REQUIRED_FIELDS[1:]:
        if not np.all(np.isfinite(event[field])):
            raise ValueError(f"Non-finite {field} in events {start}:{stop}")
    if np.any(event["x_increment"] <= 0) or np.any(event["y_increment"] <= 0):
        raise ValueError(f"Invalid sample scaling in events {start}:{stop}")

    indices = np.arange(points, dtype=np.float64)
    times = indices[None, :] - event["x_reference"][:, None]
    times *= event["x_increment"][:, None]
    times += event["x_origin"][:, None]
    times *= 1e6  # trigger-relative microseconds

    voltage = source["adc"][start:stop].astype(np.float64)
    voltage -= event["y_reference"][:, None]
    voltage *= event["y_increment"][:, None]
    voltage += event["y_origin"][:, None]
    voltage *= 1e3  # millivolts
    timestamp_ns = event["received_unix_ns"].astype(np.int64)
    return {"unixtime": timestamp_ns.astype(np.float64) / 1e9,
            "waveform_time": times, "waveform_voltage": voltage}


def json_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def settings_titles(source, count, points):
    """Human-readable top-level key titles, visible in ROOT's .ls output."""
    config = json.loads(source.attrs.get("config_json", "{}"))
    if not isinstance(config, dict):
        raise ValueError("Saved config_json must be an object")

    def display(value, unit=""):
        if value is None:
            return "not recorded"
        if isinstance(value, (float, int)):
            return f"{value:g}" + (f" {unit}" if unit else "")
        return str(value)

    trigger = config.get("trigger_level")
    requested_bin = config.get("bin_width_us")
    actual_bin = source.attrs.get("actual_bin_width_us", source["events"][0]["x_increment"] * 1e6)
    titles = {
        "channel": f"CH{config['channel']}" if config.get("channel") is not None else "not recorded",
        "trigger_level": display(trigger * 1000 if trigger is not None else None, "mV"),
        "trigger_slope": display(config.get("slope")),
        "before_trigger": display(config.get("gate_before_us"), "us"),
        "after_trigger": display(config.get("gate_after_us"), "us"),
        "waveform_bin_requested": ("native sample spacing (0 us)" if requested_bin == 0
                                   else display(requested_bin, "us")),
        "waveform_bin_actual": display(float(actual_bin), "us"),
        "run_number": display(config.get("run_number")),
        "run_started_utc": display(source.attrs.get("run_started_utc", config.get("started_utc"))),
        "baseline_samples": display(source.attrs.get("baseline_samples", config.get("baseline_samples"))),
        "target_events": display(config.get("count")),
        "saved_events": str(count),
        "stored_points": str(points),
        "instrument": display(source.attrs.get("instrument")),
        "run_status": display(source.attrs.get("status")),
    }
    mapped = {"channel", "trigger_level", "slope", "gate_before_us", "gate_after_us", "bin_width_us",
              "run_number", "started_utc", "baseline_samples", "count"}
    # Retain other saved options as individually visible keys as well.
    for key, value in config.items():
        if key not in mapped:
            name = "config_" + re.sub(r"[^A-Za-z0-9_]", "_", key)
            titles[name] = display(value)
    return titles


def named_setting(name, title):
    import uproot
    obj = uproot.models.TNamed.Model_TNamed.empty()
    obj._bases.append(uproot.models.TObject.Model_TObject.empty())
    obj._members.update(fName=name, fTitle=title)
    obj._deeply_writable = True
    return obj


def unpack(input_path, output_dir=None, batch_mib=64):
    np, h5py = dependencies()
    try:
        import uproot
    except ModuleNotFoundError as exc:
        raise RuntimeError("Install Uproot: python -m pip install 'uproot>=5,<6'") from exc
    if batch_mib < 1:
        raise ValueError("Batch memory must be at least 1 MiB")
    input_path = Path(input_path).resolve()
    if input_path.suffix.lower() not in (".dat", ".h5", ".hdf5"):
        raise ValueError("Expected an EXR .dat, .h5, or .hdf5 input file")
    destination = Path(output_dir).resolve() if output_dir else input_path.parent
    output_path = destination / (input_path.stem + ".root")

    with h5py.File(input_path, "r") as source:
        count, points = validate_run(source)
        # Allow headroom for reconstructed arrays and compression buffers.
        # At least one event must fit, even if it exceeds this soft target.
        batch_events = max(1, int(batch_mib * 1024**2) // (48 * points + 256))
        branch_types = {
            "unixtime": np.dtype("float64"),
            "waveform_time": np.dtype((np.float64, (points,))),
            "waveform_voltage": np.dtype((np.float64, (points,))),
        }
        metadata = {
            "source_file": str(input_path), "committed_events": count,
            "tree": "events",
            "units": {"unixtime": "Unix seconds",
                      "waveform_time": "us relative to trigger", "waveform_voltage": "mV"},
            "timestamp_note": "Host receive time, not a hardware trigger timestamp",
            "waveform_note": "Restored from the stored ADC waveform; original pre-binning samples cannot be recovered",
            "source_attributes": dict(source.attrs),
        }
        metadata_json = json.dumps(metadata, default=json_value, ensure_ascii=True)
        destination.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix="." + output_path.stem + "-",
                                               suffix=".root.partial", dir=destination)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with uproot.recreate(temporary, compression=uproot.ZLIB(4)) as target:
                # Explicitly create a classic TTree rather than an RNTuple.
                tree = target.mktree("events", branch_types,
                                     title="EXR waveform events; waveform time in us, voltage in mV")
                for start in range(0, count, batch_events):
                    stop = min(start + batch_events, count)
                    batch = reconstruct_batch(source, start, stop, points)
                    tree.extend(batch)
                    del batch
                    print(f"Unpacked {stop}/{count} events", flush=True)
                target["metadata"] = metadata_json
                for name, title in settings_titles(source, count, points).items():
                    target[name] = named_setting(name, title)
            # Replace an older ROOT output only after conversion succeeds.
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
    return output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Completed EXR .dat or .h5 run files")
    parser.add_argument("--output-dir", help="Default: same directory as each input")
    parser.add_argument("--batch-mib", type=int, default=64, help="Soft working-memory target; minimum one event")
    args = parser.parse_args()
    try:
        for input_path in args.inputs:
            print("Saved: " + str(unpack(input_path, args.output_dir, args.batch_mib)))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
