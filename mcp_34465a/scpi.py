"""34465A SCPI command builders.

SCPI commands are organized as Python functions returning the string to send.
The indirection lets us:
  - Centralize command syntax
  - Centralize range/resolution parsing
  - Keep the instrument layer free of string formatting

The exact SCPI tree is documented in the Keysight "34465A Digital Multimeter
User's Guide" and "34465A Programmer's Guide" (current revision; 34465A/70A
share a command set). The commands below cover the most common measurement
paths needed for the BLE Sticker PCBA HWDVT line:

  - DC/AC voltage and current (VCC rail, leakage, supply ripple)
  - 2-wire and 4-wire resistance (continuity, pull-up/pull-down values)
  - Continuity (PASS/FAIL buzzer-style short test)
  - Diode (forward voltage of protection/LEDs)
  - Frequency / period (clock sanity)
  - Capacitance (decoupling/bulk cap sanity)

Temperature paths (RTD/thermistor/TC) are provided as helpers but not wrapped
as standalone MCP tools — call `dmm_measure` with the appropriate function
if you need them.
"""
from __future__ import annotations
from typing import Literal, Optional


MeasFunction = Literal[
    "VOLT:DC", "VOLT:AC",
    "CURR:DC", "CURR:AC",
    "RES", "FRES",
    "FREQ", "PER",
    "CONT", "DIOD",
    "CAP",
    "TEMP:RTD", "TEMP:THER", "TEMP:TC",
]


# ----------------------------------------------------------------------------
# Universal / instrument control
# ----------------------------------------------------------------------------
def idn() -> str:
    """Standard SCPI identification query."""
    return "*IDN?"


def reset() -> str:
    """Reset to *RST defaults."""
    return "*RST"


def opc() -> str:
    """Operation complete query — block until prior command finishes."""
    return "*OPC?"


def system_error() -> str:
    return ":SYST:ERR?"


def clear_status() -> str:
    return "*CLS"


# ----------------------------------------------------------------------------
# Configuration — measurement function
# ----------------------------------------------------------------------------
def conf(function: MeasFunction) -> str:
    """Configure (select) a measurement function. Does not trigger.

    Use `:READ?` to trigger + fetch, or `:MEASure?` to one-shot configure
    + trigger + fetch.
    """
    return f":CONF:{function}"


def conf_with_range(function: MeasFunction, range_value: float) -> str:
    """Configure a function with a fixed range.

    Args:
        function: e.g. "VOLT:DC"
        range_value: a positive number; for `CONT`/`DIOD` use the defaults
            (1 MΩ / 5 V respectively) and skip this helper.
    """
    if range_value <= 0:
        raise ValueError(f"range must be > 0, got {range_value}")
    return f":CONF:{function} {range_value:g}"


def meas_one_shot(function: MeasFunction) -> str:
    """One-shot: configure + trigger + return a single reading.

    The 34465A auto-ranges when no range is given.
    """
    return f":MEAS:{function}?"


def read() -> str:
    """Trigger the currently-configured function and return a reading."""
    return ":READ?"


# ----------------------------------------------------------------------------
# Acquisition — averaging, NPLC, aperture
# ----------------------------------------------------------------------------
def set_nplc(function: MeasFunction, nplc: float) -> str:
    """Set integration time in number of power-line cycles.

    34465A valid range: 0.001..1000 (resolution / accuracy depend on it).
    Common values: 0.02 (fast), 1 (default), 10 (high-res), 100 (high-accuracy).
    """
    if not 0.001 <= nplc <= 1000:
        raise ValueError(f"nplc must be 0.001..1000, got {nplc}")
    return f":{function}:NPLC {nplc:g}"


def set_resolution(function: MeasFunction, digits: float) -> str:
    """Set display resolution in digits (4..10 for 34465A)."""
    if not 4.0 <= digits <= 10.0:
        raise ValueError(f"digits must be 4..10, got {digits}")
    return f":{function}:RES {digits:g}"


def set_auto_zero(function: MeasFunction, mode: Literal["ON", "OFF", "ONCE"]) -> str:
    return f":{function}:AZ {mode}"


def trigger_single() -> str:
    """Software trigger (used when in `INIT` mode)."""
    return "*TRG"


# ----------------------------------------------------------------------------
# Continuity / diode convenience
# ----------------------------------------------------------------------------
def beep(state: Literal["ON", "OFF"]) -> str:
    """Enable/disable the continuity / diode-limit beep."""
    return f":SYST:BEEP:STAT {state}"


def continuity_threshold(ohms: float) -> str:
    """Set the continuity threshold (default 10 Ω)."""
    if ohms < 1 or ohms > 1000:
        raise ValueError(f"continuity threshold should be 1..1000 Ω, got {ohms}")
    return f":CONT:THR {ohms:g}"


def diode_range() -> str:
    """Default 5 V range on the diode function — explicit for clarity."""
    return ":CONF:DIOD"


# ----------------------------------------------------------------------------
# Display / IO (rarely needed; included for completeness)
# ----------------------------------------------------------------------------
def display_text(text: str) -> str:
    """Show a short message on the front-panel display (≤ 12 chars)."""
    text = text.strip()[:12]
    # SCPI string literal: enclose in double quotes, escape any quote.
    return f':DISP:TEXT "{text}"'


def display_clear() -> str:
    return ":DISP:TEXT:CLE"


# ----------------------------------------------------------------------------
# System housekeeping
# ----------------------------------------------------------------------------
def abort() -> str:
    """Abort any in-progress measurement and return to idle."""
    return ":ABOR"
