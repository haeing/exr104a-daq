"""EXR104A SCPI driver. Importing this module does not connect to hardware."""

import csv
import math
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class CaptureCancelled(Exception):
    """The caller requested cancellation before downloading a waveform."""


def scpi_integer(text):
    """Accept integral SCPI numbers, including scientific notation."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid SCPI integer: {text!r}") from exc
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError(f"Invalid SCPI integer: {text!r}")
    return int(value)


@dataclass(frozen=True)
class Preamble:
    points: int
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float
    text: str

    @classmethod
    def parse(cls, text):
        fields = next(csv.reader([text.strip()]))
        if len(fields) < 10 or scpi_integer(fields[0]) != 2:
            raise ValueError("Expected a WORD waveform preamble")
        result = cls(scpi_integer(fields[2]), *map(float, fields[4:10]), text.strip())
        if result.points < 2 or result.x_increment <= 0 or result.y_increment <= 0:
            raise ValueError("No valid waveform or invalid sample scaling")
        if not all(math.isfinite(v) for v in (
            result.x_increment, result.x_origin, result.x_reference,
            result.y_increment, result.y_origin, result.y_reference,
        )):
            raise ValueError("Non-finite waveform scaling")
        return result


class Scope:
    def __init__(self, instrument):
        self.instrument = instrument
        self.configured = False

    def check_errors(self, context=""):
        errors = []
        for _ in range(32):
            # EXR defaults to a numeric-only response. STRing includes the
            # firmware's actual description, which must not be guessed by code.
            reply = self.instrument.query(":SYSTem:ERRor? STRing").strip()
            if scpi_integer(reply.split(",", 1)[0]) == 0:
                break
            errors.append(reply)
        if errors:
            location = f" [{context}]" if context else ""
            raise RuntimeError("SCPI error" + location + ": " + "; ".join(errors))

    def configure(self, channel, level, slope, points=None, time_range=None,
                  gate_before_us=None, gate_after_us=None):
        if channel not in (1, 2, 3, 4) or not math.isfinite(level):
            raise ValueError("Invalid channel or trigger level")
        slopes = {"rising": "POSitive", "falling": "NEGative", "either": "EITHer"}
        edge = slopes[slope]
        i = self.instrument
        i.write(":SYSTem:HEADer OFF")
        identity = i.query("*IDN?").strip()
        fields = identity.split(",")
        if len(fields) < 2 or fields[1].strip().upper() != "EXR104A":
            raise RuntimeError("Expected EXR104A, received: " + identity)
        # Report existing errors before changing acquisition settings.
        self.check_errors("before configuration; errors already in instrument queue")
        commands = [
            ":STOP",
            f":CHANnel{channel}:DISPlay ON",
            ":ACQuire:MODE RTIMe",
            ":ACQuire:AVERage OFF",
            ":ACQuire:COMPlete 100",
            ":TRIGger:MODE EDGE",
            f":TRIGger:EDGE:SOURce CHANnel{channel}",
            ":TRIGger:EDGE:COUPling DC",
            f":TRIGger:EDGE:SLOPe {edge}",
            f":TRIGger:LEVel CHANnel{channel},{level:.17g}",
            # Never allow an automatic acquisition without a real trigger.
            ":TRIGger:SWEep TRIGgered",
            f":WAVeform:SOURce CHANnel{channel}",
            ":WAVeform:FORMat WORD",
            ":WAVeform:BYTeorder LSBFirst",
            ":WAVeform:STReaming OFF",
        ]
        if points is not None:
            commands.append(f":ACQuire:POINts {points}")
        if time_range is not None:
            commands.append(f":TIMebase:RANGe {time_range:.17g}")
        if gate_before_us is not None or gate_after_us is not None:
            if (gate_before_us is None or gate_after_us is None
                    or not all(math.isfinite(v) and v >= 0 for v in (gate_before_us, gate_after_us))
                    or gate_before_us + gate_after_us <= 0):
                raise ValueError("Provide nonnegative before/after gate durations with a positive total")
            duration = (gate_before_us + gate_after_us) * 1e-6
            center = (gate_after_us - gate_before_us) * 0.5e-6
            commands.extend([":TIMebase:REFerence CENTer",
                             f":TIMebase:RANGe {duration:.17g}",
                             f":TIMebase:POSition {center:.17g}"])
        for command in commands:
            i.write(command)
            self.check_errors(command)
        self.channel = channel
        self.configured = True
        return identity

    def capture(self, container=list, cancel_event=None):
        if not self.configured:
            raise RuntimeError("Configure the scope before capture")
        i = self.instrument
        if cancel_event is not None and cancel_event.is_set():
            raise CaptureCancelled()
        # Use the documented Single/ADER synchronization method. ADER is an
        # event latch: clear the previous event while stopped before rearming.
        # OPC alone after Single is not evidence of a new trigger.
        if scpi_integer(i.query(":STOP;*OPC?")) != 1:
            raise RuntimeError("Could not stop acquisition before arming")
        self.check_errors(":STOP;*OPC?")
        scpi_integer(i.query(":ADER?"))
        self.check_errors(":ADER? (clear previous acquisition event)")
        i.write(":SINGle")
        # An absent trigger is normal: wait until completion or cancellation.
        # VISA's timeout only bounds each individual network query.
        while True:
            if cancel_event is not None and cancel_event.is_set():
                i.write(":STOP")
                raise CaptureCancelled()
            if scpi_integer(i.query(":ADER?")) == 1:
                break
            time.sleep(0.05)
        self.check_errors(":SINGle / :ADER? (new acquisition completed)")
        if scpi_integer(i.query("*OPC?")) != 1:
            raise RuntimeError("Waveform processing did not complete")
        # Select the transfer source explicitly after the completed acquisition.
        command = f":WAVeform:SOURce CHANnel{self.channel}"
        i.write(command)
        self.check_errors(command)
        text = i.query(":WAVeform:PREamble?")
        self.check_errors(":WAVeform:PREamble?")
        preamble = Preamble.parse(text)
        values = i.query_binary_values(
            ":WAVeform:DATA?", datatype="h", is_big_endian=False,
            container=container, header_fmt="ieee", expect_termination=True,
        )
        self.check_errors(":WAVeform:DATA?")
        if len(values) != preamble.points:
            raise RuntimeError(f"Incomplete waveform: {len(values)}/{preamble.points} points")
        return values, preamble
