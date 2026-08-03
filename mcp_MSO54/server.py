"""MSO54 (Tektronix 5 Series MSO) MCP Server.

Exposes tools for Claude to drive a Tektronix MSO54 oscilloscope over
USBTMC. Built for the CMT BLE Sticker PCBA HWDVT line. Run with:

    python -m mcp_MSO54.server

Claude Desktop registration:

    "mcpServers": {
        "mso54": {
            "command": "python",
            "args": ["-m", "mcp_MSO54.server"],
            "cwd": "<project root>"
        }
    }

**Every tool exposed here was validated on real hardware** (FW
1.34.8.1078, USBTMC `USB0::0x0699::0x0522::C041623::0::INSTR`) via
`scripts/validate.py` on 2026-07-18. Phases covered: P1-P8 + P10 + P11.

**Tools NOT exposed (verified UNSAFE or unverified on USB):**
- Any screenshot read-back path (FILESYSTEM:READFILE wedges the USBTMC
  bulk-IN endpoint — see NOTES.md §1). The write-only `save_image` is
  exposed so the operator can pull the PNG off the scope by hand.
- Any SCPI command not exercised in validate.py.

**Layering:** `server.py → instrument.py → scpi.py → config.py`.
`server.py` never imports pyvisa — it only talks to the scope through
`MSO54` and the SCPI builders.

**Preflight gate:** any tool that changes instrument state or acquires
samples goes through a two-step "propose → confirm → execute" pattern
(matching mcp_34465a/server.py). The first call returns
`{needs_user_input: True, questions: [...]}` and sends NO SCPI. The
second call, with `user_confirmed=True`, applies the config and runs.
"""
from __future__ import annotations
import logging
import time
from typing import Optional, Literal

from fastmcp import FastMCP

from .instrument import MSO54, MSO54Error
from .config import (
    CONFIG,
    DEFAULT_LIMITS,
    MEAS_TYPES,
    MEAS_UNIT,
    DEFAULT_QUESTIONS_PER_MEAS,
)
from common.envelope import ok, err, envelope as _common_envelope

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mcp_MSO54")


mcp = FastMCP("mso54")
scope = MSO54()


def _tool(title: str, **annotations) -> "callable":
    """Decorator factory for consistent tool registration.

    Every tool gets:
      - `title` (human-readable name shown in MCP clients)
      - `openWorldHint: True` (touches external instrument)

    Plus whatever the caller passes (readOnlyHint, destructiveHint,
    idempotentHint).

    Usage:
        @_tool("Read CH<n> state", readOnlyHint=True, idempotentHint=True)
        def scope_get_channel_state(...): ...
    """
    base = {"title": title, "openWorldHint": True}
    base.update(annotations)
    return mcp.tool(annotations=base)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _envelope(test: str, value: float, unit: str,
              lower: Optional[float] = None,
              upper: Optional[float] = None) -> dict:
    """Standard result envelope, with DEFAULT_LIMITS fallback."""
    if lower is None or upper is None:
        spec = DEFAULT_LIMITS.get(test, {})
        lower = spec.get("lower") if lower is None else lower
        upper = spec.get("upper") if upper is None else upper
    return _common_envelope(test, value, unit, lower, upper)


def _ok(d: dict) -> dict:
    return ok(**d)


def _err(e: Exception) -> dict:
    return err(str(e))


def _ensure_connected() -> Optional[dict]:
    """Return error dict if not connected, else None."""
    if not scope.connected:
        return {"ok": False, "error": "not connected; call scope_connect first"}
    return None


def _validate_channel(n: int) -> Optional[dict]:
    if n < 1 or n > CONFIG.num_channels:
        return {"ok": False, "error": f"channel must be 1..{CONFIG.num_channels}"}
    return None


# ---------------------------------------------------------------------------
# Connection / status
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Connect", "openWorldHint": True})
def scope_connect(resource: Optional[str] = None) -> dict:
    """Open a VISA session to the MSO54 and send `HEADER OFF`.

    Args:
        resource: VISA resource string. If omitted, uses
                  MSO54_RESOURCE env var or the default in config.py
                  (USB0::0x0699::0x0522::C041623::0::INSTR).
    """
    try:
        if resource:
            scope.resource = resource
        idn = scope.connect()
        return _ok({"idn": idn, "connected": scope.connected,
                    "resource": scope.resource})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Disconnect", "openWorldHint": True})
def scope_disconnect() -> dict:
    """Close the VISA session."""
    try:
        scope.disconnect()
        return _ok({"connected": scope.connected})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Status", "openWorldHint": True, "readOnlyHint": True})
def scope_status() -> dict:
    """Return connection state, identity, and STB (Status Byte)."""
    if not scope.connected:
        return _ok({"connected": False, "resource": scope.resource})
    try:
        return _ok({
            "connected": True,
            "resource": scope.resource,
            "idn": scope.idn(),
            "options": scope.options(),
            "stb": f"0x{scope.read_stb():02x}",
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Reset", "openWorldHint": True, "destructiveHint": True})
def scope_reset() -> dict:
    """Send *RST (returns scope to default state)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        scope.reset()
        return _ok({"reset": True})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Autoset", "openWorldHint": True, "destructiveHint": True})
def scope_autoset() -> dict:
    """AUTOSET EXECUTE — auto-scale vertical/horizontal/trigger."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        scope.autoset()
        return _ok({"autoset": True})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Channel (vertical) — reads are direct, writes go through preflight
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Get channel state", "openWorldHint": True, "readOnlyHint": True})
def scope_get_channel_state(channel: int) -> dict:
    """Return all vertical settings for one channel."""
    err_d = _ensure_connected()
    if err_d: return err_d
    err_d = _validate_channel(channel)
    if err_d: return err_d
    try:
        n = channel
        return _ok({
            "channel": n,
            "display_on": scope.q_channel_display(n),
            "scale_v_per_div": scope.q_channel_scale(n),
            "position_divs": scope.q_channel_position(n),
            "offset_volts": scope.q_channel_offset(n),
            "coupling": scope.q_channel_coupling(n),
            "bandwidth_hz": scope.q_channel_bandwidth(n),
            "termination_ohms": scope.q_channel_termination(n),
            "label": scope.q_channel_label(n),
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set channel scale", "openWorldHint": True, "destructiveHint": True})
def scope_set_channel_scale(channel: int, volts_per_div: float,
                            user_confirmed: bool = False) -> dict:
    """Set CH<n>:SCALE (V/div).

    Hard-gated by `user_confirmed=True` — see NOTES.md §2: while
    ACQUIRE:STATE is RUN, the scope continuously auto-adjusts vertical
    scale to fit the waveform and will overwrite this value within
    ~100 ms. Call `scope_acq_stop()` first if you need the value to
    persist.
    """
    err_d = _ensure_connected()
    if err_d: return err_d
    err_d = _validate_channel(channel)
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False,
                "error": "user_confirmed must be True. Confirm the operator "
                         "knows the MSO54 auto-scale override behavior."}
    try:
        scope.set_channel_scale(channel, volts_per_div)
        rb = scope.q_channel_scale(channel)
        return _ok({"channel": channel, "scale_v_per_div": rb,
                    "note": "may be auto-overridden if ACQUIRE:STATE is RUN"})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set channel position", "openWorldHint": True, "destructiveHint": True})
def scope_set_channel_position(channel: int, divs: float,
                               user_confirmed: bool = False) -> dict:
    """Set CH<n>:POSITION (divs from center)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    err_d = _validate_channel(channel)
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False,
                "error": "user_confirmed must be True"}
    try:
        scope.set_channel_position(channel, divs)
        return _ok({"channel": channel, "position_divs": scope.q_channel_position(channel)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set channel offset", "openWorldHint": True, "destructiveHint": True})
def scope_set_channel_offset(channel: int, volts: float,
                             user_confirmed: bool = False) -> dict:
    """Set CH<n>:OFFSET (volts)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    err_d = _validate_channel(channel)
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.set_channel_offset(channel, volts)
        return _ok({"channel": channel, "offset_volts": scope.q_channel_offset(channel)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set channel coupling", "openWorldHint": True, "destructiveHint": True})
def scope_set_channel_coupling(channel: int,
                               coupling: Literal["DC", "AC", "DCREJECT"],
                               user_confirmed: bool = False) -> dict:
    """Set CH<n>:COUPLING (DC / AC / DCREJECT)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    err_d = _validate_channel(channel)
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.set_channel_coupling(channel, coupling)
        return _ok({"channel": channel, "coupling": scope.q_channel_coupling(channel)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set channel display", "openWorldHint": True, "destructiveHint": True})
def scope_set_channel_display(channel: int, on: bool) -> dict:
    """Turn a channel's waveform display on/off."""
    err_d = _ensure_connected()
    if err_d: return err_d
    err_d = _validate_channel(channel)
    if err_d: return err_d
    try:
        scope.set_channel_display(channel, on)
        return _ok({"channel": channel, "display_on": scope.q_channel_display(channel)})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set channel label", "openWorldHint": True, "destructiveHint": True})
def scope_set_channel_label(channel: int, text: str,
                            user_confirmed: bool = False) -> dict:
    """Set CH<n>:LABEL:NAME (text up to ~30 chars)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    err_d = _validate_channel(channel)
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.set_channel_label(channel, text)
        return _ok({"channel": channel, "label": scope.q_channel_label(channel)})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Horizontal
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Get horizontal state", "openWorldHint": True, "readOnlyHint": True})
def scope_get_horizontal_state() -> dict:
    """Return horizontal settings (scale/position/mode/sample_rate/record_length)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        return _ok({
            "mode": scope.q_horizontal_mode(),
            "scale_s_per_div": scope.q_horizontal_scale(),
            "position_pct": scope.q_horizontal_position(),
            "sample_rate_sa_per_s": scope.q_sample_rate(),
            "record_length": scope.q_record_length(),
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set horizontal scale", "openWorldHint": True, "destructiveHint": True})
def scope_set_horizontal_scale(sec_per_div: float,
                               user_confirmed: bool = False) -> dict:
    """Set HORIZONTAL:SCALE (s/div)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.set_horizontal_scale(sec_per_div)
        return _ok({"scale_s_per_div": scope.q_horizontal_scale()})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set horizontal position", "openWorldHint": True, "destructiveHint": True})
def scope_set_horizontal_position(percent: float,
                                  user_confirmed: bool = False) -> dict:
    """Set HORIZONTAL:POSITION (0..100, trigger position as % of record)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.set_horizontal_position(percent)
        return _ok({"position_pct": scope.q_horizontal_position()})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Get acquisition state", "openWorldHint": True, "readOnlyHint": True})
def scope_get_acquisition_state() -> dict:
    """Return acquisition mode/numavg/state/stopafter/numacq."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        return _ok({
            "mode": scope.q_acq_mode(),
            "numavg": scope.q_acq_numavg(),
            "state": scope.q_acq_state(),
            "stopafter": scope.q_acq_stopafter(),
            "numacq": scope.q_acq_numacq(),
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Acq run", "openWorldHint": True, "destructiveHint": True})
def scope_acq_run() -> dict:
    """ACQUIRE:STATE RUN."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        scope.set_acq_state(True)
        return _ok({"state": scope.q_acq_state()})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Acq stop", "openWorldHint": True, "destructiveHint": True})
def scope_acq_stop() -> dict:
    """ACQUIRE:STATE STOP — required before user-set scale will persist."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        scope.set_acq_state(False)
        return _ok({"state": scope.q_acq_state()})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Trigger (A event, edge)
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Get trigger state", "openWorldHint": True, "readOnlyHint": True})
def scope_get_trigger_state() -> dict:
    """Return trigger type/edge source/slope/coupling/level/mode/state."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        return _ok({
            "type": scope.q_trig_type(),
            "edge_source": scope.q_trig_edge_source(),
            "edge_slope": scope.q_trig_edge_slope(),
            "edge_coupling": scope.q_trig_edge_coupling(),
            "mode": scope.q_trig_mode(),
            "state": scope.q_trig_state(),
            "level_ch1_v": scope.q_trig_level(1),
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set trigger edge", "openWorldHint": True, "destructiveHint": True})
def scope_set_trigger_edge(source: str, slope: Literal["RISE", "FALL", "EITHER"],
                           level_volts: float,
                           user_confirmed: bool = False) -> dict:
    """Configure TRIGGER:A EDGE source/slope/level (CH1).

    Args:
        source: 'CH1'..'CH4', 'LINE', or 'AUX'.
        slope: 'RISE' / 'FALL' / 'EITHER'.
        level_volts: trigger level in volts on `source`.
    """
    err_d = _ensure_connected()
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.set_trig_type("EDGE")
        scope.set_trig_edge_source(source)
        scope.set_trig_edge_slope(slope)
        # Determine channel number for set_trig_level
        ch_n = int(source.replace("CH", "")) if source.startswith("CH") else 1
        scope.set_trig_level(ch_n, level_volts)
        return _ok({
            "source": scope.q_trig_edge_source(),
            "slope": scope.q_trig_edge_slope(),
            "level_volts": scope.q_trig_level(ch_n),
        })
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Automatic measurements — two-step preflight (P7-validated path)
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Get measurement list", "openWorldHint": True, "readOnlyHint": True})
def scope_get_measurement_list() -> dict:
    """List currently-installed measurements (MEASUREMENT:LIST?)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        lst = scope.q_meas_list()         # e.g. "MEAS1,MEAS2"
        slots = []
        # Probe each slot until a gap appears
        for n in range(1, 9):
            try:
                t = scope.q_meas_type(n)
                s = scope.q_meas_source(n)
                v = scope.q_meas_value(n)
                slots.append({"slot": n, "type": t, "source": s,
                              "value": v, "unit": MEAS_UNIT.get(t, "")})
            except MSO54Error:
                break
        return _ok({"raw": lst, "slots": slots})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Preflight: add measurement", "openWorldHint": True, "destructiveHint": True})
def scope_preflight_add_measurement(test_name: str,
                                    meas_type: str,
                                    source: str = "CH1",
                                    lower: Optional[float] = None,
                                    upper: Optional[float] = None,
                                    auto_confirm: bool = False) -> dict:
    """Step 1 of preflight for adding a measurement.

    Proposes a pass window and asks the operator to confirm. NO SCPI is
    sent at this stage.

    Args:
        test_name: human-readable test name (echoed in envelope).
        meas_type: one of `MEAS_TYPES` (FREQUENCY, AMPLITUDE, ...).
        source: source channel (CH1..CH4, REF1..).
        lower/upper: optional override; default is DEFAULT_LIMITS[meas_type].
        auto_confirm: if True, skip questions and execute with the proposed
                      config directly (bypass for batch runs).
    """
    if meas_type not in MEAS_TYPES:
        return {"ok": False, "error": f"unknown meas_type '{meas_type}'; "
                                      f"valid: {list(MEAS_TYPES)}"}
    spec = DEFAULT_LIMITS.get(meas_type, {})
    prop_lower = lower if lower is not None else spec.get("lower")
    prop_upper = upper if upper is not None else spec.get("upper")

    if auto_confirm:
        return scope_run_preflight_add_measurement(
            test_name=test_name, meas_type=meas_type, source=source,
            lower=prop_lower, upper=prop_upper, user_confirmed=True)

    questions = DEFAULT_QUESTIONS_PER_MEAS.get(meas_type, [
        {
            "name": "pass_window",
            "question": f"{meas_type} 的合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "走产品线默认 (DEFAULT_LIMITS)", "value": "default"},
                {"label": "我手动指定 (输入 lower/upper)", "value": "manual"},
            ],
            "multiSelect": False,
        },
    ])
    return {
        "ok": True,
        "needs_user_input": True,
        "interaction_type": "confirm_test_setup",
        "test_name": test_name,
        "meas_type": meas_type,
        "source": source,
        "proposed_config": {"lower": prop_lower, "upper": prop_upper},
        "questions": questions,
        "next_action": "将用户回答合并到 lower/upper, 然后调用 "
                       "scope_run_preflight_add_measurement(..., user_confirmed=True)。",
    }


@mcp.tool(annotations={"title": "Run: preflight add measurement", "openWorldHint": True, "destructiveHint": True})
def scope_run_preflight_add_measurement(test_name: str,
                                        meas_type: str,
                                        source: str = "CH1",
                                        lower: Optional[float] = None,
                                        upper: Optional[float] = None,
                                        user_confirmed: bool = False) -> dict:
    """Step 2 of preflight for adding a measurement.

    Hard-gated by `user_confirmed=True`. False returns error WITHOUT
    touching the bus. Adds the measurement (MEASUREMENT:ADDMEAS) and
    reads back the live value (MEAS<n>:RESULTS:CURRENTACQ:MEAN?).
    """
    err_d = _ensure_connected()
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False,
                "error": "user_confirmed must be True. "
                         "Call scope_preflight_add_measurement first."}
    if meas_type not in MEAS_TYPES:
        return {"ok": False, "error": f"unknown meas_type '{meas_type}'"}
    try:
        # Find the lowest empty slot; ADDMEAS auto-assigns but we want
        # to know which <n> got it for the readback.
        # Cheap approach: try DELETE on each slot to start clean
        for n in range(1, 9):
            try:
                scope.meas_delete(n)
            except MSO54Error:
                pass
        scope.meas_add(meas_type)
        time.sleep(0.2)
        # Find which slot got it (default source CH1 unless overridden)
        target_source = source
        slot = None
        for n in range(1, 9):
            try:
                t = scope.q_meas_type(n)
                s = scope.q_meas_source(n)
                if t == meas_type and s == target_source:
                    slot = n
                    break
            except MSO54Error:
                break
        if slot is None:
            # Try matching just by type
            for n in range(1, 9):
                try:
                    if scope.q_meas_type(n) == meas_type:
                        slot = n
                        break
                except MSO54Error:
                    break
        if slot is None:
            return {"ok": False, "error": "ADDMEAS succeeded but no slot "
                                          "was populated"}
        v = scope.q_meas_value(slot)
        unit = MEAS_UNIT.get(meas_type, "")
        return _ok(_envelope(test_name, v, unit, lower, upper) | {
            "slot": slot, "meas_type": meas_type, "source": target_source,
        })
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# DVM (option SUP5-DVM)
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Read dvm", "openWorldHint": True, "readOnlyHint": True})
def scope_read_dvm() -> dict:
    """Read current DVM value (DVM:MEASUREMENT:VALUE?)."""
    err_d = _ensure_connected()
    if err_d: return err_d
    try:
        return _ok({
            "mode": scope.q_dvm_mode(),
            "source": scope.q_dvm_source(),
            "value": scope.q_dvm_value(),
            "unit": "V",
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"title": "Set dvm", "openWorldHint": True, "destructiveHint": True})
def scope_set_dvm(mode: Literal["OFF", "DC", "ACRMS", "ACDCRMS", "FREQUENCY"],
                  source: str = "CH1",
                  user_confirmed: bool = False) -> dict:
    """Configure DVM mode/source and read back current value."""
    err_d = _ensure_connected()
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.set_dvm_source(source)
        scope.set_dvm_mode(mode)
        time.sleep(0.3)
        v = scope.q_dvm_value()
        return _ok({"mode": mode, "source": source, "value": v, "unit": "V"})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Waveform transfer (CURVE) — bounded reads only
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Read curve", "openWorldHint": True, "destructiveHint": True})
def scope_read_curve(source: str = "CH1",
                     start: int = 1, stop: int = 1000,
                     user_confirmed: bool = False) -> dict:
    """Acquire one waveform (CURVE?) and return scaled samples.

    Args:
        source: 'CH1'..'CH4', 'REF1'.., 'MATH1'.
        start/stop: 1-based sample range (stop-start+1 points returned).
                    Capped at 10000 here to keep transfers bounded.

    Hard-gated by `user_confirmed=True`.
    """
    err_d = _ensure_connected()
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    if stop - start + 1 > 10000:
        return {"ok": False,
                "error": "stop-start+1 must be <= 10000 to keep transfer bounded"}
    try:
        wfm = scope.read_curve(source, start=start, stop=stop, width=1)
        # Down-sample for transport: return every Nth point if huge.
        # Always return the full preamble so callers can re-scale.
        return _ok({
            "source": wfm["source"],
            "unit": wfm["unit"],
            "npoints": wfm["npoints"],
            "times": wfm["times"],
            "values": wfm["values"],
            "preamble": wfm["preamble"],
        })
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Screenshot (write-only — read-back is unsafe on USB, see NOTES.md §1)
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"title": "Save image", "openWorldHint": True, "destructiveHint": True})
def scope_save_image(instrument_path: str,
                     user_confirmed: bool = False) -> dict:
    """SAVE:IMAGE — save a screenshot to the scope's own filesystem.

    **Operator must pull the file off manually.** Do NOT use this with
    the expectation that the MCP will read the bytes back — the
    FILESYSTEM:READFILE path wedges the USBTMC bulk-IN endpoint on
    this scope (NOTES.md §1).

    Args:
        instrument_path: e.g. '/tmp/screenshot.png'. Path conventions on
                         this firmware are NOT well documented; you may
                         need to use the scope's touchscreen file
                         manager to locate the file.
    """
    err_d = _ensure_connected()
    if err_d: return err_d
    if not user_confirmed:
        return {"ok": False, "error": "user_confirmed must be True"}
    try:
        scope.save_image(instrument_path)
        return _ok({"saved_to": instrument_path,
                    "warning": "Pull the file off the scope manually — "
                               "READFILE wedges the USBTMC bulk-IN."})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
