import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_daq import HAS_STORAGE
from unpacker import REQUIRED_FIELDS, reconstruct_batch, unpack, validate_run

HAS_UPROOT = HAS_STORAGE and importlib.util.find_spec("uproot") is not None


@unittest.skipUnless(HAS_STORAGE, "numpy and h5py are not installed")
class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import h5py
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "run00001.dat"
        dtype = [("received_unix_ns", "<i8")] + [(field, "<f8") for field in REQUIRED_FIELDS[1:]]
        event = np.zeros(2, dtype=dtype)
        event[0] = (1700000000123456789, 2e-9, -4e-9, 1, 0.01, 0.1, 2)
        event[1] = (1700000001987654321, 4e-9, 1e-9, 0.5, 0.02, -0.1, 1)
        with h5py.File(self.path, "w") as f:
            f.attrs.update(status="complete", committed_events=2, schema_version=1,
                           run_started_utc="2026-10-15T00:00:00+00:00")
            f.create_dataset("events", data=event)
            f.create_dataset("adc", data=np.array([[0, 2, -3, 4], [1, 2, 3, 4]], dtype=np.int16))

    def test_microseconds_millivolts_and_event_unix_seconds(self):
        import numpy as np
        import h5py
        with h5py.File(self.path) as f:
            self.assertEqual(validate_run(f), (2, 4))
            batch = reconstruct_batch(f, 0, 2, 4)
        np.testing.assert_allclose(batch["waveform_time"],
                                   [[-0.006, -0.004, -0.002, 0], [-0.001, 0.003, 0.007, 0.011]], atol=1e-15)
        np.testing.assert_allclose(batch["waveform_voltage"], [[80, 100, 50, 120], [-100, -80, -60, -40]])
        self.assertEqual(set(batch), {"unixtime", "waveform_time", "waveform_voltage"})
        np.testing.assert_allclose(batch["unixtime"], [1700000000.1234567, 1700000001.9876543], rtol=0, atol=1e-6)

    def test_only_committed_events_are_selected(self):
        import h5py
        with h5py.File(self.path, "r+") as f:
            f.attrs["committed_events"] = 1
        with h5py.File(self.path) as f:
            count, points = validate_run(f)
            batch = reconstruct_batch(f, 0, count, points)
        self.assertEqual(batch["waveform_time"].shape, (1, 4))

    def test_active_and_inconsistent_files_are_rejected(self):
        import h5py
        with h5py.File(self.path, "r+") as f:
            f.attrs["status"] = "running"
            with self.assertRaisesRegex(ValueError, "Stop acquisition"):
                validate_run(f)
            f.attrs["status"] = "failed"
            f.attrs["committed_events"] = 3
            with self.assertRaisesRegex(ValueError, "shorter"):
                validate_run(f)

    def test_failed_conversion_leaves_no_partial_output(self):
        target = self.path.with_suffix(".root")
        target.write_bytes(b"previous ROOT output")
        fake_uproot = SimpleNamespace(ZLIB=lambda level: None,
                                     recreate=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with patch.dict("sys.modules", {"uproot": fake_uproot}):
            with self.assertRaisesRegex(OSError, "disk full"):
                unpack(self.path)
        self.assertEqual(target.read_bytes(), b"previous ROOT output")
        self.assertEqual(list(Path(self.tmp.name).glob("*.partial")), [])
        self.assertTrue(self.path.exists())

    @unittest.skipUnless(HAS_UPROOT, "uproot is not installed")
    def test_existing_root_is_replaced_after_success(self):
        import uproot
        target = self.path.with_suffix(".root")
        target.write_bytes(b"existing ROOT data")
        self.assertEqual(unpack(self.path), target)
        with uproot.open(target, handler=uproot.source.file.MemmapSource) as f:
            self.assertEqual(f['events'].num_entries, 2)

    @unittest.skipUnless(HAS_UPROOT, "uproot is not installed")
    def test_actual_root_ttree_roundtrip(self):
        import numpy as np
        import uproot
        import h5py
        import json
        with h5py.File(self.path, 'r+') as source:
            source.attrs['config_json'] = json.dumps({
                'channel': 3, 'trigger_level': 0.1, 'slope': 'rising',
                'gate_before_us': 2, 'gate_after_us': 6, 'bin_width_us': 0.01,
                'run_number': 1, 'count': 1000, 'ip': '192.168.10.103'})
        output = unpack(self.path)
        self.assertEqual(output.name, "run00001.root")
        with h5py.File(self.path) as f:
            expected = reconstruct_batch(f, 0, 2, 4)
        with uproot.open(output, handler=uproot.source.file.MemmapSource) as f:
            tree = f["events"]
            self.assertEqual(tree.classname, "TTree")
            self.assertEqual(tree.num_entries, 2)
            for field in expected:
                np.testing.assert_array_equal(tree[field].array(library="np"), expected[field])
            import json
            metadata = json.loads(str(f["metadata"]))
            self.assertEqual(metadata["units"]["waveform_voltage"], "mV")
            self.assertEqual(metadata["units"]["waveform_time"], "us relative to trigger")
            expected_titles = {'channel': 'CH3', 'trigger_level': '100 mV',
                               'before_trigger': '2 us', 'after_trigger': '6 us',
                               'trigger_slope': 'rising', 'waveform_bin_requested': '0.01 us',
                               'config_ip': '192.168.10.103', 'saved_events': '2',
                               'run_started_utc': '2026-10-15T00:00:00+00:00'}
            for name, title in expected_titles.items():
                self.assertEqual(f[name].classname, 'TNamed')
                self.assertEqual(f[name].member('fTitle'), title)
                # .ls reads key titles without loading each object.
                self.assertEqual(f.key(name).fTitle, title)


if __name__ == "__main__":
    unittest.main()
