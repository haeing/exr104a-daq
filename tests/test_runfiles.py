from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from runfiles import allocate_run, next_run_number


class RunNumberTests(unittest.TestCase):
    def test_existing_files_restart_and_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(next_run_number(directory), 1)
            path, number = allocate_run(directory)
            self.assertEqual(path.name, "run00001.dat")
            self.assertEqual(number, 1)
            # Even a reserved number with no acquired file is not reused.
            self.assertEqual(allocate_run(directory)[1], 2)
            (Path(directory) / "run00042.dat").write_bytes(b"existing")
            path, number = allocate_run(directory)
            self.assertEqual(number, 43)
            self.assertEqual(path.name, "run00043.dat")

    def test_concurrent_allocations_are_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            with ThreadPoolExecutor(max_workers=4) as pool:
                numbers = list(pool.map(lambda _: allocate_run(directory)[1], range(20)))
            self.assertEqual(sorted(numbers), list(range(1, 21)))
            self.assertEqual(next_run_number(directory), 21)
