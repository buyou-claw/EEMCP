"""Keysight 34465A MCP Server.

Exposes atomic tools for Claude to drive a 34465A 6½-digit digital
multimeter. Designed to be paired with a DUT-control skill (not included
here) that powers the PCBA, switches test points, etc.

Run with:  python -m mcp_34465a.server
Add to Claude Desktop config:

  "mcpServers": {
    "dmm34465a": {
      "command": "python",
      "args": ["-m", "mcp_34465a.server"],
      "cwd": "<project root, e.g. E:\\\\other\\\\ai_project\\\\EEMCP>"
    }
  }
"""
from __future__ import annotations
import logging
from typing import Optional, Literal

from fastmcp import FastMCP

from .instrument import DMM34465A, DMM34465AError
from .config import CONFIG, DEFAULT_LIMITS, MEAS_FUNCTIONS, DEFAULT_QUESTIONS_PER_FUNCTION
from common.envelope import ok, err, envelope as _common_envelope

# JSON-Schema-friendly Literal of the supported 34465A measurement
# functions. The runtime set is `MEAS_FUNCTIONS` (also imported above),
# but exposing this as a Literal lets FastMCP advertise the valid
# values in the inputSchema — LLM clients see a constrained string
# dropdown instead of a free-form "string" with no hints.
_MeasFn = Literal[
    "VOLT:DC", "VOLT:AC", "CURR:DC", "CURR:AC", "RES", "FRES",
    "FREQ", "PER", "CONT", "DIOD", "CAP",
    "TEMP:RTD", "TEMP:THER", "TEMP:TC",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("mcp_34465a")

mcp = FastMCP("dmm34465a")
dmm = DMM34465A()

# Per-function unit. Used by both `dmm_measure` and `dmm_scan_test_points`
# so the result envelope is consistent regardless of entry point.
_UNIT_MAP = {
    "VOLT:DC": "V", "VOLT:AC": "V",
    "CURR:DC": "A", "CURR:AC": "A",
    "RES": "Ω", "FRES": "Ω",
    "FREQ": "Hz", "PER": "s",
    "CONT": "Ω", "DIOD": "V", "CAP": "F",
    "TEMP:RTD": "°C", "TEMP:THER": "°C", "TEMP:TC": "°C",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _envelope(test: str, value: float, unit: str,
              lower: Optional[float] = None,
              upper: Optional[float] = None) -> dict:
    """Standard result envelope — matches the mcp_8852b shape so the AI can
    consume results uniformly across instruments.

    Implementation lives in `common.envelope` (shared with mcp_js220).
    This wrapper adds the DEFAULT_LIMITS fallback (per-function default
    pass window) that the common helper doesn't know about.
    """
    if lower is None or upper is None:
        spec = DEFAULT_LIMITS.get(test, {})
        lower = spec.get("lower") if lower is None else lower
        upper = spec.get("upper") if upper is None else upper
    return _common_envelope(test, value, unit, lower, upper)


def _ok(d: dict) -> dict:
    return ok(**d)


def _err(e: Exception) -> dict:
    return err(str(e))


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def dmm_connect(resource: Optional[str] = None) -> dict:
    """Open a VISA session to the 34465A.

    Args:
        resource: VISA resource string. If omitted, uses
                  DMM34465A_RESOURCE env var or the default in config.py
                  (USB0::0x2A8D::0x0101::MY57515137::0::INSTR).
                  Other examples:
                    TCPIP0::192.168.1.50::5025::SOCKET   (LAN)
                    GPIB0::17::INSTR                      (GPIB)
    """
    try:
        if resource:
            dmm.resource = resource
        idn = dmm.connect()
        return _ok({"idn": idn, "connected": dmm.connected})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def dmm_disconnect() -> dict:
    """Close the VISA session."""
    try:
        dmm.disconnect()
        return _ok({"connected": dmm.connected})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
def dmm_status() -> dict:
    """Return connection state and instrument identity (queries *IDN? again)."""
    if not dmm.connected:
        return _ok({"connected": False, "resource": dmm.resource})
    try:
        return _ok({"connected": True, "resource": dmm.resource, "idn": dmm.idn()})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def dmm_reset() -> dict:
    """Send *RST to the 34465A. Returns it to power-on defaults."""
    try:
        dmm.reset()
        return _ok({"reset": True})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Per-function measurement tools
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP. The MCP test surface is `dmm_run_preflight_measure`
# (after `dmm_preflight_measure` has proposed). This function is the
# Python-library implementation, importable by scripts and `verify_preflight.py`:
#   from mcp_34465a.server import dmm_measure_volt_dc  # noqa
# Re-adding @mcp.tool() here is intentionally a no-op — the gate is
# enforced by removing the decorator, not by a runtime check (scripts
# that import this directly bypass the gate by design).
def dmm_measure_volt_dc(lower: Optional[float] = None,
                        upper: Optional[float] = None) -> dict:
    """Measure DC voltage (V). Auto-range, one-shot."""
    try:
        v = dmm.measure_volt_dc()
        return _ok(_envelope("VOLT:DC", v, "V", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_volt_ac(lower: Optional[float] = None,
                        upper: Optional[float] = None) -> dict:
    """Measure AC voltage (V RMS). Auto-range, one-shot."""
    try:
        v = dmm.measure_volt_ac()
        return _ok(_envelope("VOLT:AC", v, "V", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_curr_dc(lower: Optional[float] = None,
                        upper: Optional[float] = None) -> dict:
    """Measure DC current (A). Auto-range, one-shot."""
    try:
        v = dmm.measure_curr_dc()
        return _ok(_envelope("CURR:DC", v, "A", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_curr_ac(lower: Optional[float] = None,
                        upper: Optional[float] = None) -> dict:
    """Measure AC current (A RMS). Auto-range, one-shot."""
    try:
        v = dmm.measure_curr_ac()
        return _ok(_envelope("CURR:AC", v, "A", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_resistance(lower: Optional[float] = None,
                           upper: Optional[float] = None) -> dict:
    """Measure 2-wire resistance (Ω). Auto-range, one-shot."""
    try:
        v = dmm.measure_res()
        return _ok(_envelope("RES", v, "Ω", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_resistance_4w(lower: Optional[float] = None,
                              upper: Optional[float] = None) -> dict:
    """Measure 4-wire (Kelvin) resistance (Ω). Auto-range, one-shot.

    Use the 4-wire inputs (rear panel) on the 34465A for low-resistance
    measurements (< 10 Ω) to cancel out lead resistance.
    """
    try:
        v = dmm.measure_fres()
        return _ok(_envelope("FRES", v, "Ω", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_frequency(lower: Optional[float] = None,
                          upper: Optional[float] = None) -> dict:
    """Measure frequency (Hz). Auto-range, one-shot."""
    try:
        v = dmm.measure_freq()
        return _ok(_envelope("FREQ", v, "Hz", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_period(lower: Optional[float] = None,
                       upper: Optional[float] = None) -> dict:
    """Measure period (s). Auto-range, one-shot."""
    try:
        v = dmm.measure_per()
        return _ok(_envelope("PER", v, "s", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_continuity(lower: Optional[float] = None,
                           upper: Optional[float] = None) -> dict:
    """Measure continuity (Ω). The 34465A emits a beep below the threshold.

    Default pass window is ≤ 10 Ω (DEFAULT_LIMITS['CONT']). Override
    lower/upper per call, or change the instrument threshold with
    `dmm_set_continuity_threshold` (in ohms).
    """
    try:
        v = dmm.measure_cont()
        return _ok(_envelope("CONT", v, "Ω", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_diode(lower: Optional[float] = None,
                      upper: Optional[float] = None) -> dict:
    """Measure diode forward voltage (V). One-shot."""
    try:
        v = dmm.measure_diode()
        return _ok(_envelope("DIOD", v, "V", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_capacitance(lower: Optional[float] = None,
                            upper: Optional[float] = None) -> dict:
    """Measure capacitance (F). Auto-range, one-shot."""
    try:
        v = dmm.measure_cap()
        return _ok(_envelope("CAP", v, "F", lower, upper))
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure_temperature(probe: Literal["RTD", "THER", "TC"] = "RTD",
                            lower: Optional[float] = None,
                            upper: Optional[float] = None) -> dict:
    """Measure temperature (°C).

    Args:
        probe: temperature transducer type:
            - "RTD"  : RTD (default 10 kΩ; configure with `:TEMP:RTD:TYPE`)
            - "THER" : thermistor (default 10 kΩ; configure similarly)
            - "TC"   : thermocouple (default type J; configure with
                       `:TEMP:TC:TYPE`)
        lower/upper: optional override of the pass window (°C).
    """
    try:
        if probe == "RTD":
            v = dmm.measure_temp_rtd()
            test = "TEMP:RTD"
        elif probe == "THER":
            v = dmm.measure_temp_ther()
            test = "TEMP:THER"
        elif probe == "TC":
            v = dmm.measure_temp_tc()
            test = "TEMP:TC"
        else:
            return {"ok": False, "error": f"unknown probe type: {probe}"}
        return _ok(_envelope(test, v, "°C", lower, upper))
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Generic dispatcher
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_measure(function: _MeasFn,
                lower: Optional[float] = None,
                upper: Optional[float] = None) -> dict:
    """Generic one-shot measurement by SCPI function name.

    Use this when the typed helpers don't cover what you need.
    Args:
        function: one of MEAS_FUNCTIONS, e.g. "VOLT:DC", "FRES",
                  "TEMP:TC", ...
        lower/upper: optional pass-window override.
    """
    if function not in MEAS_FUNCTIONS:
        return {"ok": False,
                "error": f"unknown function '{function}'; "
                         f"valid: {list(MEAS_FUNCTIONS)}"}
    try:
        v = dmm.measure(function)
        return _ok(_envelope(function, v, _UNIT_MAP.get(function, ""),
                             lower, upper))
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_set_nplc(function: _MeasFn, nplc: float) -> dict:
    """Set integration time (NPLC) for a function.

    Args:
        function: SCPI function ("VOLT:DC", "CURR:DC", ...)
        nplc: 0.001..1000. Use 0.02 for fast, 1 for default, 10/100 for
              high-resolution/high-accuracy.
    """
    try:
        dmm.set_nplc(function, nplc)
        return _ok({"function": function, "nplc": nplc})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_set_resolution(function: _MeasFn, digits: float) -> dict:
    """Set display resolution in digits for a function (4..10 for 34465A)."""
    try:
        dmm.set_resolution(function, digits)
        return _ok({"function": function, "digits": digits})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_set_continuity_threshold(ohms: float) -> dict:
    """Set continuity beep/limit threshold (1..1000 Ω). Default is 10 Ω."""
    try:
        dmm.set_continuity_threshold(ohms)
        return _ok({"threshold_ohms": ohms})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above.
def dmm_set_beep(state: Literal["ON", "OFF"]) -> dict:
    """Enable/disable the front-panel beep for continuity / diode limits."""
    try:
        dmm.set_beep(state)
        return _ok({"beep": state})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# High-level: scan a list of test points
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP — see dmm_measure_volt_dc() above. Batch scans
# are a per-test operator decision; route each test point through
# `dmm_preflight_measure` / `dmm_run_preflight_measure` instead, or
# call this function directly from a script with the operator's plan.
def dmm_scan_test_points(
    points: list[dict],
) -> dict:
    """Run a batch of one-shot measurements and return a structured report.

    Each item in `points` is a dict:
        {
          "name":   "VCC_MAIN",            # required
          "function": "VOLT:DC",            # required (one of MEAS_FUNCTIONS)
          "lower": 3.0,                     # optional
          "upper": 3.6                      # optional
        }

    Example:
        dmm_scan_test_points([
            {"name": "VCC_MAIN",  "function": "VOLT:DC", "lower": 3.0, "upper": 3.6},
            {"name": "VBAT",      "function": "VOLT:DC", "lower": 2.7, "upper": 3.3},
            {"name": "ANT_RF",    "function": "RES",     "lower": 0,   "upper": 5},
            {"name": "TEST_LED",  "function": "DIOD",    "lower": 1.5, "upper": 2.5},
        ])
    """
    if not dmm.connected:
        return {"ok": False, "error": "not connected; call dmm_connect first"}
    if not points:
        return {"ok": False, "error": "points list is empty"}
    results = []
    for p in points:
        name = p.get("name")
        fn = p.get("function")
        if not name or not fn:
            results.append({"ok": False, "name": name, "error": "missing name/function"})
            continue
        if fn not in MEAS_FUNCTIONS:
            results.append({"ok": False, "name": name, "error": f"unknown function {fn}"})
            continue
        try:
            v = dmm.measure(fn)
            unit = _UNIT_MAP.get(fn, "")
            env = _envelope(fn, v, unit, p.get("lower"), p.get("upper"))
            env["name"] = name
            env["ok"] = True
            results.append(env)
        except Exception as e:
            results.append({"ok": False, "name": name, "error": str(e)})
    overall_pass = all(r.get("pass") is True for r in results)
    return {"ok": True, "overall_pass": overall_pass, "points": results}


# ---------------------------------------------------------------------------
# Interactive preflight (added per INTERACTIVE_DESIGN.md § 4.1)
# ---------------------------------------------------------------------------
# These tools implement the two-step "confirm-test-setup" pattern:
#
#   1. `dmm_preflight_measure(...)`  — propose a config + ask the user
#      via `needs_user_input=true`. **No SCPI is sent** at this stage —
#      it's a pure offline interaction request the AI client renders with
#      `AskUserQuestion`. Idempotent: call it as many times as you like
#      to "look at the proposal again" before committing.
#
#   2. `dmm_run_preflight_measure(...)` — apply the user-confirmed
#      config, run the measurement, return the standard envelope.
#      **Hard-gated by `user_confirmed=True`**: if False, returns error
#      without touching the bus.
#
# Bypass: pass `auto_confirm=True` to step 1 to skip the questions
# entirely and apply the proposed config directly. Use this for batch /
# scripted runs where the operator has already chosen the test plan.

# Dispatch table: SCPI function string -> DMM34465A one-shot method.
# Only SCPI-once functions are listed; the dispatch falls back to
# `dmm.measure(function)` (which still works) for everything else.
_PREFLIGHT_MEASURE_DISPATCH = {
    "VOLT:DC":   lambda d: d.measure_volt_dc,
    "VOLT:AC":   lambda d: d.measure_volt_ac,
    "CURR:DC":   lambda d: d.measure_curr_dc,
    "CURR:AC":   lambda d: d.measure_curr_ac,
    "RES":       lambda d: d.measure_res,
    "FRES":      lambda d: d.measure_fres,
    "FREQ":      lambda d: d.measure_freq,
    "PER":       lambda d: d.measure_per,
    "CONT":      lambda d: d.measure_cont,
    "DIOD":      lambda d: d.measure_diode,
    "CAP":       lambda d: d.measure_cap,
    "TEMP:RTD":  lambda d: d.measure_temp_rtd,
    "TEMP:THER": lambda d: d.measure_temp_ther,
    "TEMP:TC":   lambda d: d.measure_temp_tc,
}


def _preflight_questions_for(function: str) -> list[dict]:
    """Return the question template for the given SCPI function.

    Falls back to the generic template (pass_window + nplc) if the
    function isn't in DEFAULT_QUESTIONS_PER_FUNCTION. The list is a
    shallow copy so callers may mutate freely without polluting config.
    """
    if function in DEFAULT_QUESTIONS_PER_FUNCTION:
        return list(DEFAULT_QUESTIONS_PER_FUNCTION[function])
    # Generic fallback
    return [
        {
            "name": "pass_window",
            "question": f"{function} 的合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "走产品线默认 (DEFAULT_LIMITS)", "value": "default"},
                {"label": "我手动指定 (输入 lower/upper)", "value": "manual"},
            ],
            "multiSelect": False,
        },
        {
            "name": "nplc",
            "question": "积分时间 NPLC?",
            "header": "NPLC",
            "options": [
                {"label": "1 (默认, ~20Hz, 6½位)", "value": 1.0},
                {"label": "10 (高准确度, ~2Hz)", "value": 10.0},
                {"label": "0.001 (快速, ~228Hz)", "value": 0.001},
            ],
            "multiSelect": False,
        },
    ]


def _preflight_rationale(function: str, lower: float, upper: float) -> str:
    """One-paragraph Chinese rationale explaining why we propose the
    given window. The intent is for the AI client to show this to the
    user so they understand the trade-off before confirming.
    """
    spec = DEFAULT_LIMITS.get(function, {})
    default_lo, default_hi = spec.get("lower"), spec.get("upper")
    function_chinese = {
        "VOLT:DC":   "DC 电压",   "VOLT:AC": "AC 电压",
        "CURR:DC":   "DC 电流",   "CURR:AC": "AC 电流",
        "RES":       "2-wire 电阻", "FRES": "4-wire 电阻",
        "FREQ":      "频率",      "PER": "周期",
        "CONT":      "通断",      "DIOD": "二极管正向压降",
        "CAP":       "电容",
        "TEMP:RTD":  "温度 (RTD)", "TEMP:THER": "温度 (热敏电阻)",
        "TEMP:TC":   "温度 (热电偶)",
    }.get(function, function)
    if default_lo is not None and (lower, upper) != (default_lo, default_hi):
        return (f"{function_chinese}测量: DEFAULT_LIMITS['{function}']="
                f"[{default_lo}, {default_hi}] 是产品线宽松值, "
                f"已提议收紧到 [{lower}, {upper}] 以贴合本测试点的实际规格。")
    return (f"{function_chinese}测量: 提议区间 [{lower}, {upper}], "
            f"与 DEFAULT_LIMITS['{function}'] 一致。")


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
def dmm_preflight_measure(test_name: str,
                          function: _MeasFn,
                          lower: Optional[float] = None,
                          upper: Optional[float] = None,
                          nplc: Optional[float] = None,
                          resolution: Optional[float] = None,
                          auto_range: bool = True,
                          auto_confirm: bool = False) -> dict:
    """Step 1 of the interactive preflight flow.

    Propose a measurement configuration for `test_name` on `function`,
    return a structured interaction request the AI client renders with
    `AskUserQuestion`. **No SCPI commands are sent** in this step —
    the DMM is not touched in any way. The proposal is idempotent and
    can be called multiple times to "look at it again".

    Args:
        test_name:    human-readable test name (e.g. "VCC_MAIN")
        function:     SCPI function string ("VOLT:DC", "RES", "DIOD", ...)
        lower/upper:  optional override; default is DEFAULT_LIMITS[function]
        nplc:         optional NPLC override; default 1.0
        resolution:   optional display-digits override; default None
                      (use the instrument's own default)
        auto_range:   whether the DMM should auto-range (default True)
        auto_confirm: if True, skip the questions and execute the
                      proposed config directly (bypass mode for
                      batch / scripted runs). Default False.

    Returns (auto_confirm=False):
        {
            "ok": true,
            "needs_user_input": true,
            "interaction_type": "confirm_test_setup",
            "test_name": ...,
            "function": ...,
            "proposed_config": {lower, upper, nplc, resolution, auto_range},
            "rationale": "...",
            "questions": [...],
            "next_action": "..."
        }

    Returns (auto_confirm=True): standard envelope (no questions).
    """
    if function not in MEAS_FUNCTIONS:
        return {"ok": False,
                "error": f"unknown function '{function}'; "
                         f"valid: {list(MEAS_FUNCTIONS)}"}

    # Fill proposed config from defaults
    spec = DEFAULT_LIMITS.get(function, {})
    prop_lower = lower if lower is not None else spec.get("lower")
    prop_upper = upper if upper is not None else spec.get("upper")
    prop_nplc = nplc if nplc is not None else 1.0
    prop_resolution = resolution  # may be None

    proposed_config = {
        "lower": prop_lower,
        "upper": prop_upper,
        "nplc": prop_nplc,
        "resolution": prop_resolution,
        "auto_range": auto_range,
    }
    rationale = _preflight_rationale(
        function,
        prop_lower if prop_lower is not None else 0.0,
        prop_upper if prop_upper is not None else 0.0,
    )

    # Bypass mode: apply proposed config and execute, return envelope.
    if auto_confirm:
        return dmm_run_preflight_measure(
            test_name=test_name,
            function=function,
            lower=prop_lower if prop_lower is not None else 0.0,
            upper=prop_upper if prop_upper is not None else 0.0,
            nplc=prop_nplc,
            resolution=prop_resolution,
            auto_range=auto_range,
            user_confirmed=True,  # auto_confirm implies consent
        )

    # Interaction mode: NO SCPI. Just shape the proposal.
    questions = _preflight_questions_for(function)
    next_action = (
        "将用户在 questions 中的回答合并到 proposed_config, "
        "然后调用 dmm_run_preflight_measure(..., user_confirmed=True) 执行测量。"
    )
    return {
        "ok": True,
        "needs_user_input": True,
        "interaction_type": "confirm_test_setup",
        "test_name": test_name,
        "function": function,
        "proposed_config": proposed_config,
        "rationale": rationale,
        "questions": questions,
        "next_action": next_action,
    }


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def dmm_run_preflight_measure(test_name: str,
                              function: _MeasFn,
                              lower: float,
                              upper: float,
                              nplc: float = 1.0,
                              resolution: Optional[float] = None,
                              auto_range: bool = True,
                              user_confirmed: bool = False) -> dict:
    """Step 2 of the interactive preflight flow.

    Apply the user-confirmed measurement configuration and execute one
    shot. Returns the standard envelope (same shape as
    `dmm_measure_volt_dc`, `dmm_measure_resistance`, etc.).

    Hard gate: `user_confirmed` MUST be `True` to execute. The default
    is `False`, in which case the call returns an error WITHOUT
    touching the bus. This prevents an over-eager client from running
    a measurement the user hasn't reviewed.

    Args:
        test_name:    human-readable test name (echoed in error messages)
        function:     SCPI function string ("VOLT:DC", "RES", ...)
        lower/upper:  the user-confirmed pass window
        nplc:         integration time (default 1.0)
        resolution:   optional display-digits override
        auto_range:   whether the DMM should auto-range (default True)
        user_confirmed: MUST be True to execute. False returns error.

    Returns: standard envelope `{ok, test, value, unit, lower, upper, pass, ...}`.
    """
    # Hard gate — the user MUST have explicitly confirmed.
    if not user_confirmed:
        return {"ok": False,
                "error": "user_confirmed must be True to execute. "
                         "Call dmm_preflight_measure first and confirm with the user."}
    if function not in MEAS_FUNCTIONS:
        return {"ok": False,
                "error": f"unknown function '{function}'; "
                         f"valid: {list(MEAS_FUNCTIONS)}"}
    if not dmm.connected:
        return {"ok": False,
                "error": "not connected; call dmm_connect first"}
    try:
        # Apply nplc (always — it's the integration time)
        dmm.set_nplc(function, nplc)
        # Apply resolution if provided
        if resolution is not None:
            dmm.set_resolution(function, resolution)
        # Dispatch to the right measure_* helper
        dispatch = _PREFLIGHT_MEASURE_DISPATCH.get(function)
        if dispatch is not None:
            v = dispatch(dmm)()
        else:
            v = dmm.measure(function)
        unit = _UNIT_MAP.get(function, "")
        return _ok(_envelope(function, v, unit, lower, upper))
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
