"""Anritsu MT8852B MCP Server.

Exposes atomic tools for Claude to drive an Anritsu MT8852B Bluetooth
Test Set. One tool per BLE TX measurement (LEOP, LEICD, LEMI), plus the
connection / configuration helpers and a high-level
`bt_run_tx_test` that does the full Configure → Start → Wait → Read
recipe.

The MT8852B is NOT a standard SCPI instrument — see `scpi.py` for the
actual command set, and `NOTES.md` for the pitfalls.

Run with:  python -m mcp_8852b.server
Claude Desktop config:

    "mcpServers": {
      "mt8852b": {
        "command": "python",
        "args": ["-m", "mcp_8852b.server"],
        "cwd": "<project root, e.g. E:\\\\other\\\\ai_project\\\\EEMCP>"
      }
    }
"""
from __future__ import annotations
import time
import logging
from typing import Optional, Literal

from fastmcp import FastMCP

from .instrument import MT8852B, MT8852BError, OResult
from .state_machine import (
    MT8852BSession, State, TxTestPlan, TxTestResult,
)
from .config import CONFIG
from . import scpi
from .interactive import (
    Preflight, run_preflight, run_campaign,
    make_bench_log_handler, NeedInput,
)
from .report import TEST_CATALOG

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("mcp_8852b")

mcp = FastMCP("mt8852b")
session = MT8852BSession()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ok(d: dict) -> dict:
    return {"ok": True, **d}


def _err(e: Exception) -> dict:
    return {"ok": False, "error": str(e)}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _check_field(r: OResult, idx: int) -> str:
    if idx >= len(r.fields):
        raise MT8852BError(
            f"ORESULT for {r.header} has only {len(r.fields)} data fields; "
            f"needed index {idx}. Raw: {r.raw!r}")
    return r.fields[idx]


def _parse_float(s: str) -> float:
    """Anritsu emits some fields in scientific notation (e.g. '2.717e+005').
    float() handles those fine."""
    return float(s)


def _envelope_tx(test: str, header: str, value: float, unit: str,
                 lower: Optional[float], upper: Optional[float],
                 plan: TxTestPlan, r: OResult,
                 raw_fields: list[str]) -> TxTestResult:
    passed: Optional[bool]
    if lower is None or upper is None:
        passed = None
    else:
        passed = lower <= value <= upper
    if passed and r.pass_fail == "FAIL":
        passed = False
    if r.valid is False:
        passed = None   # don't claim pass when results are invalid
    return TxTestResult(
        test=test,
        header=header,
        channel=plan.channel,
        capture_type=plan.capture_type,
        value=value,
        unit=unit,
        lower=lower, upper=upper,
        pass_=passed,
        raw_fields=raw_fields,
        raw_text=r.raw,
        timestamp=_now(),
    )


# Bluetooth-spec default pass windows. These are TIGHT spec limits
# (RF-PHY.TS.p21 §6.2); the user should override per-DUT where needed.
DEFAULT_LIMITS = {
    # LEOP: avg power in dBm. RF-PHY.TS.p21 §6.2.1 — "≥ -20 dBm" min.
    # The actual upper bound depends on the DUT's PA / FCC limits;
    # 20 dBm is a generous default for the BLE Sticker.
    "LEOP":   {"lower": -20.0, "upper": 20.0, "unit": "dBm"},
    # LEICD: average Fn (carrier-frequency offset) in Hz. ±150 kHz is
    # the spec value for BLE 1M/2M; we keep it in Hz here.
    "LEICD":  {"lower": -150000, "upper": 150000, "unit": "Hz"},
    # LEMI: Δf1 average in Hz. Spec minimum is 185 kHz; no max in spec.
    "LEMI":   {"lower": 185000.0, "upper": None, "unit": "Hz"},
}


def _limits_for(header: str) -> dict:
    return DEFAULT_LIMITS.get(header, {})


# ---------------------------------------------------------------------------
# Connection / housekeeping
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def bt_connect(resource: Optional[str] = None) -> dict:
    """Open a VISA session to the MT8852B.

    Args:
        resource: VISA resource string. If omitted, uses
                  MT8852B_RESOURCE env var or the default in config.py
                  (GPIB0::27::INSTR).
    """
    try:
        if resource:
            session.instr.resource = resource
        idn = session.connect()
        return _ok({"idn": idn, "state": session.state.value,
                    "resource": session.instr.resource})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def bt_disconnect() -> dict:
    """Close the VISA session."""
    try:
        session.disconnect()
        return _ok({"connected": False})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
def bt_status() -> dict:
    """Return the current session state and configured plan (if any)."""
    try:
        return _ok(session.status())
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP. The MCP test surface is `bt_run_full_suite`
# (see server.py:bt_run_full_suite). This function is the Python-library
# implementation, importable by run_ble_full_suite.py and other scripts:
#   from mcp_8852b.server import session, bt_reset  # noqa
# Re-adding @mcp.tool() here is intentionally a no-op — the gate is
# enforced by removing the decorator, not by a runtime check (scripts
# that import this directly bypass the gate by design).
def bt_reset() -> dict:
    """Send *RST to the MT8852B and return to IDLE."""
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected"}
        session.instr.reset()
        session.state = State.IDLE
        session.plan = None
        return _ok({"reset": True, "state": session.state.value})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see bt_reset() above.
def bt_set_eut_interface(
    interface: Literal["RS232", "USB", "BLE2WIRE", "USBBLE2WIRE", "USBADAPTOR"],
    baud: int = 115200,
    handshake: Literal["NONE", "RTSCTS"] = "NONE",
) -> dict:
    """Tell the MT8852B how it's wired to the DUT (front-panel "EUT
    Control" port). Required before any BLE TX test.

    Interfaces:
      - `RS232`         — UART HCI, 4-wire, 8N1. Pass `baud` (default
                          115200; manual §5-17 supports 1200 / 2400 /
                          4800 / 9600 / 19200 / 38400 / 57600 / 115200 /
                          230400 / 460800) and `handshake` (NONE /
                          RTSCTS — XON/XOFF is NOT supported by the
                          8852B EUT link).
      - `USB`           — USB HCI. The DUT enumerates as a Bluetooth
                          HCI device on the 8852B's USB port.
      - `BLE2WIRE`      — Anritsu proprietary 2-wire (CC debug-style).
                          Most BLE Sticker designs that don't expose
                          standard HCI use this.
      - `USBBLE2WIRE`   — USB-to-2Wire adapter (USB upstream, 2-wire
                          downstream to DUT).
      - `USBADAPTOR`    — USB-to-serial adapter (CDC ACM).

    `handshake` defaults to NONE; pass `handshake="RTSCTS"` when the
    DUT needs CTS asserted (the official BLE Measurement Software
    v1.20.012 uses RTSCTS for the lab's BLE Sticker DUT — see the
    header dialog).

    Note: changing the interface is sticky. The 8852B will also
    "initialise the EUT for tests" (per manual Ch. 5) — typically a
    HCI_Reset, which means the BLE controller reboots. Allow ~1 s
    before the next TX test.
    """
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected; call bt_connect first"}
        if interface == "RS232" and baud <= 0:
            return {"ok": False, "error": f"RS232 requires positive baud, got {baud}"}
        actual = session.set_eut_interface(interface, baud, handshake)
        return _ok({
            "interface": actual,
            "baud": baud if actual == "RS232" else None,
            "handshake": handshake if actual == "RS232" else None,
            "eut_address": session.eut.eut_address,
            "state": session.state.value,
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def bt_eut_interface() -> dict:
    """Query the current EUT control interface setting and the DUT's
    BD_ADDR (12-hex, all-zero if the 8852B has not yet acquired it)."""
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected"}
        # Re-read both fields so the caller sees fresh values.
        try:
            session.eut.interface = session.instr.eut_interface()
        except Exception:
            pass
        try:
            addr = session.read_eut_addr()
            if addr and addr != "000000000000":
                session.eut.eut_address = addr
        except Exception:
            pass
        return _ok(session.eut.to_dict())
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def bt_diag_eut_link() -> dict:
    """One-shot view of the 8852B↔DUT link state.

    Returns every diagnostic the 8852B can give us about how it's
    talking to the DUT, so a broken link is debuggable without
    running a real test:

      - EUTSRCE      : the active EUT control source (RS232 / USB / ...)
      - EUTRS232     : the configured RS232 baud rate (or "n/a")
      - EUTHANDSHAKE : the active flow-control mode
      - EUTADDR      : the DUT's BD_ADDR (12 hex, "000000000000" if not
                       yet acquired — that's the "link is down" signal)
      - EUTLEFEAT    : the DUT's BLE features bitmap (16 hex)
      - EUTFEAT      : the DUT's classic-BT features bitmap (16 hex)
      - EUTNAME      : the DUT's user-friendly name
      - ERRLST       : the 38-char error latch — the most useful single
                       diagnostic for a broken HCI link. Cleared on read.
      - *INS?        : the 8852B's instrument-status register, with
                       CON/DIS/CMP/INQ/OVT/LEFG bits decoded.

    Use this when bt_run_tx_test fails or the EUT appears stuck.
    """
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected; call bt_connect first"}

        def _q(cmd: str) -> str:
            """Query helper that swallows exceptions so one bad
            query doesn't sink the whole diagnostic."""
            try:
                return session.instr.query(cmd).strip()
            except Exception as e:
                return f"<query failed: {e}>"

        def _split(raw: str, prefix: str) -> str:
            """Strip the 'SYSCFG? FOO,' prefix the 8852B echoes back."""
            return raw.split(",", 1)[-1].strip() if raw else raw

        iface_raw = _q(scpi.eut_src_query())
        iface = _split(iface_raw, "EUTSRCE")
        is_rs232 = iface.upper() == "RS232"

        diag: dict = {
            "eut_interface":  iface,
            "eut_baud":       _q(scpi.eut_rs232_query()) if is_rs232 else "n/a",
            "eut_handshake":  _q(scpi.eut_handshake_query()) if is_rs232 else "n/a",
            "eut_addr":       _split(_q(scpi.eut_addr_query()), "EUTADDR"),
            "eut_le_feat":    _q(scpi.eut_le_feat_query()),
            "eut_feat":       _q(scpi.eut_feat_query()),
            "eut_name":       _q(scpi.eut_name_query()),
            "error_latch":    _q(scpi.system_error()),
        }
        # *INS? decoded
        try:
            ins_val = session.instr.ins()
            diag["ins_raw"] = ins_val
            diag["ins_bits"] = {
                "CON":  bool(ins_val & 0x01),
                "DIS":  bool(ins_val & 0x02),
                "CMP":  bool(ins_val & 0x04),
                "INQ":  bool(ins_val & 0x08),
                "OVT":  bool(ins_val & 0x10),
                "LEFG": bool(ins_val & 0x20),
            }
        except Exception as e:
            diag["ins_error"] = str(e)

        # Heuristic: link-up if EUTADDR is non-zero and not all-zero.
        diag["link_up"] = (
            diag["eut_addr"] not in ("", "000000000000",
                                     "<query failed: >",
                                     "n/a")
            and not diag["eut_addr"].startswith("<query failed")
        )
        if not diag["link_up"]:
            diag["hint"] = (
                "EUTADDR is all-zero — the 8852B has not acquired the "
                "DUT. Call bt_set_eut_interface(...) to re-init, or "
                "check the cable / power / baud rate / handshake. "
                "ERRLST (above) has the last HCI error code."
            )
        return _ok(diag)
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def bt_boot_status() -> dict:
    """Read the last self-test result (BOOTSTATUS?). Useful right after
    connect to confirm the unit is healthy."""
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected"}
        s = session.instr.boot_status()
        return _ok({"boot_status": s})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def bt_ins() -> dict:
    """Read the Instrument Status Register (*INS?) and decode the bits.

    Bit 2 (CMP, value 4) — script/test complete (the one we wait on).
    Bit 0 (CON, value 1) — connected.
    Bit 3 (INQ, value 8) — inquiry complete.
    Bit 1 (DIS, value 2) — disconnected.
    Bit 4 (OVT, value 16) — over-temperature.
    Bit 5 (LEFG, value 32) — BLE frame-gen done.
    """
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected"}
        raw = session.instr.ins()
        bits = {
            "CON":  bool(raw & 0x01),
            "DIS":  bool(raw & 0x02),
            "CMP":  bool(raw & 0x04),
            "INQ":  bool(raw & 0x08),
            "OVT":  bool(raw & 0x10),
            "LEFG": bool(raw & 0x20),
        }
        return _ok({"raw": raw, "bits": bits})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# High-level: full BLE TX test in one call
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP — see bt_reset() above.
def bt_run_tx_test(
    channel: int,
    meas: Literal["LEOP", "LEOP2M", "LEOPLR8", "LEICD", "LEICD2M", "LEICDLR8",
                  "LEMI", "LEMI2M", "LEMILR8"] = "LEOP",
    capture_type: Literal["BLE", "2LE", "BLR"] = "BLE",
    packet_length: int = 37,
    mod_type: Literal["NA", "MOD10101010", "MOD11110000", "MOD11111111"] = "NA",
    range_setting: Literal["AUTO", "1", "2", "3"] = "AUTO",
    timeout_s: float = 30.0,
    lower: Optional[float] = None,
    upper: Optional[float] = None,
) -> dict:
    """Run a single BLE TX measurement end-to-end.

    Performs the full Anritsu recipe:

        SETBLECAPTYP → SYSCFG RANGE → CFGBLECAP → OPMD SCRIPT →
        MEASBLECAPX → RUN → poll *INS? → ORESULT TEST,0,<meas>

    Args:
        channel:    BLE RF channel 0..39.
        meas:       the test to run. LEOP (output power, requires PRBS9),
                    LEICD (carrier drift, requires 10101010 for BLE/2LE
                    or 11111111 for BLR8), LEMI (modulation, requires
                    10101010 or 11110000 for BLE/2LE).
        capture_type: BLE (1 Msym/s), 2LE (2 Msym/s), BLR (coded S=2/S=8).
        packet_length: payload length in bytes (2..255).
        mod_type:   only used for LEMI*. Set NA for power / CFO tests.
        range_setting: AUTO (recommended) or a fixed range string.
        timeout_s:  how long to wait for the test to complete.
        lower/upper: optional override of the Bluetooth-spec pass window.

    Returns the standard envelope:
        { ok, test, header, channel, capture_type, value, unit, lower,
          upper, pass, raw_fields, raw, timestamp }
    """
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected; call bt_connect first"}
        if not 0 <= channel <= 39:
            return {"ok": False, "error": f"channel must be 0..39, got {channel}"}
        if meas in ("LEMI", "LEMI2M", "LEMILR8") and mod_type == "NA":
            return {"ok": False, "error":
                    f"{meas} requires mod_type (MOD10101010 / MOD11110000 / "
                    "MOD11111111), not NA"}

        # Auto-initialise the EUT link if it isn't up yet. The 8852B
        # only knows how to talk to the DUT after `SYSCFG EUTSRCE` has
        # been issued AND the EUT has come back from HCI_Reset (~10s on
        # the lab's CSR DUT). Without this guard, MEASBLECAPX would run
        # against an uninitialised link and silently fail.
        if not session.eut.eut_address or session.eut.eut_address == "000000000000":
            log.info("EUT link not initialised — running default RS232 HCI "
                     "init before MEASBLECAPX. Pass handshake explicitly "
                     "(bt_set_eut_interface) if your DUT needs RTSCTS.")
            try:
                session.set_eut_interface("RS232", baud=115200,
                                          handshake="NONE")
            except Exception as e:
                return {"ok": False,
                        "error": f"EUT auto-init failed: {e}. "
                                 "Call bt_diag_eut_link() to see why."}
            if not session.eut.eut_address:
                # set_eut_interface already logged ERRLST, but surface
                # the failure to the caller as well.
                return {"ok": False,
                        "error": "EUT link did not come up — "
                                 "EUTADDR=000000000000 after RS232 init. "
                                 "Check cable / power / baud. "
                                 "Call bt_diag_eut_link() for details."}

        # 1. Configure (steps 1–5 of the recipe)
        plan = TxTestPlan(
            capture_type=capture_type, channel=channel, meas=meas,
            mod_type=mod_type, packet_length=packet_length,
            range_setting=range_setting,
        )
        session.configure_ble_tx(plan)

        # 2. Pre-RUN cleanup — make sure CMP isn't already set from a
        # previous session that was killed mid-test. (start() also does
        # this but doing it here means the wait_complete timeout is
        # measured against a known-fresh instrument.)
        try:
            session.instr.abort()
        except Exception:
            pass
        try:
            session.instr.clear_status()
        except Exception:
            pass

        # 3. Start (step 6)
        session.start()

        # 4. Wait for CMP bit (step 7)
        ok = session.wait_complete(timeout_s=timeout_s)
        if not ok:
            return {"ok": False,
                    "error": session.last_error or "test did not complete",
                    "state": session.state.value}

        # 5. Read result (step 8)
        r = session.read_result()

        # 5. Pull the headline metric out, based on which test it is.
        try:
            result = _extract_metric(plan, r, lower, upper)
        except MT8852BError as e:
            return {"ok": False, "error": str(e),
                    "raw": r.raw, "header": r.header,
                    "valid": r.valid, "pass_fail": r.pass_fail}

        return _ok(result.to_dict())
    except Exception as e:
        return _err(e)


def _extract_metric(plan: TxTestPlan, r: OResult,
                    lower: Optional[float], upper: Optional[float]
                    ) -> TxTestResult:
    """Pull the headline value out of an ORESULT response.

    Field indexes per Anritsu MT8852B Remote Programming Manual §15-6..15-56:
      LEOP   [0..3]  valid,avg,max_avg,min_avg,peak_to_avg,failed,tested,pass/fail
                       (8 fields + valid + pass/fail = 10 items in `fields`)
                       → headline = avg = index 1
      LEICD  [0..5]  valid,avg_fn,max_pos,max_neg,drift_rate,avg_drift,max_drift,
                       failed,tested,pass/fail
                       → headline = avg_fn = index 1 (in Hz)
      LEMI   [0..3]  valid,df1_max,df1_avg,df2_max,df2_avg,ratio,failed_count,
                       count,failed,tested,pass/fail
                       → headline = df1_avg = index 2 (in Hz)
    """
    spec = _limits_for(plan.meas)
    if plan.meas in ("LEOP", "LEOP2M", "LEOPLR8",
                     "LEOPBLECTE", "LEOP2LECTE"):
        if lower is None: lower = spec.get("lower")
        if upper is None: upper = spec.get("upper")
        value = _parse_float(_check_field(r, 1))
        return _envelope_tx("TX_POWER_DBM", plan.meas, value,
                            spec.get("unit", "dBm"), lower, upper,
                            plan, r, r.fields)
    if plan.meas in ("LEICD", "LEICD2M", "LEICDLR8",
                     "LEICDBLECTE", "LEICD2LECTE"):
        if lower is None: lower = spec.get("lower")
        if upper is None: upper = spec.get("upper")
        value = _parse_float(_check_field(r, 1))
        return _envelope_tx("CFO_HZ", plan.meas, value,
                            spec.get("unit", "Hz"), lower, upper,
                            plan, r, r.fields)
    if plan.meas in ("LEMI", "LEMI2M", "LEMILR8"):
        if lower is None: lower = spec.get("lower")
        if upper is None: upper = spec.get("upper")
        value = _parse_float(_check_field(r, 2))
        return _envelope_tx("MOD_F1_AVG_HZ", plan.meas, value,
                            spec.get("unit", "Hz"), lower, upper,
                            plan, r, r.fields)
    raise MT8852BError(
        f"unsupported meas {plan.meas!r} for headline extraction. "
        "Use bt_read_oresult to get the raw block, or extend _extract_metric.")


# ---------------------------------------------------------------------------
# Low-level atomic tools (escape hatches)
# ---------------------------------------------------------------------------
# NOTE: not exposed via MCP — see bt_reset() above.
def bt_configure_ble_tx(
    channel: int,
    meas: Literal["LEOP", "LEOP2M", "LEOPLR8", "LEICD", "LEICD2M", "LEICDLR8",
                  "LEMI", "LEMI2M", "LEMILR8"] = "LEOP",
    capture_type: Literal["BLE", "2LE", "BLR"] = "BLE",
    packet_length: int = 37,
    mod_type: Literal["NA", "MOD10101010", "MOD11110000", "MOD11111111"] = "NA",
    range_setting: Literal["AUTO", "1", "2", "3"] = "AUTO",
) -> dict:
    """Apply the configuration steps of the recipe without running."""
    try:
        plan = TxTestPlan(
            capture_type=capture_type, channel=channel, meas=meas,
            mod_type=mod_type, packet_length=packet_length,
            range_setting=range_setting,
        )
        session.configure_ble_tx(plan)
        return _ok({"state": session.state.value, "plan": plan.to_dict()})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see bt_reset() above.
def bt_start() -> dict:
    """Send RUN. Transitions to TESTING."""
    try:
        session.start()
        return _ok({"state": session.state.value})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see bt_reset() above.
def bt_wait_test_complete(timeout_s: float = 30.0,
                          poll_s: float = 0.1) -> dict:
    """Poll *INS? until the CMP bit is set (or timeout)."""
    try:
        ok = session.wait_complete(timeout_s=timeout_s, poll_s=poll_s)
        return _ok({"complete": ok, "state": session.state.value})
    except Exception as e:
        return _err(e)


# NOTE: not exposed via MCP — see bt_reset() above.
def bt_abort() -> dict:
    """ABORT — stop whatever is running and return to IDLE."""
    try:
        session.stop()
        return _ok({"state": session.state.value})
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def bt_read_oresult(test: str, ext_code: int = 0) -> dict:
    """Read ORESULT TEST,<ext>,<test> and return the parsed block.

    Use this when `bt_run_tx_test` doesn't cover your test. Common
    headers: LEOP, LEOP2M, LEICD, LEICD2M, LEMI, LEMI2M, LESS, LEMP, LEPRI.

    Returns: { ok, header, ext_code, valid, fields, pass_fail, raw }
    """
    try:
        if session.state != State.COMPLETE:
            return {"ok": False,
                    "error": f"cannot read in state {session.state.value}; "
                             "wait_complete first"}
        r = session.instr.read_oresult(test, ext_code)
        session.last_result = r
        session.state = State.IDLE
        return _ok({
            "header": r.header,
            "ext_code": r.ext_code,
            "valid": r.valid,
            "fields": r.fields,
            "pass_fail": r.pass_fail,
            "raw": r.raw,
        })
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def bt_read_script_result(ext_code: int = 0) -> dict:
    """Read the whole script's ORESULT and parse into a per-header dict."""
    try:
        if session.state != State.COMPLETE:
            return {"ok": False,
                    "error": f"cannot read in state {session.state.value}; "
                             "wait_complete first"}
        results = session.instr.read_oresult_script(ext_code)
        out = {k: {
            "header": v.header, "ext_code": v.ext_code, "valid": v.valid,
            "fields": v.fields, "pass_fail": v.pass_fail, "raw": v.raw,
        } for k, v in results.items()}
        session.state = State.IDLE
        return _ok({"results": out})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# High-level: interactive BLE test campaign (refactor — see SPEC_mcp_interaction.md)
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def bt_preflight() -> dict:
    """Snapshot the bench's state without prompting or running tests.

    Returns the same shape as `bt_diag_eut_link` plus OPTSTATUS (with
    parsed installed options), BOOTSTATUS, and the bench-ble-ready
    probe (OPMD / SCPTSEL / SCPTTSTGP / LEPKTLEN). Use this BEFORE
    `bt_run_full_suite` so the operator can see the bench's state
    and decide which EUT interface to use.

    This tool is also called automatically on the first
    `bt_run_full_suite` invocation (per spec §5.5).
    """
    try:
        if not session.instr.connected:
            return {"ok": False, "error": "not connected; call bt_connect first"}
        p = run_preflight(session)
        return _ok(p.to_dict())
    except Exception as e:
        return _err(e)


@mcp.tool(annotations={"openWorldHint": True, "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def bt_run_full_suite(
    eut_interface: Optional[Literal[
        "RS232", "USB", "BLE2WIRE", "USBBLE2WIRE", "USBADAPTOR", "NONE"
    ]] = None,
    eut_baud: Optional[int] = None,
    eut_handshake: Optional[Literal["NONE", "RTSCTS"]] = None,
    tests: Optional[list[Literal[
        "LEOP", "LEICD", "LEMI", "LESS", "LEMP", "LEPRI", "LEPS"
    ]]] = None,
    test_params: Optional[dict] = None,
    channels: Optional[list[int]] = None,
    capture_type: Optional[Literal["BLE", "2LE", "BLR"]] = None,
    packet_length: Optional[int] = None,
    report_dir: Optional[str] = None,
    run_label: Optional[str] = None,
) -> dict:
    """Run an interactive BLE TX/RX test campaign.

    .. note::
       The MCP channel **always** asks the operator before running any
       test. There is no `non_interactive` parameter — the operator
       confirmation gate is enforced for every invocation. For batch /
       scripted runs that need to skip prompting, import
       `mcp_8852b.interactive.run_campaign` directly and pass
       `non_interactive=True`. See `mcp_8852b/server.py` header for the
       rationale; the runtime check is `verify_mcp8852b_gate.py`.

    **Non-skippable final confirmation.** After every campaign
    parameter has been collected (or pre-filled by the AI), the tool
    emits a `state="awaiting_input"` envelope with
    `prompt.id="final_confirm"` showing the full plan (EUT interface,
    tests, channels, capture type, packet length, run label) and
    choices `["Yes, run all", "Cancel"]`. Only after the operator picks
    "Yes, run all" does the tool issue `session.start()` /
    `MEASBLECAPX`. This closes the loophole where an AI client
    pre-fills every kwarg and skips every question, so the operator
    never sees the plan before bench I/O. Enforcement is via the
    `force_final_confirm=True` arg on the internal `run_campaign` call
    below (verified by `verify_mcp8852b_gate.py` test 5).

    The contract is **stateless across calls**: the tool returns
    `state="awaiting_input"` with a `prompt` envelope describing the
    question (and any `choices`); the caller invokes the tool again
    with the corresponding override parameter set. This lets a chat
    agent show one question at a time to the operator.

    Per spec §5.5, the first call runs the preflight (see `bt_preflight`)
    and shows the operator the bench's state (EUTSRCE, OPTSTATUS, etc.)
    so the EUT-interface prompt has a meaningful default.

    On the first call, ALL of `eut_interface`, `tests`, `capture_type`,
    `channels`, `packet_length`, `run_label` are None — the tool asks
    for them one at a time. Once the caller passes a value (e.g. by
    calling again with `eut_interface="RS232"`), that field is
    considered answered and the next missing one is asked.

    Reuses the module-level `session` — same VISA transport the atomic
    tools use. Honors `skip_if_ready=True` on the EUT init (v6.8
    default — see SPEC §7.3).

    Returns one of:
      {"ok": True,  "state": "running",  "report_dir": "...",
       "started": "...", "ended": "...", "results": [...]}
      {"ok": True,  "state": "awaiting_input",
       "prompt": {"id": "...", "question": "...", "choices": [...]},
       "session": {"completed": [...], "remaining": [...]}}
      {"ok": False, "error": "..."}

    The new `mcp_8852b/interactive.py` module owns the prompt engine
    and the preflight; this tool is a thin wrapper.
    """
    try:
        if not session.instr.connected:
            return {"ok": False,
                    "error": "not connected; call bt_connect first"}
        # If a report_dir was given, set up a bench.log handler so the
        # pyvisa traffic during this run is captured.
        log_handler = None
        if report_dir:
            from pathlib import Path
            log_handler = make_bench_log_handler(Path(report_dir))
        return run_campaign(
            session,
            eut_interface=eut_interface,
            eut_baud=eut_baud,
            eut_handshake=eut_handshake,
            tests=tests,
            test_params=test_params,
            channels=channels,
            capture_type=capture_type,
            packet_length=packet_length,
            report_dir=report_dir,
            run_label=run_label,
            non_interactive=False,  # forced: MCP gate — see note in docstring
            force_final_confirm=True,  # forced: final plan-confirmation gate
            log_file_handler=log_handler,
        )
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()