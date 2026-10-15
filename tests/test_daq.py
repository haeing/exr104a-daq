import importlib.util
import tempfile
from pathlib import Path
import unittest

from scope import Preamble, Scope, scpi_integer
from exr_daq import Store, features, parser, gate_waveform, storage_factor


PREAMBLE = '2,1,4,1,1e-9,-2e-9,0,0.01,0.2,0,1,"metadata"'


class FakeInstrument:
    def __init__(self):
        self.commands = []
        self.timeout = 30000
        self.fail_wait = False
        self.armed = False
        self.values = [0, 0, 10, -5]

    def write(self, command):
        self.commands.append(command)
        if command == ':SINGle':
            self.armed = True

    def query(self, command):
        self.commands.append(command)
        if command == '*IDN?':
            return 'KEYSIGHT TECHNOLOGIES,EXR104A,MY123,1.0'
        if command == ':SYSTem:ERRor? STRing':
            return '0,"No error"'
        if command in (':STOP;*OPC?', '*OPC?'):
            if command.startswith(':STOP'):
                self.armed = False
            return '1'
        if command == ':ADER?':
            if self.fail_wait and self.armed:
                raise TimeoutError('No trigger')
            return '1' if self.armed else '0'
        if command == ':WAVeform:PREamble?':
            return PREAMBLE
        raise AssertionError(command)

    def query_binary_values(self, command, **kwargs):
        self.commands.append(command)
        assert kwargs['datatype'] == 'h'
        assert kwargs['is_big_endian'] is False
        return kwargs['container'](self.values)


class DriverTests(unittest.TestCase):
    def test_gate_sets_scope_range_reference_and_position(self):
        i = FakeInstrument()
        Scope(i).configure(3, 0.1, 'rising', gate_before_us=2, gate_after_us=6)
        self.assertIn(':TIMebase:REFerence CENTer', i.commands)
        settings = {c.split(' ')[0]: c.split(' ')[1] for c in i.commands if c.startswith(':TIMebase:')}
        self.assertAlmostEqual(float(settings[':TIMebase:RANGe']), 8e-6)
        self.assertAlmostEqual(float(settings[':TIMebase:POSition']), 2e-6)
    def test_scpi_integer_scientific_notation(self):
        for text, expected in [('0.0E+00', 0), ('+1.0E+00', 1),
                               ('-2.22E+02', -222), (' 4.0E+00 ', 4)]:
            self.assertEqual(scpi_integer(text), expected)
        for text in ('0.5', 'nan', 'inf', 'garbage'):
            with self.assertRaises(ValueError):
                scpi_integer(text)

    def test_scientific_notation_instrument_responses(self):
        instrument = FakeInstrument()
        original_query = instrument.query

        def query(command):
            reply = original_query(command)
            if command == ':SYSTem:ERRor? STRing':
                return '0.0E+00,"No error"'
            if command in (':STOP;*OPC?', '*OPC?', ':ADER?'):
                return reply + '.0E+00'
            if command == ':WAVeform:PREamble?':
                return reply.replace('2,1,4,1,', '2.0E+00,1,4.0E+00,1,', 1)
            return reply

        instrument.query = query
        scope = Scope(instrument)
        scope.configure(3, 1.0, 'rising')
        raw, preamble = scope.capture()
        self.assertEqual(len(raw), preamble.points)

    def test_error_includes_failing_command_and_firmware_message(self):
        instrument = FakeInstrument()
        original_query = instrument.query
        error_sent = False

        def query(command):
            nonlocal error_sent
            if (command == ':SYSTem:ERRor? STRing'
                    and instrument.commands[-1] == ':WAVeform:FORMat WORD'
                    and not error_sent):
                error_sent = True
                return '41,"Waveform data is not valid."'
            return original_query(command)

        instrument.query = query
        with self.assertRaisesRegex(RuntimeError,
                                    r'\[:WAVeform:FORMat WORD\]: 41,"Waveform data is not valid\."'):
            Scope(instrument).configure(3, 1.0, 'rising')
        self.assertNotIn(':WAVeform:STReaming OFF', instrument.commands)

    def test_existing_error_identified_before_configuration(self):
        instrument = FakeInstrument()
        original_query = instrument.query
        replies = iter(['41,"existing error"', '0,"No error"'])

        def query(command):
            if command == ':SYSTem:ERRor? STRing':
                return next(replies)
            return original_query(command)

        instrument.query = query
        with self.assertRaisesRegex(RuntimeError, 'before configuration.*existing error'):
            Scope(instrument).configure(3, 1.0, 'rising')
        self.assertNotIn(':TRIGger:MODE EDGE', instrument.commands)

    def setup_scope(self):
        instrument = FakeInstrument()
        scope = Scope(instrument)
        scope.configure(2, -0.05, 'falling')
        return scope, instrument

    def test_triggered_acquisition_and_scaling(self):
        scope, i = self.setup_scope()
        self.assertIn(':TRIGger:SWEep TRIGgered', i.commands)
        self.assertIn(':TRIGger:EDGE:SOURce CHANnel2', i.commands)
        self.assertIn(':TRIGger:EDGE:SLOPe NEGative', i.commands)
        self.assertIn(':TRIGger:LEVel CHANnel2,-0.050000000000000003', i.commands)
        raw, p = scope.capture()
        self.assertEqual(raw, i.values)
        self.assertEqual(p.x_origin, -2e-9)
        self.assertEqual(p.y_origin, 0.2)
        self.assertLess(i.commands.index(':SINGle'),
                        i.commands.index(':WAVeform:DATA?'))
        arm = i.commands.index(':SINGle')
        self.assertIn(':ADER?', i.commands[:arm])
        self.assertIn(':ADER?', i.commands[arm + 1:])
        self.assertIn(':WAVeform:SOURce CHANnel2', i.commands[arm + 1:])

    def test_timeout_does_not_download_stale_waveform(self):
        scope, i = self.setup_scope()
        i.fail_wait = True
        with self.assertRaises(TimeoutError):
            scope.capture()
        self.assertNotIn(':WAVeform:DATA?', i.commands)

    def test_default_trigger_wait_is_unlimited_and_cancellable(self):
        import threading
        from unittest.mock import patch
        from scope import CaptureCancelled
        scope, i = self.setup_scope()
        original_query = i.query
        event = threading.Event()
        polls = []

        def query(command):
            if command == ':ADER?' and i.armed:
                polls.append(command)
                if len(polls) == 5:
                    event.set()
                return '0'
            return original_query(command)

        i.query = query
        with patch('scope.time.monotonic', side_effect=AssertionError('No trigger deadline expected')):
            with patch('scope.time.sleep'):
                with self.assertRaises(CaptureCancelled):
                    scope.capture(cancel_event=event)
        self.assertEqual(len(polls), 5)
        self.assertNotIn(':WAVeform:DATA?', i.commands)
        self.assertEqual(i.commands[-1], ':STOP')

    def test_incomplete_transfer_rejected(self):
        scope, i = self.setup_scope()
        i.values = [1, 2]
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            scope.capture()

    def test_invalid_preamble(self):
        for text in ('1,1,4,1,1,0,0,1,0,0', PREAMBLE.replace('1e-9', 'nan'),
                     PREAMBLE.replace('0.01', '0')):
            with self.assertRaises(ValueError):
                Preamble.parse(text)

    def test_cli_channel_required(self):
        args = parser().parse_args(['acquire', '--channel', '3', '--trigger-level', '0.1'])
        self.assertEqual(args.channel, 3)
        self.assertEqual(args.ip, '192.168.10.103')


HAS_STORAGE = all(importlib.util.find_spec(x) is not None for x in ('numpy', 'h5py'))


@unittest.skipUnless(HAS_STORAGE, 'numpy and h5py are not installed')
class StorageTests(unittest.TestCase):
    def test_native_grid_stays_fixed_when_boundary_sample_count_changes(self):
        import numpy as np
        import h5py
        from dataclasses import replace
        p = Preamble.parse('2,1,10,1,1e-9,-2e-9,0,0.01,0,0')
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'native.dat', {'gate_before_us': 0.002,
                          'gate_after_us': 0.008, 'bin_width_us': 0}, 'fake')
            try:
                store.append(np.arange(10, dtype=np.int16), p, 2)
                store.append(np.arange(9, dtype=np.int16),
                             replace(p, points=9, x_origin=-1.8e-9), 2)
                self.assertEqual(store.file['adc'].shape, (2, 10))
                for field in ('x_increment', 'x_origin', 'x_reference', 'stored_points'):
                    self.assertEqual(store.file['events'][0][field], store.file['events'][1][field])
                self.assertEqual(store.file['events'][1]['original_points'], 9)
                self.assertEqual(store.file['adc'][1, -1], 8)
            finally:
                store.close()

    def test_fixed_gate_bins_with_varying_native_lengths_and_intervals(self):
        import numpy as np
        import h5py
        from dataclasses import replace
        p = Preamble.parse('2,1,10,1,1e-9,-2e-9,0,0.01,0,0')
        records = [(np.arange(n, dtype=np.int16), replace(p, points=n, x_increment=10e-9/n))
                   for n in (10, 11, 16, 8)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixed.dat'
            store = Store(path, {'bin_width_us': 0.002, 'gate_before_us': 0.002,
                                 'gate_after_us': 0.008}, 'fake')
            try:
                for raw, preamble in records:
                    store.append(raw, preamble, 2)
            finally:
                store.close()
            with h5py.File(path) as f:
                self.assertEqual(f.attrs['committed_events'], 4)
                self.assertEqual(f['adc'].shape, (4, 5))
                for index, (raw, preamble) in enumerate(records):
                    event = f['events'][index]
                    times = preamble.x_origin + np.arange(len(raw))*preamble.x_increment
                    expected = []
                    for k in range(5):
                        lo, hi = -2e-9+k*2e-9, -2e-9+(k+1)*2e-9
                        selected = raw[(times >= lo-1e-17) & (times < hi-1e-17)]
                        expected.append(np.rint(selected.mean()))
                    np.testing.assert_array_equal(f['adc'][index], expected)
                    self.assertEqual(event['stored_points'], 5)
                    self.assertEqual(event['original_points'], len(raw))
                    self.assertAlmostEqual(event['x_increment'], 2e-9)
                    self.assertAlmostEqual(event['x_origin'], -1e-9)
                    self.assertEqual(event['x_reference'], 0)
                    self.assertAlmostEqual(event['positive_height_v'], (len(raw)-1-0.5)*0.01)

    def test_non_gate_acquisition_rejects_incompatible_lengths(self):
        import numpy as np
        p = Preamble.parse(PREAMBLE)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'fixed.dat', {}, 'fake')
            try:
                store.append(np.array([1, 2, 3, 4]), p, 1)
                with self.assertRaisesRegex(ValueError, 'Stored waveform length changed'):
                    store.append(np.arange(7), p, 1)
                self.assertEqual(store.file.attrs['committed_events'], 1)
            finally:
                store.close()

    def test_gate_excludes_outside_pulses_and_preserves_trigger_time(self):
        import numpy as np
        p = Preamble.parse('2,1,8,1,1e-6,-4e-6,0,0.01,0,0')
        raw = np.array([100, 0, 0, 1, 9, 3, 200, 0], dtype=np.int16)
        selected, gate = gate_waveform(raw, p, {'gate_before_us': 2, 'gate_after_us': 2})
        np.testing.assert_array_equal(selected, [0, 1, 9, 3])
        self.assertAlmostEqual(gate.x_origin, -2e-6)
        self.assertEqual(gate.points, 4)
        self.assertAlmostEqual(features(selected, gate, 1)[3], 0.09)
        self.assertEqual(storage_factor(gate, {'bin_width_us': 2.1}), 2)
        self.assertEqual(storage_factor(gate, {'bin_width_us': 0.1}), 1)
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            gate_waveform(raw, p, {'gate_before_us': 9, 'gate_after_us': 2})

    def test_binning_preserves_original_features_and_bin_center_times(self):
        import numpy as np
        import h5py
        p = Preamble.parse('2,1,5,1,1e-9,-2e-9,2,0.01,0.2,0')
        raw = np.array([0, 2, 10, 4, 99], dtype=np.int16)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'binned.dat'
            store = Store(path, {'binning': 2}, 'fake')
            try:
                result = store.append(raw, p, 2)
            finally:
                store.close()
            self.assertAlmostEqual(result[3], 0.98)
            with h5py.File(path, 'r') as f:
                np.testing.assert_array_equal(f['adc'][0], [1, 7])
                event = f['events'][0]
                times = (np.arange(2) - event['x_reference']) * event['x_increment'] + event['x_origin']
                np.testing.assert_allclose(times, [-3.5e-9, -1.5e-9], atol=1e-20)
                self.assertEqual(f.attrs['original_points'], 5)
                self.assertEqual(f.attrs['stored_points'], 2)
                self.assertEqual(f.attrs['discarded_tail_samples'], 1)
                self.assertAlmostEqual(event['positive_height_v'], 0.98)

    def test_roundtrip_raw_features_and_no_overwrite(self):
        import numpy as np
        import h5py
        p = Preamble.parse(PREAMBLE)
        raw = np.array([0, 0, 10, -5], dtype=np.int16)
        result = features(raw, p, 2)
        np.testing.assert_allclose(result, [0.2, 0.3, 0.15, 0.1, 0.05])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.h5'
            store = Store(path, {'channel': 1}, 'fake')
            try:
                store.append(raw, p, 2)
                store.append(raw, p, 2)
                with self.assertRaises(ValueError):
                    store.append(np.array([0, 1, 2]), p, 1)
            finally:
                store.close()
            with h5py.File(path, 'r') as f:
                self.assertEqual(f.attrs['committed_events'], 2)
                self.assertEqual(f['adc'].dtype, np.dtype('<i2'))
                np.testing.assert_array_equal(f['adc'][1], raw)
                self.assertAlmostEqual(f['events'][0]['positive_height_v'], 0.1)
                self.assertEqual(f['adc'].compression, 'lzf')
            with self.assertRaises(FileExistsError):
                Store(path, {}, 'fake')


if __name__ == '__main__':
    unittest.main()
