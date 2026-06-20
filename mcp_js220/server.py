"""Joulescope JS220 MCP Server.

Exposes atomic tools for Claude to drive a Joulescope JS220 precision
energy analyzer. Designed to be paired with `mcp_34465a` (DMM) and
`mcp_8852b` (BT tester) for the CMT BLE Sticker HWDVT line:

    mcp_34465a  -> DC voltage / current / resistance
    mcp_8852b   -> BT RF power / freq / sensitivity
    mcp_js220   -> current, voltage, power, energy (high-rate streaming)

Run with:  python -m mcp_js220.server

Add to Claude Desktop config:
  "mcpServers": {
    "js220": {
      "command": "python",
      "args": ["-m", "mcp_js220.server"],
      "cwd": "e:\\Centrak\\BLE Sticker\\test\\HWTEST"
    }
  }
"""
from __future__ import annotations
import time
import csv
import json
import logging
import statistics
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal

from fastmcp import FastMCP

from .instrument import JS220, JS220Error
from .config import CONFIG, DEFAULT_LIMITS, PREFLIGHT_PRESETS, PREFLIGHT_I_RANGE_TABLE
from .state_machine import State, StateError
from common.envelope import ok, err, envelope as _envelope_base

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("mcp_js220")

mcp = FastMCP("js220")
js = JS220()

# Reports root — same convention as mcp_34465a (per-run subfolders)
REPORTS_ROOT = Path(r"e:\Centrak\BLE Sticker\test\HWTEST\reports")
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Capabilities / configuration discovery
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def js220_get_capabilities() -> dict:
    """Enumerate the JS220's configurable parameters and valid values.

    Call this BEFORE a test so you can see what's available. The
    returned dict has:
      - `device`: model, firmware, hardware version
      - `parameters`: for each user-settable parameter, its current
        value, the list of valid options, the units, and a brief
        description.
      - `default_test_recommendations`: a few preset configurations
        matched to common test types (long-log, peak-capture, idle, etc).

    Use this together with `js220_recommend_config` to pick a starting
    point, then `js220_set_*` to apply the chosen values, then
    `js220_capture_window` or `js220_run_power_profile` to capture.
    """
    if not js.connected:
        return {"ok": False, "error": "not connected; call js220_connect first"}
    try:
        from .config import (
            VALID_SOURCES, VALID_CURRENT_RANGES, VALID_SAMPLING_FREQS,
            VALID_REDUCTION_FREQS, VALID_IO_VOLTAGES,
        )
        params = {}
        for name, valid, units, brief in [
            ("source",              VALID_SOURCES,         "",   "Signal source: 'off' = sensor off, 'raw' = normal streaming"),
            ("i_range",             VALID_CURRENT_RANGES, "A",  "Current measurement range; 'auto' lets the JS220 down-range intelligently"),
            ("sampling_frequency",  VALID_SAMPLING_FREQS, "Hz", "Input ADC sampling rate; higher = better min/max resolution in each window"),
            ("reduction_frequency", VALID_REDUCTION_FREQS, "Hz", "How often statistics callbacks fire; controls per-tick aggregation window size"),
            ("io_voltage",          VALID_IO_VOLTAGES,    "V",  "GPI/GPO/SPI/I2C logic level; must match DUT's MCU rail"),
            ("sensor_power",        ("on", "off"),         "",   "Sensor power; turn off when not measuring to avoid back-powering the DUT"),
        ]:
            try:
                current = js.parameter_get(name)
            except Exception:
                current = None
            params[name] = {
                "current": current,
                "valid_values": list(valid),
                "units": units,
                "brief": brief,
            }
        return _ok({
            "device": {
                "device_path": js._device.device_path,
                "model": js.idn_info.get("model"),
                "serial_number": js.idn_info.get("serial_number"),
                "hardware_version": js.idn_info.get("hardware_version"),
                "ctl_fw": (js.idn_info.get("ctl") or {}).get("fw", {}).get("ver"),
                "sensor_fw": (js.idn_info.get("sensor") or {}).get("fw", {}).get("ver"),
                "fpga_ver": (js.idn_info.get("sensor") or {}).get("fpga", {}).get("ver"),
                "input_sampling_frequency_Hz": js._device.input_sampling_frequency,
                "output_sampling_frequency_Hz": js._device.output_sampling_frequency,
                "buffer_duration_s": js._device.buffer_duration,
            },
            "parameters": params,
            "default_test_recommendations": {
                "long_idle_log": {
                    "use_when": "monitoring standby/connected-but-idle state for minutes to hours",
                    "sampling_frequency": "1 kHz",
                    "reduction_frequency": "2 Hz",
                    "i_range": "auto",
                    "io_voltage": "3.3V",
                },
                "ble_active_bursts": {
                    "use_when": "capturing BLE TX/RX bursts (e.g. advertising or connection events)",
                    "sampling_frequency": "100 kHz",
                    "reduction_frequency": "10 Hz",
                    "i_range": "auto",
                    "io_voltage": "3.3V",
                },
                "fine_peak_capture": {
                    "use_when": "you need to see the shape of a single TX burst (e.g. inrush spikes, dI/dt)",
                    "sampling_frequency": "2 MHz",
                    "reduction_frequency": "1 Hz",
                    "i_range": "10 A",
                    "io_voltage": "3.3V",
                },
                "ultra_low_current": {
                    "use_when": "standby current is < 1 µA and you need clean µA-level resolution",
                    "sampling_frequency": "100 Hz",
                    "reduction_frequency": "2 Hz",
                    "i_range": "18 µA",
                    "io_voltage": "3.3V",
                },
                "high_current_load": {
                    "use_when": "DUT draws > 100 mA continuously (e.g. with motors, radios, displays)",
                    "sampling_frequency": "10 kHz",
                    "reduction_frequency": "20 Hz",
                    "i_range": "10 A",
                    "io_voltage": "3.3V",
                },
            },
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def js220_recommend_config(test_type: str = "ble_active_bursts",
                            expected_current_a: float = 0.05) -> dict:
    """Pick a starting config for a given test type and expected current.

    Args:
        test_type: one of "long_idle_log", "ble_active_bursts",
            "fine_peak_capture", "ultra_low_current", "high_current_load".
            (See `js220_get_capabilities` for what each means.)
        expected_current_a: your guess at the typical current draw (A).
            Used to nudge the current range. E.g. 0.0001 = 100 µA,
            0.05 = 50 mA, 0.5 = 500 mA.

    Returns the recommended (sampling_frequency, reduction_frequency,
    i_range, io_voltage) plus the reasoning. The recommendation is
    NOT auto-applied — call `js220_set_*` to apply each, then start
    the capture.
    """
    # Build a recommendation table
    base = {
        "long_idle_log":      {"sampling_frequency": "1 kHz",   "reduction_frequency": "2 Hz",   "i_range": "auto",    "io_voltage": "3.3V"},
        "ble_active_bursts":  {"sampling_frequency": "100 kHz", "reduction_frequency": "10 Hz",  "i_range": "auto",    "io_voltage": "3.3V"},
        "fine_peak_capture":  {"sampling_frequency": "2 MHz",   "reduction_frequency": "1 Hz",   "i_range": "10 A",    "io_voltage": "3.3V"},
        "ultra_low_current":  {"sampling_frequency": "100 Hz",  "reduction_frequency": "2 Hz",   "i_range": "18 µA",   "io_voltage": "3.3V"},
        "high_current_load":  {"sampling_frequency": "10 kHz",  "reduction_frequency": "20 Hz",  "i_range": "10 A",    "io_voltage": "3.3V"},
    }
    rec = dict(base.get(test_type, base["ble_active_bursts"]))

    # Nudge the current range based on expected_current_a
    # Map magnitude to a range that's 5-10x the expected value
    expected = max(1e-9, expected_current_a)
    if expected < 18e-6:    rec["i_range"] = "18 µA"
    elif expected < 180e-6: rec["i_range"] = "180 µA"
    elif expected < 1.8e-3:  rec["i_range"] = "1.8 mA"
    elif expected < 18e-3:   rec["i_range"] = "18 mA"
    elif expected < 180e-3:  rec["i_range"] = "180 mA"
    elif expected < 2.0:     rec["i_range"] = "2 A"
    else:                    rec["i_range"] = "10 A"

    # Compute effective capture behavior
    samp_hz = float(rec["sampling_frequency"].split()[0]) * (
        1_000_000 if "MHz" in rec["sampling_frequency"] else
        1_000 if "kHz" in rec["sampling_frequency"] else 1)
    red_hz = float(rec["reduction_frequency"].split()[0])
    samples_per_window = samp_hz / red_hz if red_hz > 0 else 0

    reasoning = []
    reasoning.append(
        f"Test type '{test_type}' → base preset: "
        f"sampling={rec['sampling_frequency']}, reduction={rec['reduction_frequency']}, range={rec['i_range']}"
    )
    reasoning.append(
        f"Expected current {expected_current_a*1000:.4f} mA → i_range '{rec['i_range']}' "
        f"selected to give ~5-10x headroom for peaks."
    )
    reasoning.append(
        f"Effective: {samp_hz:.0f} Hz input sampled at {red_hz:.0f} Hz stats rate "
        f"→ {samples_per_window:.0f} samples per stats window "
        f"(good for resolving sub-millisecond peaks: {samples_per_window >= 1000})."
    )
    if samples_per_window < 100:
        reasoning.append(
            "WARNING: <100 samples per window — sub-millisecond peaks may be missed. "
            "Consider raising sampling_frequency or lowering reduction_frequency."
        )

    return _ok({
        "test_type": test_type,
        "expected_current_a": expected_current_a,
        "recommended": rec,
        "reasoning": reasoning,
    })


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_configure_for_test  # noqa
def js220_configure_for_test(test_type: str = "ble_active_bursts",
                             expected_current_a: float = 0.05,
                             apply: bool = False) -> dict:
    """Convenience: pick a config, optionally apply it.

    Same inputs as `js220_recommend_config`. If `apply=True`, also
    calls the underlying `js220_set_*` tools for you. Otherwise just
    returns the recommendation so you can review it.

    Typical flow:
        1. rec = js220_configure_for_test("ble_active_bursts", 0.05)
        2. user reviews, then re-calls with apply=True
        3. js220_capture_window(2.0) or js220_run_power_profile(...)
    """
    try:
        if not js.connected:
            return {"ok": False, "error": "not connected"}
        rec_result = js220_recommend_config(test_type, expected_current_a)
        if not rec_result.get("ok"):
            return rec_result
        rec = rec_result["recommended"]
        if not apply:
            return _ok({
                "applied": False,
                "recommended": rec,
                "note": "Pass apply=True to write these values to the JS220.",
            })
        # Apply in dependency order
        applied = {}
        applied["io_voltage"] = js.set_io_voltage(rec["io_voltage"])
        applied["sampling_frequency"] = js.set_sampling_frequency(rec["sampling_frequency"])
        applied["reduction_frequency"] = js.set_reduction_frequency(rec["reduction_frequency"])
        applied["i_range"] = js.set_current_range(rec["i_range"])
        return _ok({"applied": True, "values": applied})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _envelope(test: str, value: float, unit: str,
              lower: Optional[float] = None,
              upper: Optional[float] = None) -> dict:
    """Standard result envelope — matches the mcp_34465a / mcp_8852b shape
    so the AI can consume results uniformly across instruments.

    Implementation lives in `common.envelope` (shared with mcp_34465a).
    This wrapper adds the DEFAULT_LIMITS fallback (per-function default
    pass window) that the common helper doesn't know about.
    """
    if lower is None or upper is None:
        spec = DEFAULT_LIMITS.get(test, {})
        lower = spec.get("lower") if lower is None else lower
        upper = spec.get("upper") if upper is None else upper
    return _envelope_base(test, value, unit, lower, upper)


def _ok(d: dict) -> dict:
    return ok(**d)


def _err(e: Exception) -> dict:
    return err(str(e))


def _summarize_stats(samples: list[dict]) -> dict:
    """Build a per-signal summary (avg/std/min/max/peak) from a list of
    stats ticks. The accumulator (charge/energy) is reported as the
    final value at the end of the window.
    """
    def collect(sig_key: str, stat_key: str) -> list[float]:
        out = []
        for s in samples:
            try:
                out.append(float(s["signals"][sig_key][stat_key]["value"]))
            except (KeyError, TypeError):
                continue
        return out

    out: dict = {"n_samples": len(samples)}
    for sig in ("current", "voltage", "power"):
        for stat in ("avg", "std", "min", "max"):
            vals = collect(sig, stat)
            if vals:
                if stat == "avg":
                    out[f"{sig}_avg"] = statistics.fmean(vals)
                elif stat == "std":
                    out[f"{sig}_std"] = statistics.fmean(vals)
                elif stat == "min":
                    out[f"{sig}_min"] = min(vals)
                elif stat == "max":
                    out[f"{sig}_max"] = max(vals)
    # Peak across the whole window (max of maxes)
    for sig in ("current", "voltage", "power"):
        m = out.get(f"{sig}_max")
        if m is not None:
            out[f"{sig}_peak"] = m
    # Accumulators
    if samples:
        last = samples[-1]
        try:
            out["charge_C"] = float(last.get("charge", {}).get("value", 0.0))
        except (TypeError, ValueError):
            out["charge_C"] = 0.0
        try:
            out["energy_J"] = float(last.get("energy", {}).get("value", 0.0))
        except (TypeError, ValueError):
            out["energy_J"] = 0.0
    return out


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def js220_list_devices() -> dict:
    """List the JS220 devices scan() currently sees over USB.

    No side effects — useful for verifying the instrument is plugged in
    before calling `js220_connect`.
    """
    try:
        return _ok({"devices": js.list_devices()})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def js220_connect(device_path: Optional[str] = None) -> dict:
    """Open the first matching JS220 (or scan()[0] if no path given).

    Args:
        device_path: optional substring match against the device's path,
            e.g. "005742" or "u/js220/005742". Case-insensitive. If
            omitted, the first device scan() finds is used.
    """
    try:
        info = js.connect(device_path)
        return _ok({
            "device_path": info["device_path"],
            "idn": info["idn"],
            "state": js.state.value,
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def js220_disconnect() -> dict:
    """Close the JS220 session. Safe to call from any state (force-stops first)."""
    try:
        js.disconnect()
        return _ok({"connected": js.connected, "state": js.state.value})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def js220_status() -> dict:
    """Return connection state, IDN info, and current parameter settings."""
    try:
        if not js.connected:
            return _ok({"connected": False, "state": js.state.value})
        out = {
            "connected": True,
            "state": js.state.value,
            "idn": js.idn_info,
            "device_path": js._device.device_path,
            "parameters": {
                "source": js.parameter_get("source"),
                "i_range": js.parameter_get("i_range"),
                "sampling_frequency": js.parameter_get("sampling_frequency"),
                "reduction_frequency": js.parameter_get("reduction_frequency"),
                "io_voltage": js.parameter_get("io_voltage"),
                "sensor_power": js.parameter_get("sensor_power"),
            },
        }
        return _ok(out)
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def js220_reset() -> dict:
    """Disconnect + reconnect (soft reset). Clears error state."""
    try:
        js.reset()
        return _ok({"reset": True, "state": js.state.value, "idn": js.idn_info})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_set_source  # noqa
def js220_set_source(value: Literal["off", "raw"]) -> dict:
    """Set the signal source: 'off' = sensor off, 'raw' = normal streaming.

    Note: the MCP-managed start()/stop() helpers will set this for you
    — you only need this tool if you want to manually control the
    source independent of streaming.
    """
    try:
        v = js.set_source(value)
        return _ok({"source": v})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_set_current_range  # noqa
def js220_set_current_range(value: str) -> dict:
    """Set the current measurement range.

    Args:
        value: one of "auto", "10 A", "2 A", "180 mA", "18 mA",
            "1.8 mA", "180 µA", "18 µA", "off". "auto" lets the JS220
            down-range as current drops; only fix a range if you know
            the expected current to maximize accuracy.
    """
    try:
        v = js.set_current_range(value)
        return _ok({"i_range": v})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_set_sampling_frequency  # noqa
def js220_set_sampling_frequency(value: str) -> dict:
    """Set the input sampling frequency (Hz).

    Args:
        value: one of "2 MHz", "1 MHz", "500 kHz", "200 kHz",
            "100 kHz", "50 kHz", "20 kHz", "10 kHz", "5 kHz", "2 kHz",
            "1 kHz", "500 Hz", "200 Hz", "100 Hz", "50 Hz", "20 Hz",
            "10 Hz". 100 kHz is a good default for BLE bursts.
    """
    try:
        v = js.set_sampling_frequency(value)
        return _ok({"sampling_frequency": v})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_set_reduction_frequency  # noqa
def js220_set_reduction_frequency(value: str) -> dict:
    """Set the statistics output frequency (Hz).

    Args:
        value: one of "100 Hz", "50 Hz", "20 Hz", "10 Hz", "5 Hz",
            "2 Hz", "1 Hz". The driver fires stats_callback at this
            rate while streaming. 10 Hz is a good default.
    """
    try:
        v = js.set_reduction_frequency(value)
        return _ok({"reduction_frequency": v})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_set_io_voltage  # noqa
def js220_set_io_voltage(value: str) -> dict:
    """Set the GPIO/I2C/SPI logic level voltage.

    Args:
        value: one of "1.8V", "2.1V", "2.5V", "2.7V", "3.0V", "3.3V",
            "3.6V", "5.0V". Should match the DUT's MCU rail (3.3V for
            the BLE Sticker).
    """
    try:
        v = js.set_io_voltage(value)
        return _ok({"io_voltage": v})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_clear_accumulators  # noqa
def js220_clear_accumulators() -> dict:
    """Reset the charge (C) and energy (J) accumulators to zero.

    Use this between test phases (e.g. before a "transmit burst"
    capture) so the accumulator delta is exactly the phase energy.
    """
    try:
        js.clear_accumulators()
        return _ok({"cleared": True})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Streaming lifecycle (low-level)
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_start  # noqa
def js220_start() -> dict:
    """Turn the sensor on and start streaming statistics.

    After this call, the device is in STREAMING state and statistics
    are accumulating internally. Use `js220_collect` or
    `js220_capture_window` to retrieve data.
    """
    try:
        js.start()
        return _ok({"streaming": True, "state": js.state.value})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_stop  # noqa
def js220_stop() -> dict:
    """Stop streaming and turn the sensor off."""
    try:
        js.stop()
        return _ok({"streaming": False, "state": js.state.value})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_collect  # noqa
def js220_collect(duration_s: float = 1.0) -> dict:
    """Stream for `duration_s` seconds and return a summary.

    Convenience wrapper around start/sleep/stop. Use this for short,
    one-off measurements. For long logs, use `js220_run_power_profile`
    which writes a CSV + report.
    """
    try:
        if not js.connected:
            return {"ok": False, "error": "not connected; call js220_connect first"}
        samples = js.collect_for(duration_s)
        summary = _summarize_stats(samples)
        return _ok({"duration_s": duration_s, "summary": summary})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# One-shot measurement tools (return the standard envelope)
# ---------------------------------------------------------------------------
def _one_shot_measure(signal: str, stat: str, duration_s: float,
                      test: str, unit: str,
                      lower: Optional[float], upper: Optional[float]) -> dict:
    if not js.connected:
        return {"ok": False, "error": "not connected; call js220_connect first"}
    samples = js.collect_for(duration_s)
    if not samples:
        return {"ok": False, "error": f"no stats samples received in {duration_s}s "
                                       f"(check sensor is powered and source is on)"}
    vals = []
    for s in samples:
        try:
            vals.append(float(s["signals"][signal][stat]["value"]))
        except (KeyError, TypeError):
            continue
    if not vals:
        return {"ok": False, "error": f"no {signal}.{stat} values extracted"}
    value = statistics.fmean(vals) if stat == "avg" else (
        min(vals) if stat == "min" else max(vals))
    return _ok(_envelope(test, value, unit, lower, upper))


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_measure_current_avg  # noqa
def js220_measure_current_avg(duration_s: float = 1.0,
                               lower: Optional[float] = None,
                               upper: Optional[float] = None) -> dict:
    """Measure average current (A) over `duration_s` seconds.

    Args:
        duration_s: window length in seconds (default 1.0).
        lower/upper: optional pass-window override (A).
    """
    try:
        return _one_shot_measure("current", "avg", duration_s,
                                 "CURR:AVG", "A", lower, upper)
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_measure_voltage_avg  # noqa
def js220_measure_voltage_avg(duration_s: float = 1.0,
                               lower: Optional[float] = None,
                               upper: Optional[float] = None) -> dict:
    """Measure average voltage (V) over `duration_s` seconds."""
    try:
        return _one_shot_measure("voltage", "avg", duration_s,
                                 "VOLT:AVG", "V", lower, upper)
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_measure_power_avg  # noqa
def js220_measure_power_avg(duration_s: float = 1.0,
                            lower: Optional[float] = None,
                            upper: Optional[float] = None) -> dict:
    """Measure average power (W) over `duration_s` seconds."""
    try:
        return _one_shot_measure("power", "avg", duration_s,
                                 "POW:AVG", "W", lower, upper)
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_measure_current_peak  # noqa
def js220_measure_current_peak(duration_s: float = 1.0,
                                lower: Optional[float] = None,
                                upper: Optional[float] = None) -> dict:
    """Measure peak current (A) over `duration_s` seconds.

    "Peak" is the maximum of the per-tick `current.max` value, which
    is the highest instantaneous current observed within each
    reduction window.
    """
    try:
        return _one_shot_measure("current", "max", duration_s,
                                 "CURR:PEAK", "A", lower, upper)
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_measure_power_peak  # noqa
def js220_measure_power_peak(duration_s: float = 1.0,
                             lower: Optional[float] = None,
                             upper: Optional[float] = None) -> dict:
    """Measure peak power (W) over `duration_s` seconds."""
    try:
        return _one_shot_measure("power", "max", duration_s,
                                 "POW:PEAK", "W", lower, upper)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# High-level: capture window (returns raw samples + summary)
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_capture_window  # noqa
def js220_capture_window(duration_s: float = 1.0) -> dict:
    """Stream for `duration_s` seconds and return per-signal avg/std/min/max.

    This is the workhorse for power profiling. It does NOT write to
    disk; for full CSV logs use `js220_run_power_profile`.
    """
    try:
        if not js.connected:
            return {"ok": False, "error": "not connected"}
        samples = js.collect_for(duration_s)
        summary = _summarize_stats(samples)
        return _ok({
            "duration_s": duration_s,
            "n_samples": len(samples),
            "summary": summary,
        })
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# High-level: full power profile (writes CSV + summary to reports/)
# ---------------------------------------------------------------------------
def _write_csv(path: Path, samples: list[dict]) -> None:
    """Write a flat CSV of per-tick stats. Uses 'time' as the first
    column; other columns are signal.stat pairs.
    """
    fields = [
        "t_s", "sample_freq_Hz",
        "current_avg_A", "current_std_A", "current_min_A", "current_max_A", "current_p2p_A",
        "voltage_avg_V", "voltage_std_V", "voltage_min_V", "voltage_max_V", "voltage_p2p_V",
        "power_avg_W",   "power_std_W",   "power_min_W",   "power_max_W",   "power_p2p_W",
        "charge_C", "energy_J",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for s in samples:
            row = []
            t = s.get("time", {})
            try:
                t_range = t["range"]["value"]
                row.append(t_range[1] - t_range[0])
            except (KeyError, TypeError):
                row.append("")
            try:
                row.append(t["sample_freq"]["value"])
            except (KeyError, TypeError):
                row.append("")
            for sig in ("current", "voltage", "power"):
                for stat in ("avg", "std", "min", "max", "p2p"):
                    try:
                        row.append(s["signals"][sig][stat]["value"])
                    except (KeyError, TypeError):
                        row.append("")
            for acc in ("charge", "energy"):
                try:
                    row.append(s[acc]["value"])
                except (KeyError, TypeError):
                    row.append("")
            w.writerow(row)


def _write_markdown(md_path: Path, run_label: str, duration_s: float,
                    rate_hz: float, n_samples: int, summary: dict,
                    meta: dict) -> None:
    with md_path.open("w", encoding="utf-8") as f:
        f.write(f"# {run_label}\n\n")
        f.write(f"- **Device**: {meta.get('device_path')}\n")
        f.write(f"- **Duration**: {duration_s:.2f} s\n")
        f.write(f"- **Samples**: {n_samples} (target rate ~{rate_hz:.1f} Hz)\n")
        f.write(f"- **Started**: {meta.get('started')}\n")
        f.write(f"- **Sampling freq**: {meta.get('sampling_frequency')}\n")
        f.write(f"- **Reduction freq**: {meta.get('reduction_frequency')}\n")
        f.write(f"- **Current range**: {meta.get('i_range')}\n\n")
        f.write("## Per-signal summary\n\n")
        f.write("| Signal | avg | std | min | max (peak) |\n")
        f.write("|---|---|---|---|---|\n")
        for sig, unit in (("current", "A"), ("voltage", "V"), ("power", "W")):
            f.write(f"| {sig} ({unit}) | "
                    f"{summary.get(f'{sig}_avg', 0):.6g} | "
                    f"{summary.get(f'{sig}_std', 0):.6g} | "
                    f"{summary.get(f'{sig}_min', 0):.6g} | "
                    f"{summary.get(f'{sig}_max', 0):.6g} |\n")
        f.write(f"\n## Accumulators\n\n")
        f.write(f"- **Charge**: {summary.get('charge_C', 0):.6f} C\n")
        f.write(f"- **Energy**: {summary.get('energy_J', 0):.6f} J\n")


# NOTE: not exposed via MCP. The MCP test surface is js220_run_preflight_capture
# (after js220_preflight_capture has proposed). This function is the Python-library
# implementation, importable by scripts:
#   from mcp_js220.server import js220_run_power_profile  # noqa
def js220_run_power_profile(label: Optional[str] = None,
                            duration_s: float = 60.0,
                            rate_hz: float = 10.0) -> dict:
    """Capture a long power profile and write to reports/<label>/.

    Args:
        label: subfolder name under reports/. Default: js220_profile_<timestamp>.
        duration_s: capture length in seconds.
        rate_hz: desired reduction_frequency in Hz (one of
            "100"/"50"/"20"/"10"/"5"/"2"/"1"). The closest valid
            reduction_frequency is chosen; samples are not skipped,
            just delivered at that rate.

    The function sets the reduction_frequency, clears accumulators,
    streams for `duration_s`, then writes:
        reports/<label>/data.csv
        reports/<label>/summary.json
        reports/<label>/report.md
    """
    try:
        if not js.connected:
            return {"ok": False, "error": "not connected"}
        if duration_s <= 0:
            return {"ok": False, "error": "duration_s must be > 0"}

        # Map rate_hz to a valid reduction_frequency string
        valid = ("100 Hz", "50 Hz", "20 Hz", "10 Hz", "5 Hz", "2 Hz", "1 Hz")
        closest = min(valid, key=lambda v: abs(int(v.split()[0]) - rate_hz))
        # Apply via set_reduction_frequency so default is updated
        js.set_reduction_frequency(closest)
        actual_rate = float(closest.split()[0])

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_label = label or f"js220_profile_{stamp}"
        run_dir = REPORTS_ROOT / run_label
        run_dir.mkdir(parents=True, exist_ok=True)
        csv_path = run_dir / "data.csv"
        json_path = run_dir / "summary.json"
        md_path = run_dir / "report.md"
        # .stop sentinel: if this file appears mid-run, break the loop
        # cleanly (aligned with §18 of BUILDING_AN_INSTRUMENT_MCP.md and
        # with the CLI script's behavior in
        # mcp_js220/scripts/run_js220_power_profile.py:161,191).
        stop_file = run_dir / ".stop"

        # Start streaming
        js.start()
        meta = {
            "device_path": js._device.device_path,
            "started": datetime.now().isoformat(timespec="seconds"),
            "sampling_frequency": js.parameter_get("sampling_frequency"),
            "reduction_frequency": js.parameter_get("reduction_frequency"),
            "i_range": js.parameter_get("i_range"),
        }
        # Collect for the requested duration
        end_time = time.monotonic() + duration_s
        # Background: also fsync the CSV each ~0.5 s for live-safety
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            header_written = [False]
            stop_flag = [False]

            def flush_samples():
                # Periodically flush partial samples to disk
                import os
                while not stop_flag[0]:
                    time.sleep(0.5)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass

            flush_thread = threading.Thread(target=flush_samples, daemon=True)
            flush_thread.start()
            # We can't pre-write the header because the row format is
            # a fixed schema; do that first, then write rows as they
            # arrive.
            fields = [
                "t_s", "sample_freq_Hz",
                "current_avg_A", "current_std_A", "current_min_A", "current_max_A", "current_p2p_A",
                "voltage_avg_V", "voltage_std_V", "voltage_min_V", "voltage_max_V", "voltage_p2p_V",
                "power_avg_W",   "power_std_W",   "power_min_W",   "power_max_W",   "power_p2p_W",
                "charge_C", "energy_J",
            ]
            w = csv.writer(f)
            w.writerow(fields)
            header_written[0] = True
            f.flush()

            last_count = 0
            stopped_by = "duration"
            while time.monotonic() < end_time:
                if stop_file.exists():
                    stopped_by = "stop_file"
                    break
                time.sleep(min(0.2, end_time - time.monotonic()))
                snap = js.get_stats_snapshot()
                for s in snap[last_count:]:
                    row = []
                    t = s.get("time", {})
                    try:
                        t_range = t["range"]["value"]
                        row.append(t_range[1] - t_range[0])
                    except (KeyError, TypeError):
                        row.append("")
                    try:
                        row.append(t["sample_freq"]["value"])
                    except (KeyError, TypeError):
                        row.append("")
                    for sig in ("current", "voltage", "power"):
                        for stat in ("avg", "std", "min", "max", "p2p"):
                            try:
                                row.append(s["signals"][sig][stat]["value"])
                            except (KeyError, TypeError):
                                row.append("")
                    for acc in ("charge", "energy"):
                        try:
                            row.append(s[acc]["value"])
                        except (KeyError, TypeError):
                            row.append("")
                    w.writerow(row)
                last_count = len(snap)
                f.flush()
            stop_flag[0] = True
            flush_thread.join(timeout=1.0)
            samples = js.get_stats_snapshot()

        # Stop streaming
        js.stop()

        # Summary + report
        summary = _summarize_stats(samples)
        summary_json = {
            "run_label": run_label,
            "device_path": meta["device_path"],
            "started": meta["started"],
            "duration_s": duration_s,
            "rate_hz_actual": actual_rate,
            "n_samples": len(samples),
            "sampling_frequency": meta["sampling_frequency"],
            "reduction_frequency": meta["reduction_frequency"],
            "i_range": meta["i_range"],
            "stopped_by": stopped_by,
            "summary": summary,
        }
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(summary_json, f, indent=2, ensure_ascii=False)
        _write_markdown(md_path, run_label, duration_s, actual_rate,
                        len(samples), summary, meta)

        return _ok({
            "run_label": run_label,
            "run_dir": str(run_dir),
            "n_samples": len(samples),
            "duration_s": duration_s,
            "rate_hz_actual": actual_rate,
            "stopped_by": stopped_by,
            "summary": summary,
        })
    except Exception as e:
        # Make sure we don't leave the device in STREAMING on error
        try:
            js.stop()
        except Exception as cleanup_err:
            log.warning("js.stop() cleanup after error failed: %s",
                        cleanup_err)
        return _err(e)


# ---------------------------------------------------------------------------
# Interactive preflight (added per INTERACTIVE_DESIGN.md § 4.2)
# ---------------------------------------------------------------------------
# These tools implement the two-step "confirm-test-setup" pattern:
#
#   1. `js220_preflight_capture(...)` — propose sampling/reduction/range
#      based on `test_type` + `expected_current_a`, return a structured
#      interaction request the AI client renders with `AskUserQuestion`.
#      **NO USB / `parameter_set` / `start()` is called** in this step —
#      the device is not touched at all. Idempotent.
#
#   2. `js220_run_preflight_capture(...)` — apply the user-confirmed
#      config, run one capture window, return the standard envelope.
#      **Hard-gated by `user_confirmed=True`**: if False, returns error
#      without touching the bus.
#
# Bypass: pass `auto_confirm=True` to step 1 to skip questions and
# apply the proposed config directly. Use this for batch / scripted
# runs where the operator has already chosen the test plan.


def _nudge_i_range(expected_current_a: float, peak_current_a: Optional[float]) -> str:
    """Pick an i_range that gives ~5-10x headroom over the larger of
    expected/peak current. Mirrors the logic in `js220_recommend_config`
    but uses the static table in config.py so it can run offline (the
    preflight step must not touch the device).
    """
    # Use peak if given, else 10x expected (which itself already implies
    # ~10x headroom for typical expected vs occasional peaks).
    target = max(1e-9, peak_current_a if peak_current_a is not None
                 else expected_current_a * 10.0)
    for upper, rng in PREFLIGHT_I_RANGE_TABLE:
        if upper is None or target < upper:
            return rng
    return "10 A"


def _preflight_questions(test_type: str, proposed_i_range: str) -> list[dict]:
    """Build the question list for the preflight capture flow.

    Always 3 questions: confirm the test_type, confirm the i_range
    (with 5 candidate options so the user can pick a different
    headroom), and confirm the pass_window for the resulting
    CURR:AVG measurement.
    """
    return [
        {
            "name": "test_type",
            "question": "确认 capture 类型?",
            "header": "Test type",
            "options": [
                {"label": t, "value": t}
                for t in PREFLIGHT_PRESETS.keys()
            ] + [
                {"label": "其他 (我手动指定 preset)", "value": "manual"},
            ],
            "multiSelect": False,
        },
        {
            "name": "i_range",
            "question": f"当前提议 i_range = '{proposed_i_range}', 改不改?",
            "header": "I range",
            "options": [
                {"label": f"{proposed_i_range} (提议, ~5-10x headroom)", "value": proposed_i_range},
                {"label": "auto (让 JS220 自动选)", "value": "auto"},
                {"label": "10 A (锁大档, 避免切量程)", "value": "10 A"},
                {"label": "18 µA (锁最灵敏档, 测 sleep)", "value": "18 µA"},
                {"label": "其他 (我手动指定)", "value": "manual"},
            ],
            "multiSelect": False,
        },
        {
            "name": "pass_window",
            "question": "CURR:AVG 的合格区间? (走 DEFAULT_LIMITS 还是手动指定)",
            "header": "Pass window",
            "options": [
                {"label": "走默认 0-500 mA (产品线宽松)", "value": "default"},
                {"label": "我手动指定 (输入 mA)", "value": "manual"},
            ],
            "multiSelect": False,
        },
    ]


def _preflight_rationale(test_type: str, expected_a: float, peak_a: Optional[float],
                         i_range: str, duration_s: float) -> str:
    """One-paragraph Chinese rationale for why we propose this config."""
    preset = PREFLIGHT_PRESETS.get(test_type, {})
    preset_rationale = preset.get("rationale", "")
    peak_str = f" / 峰值 {peak_a*1000:.2f} mA" if peak_a is not None else ""
    return (
        f"test_type='{test_type}' → 建议 sampling/reduction/io_voltage "
        f"从 preset 复制; i_range='{i_range}' 基于 expected_current="
        f"{expected_a*1000:.4f} mA{peak_str} 算出 ~5-10× headroom; "
        f"duration_s={duration_s:.1f} s。{preset_rationale}"
    )


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def js220_preflight_capture(test_name: str,
                            test_type: str = "ble_active_bursts",
                            expected_current_a: float = 0.05,
                            peak_current_a: Optional[float] = None,
                            duration_s: float = 60.0,
                            io_voltage: Optional[str] = None,
                            lower: Optional[float] = None,
                            upper: Optional[float] = None,
                            auto_confirm: bool = False) -> dict:
    """Step 1 of the interactive preflight flow for the JS220.

    Propose a capture configuration (sampling_frequency,
    reduction_frequency, i_range, io_voltage, duration_s, pass_window)
    for `test_name`, return a structured interaction request the AI
    client renders with `AskUserQuestion`. **No USB commands are
    sent** in this step — the device is not touched. The proposal
    is idempotent.

    Args:
        test_name:          human-readable test name (e.g. "BLE_TX_BURST")
        test_type:          one of PREFLIGHT_PRESETS keys
                            (long_idle_log, ble_active_bursts,
                            fine_peak_capture, ultra_low_current,
                            high_current_load)
        expected_current_a: operator's guess at typical current (A)
        peak_current_a:     operator's guess at peak current (A), or None
        duration_s:         capture window in seconds (default 60.0)
        io_voltage:         optional override; default "3.3V"
        lower/upper:        optional pass window override; default
                            DEFAULT_LIMITS['CURR:AVG']
        auto_confirm:       if True, skip questions and execute the
                            proposed config directly (bypass mode for
                            batch / scripted runs). Default False.

    Returns (auto_confirm=False):
        {
            "ok": true,
            "needs_user_input": true,
            "interaction_type": "confirm_test_setup",
            "test_name": ...,
            "test_type": ...,
            "proposed_config": {sampling_frequency, reduction_frequency,
                                 i_range, io_voltage, duration_s, lower, upper},
            "rationale": "...",
            "questions": [...],
            "next_action": "..."
        }

    Returns (auto_confirm=True): standard envelope (no questions).
    """
    if test_type not in PREFLIGHT_PRESETS:
        return {"ok": False,
                "error": f"unknown test_type '{test_type}'; "
                         f"valid: {list(PREFLIGHT_PRESETS)}"}

    preset = PREFLIGHT_PRESETS[test_type]
    base_cfg = preset["default_config"]

    # Compute proposed config
    prop_sampling = base_cfg["sampling_frequency"]
    prop_reduction = base_cfg["reduction_frequency"]
    # Override i_range with our nudged one unless preset hard-locks a
    # specific value that the nudge would have picked anyway.
    prop_i_range = _nudge_i_range(expected_current_a, peak_current_a)
    prop_io_voltage = io_voltage if io_voltage is not None else base_cfg["io_voltage"]
    prop_lower = lower if lower is not None else DEFAULT_LIMITS["CURR:AVG"]["lower"]
    prop_upper = upper if upper is not None else DEFAULT_LIMITS["CURR:AVG"]["upper"]

    proposed_config = {
        "test_type": test_type,
        "sampling_frequency": prop_sampling,
        "reduction_frequency": prop_reduction,
        "i_range": prop_i_range,
        "io_voltage": prop_io_voltage,
        "duration_s": duration_s,
        "expected_current_a": expected_current_a,
        "peak_current_a": peak_current_a,
        "lower": prop_lower,
        "upper": prop_upper,
    }
    rationale = _preflight_rationale(test_type, expected_current_a,
                                     peak_current_a, prop_i_range, duration_s)

    # Bypass mode: apply proposed config and execute, return envelope.
    if auto_confirm:
        return js220_run_preflight_capture(
            test_name=test_name,
            test_type=test_type,
            expected_current_a=expected_current_a,
            peak_current_a=peak_current_a,
            duration_s=duration_s,
            io_voltage=prop_io_voltage,
            sampling_frequency=prop_sampling,
            reduction_frequency=prop_reduction,
            i_range=prop_i_range,
            lower=prop_lower,
            upper=prop_upper,
            user_confirmed=True,  # auto_confirm implies consent
        )

    # Interaction mode: NO USB. Just shape the proposal.
    questions = _preflight_questions(test_type, prop_i_range)
    next_action = (
        "将用户在 questions 中的回答合并到 proposed_config, "
        "然后调用 js220_run_preflight_capture(..., user_confirmed=True) 执行采集。"
    )
    return {
        "ok": True,
        "needs_user_input": True,
        "interaction_type": "confirm_test_setup",
        "test_name": test_name,
        "test_type": test_type,
        "proposed_config": proposed_config,
        "rationale": rationale,
        "questions": questions,
        "next_action": next_action,
    }


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def js220_run_preflight_capture(test_name: str,
                                test_type: str = "ble_active_bursts",
                                expected_current_a: float = 0.05,
                                peak_current_a: Optional[float] = None,
                                duration_s: float = 60.0,
                                io_voltage: str = "3.3V",
                                sampling_frequency: Optional[str] = None,
                                reduction_frequency: Optional[str] = None,
                                i_range: Optional[str] = None,
                                lower: Optional[float] = None,
                                upper: Optional[float] = None,
                                user_confirmed: bool = False) -> dict:
    """Step 2 of the interactive preflight flow for the JS220.

    Apply the user-confirmed capture configuration, stream for
    `duration_s` seconds, and return the standard envelope for the
    CURR:AVG measurement. This is the JS220-equivalent of
    `dmm_run_preflight_measure`.

    Hard gate: `user_confirmed` MUST be `True` to execute. The default
    is `False`, in which case the call returns an error WITHOUT
    touching the USB bus. This prevents an over-eager client from
    running a capture the user hasn't reviewed.

    Args:
        test_name, test_type, expected_current_a, peak_current_a,
        duration_s, lower, upper: same as js220_preflight_capture
        io_voltage:                e.g. "3.3V" (default)
        sampling_frequency:        e.g. "100 kHz" (required if user_confirmed=True;
                                   falls back to preset default if None)
        reduction_frequency:       e.g. "10 Hz" (required if user_confirmed=True;
                                   falls back to preset default if None)
        i_range:                   e.g. "auto" (required if user_confirmed=True;
                                   falls back to nudge result if None)
        user_confirmed:            MUST be True to execute.

    Returns: standard envelope `{ok, test, value, unit, lower, upper, pass, ...}`.
    """
    # Hard gate — the user MUST have explicitly confirmed.
    if not user_confirmed:
        return {"ok": False,
                "error": "user_confirmed must be True to execute. "
                         "Call js220_preflight_capture first and confirm with the user."}
    if test_type not in PREFLIGHT_PRESETS:
        return {"ok": False,
                "error": f"unknown test_type '{test_type}'; "
                         f"valid: {list(PREFLIGHT_PRESETS)}"}
    if not js.connected:
        return {"ok": False,
                "error": "not connected; call js220_connect first"}
    if duration_s <= 0:
        return {"ok": False, "error": "duration_s must be > 0"}

    preset = PREFLIGHT_PRESETS[test_type]
    base_cfg = preset["default_config"]
    # Fall back to preset defaults if user didn't override
    sf = sampling_frequency or base_cfg["sampling_frequency"]
    rf = reduction_frequency or base_cfg["reduction_frequency"]
    ir = i_range or _nudge_i_range(expected_current_a, peak_current_a)

    # Resolve pass window (user override > DEFAULT_LIMITS)
    lo = lower if lower is not None else DEFAULT_LIMITS["CURR:AVG"]["lower"]
    hi = upper if upper is not None else DEFAULT_LIMITS["CURR:AVG"]["upper"]

    try:
        js.set_io_voltage(io_voltage)
        js.set_sampling_frequency(sf)
        js.set_reduction_frequency(rf)
        js.set_current_range(ir)
        return _one_shot_measure("current", "avg", duration_s,
                                 "CURR:AVG", "A", lo, hi)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
