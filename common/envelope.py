"""Common envelope helpers shared by mcp_34465a and mcp_js220.

The standard result envelope (test/value/unit/lower/upper/pass/
timestamp) is the unifying contract across all three instrument
packages — see `BUILDING_AN_INSTRUMENT_MCP.md` §5.2 and
`mcp_8852b/reference/_ble_msm_samples/Capture.txt` for the original
empirical observations.

mcp_8852b does NOT use this helper (its TxTestResult dataclass has
a different shape — it adds header/channel/capture_type/raw_fields/raw
on top of the 7 base fields), so the helper is a sibling of
mcp_8852b/, not a parent of it.
"""
from __future__ import annotations
import time
from typing import Optional


def ok(**fields) -> dict:
    """Wrap a payload in {"ok": True, ...fields}."""
    return {"ok": True, **fields}


def err(msg: str) -> dict:
    """Standard error envelope: {"ok": False, "error": msg}."""
    return {"ok": False, "error": msg}


def envelope(test: str, value, unit: str,
             lower: Optional[float], upper: Optional[float],
             passed: Optional[bool] = None) -> dict:
    """Build the standard 7-key result envelope.

    Args:
        test: SCPI function string (e.g. "VOLT:DC", "CURR:AVG")
              or measurement label.
        value: measured value (number).
        unit: unit string (e.g. "V", "A", "°C").
        lower: pass-window lower bound (or None for unknown).
        upper: pass-window upper bound (or None for unknown).
        passed: explicit pass/fail. If None, computed from
                `lower <= value <= upper`. Stays None if either
                bound is None.
    """
    if passed is None and lower is not None and upper is not None:
        passed = lower <= value <= upper
    return {
        "test": test,
        "value": value,
        "unit": unit,
        "lower": lower,
        "upper": upper,
        "pass": passed,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
