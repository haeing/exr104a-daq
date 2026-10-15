# EXR104A waveform DAQ

Python acquisition and a local web analyzer for the Keysight EXR104A at `192.168.10.103`.
Select a channel and edge trigger, collect waveforms, save them in HDF5, and monitor pulse-height histograms.

## Installation

Use Python 3.8 or newer. Python 3.10 is available on this PC; the system `python` command outside a virtual environment may refer to Python 2.

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The PC must be able to reach the scope over LAN, and the scope's remote control service must be enabled.
The default connection uses PyVISA-py and VXI-11:

```text
TCPIP0::192.168.10.103::inst0::INSTR
```

If the scope's SCPI socket service is enabled, use `--resource TCPIP0::192.168.10.103::5025::SOCKET` instead.
When no explicit resource is provided and VXI-11 returns `VI_ERROR_RSRC_NFOUND`,
the application also attempts the SCPI socket service on port 5025. Explicit resources are used as specified.
To read the instrument identity without changing acquisition settings:

```bash
python exr_daq.py identify
```

A successful ping checks ICMP connectivity; an identity query checks the actual VISA/SCPI connection.

## Web GUI

Start the server from a normal terminal:

```bash
source .venv/bin/activate
python web_gui.py
```

Open **http://127.0.0.1:8000** and click **Start acquisition**.
Starting the server or opening the page does not start acquisition.
The default server is accessible from this PC only.

| Setting | Default | Description |
| --- | --- | --- |
| Channel | CH3 | Acquisition channel and trigger source; CH1–CH4 supported. |
| Trigger level | 100 mV | Hardware edge-trigger threshold, entered in millivolts. |
| Trigger edge | Rising | Rising, falling, or either edge. |
| Waveforms | 1,000 | Number of events to save. |
| Waveform bin width | 0 μs | Stored time-bin width; 0 locks the first event's native time grid. |
| Before trigger | 1 μs | Acquisition, storage, and analysis time before the trigger. |
| After trigger | 3 μs | Acquisition, storage, and analysis time after the trigger. |
| Baseline samples | First 10% | Number of unbinned samples at the start of the gate used for the baseline. |

The trigger is at **0 μs**. For example, Before `2` and After `6` select **[−2, +6) μs**.
The scope timebase uses a center reference, a range of `before + after`, and a position of `(after − before) / 2`.
The received preamble is used to select the actual gate. Acquisition stops with an error if the waveform does not cover that gate.

**Trigger waiting is unlimited.** If no trigger arrives, acquisition keeps waiting until you click **Stop acquisition**.
There is no trigger-timeout setting. Individual VISA communication operations retain a timeout to detect communication failures.
Stopping during a transfer or file write may wait for that operation to finish. Already committed events are preserved.

Channel, trigger, gate, and storage settings are fixed during a run. To change them, stop the run, edit the settings, and start a new run.
Avoid running CLI acquisition or another acquisition server against the same scope simultaneously.
Stop the server with `Ctrl+C` in its terminal.

### Live displays

- Pulse-height histogram with selectable metric and bin count, refreshed every second.
- Latest waveform preview with peak-preserving display reduction.
- Saved event count, acquisition rate, elapsed time, and file size.
- **Run started** date and time, shown in the browser's local timezone, including the timezone label.
- Current output file path and next automatic run filename.

The preview and pulse-height analysis use the unbinned samples inside the selected gate.
Changing histogram bins does not change waveform storage bins.

### Waveform storage bins

With a positive bin width, samples are averaged into fixed time bins anchored to the gate start.
For example, `0.01 μs` means **10 ns per stored bin**. An 8 μs gate produces 800 bins at this width.
The bin width, bin count, gate, and stored time axis remain identical for every event in the run,
even if the scope returns slightly different native sample counts or sample alignment.
Averaged ADC values are rounded to signed 16-bit integers.

A bin width of `0` fixes the first event's native sample spacing and time grid for the run.
Later waveforms are aligned to that grid by linear interpolation. At a boundary, the nearest sample may be used
within one native sample interval; larger coverage gaps are rejected.

A positive bin width smaller than the native sample interval, or a time bin containing no acquired samples,
causes an error. If the gate duration is not a multiple of the requested width, only complete bins are stored;
analysis still includes the trailing gate samples.

Binning reduces waveform detail and cannot reconstruct the original waveform.
**Per-event heights and histogram values are calculated before storage binning**, so averaging does not lower those saved height values.
Binning reduces disk usage; it does not itself reduce the waveform transferred from the scope to the PC.

### Storage and run numbering

The default data directory is **`/mnt/exr104a-data`**. The user running the server must have write access to it.
Use `--data-dir` to select another directory.

Files are automatically named:

```text
/mnt/exr104a-data/run00001.dat
/mnt/exr104a-data/run00002.dat
/mnt/exr104a-data/run00003.dat
```

The `.dat` extension contains **HDF5 data**, not a custom raw binary format.
Existing files are never overwritten. Run numbering checks existing `run*.dat` files and a persistent `.run-counter`,
with a file lock to coordinate number allocation. Numbering continues after a server restart.
Numbers are reserved when a run starts, so failed connections or aborted runs can leave gaps.

### Demo mode

To test the GUI and file storage without connecting to hardware:

```bash
python web_gui.py --demo --data-dir data/demo
```

The page displays **DEMO · SIMULATED DATA**. Demo mode writes synthetic events and never connects to the scope.
The web charts use local HTML, CSS, and JavaScript; they require no CDN, ROOT, Node.js, or additional Python web package.
For server address, port, scope resource, and VISA backend options:

```bash
python web_gui.py --help
```

## CLI acquisition

Collect 10,000 waveforms from CH3 using a 100 mV rising-edge trigger,
a gate of −2 to +6 μs, and fixed 10 ns storage bins:

```bash
python exr_daq.py acquire --channel 3 --trigger-level 0.1 --slope rising \
  --count 10000 --bin-width-us 0.01 --gate-before-us 2 --gate-after-us 6
```

CLI trigger levels use **volts**; the web GUI uses **millivolts**.
The command above automatically allocates the next `runNNNNN.dat` in `/mnt/exr104a-data`.

Example for negative pulses, with an explicit output path:

```bash
python exr_daq.py acquire --channel 2 --trigger-level=-0.02 --slope falling \
  --count 10000 --baseline-samples 100 --output data/ch2_run001.h5
```

| Option | Description |
| --- | --- |
| `--channel` | Acquisition and trigger channel, 1–4; default 3. |
| `--trigger-level` | Trigger voltage in V; default 0.1. |
| `--slope` | `rising`, `falling`, or `either`; default `rising`. |
| `--count` | Target number of events; default 1,000. |
| `--timeout` | Timeout in seconds per VISA communication operation; default 30. Trigger waiting remains unlimited. |
| `--points` | Requested acquisition points; the scope may adjust the actual number. |
| `--time-range` | Full time range in seconds. A specified gate takes precedence. |
| `--gate-before-us`, `--gate-after-us` | Set both to define a trigger-relative gate. Without them, the CLI uses the current scope time window. |
| `--bin-width-us` | With a gate, sets fixed storage time bins. Default 0 uses the first native grid. Without a gate, rounds to an integer number of native samples. |
| `--binning` | Legacy samples-per-bin option; a positive `--bin-width-us` takes precedence. |
| `--baseline-samples` | Samples at the start of the selected waveform used for the baseline; default first 10%. |
| `--data-dir` | Directory for automatic run files; default `/mnt/exr104a-data`. |
| `--output` | Explicit output path instead of automatic run numbering. |

Without a gate, the CLI retains the existing sample-group averaging behavior and rejects changes in stored waveform length.
Use a gate and time-bin width for a fixed run time axis.

## Scope setup and acquisition behavior

Acquisition configures real-time mode, averaging off, DC edge triggering, and triggered sweep.
Set vertical scale, offset, probe attenuation, and input impedance on the scope before collecting data.
Place the baseline region in a flat part of the waveform before the pulse.
Check the scale to avoid clipping, and do not change scope settings while a run is active.

Each event follows this sequence:

1. Confirm that the scope has stopped.
2. Clear the previous acquisition-complete event (`ADER`).
3. Start a Single acquisition.
4. Wait for a new acquisition-complete event, with no trigger deadline.
5. Transfer the selected channel's waveform, apply the gate, analyze it, and save it.

Triggers occurring during transfer and storage can be missed.
Segmented acquisition would be needed to reduce dead time for faster event streams.
Communication failures abort acquisition rather than saving a stale waveform.
On normal exit, the scope remains stopped.

Completed events are retained after a stop or error. Data is flushed after every event,
but recovery after a power failure or forced process termination is not guaranteed.

## Offline histograms

After the run has stopped:

```bash
python exr_daq.py histogram /mnt/exr104a-data/run00001.dat --bins 100 \
  --metric positive_height_v --output data/height.png --show
```

Omit `--show` to save a PNG without opening a window.
Reading a file while acquisition is writing to it is not supported.

| Metric | Calculation |
| --- | --- |
| `positive_height_v` | Maximum voltage − baseline. |
| `negative_height_v` | Baseline − minimum voltage. |
| `max_v` | Maximum voltage. |
| `min_v` | Minimum voltage. |

Each waveform contributes one value to the histogram.
Extrema are calculated from unbinned gate samples; if several pulses are in the gate, the largest relevant extremum is used.

## HDF5 layout and reading data

- `/adc`: signed int16 array with shape `(events, stored_points)`, compressed with LZF.
  Gated runs use a fixed number of stored points and a fixed time grid throughout the run.
- `/events`: per-event receive timestamp, time and voltage conversion factors, baseline, extrema, positive and negative heights,
  original and stored point counts, baseline sample count, and binning metadata.
- File attributes: instrument identity, configuration, run start and file creation times, first preamble,
  storage grid information, first-event lengths and binning, status, and close time.
- `committed_events`: number of events for which waveform data and analysis values have both been committed.
  Read only this many events.

The file's `run_started_utc` attribute records when the run was started, in UTC.
The event field `received_unix_ns` records the host receive time; it is not a hardware trigger timestamp.
Per-event metadata is authoritative for original sample counts and conversion factors.

Uncompressed waveform size is approximately `2 × stored_points × events` bytes, plus metadata.
For example, 100,000 stored points × 10,000 events is about 2 GB before compression.
Compression depends on the signal. Events are written individually rather than accumulated as raw waveforms in memory.

Read a stored waveform and reconstruct its voltage and time axes:

```python
import h5py
import numpy as np

with h5py.File("/mnt/exr104a-data/run00001.dat", "r") as f:
    index = 0
    assert index < f.attrs["committed_events"]
    adc = f["adc"][index].astype(np.float64)
    event = f["events"][index]
    voltage = (adc - event["y_reference"]) * event["y_increment"] + event["y_origin"]
    time_s = (np.arange(adc.size) - event["x_reference"]) * event["x_increment"] + event["x_origin"]
    print("Run started (UTC):", f.attrs["run_started_utc"])
```

The same conversion formula applies to older files. Files created before the run-start attribute was added may not contain `run_started_utc`.

## Unpacking runs to ROOT

Convert a completed run with the unpacker:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python unpacker.py /mnt/exr104a-data/run00001.dat
```

The output is **`/mnt/exr104a-data/run00001.root`**: the basename is unchanged and only the extension changes.
The unpacker supports existing `.h5` and `.hdf5` runs as well. A ROOT installation is not required; writing uses Uproot.
Existing ROOT output files are replaced after conversion succeeds. The source data file is kept intact.

The ROOT file contains a classic **`events` TTree**, with one entry per event:

| Branch | Type | Units / meaning |
| --- | --- | --- |
| `unixtime` | `Double_t` | Host event receive time, seconds since the Unix epoch. |
| `waveform_time` | `Double_t[N]` | Waveform time axis in **μs relative to the trigger**. |
| `waveform_voltage` | `Double_t[N]` | Waveform voltage in **mV**. |

`N` is the stored waveform length for that run. Time and voltage are reconstructed using each event's
conversion factors and reference values, including the time positions of storage bins.
These are the waveforms stored in the `.dat` file; unpacking cannot restore samples removed by binning.
The timestamp is a host receive timestamp, not a hardware trigger timestamp.

A `metadata` JSON string in the ROOT file records units, the source file, and the original file attributes,
including the run start time when available.

Acquisition settings are also written as top-level **TNamed** objects. Their titles show the values
directly in ROOT's `.ls` output, without opening a settings tree:

```text
root [0] .ls
  KEY: TTree   events;1                 EXR waveform events; waveform time in us, voltage in mV
  KEY: TNamed  channel;1                CH3
  KEY: TNamed  trigger_level;1          100 mV
  KEY: TNamed  trigger_slope;1          rising
  KEY: TNamed  before_trigger;1         2 us
  KEY: TNamed  after_trigger;1          6 us
  KEY: TNamed  waveform_bin_requested;1 0.01 us
```

Other keys include the actual bin width, run number, start time in UTC, baseline sample count,
target and saved event counts, stored point count, instrument identity, run status, and additional saved options.
Settings absent from older source files are shown as `not recorded`; they are not inferred from current scope settings.
Reconvert an existing run to update its ROOT output with these objects. Use a separate output directory to preserve an older ROOT file.

To unpack several runs, use shell expansion:

```bash
python unpacker.py /mnt/exr104a-data/run*.dat
```

Use a different output directory or a batch-memory target if needed:

```bash
python unpacker.py /mnt/exr104a-data/run00001.dat --output-dir data/root --batch-mib 64
```

Conversion reads committed events in batches rather than loading the entire run into memory.
The memory target is approximate and must accommodate at least one complete event.
Stop acquisition before unpacking; active runs are rejected. A failed or stopped run can be unpacked
if it contains committed events. Files with inconsistent datasets or variable stored waveform lengths are rejected.
Output is replaced atomically only after conversion completes. If conversion fails, an existing ROOT output is preserved;
if there was no previous output, no final `.root` file is created.

Example ROOT inspection:

```cpp
TFile *f = TFile::Open("/mnt/exr104a-data/run00001.root");
TTree *events = f->Get<TTree>("events");
events->Print();
events->Scan("unixtime", "", "", 5);
```

## Tests and references

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

Tests cover SCPI parsing and command order, cancellation and unlimited trigger waiting,
transfer checks, fixed gate/time-grid storage, pulse-height analysis, HDF5 round trips,
run numbering, API validation, and demo acquisition.
NumPy and h5py are required for storage tests; otherwise those tests are skipped.
Hardware behavior and firmware compatibility must be checked on the connected scope.

- [Keysight MXR/EXR Programmer's Guide](https://www.keysight.com/bf/en/assets/9018-18183/programming-guides/9018-18183.pdf)
- [QCoDeS Infiniium driver](https://github.com/microsoft/Qcodes/blob/main/src/qcodes/instrument_drivers/Keysight/Infiniium.py)
