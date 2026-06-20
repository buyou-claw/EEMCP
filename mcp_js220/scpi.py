"""JS220 command builders.

The JS220 does NOT speak SCPI; it uses the `joulescope` Python API
(USB private protocol). This file mirrors `mcp_34465a/scpi.py`: it
centralizes the strings/values passed to `parameter_set`, the
lifecycle calls (`start`/`stop`), and any one-line SCPI-ish builders
we'd want to grep for later. The goal is the same: a single place to
update if a firmware rev changes a parameter name or option.

Conventions:
- One function per command.
- No I/O happens here; `instrument.py` is the only file that calls
  into the joulescope package.
- Functions return strings (or small dicts) and raise ValueError for
  out-of-range / unknown inputs.
"""
from __future__ import annotations
from typing import Literal, Tuple

from .config import (
    VALID_SOURCES, VALID_CURRENT_RANGES, VALID_SAMPLING_FREQS,
    VALID_REDUCTION_FREQS, VALID_IO_VOLTAGES,
)


# Convenience aliases (the values come from `parameter.options` and
# are stable across firmware 1.3.x for the JS220).
SensorPower = Literal["on", "off"]
Source = Literal["off", "raw", "pattern_usb", "pattern_control", "pattern_sensor"]
CurrentRange = Literal[
    "auto", "10 A", "2 A", "180 mA", "18 mA",
    "1.8 mA", "180 µA", "18 µA", "off",
]
SamplingFreq = Literal[
    "2 MHz", "1 MHz", "500 kHz", "200 kHz", "100 kHz",
    "50 kHz", "20 kHz", "10 kHz", "5 kHz", "2 kHz",
    "1 kHz", "500 Hz", "200 Hz", "100 Hz", "50 Hz", "20 Hz", "10 Hz",
]
ReductionFreq = Literal["100 Hz", "50 Hz", "20 Hz", "10 Hz", "5 Hz", "2 Hz", "1 Hz"]
IoVoltage = Literal["1.8V", "2.1V", "2.5V", "2.7V", "3.0V", "3.3V", "3.6V", "5.0V"]


# ----------------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------------
def source_on() -> str:
    """Enable the JS220 current/voltage sensor (raw streaming)."""
    return "raw"


def source_off() -> str:
    """Disable the sensor (no current flows in the IN+ / IN- path)."""
    return "off"


def set_source(value: str) -> str:
    if value not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {value!r}")
    return value


# ----------------------------------------------------------------------------
# Configuration — ranges, sampling
# ----------------------------------------------------------------------------
def set_current_range(value: str) -> str:
    if value not in VALID_CURRENT_RANGES:
        raise ValueError(
            f"current_range must be one of {VALID_CURRENT_RANGES}, got {value!r}"
        )
    return value


def set_sampling_frequency(value: str) -> str:
    if value not in VALID_SAMPLING_FREQS:
        raise ValueError(
            f"sampling_frequency must be one of {VALID_SAMPLING_FREQS}, got {value!r}"
        )
    return value


def set_reduction_frequency(value: str) -> str:
    if value not in VALID_REDUCTION_FREQS:
        raise ValueError(
            f"reduction_frequency must be one of {VALID_REDUCTION_FREQS}, got {value!r}"
        )
    return value


def set_io_voltage(value: str) -> str:
    if value not in VALID_IO_VOLTAGES:
        raise ValueError(
            f"io_voltage must be one of {VALID_IO_VOLTAGES}, got {value!r}"
        )
    return value


def set_sensor_power(state: SensorPower) -> str:
    if state not in ("on", "off"):
        raise ValueError(f"sensor_power must be 'on' or 'off', got {state!r}")
    return state


# ----------------------------------------------------------------------------
# Quick-reference for the package's main entry points
# ----------------------------------------------------------------------------
# (instrument.py is the single place that calls these names into
#  `parameter_set`. The functions above are validators; the strings
#  they return are the canonical values to send to the driver.)


# ----------------------------------------------------------------------------
# Statistics data structure (one entry per `reduction_frequency` tick)
# ----------------------------------------------------------------------------
# The driver delivers each tick as a dict:
#   {
#     "time": {"samples": {"value": [start, end], "units": "samples"},
#              "utc":    {"value": [start, end], "units": "time64"},
#              "sample_freq": {"value": <int Hz>},
#              "range":  {"value": [start, end], "units": "s"},
#              "delta":  {"value": <seconds>},
#              "accum_samples": {"value": [start, end]},
#              ... },
#     "signals": {
#       "current":  { "avg": {...}, "std": {...}, "min": {...}, "max": {...},
#                     "p2p": {...}, "integral": {...}, "µ": {...},
#                     "σ2": {...}, "∫": {...} },
#       "voltage":  { ... same shape ... },
#       "power":    { ... same shape ... },
#     },
#     "charge":  { "value": <C>, "units": "C" },     # accumulator
#     "energy":  { "value": <J>, "units": "J" },     # accumulator
#   }
#
# The µ / ∫ / σ2 keys are unicode (mean / integral / variance). We
# always extract the value via the english aliases ("avg", "integral",
# "std") — `instrument.py` uses _extract() for that.

# Helpers for parsing one stats tick
def _extract(signal_dict: dict, key: str) -> Tuple[float, str]:
    """Return (value, units) for `signal_dict[key]['value']`, supporting
    both the english aliases ("avg"/"std") and the unicode keys
    ("µ"/"∫"/"σ2"). Raises KeyError if missing.
    """
    d = signal_dict.get(key)
    if d is None:
        raise KeyError(f"stats signal key {key!r} missing")
    v = d.get("value")
    u = d.get("units", "")
    return v, u
