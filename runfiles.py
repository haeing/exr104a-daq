"""Persistent run numbering shared by CLI and web acquisition on Linux."""

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

DEFAULT_DATA_DIR = "/mnt/exr104a-data"
RUN_LOG_NAME = "run-log.txt"
KST = ZoneInfo("Asia/Seoul")


def write_run_log(directory, number, event, timestamp, comment):
    """Write one tab-separated line per event, with a JSON-quoted comment."""
    path = Path(directory) / RUN_LOG_NAME
    run_label = "-" if number is None else f"{number:05d}"
    prefix = f"{run_label}\t{event}\t"
    timestamp = datetime.fromisoformat(timestamp).astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{prefix}{timestamp}\t{json.dumps(comment, ensure_ascii=False)}\n"
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)


def next_run_number(directory):
    directory = Path(directory)
    maximum = 0
    if directory.exists():
        for path in directory.iterdir():
            match = re.fullmatch(r"run(\d+)\.dat", path.name)
            if match:
                maximum = max(maximum, int(match[1]))
        counter = directory / ".run-counter"
        if counter.exists():
            value = counter.read_text().strip()
            if value:
                maximum = max(maximum, int(value))
    return maximum + 1


def allocate_run(directory):
    """Reserve a number before connecting. Failed runs may leave gaps."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        number = next_run_number(directory)
        # Replace the counter atomically while holding the cross-process lock.
        temp = directory / ".run-counter.tmp"
        with temp.open("w") as stream:
            stream.write(str(number) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, directory / ".run-counter")
        return directory / f"run{number:05d}.dat", number
