"""Interactive operator prompts + MCP envelope translator for the BLE
test campaign.

Two consumers:

  1. The legacy CLI runner (`run_ble_full_suite.py`) calls the
     `prompt_*()` functions directly. Those functions print the
     spec-style prompt to stdout and read from stdin via `input()`.

  2. The new MCP tool `bt_run_full_suite` (in `mcp_8852b/server.py`)
     calls the same `prompt_*()` functions but the function inspects
     the `non_interactive` flag — if False, the prompt is translated
     to an `awaiting_input` envelope that the chat agent can show to
     the operator one question at a time.

This module is also the home of `run_preflight()` which is exposed as
the `bt_preflight` MCP tool. The preflight is the same shape as the
existing `bt_diag_eut_link` but covers more (OPTSTATUS / BOOTSTATUS /
the bench-ble-ready probe).

The state machine's three v6.8 fixes (wait_complete's transition
detection, read_result's OPMD SCRIPT + *CLS + *OPC? block, and
set_eut_interface's skip_if_ready=True default) are kept intact —
this file only calls `MT8852BSession.set_eut_interface()` and the
diagnostic helpers, it does not replace any of those methods.
"""
from __future__ import annotations
import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal, Any, Callable

from . import scpi
from .instrument import MT8852B
from .state_machine import MT8852BSession
from .report import (
    TEST_CATALOG, TestRecord, run_one_test, save_report,
    DEFAULT_CHANNELS,
)

log = logging.getLogger("mcp_8852b.interactive")


def _cli_print(non_interactive: bool, *args, **kwargs) -> None:
    """Print to stdout ONLY in CLI mode (non_interactive=False).

    In MCP / stdio mode (non_interactive=True) this is a no-op. A raw
    print() to stdout would interleave with the JSON-RPC stream and
    corrupt the MCP transport — the chat client would silently see
    garbage where it expects protocol messages.

    The convention is: every print() in this module MUST go through
    this helper, and the caller's `non_interactive` flag MUST be
    passed in. The existing `if not non_interactive:` blocks that gate
    the prompts are also kept (defense in depth) but the helper makes
    the safety structural rather than conventional: a future print()
    added outside the gate will still be a no-op in MCP mode.
    """
    if not non_interactive:
        print(*args, **kwargs)


# ----------------------------------------------------------------------------
# Preflight snapshot
# ----------------------------------------------------------------------------
@dataclass
class Preflight:
    """Snapshot of the bench's state before any operator prompts.

    Returned by `run_preflight()` and also written to
    `<report_dir>/bench_preflight.json` so a post-mortem has the bench's
    state at run-start.
    """
    idn: str = ""
    opt_status: str = ""
    installed_options: list[str] = field(default_factory=list)
    boot_status: str = ""
    opmd: str = ""
    scptsel: str = ""
    scpttstgp: str = ""
    lepktlen: str = ""
    eut_source: str = ""
    eut_rs232: str = ""
    eut_handshake: str = ""
    eut_addr: str = ""
    eut_le_feat: str = ""
    eut_feat: str = ""
    eut_name: str = ""
    errlst: str = ""
    ins_raw: Optional[int] = None
    ins_bits: dict = field(default_factory=dict)
    bench_ble_ready: bool = False
    link_up: bool = False
    captured_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def has_opt(self, opt: str) -> bool:
        """True if `opt` (e.g. "27", "37") is in OPTSTATUS."""
        return opt in self.installed_options


def _query_safe(instr: MT8852B, cmd: str) -> str:
    """One-shot `instr.query` that swallows errors so the preflight
    doesn't sink on a single bad query."""
    try:
        return instr.query(cmd).strip()
    except Exception as e:
        return f"<query failed: {e}>"


def _split(raw: str, prefix: str) -> str:
    """Strip the 'SYSCFG? FOO,' prefix the 8852B echoes back."""
    return raw.split(",", 1)[-1].strip() if raw else raw


def _parse_optstatus(raw: str) -> list[str]:
    """OPTSTATUS? returns either an 'ALL' blob or a list of option
    numbers. The shape varies by firmware; we extract digit runs."""
    if not raw:
        return []
    # e.g. "27,35,36,37" or "27, 35, 36" — pull all 1-3 digit tokens
    opts = re.findall(r"\b(\d{1,3})\b", raw)
    # filter to plausible option numbers (Anritsu options are typically
    # 1..99 for BLE features)
    return [o for o in opts if 1 <= int(o) <= 99]


def _parse_ins(ins_val: int) -> dict:
    return {
        "CON":  bool(ins_val & 0x01),
        "DIS":  bool(ins_val & 0x02),
        "CMP":  bool(ins_val & 0x04),
        "INQ":  bool(ins_val & 0x08),
        "OVT":  bool(ins_val & 0x10),
        "LEFG": bool(ins_val & 0x20),
    }


def run_preflight(sess: MT8852BSession) -> Preflight:
    """Snapshot the bench's state for the operator.

    Per spec §5.5, this is run BEFORE the operator is prompted so the
    EUT-interface prompt can show the current EUTSRCE / bench-ble-ready
    probe state and the test-selection prompt can grey out tests that
    need a missing option.

    Failure-tolerant: a single bad query sets that field to
    `<query failed: ...>` and we continue.
    """
    p = Preflight()
    p.captured_at = datetime.now().isoformat(timespec="seconds")
    if not sess.instr.connected:
        p.idn = "<not connected>"
        return p
    instr = sess.instr
    p.idn         = _query_safe(instr, scpi.idn())
    p.opt_status  = _query_safe(instr, "OPTSTATUS?")
    p.installed_options = _parse_optstatus(p.opt_status)
    try:
        p.boot_status = instr.boot_status()
    except Exception as e:
        p.boot_status = f"<boot_status failed: {e}>"
    p.opmd        = _query_safe(instr, "OPMD?")
    p.scptsel     = _query_safe(instr, "SCPTSEL?")
    p.scpttstgp   = _query_safe(instr, f"SCPTTSTGP? {sess.script}")
    p.lepktlen    = _query_safe(instr, f"LEPKTLEN? {sess.script}")
    p.eut_source  = _split(_query_safe(instr, scpi.eut_src_query()), "EUTSRCE")
    is_rs232 = p.eut_source.upper() == "RS232"
    p.eut_rs232     = (_query_safe(instr, scpi.eut_rs232_query())
                       if is_rs232 else "n/a")
    p.eut_handshake = (_query_safe(instr, scpi.eut_handshake_query())
                       if is_rs232 else "n/a")
    p.eut_addr     = _split(_query_safe(instr, scpi.eut_addr_query()), "EUTADDR")
    p.eut_le_feat  = _query_safe(instr, scpi.eut_le_feat_query())
    p.eut_feat     = _query_safe(instr, scpi.eut_feat_query())
    p.eut_name     = _query_safe(instr, scpi.eut_name_query())
    p.errlst       = _query_safe(instr, scpi.system_error())
    try:
        ins_val = instr.ins()
        p.ins_raw = ins_val
        p.ins_bits = _parse_ins(ins_val)
    except Exception as e:
        p.ins_raw = None
        p.ins_bits = {"error": str(e)}
    # Link-up heuristic (per spec §5.5 + project_8852b_eutaddr_zero_normal.md
    # — EUTADDR=0 is NORMAL for BLE-over-RS232; we use it as a soft hint,
    # not a hard error).
    p.link_up = (
        p.eut_addr not in ("", "000000000000")
        and not p.eut_addr.startswith("<query failed")
    )
    # Bench-ble-ready probe: OPMD=SCRIPT and SCPTSEL=<self.script> and
    # SCPTTSTGP ends with ,ON and LEPKTLEN returns a length. We piggyback
    # on the same logic the state machine uses in _bench_ble_ready.
    p.bench_ble_ready = (
        "SCRIPT" in p.opmd.upper()
        and str(sess.script) in p.scptsel
        and p.scpttstgp.upper().endswith(",ON")
        and p.lepktlen and "LEPKTLEN" in p.lepktlen.upper()
    )
    return p


# ----------------------------------------------------------------------------
# Prompt engines — these return the typed value when running in CLI
# mode, or raise NeedInput(...) when running in MCP mode. The MCP tool
# catches NeedInput and translates it to the awaiting_input envelope.
# ----------------------------------------------------------------------------
class NeedInput(Exception):
    """Raised by a prompt function when the MCP tool needs to ask the
    operator for the value. `envelope` is the awaiting_input shape."""

    def __init__(self, envelope: dict):
        super().__init__(envelope.get("prompt", {}).get("question", ""))
        self.envelope = envelope


# ---- prompt data: human-readable choices ----
EUT_INTERFACE_CHOICES = [
    {"label": "HCI RS232",    "value": "RS232"},
    {"label": "HCI USB",      "value": "USB"},
    {"label": "2-Wire",       "value": "BLE2WIRE"},
    {"label": "2-Wire-USB",   "value": "USBBLE2WIRE"},
    {"label": "USB-Serial",   "value": "USBADAPTOR"},
    {"label": "No Control",   "value": "NONE"},
]

DATA_RATE_CHOICES = [
    {"label": "BLE 4.0/4.1 (1 Mbps uncoded)",  "value": "BLE"},
    {"label": "BLE 5.0 2LE (2 Mbps)",          "value": "2LE"},
    {"label": "BLE 5.0 BLR (coded long range)","value": "BLR"},
]


# ---- typed prompt functions (used by both CLI and MCP) ----
def prompt_eut_interface(
    preflight: Optional[Preflight] = None,
    *,
    override: Optional[str] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> tuple[str, Optional[int], Optional[str]]:
    """Ask the operator how the MT8852B is wired to the DUT.

    Returns (interface, baud, handshake). `interface` is one of
    RS232 / USB / BLE2WIRE / USBBLE2WIRE / USBADAPTOR / NONE.

    For RS232 also returns (baud, handshake). For everything else
    baud/handshake are None.

    In MCP mode, the caller passes `override` (a value the chat agent
    has already collected) and we just validate it. If `override` is
    None and `non_interactive=False`, we raise NeedInput with the
    prompt envelope.
    """
    if override is not None:
        # Validate
        valid = {c["value"] for c in EUT_INTERFACE_CHOICES}
        if override not in valid:
            raise ValueError(
                f"eut_interface must be one of {sorted(valid)}, got {override!r}")
        return _resolve_eut_interface(override, None, None, preflight, non_interactive,
                                      cli_input)

    if non_interactive:
        # No override, no prompt — use preflight-detected state if any,
        # else default to RS232 115200 NONE.
        return _resolve_eut_interface(None, None, None, preflight,
                                      non_interactive, cli_input)

    # MCP mode — emit the envelope
    raise NeedInput({
        "ok": True,
        "state": "awaiting_input",
        "prompt": {
            "id": "eut_interface",
            "question": "How is the MT8852B wired to the DUT?",
            "choices": EUT_INTERFACE_CHOICES,
            "default": (preflight.eut_source
                        if preflight and preflight.eut_source
                        else "RS232"),
            "help": ("BLE_MSM Ch.3 - EUT Control setting on [Connection] tab. "
                     "For BLE-over-RS232, the lab's default is "
                     "RS232@115200/NONE."),
        },
    })


def _resolve_eut_interface(interface, baud, handshake,
                           preflight: Optional[Preflight],
                           non_interactive: bool,
                           cli_input: Callable[[str], str]
                           ) -> tuple[str, Optional[int], Optional[str]]:
    """Shared resolution path: fill in defaults from preflight, then
    prompt for the bits that are still missing (CLI only)."""
    if interface is None:
        # Choose default from preflight or fall back to RS232 115200 NONE.
        if preflight and preflight.eut_source and preflight.eut_source != "n/a":
            interface = preflight.eut_source
        else:
            interface = "RS232"
        if not non_interactive:
            _cli_print(non_interactive,)
            _cli_print(non_interactive,"=" * 60)
            _cli_print(non_interactive,"Step 1 / 3 - How is the MT8852B wired to the DUT?")
            _cli_print(non_interactive,"=" * 60)
            for i, c in enumerate(EUT_INTERFACE_CHOICES, 1):
                marker = "  "
                if c["value"] == "RS232":
                    marker = "* "
                _cli_print(non_interactive,f"  {marker}{i}) {c['label']}  ({c['value']})")
            _cli_print(non_interactive,)
            _cli_print(non_interactive,f"  current EUTSRCE: {preflight.eut_source if preflight else '?'}")
            raw = cli_input("Which connection? [1] > ").strip() or "1"
            try:
                idx = int(raw)
                if 1 <= idx <= len(EUT_INTERFACE_CHOICES):
                    interface = EUT_INTERFACE_CHOICES[idx-1]["value"]
            except ValueError:
                pass

    if interface == "RS232":
        if baud is None:
            default_baud = 115200
            if preflight and preflight.eut_rs232 and preflight.eut_rs232 != "n/a":
                try:
                    default_baud = int(preflight.eut_rs232)
                except (ValueError, TypeError):
                    pass
            if non_interactive:
                baud = default_baud
            else:
                _cli_print(non_interactive,)
                _cli_print(non_interactive,"RS232 settings")
                _cli_print(non_interactive,f"  Baud rate   [{default_baud}]   (1200 / 2400 / 4800 / 9600 / 19200 / 38400")
                _cli_print(non_interactive,"                          / 57600 / 115200 / 230400 / 460800)")
                raw = cli_input(f"baud [{default_baud}] > ").strip()
                baud = int(raw) if raw else default_baud
        if handshake is None:
            default_hs = "NONE"
            if preflight and preflight.eut_handshake and preflight.eut_handshake != "n/a":
                if "RTS" in preflight.eut_handshake.upper():
                    default_hs = "RTSCTS"
                else:
                    default_hs = "NONE"
            if non_interactive:
                handshake = default_hs
            else:
                _cli_print(non_interactive,f"  Handshake   [{default_hs}]     (NONE / RTSCTS  - XON/XOFF not supported)")
                raw = cli_input(f"handshake [{default_hs}] > ").strip()
                handshake = raw.upper() or default_hs
                if handshake not in ("NONE", "RTSCTS"):
                    handshake = "NONE"
    else:
        baud = None
        handshake = None
    return interface, baud, handshake


def prompt_data_rate(
    *,
    override: Optional[str] = None,
    default: str = "BLE",
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> str:
    if override is not None:
        if override not in {"BLE", "2LE", "BLR"}:
            raise ValueError(
                f"capture_type must be BLE/2LE/BLR, got {override!r}")
        return override
    if non_interactive:
        return default
    raise NeedInput({
        "ok": True,
        "state": "awaiting_input",
        "prompt": {
            "id": "capture_type",
            "question": "Data rate? (BLE_MSM Ch.4 step 9)",
            "choices": DATA_RATE_CHOICES,
            "default": default,
            "help": "1 Msymbol/s (BLE), 2 Msymbol/s (2LE), or coded long range (BLR).",
        },
    })


def prompt_channels(
    *,
    override: Optional[list[int]] = None,
    default: Optional[list[int]] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> list[int]:
    if default is None:
        default = list(DEFAULT_CHANNELS)
    if override is not None:
        for c in override:
            if not 0 <= c <= 39:
                raise ValueError(f"channel must be 0..39, got {c}")
        return list(override)
    if non_interactive:
        return list(default)
    raise NeedInput({
        "ok": True,
        "state": "awaiting_input",
        "prompt": {
            "id": "channels",
            "question": "Channels to test? (BLE_MSM Ch.4 step 2 / Ch.5 step 4)",
            "choices": [
                {"label": "low (ch 0)",   "value": [0]},
                {"label": "mid (ch 19)",  "value": [19]},
                {"label": "high (ch 39)", "value": [39]},
                {"label": "all (low+mid+high)", "value": [0, 19, 39]},
            ],
            "default": default,
            "help": "low/mid/high or 'all'. Comma-separated list also accepted (0,19,39).",
        },
    })


def prompt_packet_length(
    *,
    override: Optional[int] = None,
    default: int = 37,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> int:
    if override is not None:
        if not 2 <= override <= 255:
            raise ValueError(f"packet_length must be 2..255, got {override}")
        return override
    if non_interactive:
        return default
    raise NeedInput({
        "ok": True,
        "state": "awaiting_input",
        "prompt": {
            "id": "packet_length",
            "question": "Packet length? (BLE_MSM Ch.4 step 8)",
            "choices": [
                {"label": "37 bytes (standard)", "value": 37},
                {"label": "255 bytes (max)",     "value": 255},
            ],
            "default": default,
            "help": "Payload length in bytes; 37 is the Bluetooth-spec reference, 2..255 valid.",
        },
    })


def prompt_test_selection(
    *,
    override: Optional[list[str]] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> list[str]:
    """Ask which tests to run. Greys out tests that need a missing
    option (LEMP/LEPRI need Opt 27; LEPS needs Opt 37)."""
    # Filter catalog by preflight option availability.
    if preflight is not None:
        available = {}
        greyed = {}
        for tid, info in TEST_CATALOG.items():
            missing = [o for o in info["requires_opt"]
                       if not preflight.has_opt(o)]
            if missing:
                greyed[tid] = missing
            else:
                available[tid] = info
    else:
        available = dict(TEST_CATALOG)
        greyed = {}

    if override is not None:
        chosen = [t.upper() for t in override if t.upper() in TEST_CATALOG]
        return chosen or ["LEOP", "LEICD", "LEMI", "LESS"]
    if non_interactive:
        return ["LEOP", "LEICD", "LEMI", "LESS"]

    _cli_print(non_interactive,)
    _cli_print(non_interactive,"=" * 60)
    _cli_print(non_interactive,"Step 1 / 4 - Select tests (BLE_MSM Ch.1-3)")
    _cli_print(non_interactive,"=" * 60)
    for tid, info in TEST_CATALOG.items():
        if tid in available:
            opt_tag = ""
            if info["requires_opt"]:
                opt_tag = f"  [Opt {'/'.join(info['requires_opt'])}]"
            _cli_print(non_interactive,f"  {tid:5s}  {info['name']}  [{info['payload']}]{opt_tag}")
        else:
            opt_tag = "/".join(greyed[tid])
            _cli_print(non_interactive,f"  {tid:5s}  {info['name']}  [GREYED: missing Opt {opt_tag}]")
    _cli_print(non_interactive,)
    _cli_print(non_interactive,"Enter test IDs separated by spaces, or 'all' for the full catalog.")
    raw = cli_input("tests [all] > ").strip().lower() or "all"
    if raw == "all":
        chosen = list(available.keys())
    else:
        chosen = [t.strip().upper() for t in raw.split()
                  if t.strip().upper() in available]
    if not chosen:
        chosen = [t for t in ("LEOP", "LEICD", "LEMI", "LESS")
                  if t in available]
    return chosen


def prompt_report_dir(
    *,
    override: Optional[str] = None,
    default: Optional[str] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> str:
    """Ask for the run label suffix; returns the sanitized suffix."""
    if default is None:
        default = datetime.now().strftime("%Y%m%d_%H%M%S")
    if override is not None:
        return _sanitize_label(override)
    if non_interactive:
        return ""
    raise NeedInput({
        "ok": True,
        "state": "awaiting_input",
        "prompt": {
            "id": "run_label",
            "question": "Run label (suffix)?",
            "choices": [
                {"label": "<empty> (timestamp only)", "value": ""},
                {"label": "LEOP_only_ch0",            "value": "LEOP_only_ch0"},
                {"label": "HCI_RTSCTS_sensitivity",   "value": "HCI_RTSCTS_sensitivity"},
            ],
            "default": "",
            "help": "Free text. Sanitized to [A-Za-z0-9_-]+ (max 32 chars). Empty = timestamp only.",
        },
    })


def _sanitize_label(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "_", s)
    return s[:32]


# ----------------------------------------------------------------------------
# Per-test parameter prompts (SPEC §3).
#
# Each per-test prompt function follows the same shape as
# `prompt_eut_interface` etc.:
#   - If `override` is provided, validate it and return it as a dict.
#   - If `non_interactive=True`, return defaults (no I/O).
#   - Otherwise print the prompt to stdout (CLI) and read from stdin
#     via `cli_input`.
#   - In MCP mode the caller has set `non_interactive=True` and passed
#     `override=None`, so the function raises NeedInput with the
#     awaiting_input envelope.
#
# The returned dict's keys match the spec §3 column "SCPI" (the second
# token of the `LE*CFG` command). `apply_test_params` already knows
# how to write those keys; the prompts just produce values.
# ----------------------------------------------------------------------------
def _ask_float(label: str, default: Optional[float],
               lo: Optional[float], hi: Optional[float],
               non_interactive: bool,
               cli_input: Callable[[str], str]) -> Optional[float]:
    """CLI-mode float prompt. Returns None if the user enters a blank
    line and there is no default (used by optional override fields)."""
    if non_interactive:
        return default
    rng = ""
    if lo is not None and hi is not None:
        rng = f"  [{lo}..{hi}]"
    elif lo is not None:
        rng = f"  [>={lo}]"
    elif hi is not None:
        rng = f"  [<={hi}]"
    dflt = "" if default is None else f" [{default}]"
    raw = cli_input(f"{label}{dflt}{rng} > ").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _ask_int(label: str, default: Optional[int],
             lo: Optional[int], hi: Optional[int],
             non_interactive: bool,
             cli_input: Callable[[str], str]) -> Optional[int]:
    if non_interactive:
        return default
    rng = ""
    if lo is not None and hi is not None:
        rng = f"  [{lo}..{hi}]"
    dflt = "" if default is None else f" [{default}]"
    raw = cli_input(f"{label}{dflt}{rng} > ").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _ask_choice(label: str, default: Optional[str], choices: list[tuple[str, str]],
                non_interactive: bool,
                cli_input: Callable[[str], str]) -> Optional[str]:
    """CLI-mode choice prompt. `choices` is a list of (key, value)."""
    if non_interactive:
        return default
    dflt = "" if default is None else f" [{default}]"
    raw = cli_input(f"{label}{dflt} > ").strip().upper()
    if not raw:
        return default
    keys = {k.upper(): v for k, v in choices}
    return keys.get(raw, default)


def _ask_yes_no(label: str, default: bool,
                non_interactive: bool,
                cli_input: Callable[[str], str]) -> bool:
    if non_interactive:
        return default
    raw = cli_input(f"{label} [{'Y' if default else 'N'}] > ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "true")


# Per-test parameter envelopes. The MCP mode raises NeedInput with one
# of these; the operator's answer becomes the `override` dict on the
# next call. CLI mode prints the prompt and reads the answer.
def prompt_leop_params(
    *,
    override: Optional[dict] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> dict:
    """SPEC §3.2 — LEOP (Output Power, TX) per-test params.

    Fields: AVGMXLIM, AVGMNLIM, PEAKLIM, payload, packet_length,
    eut_tx_power (AUTO / 1 / 2 / 3).
    """
    # Defaults per spec table + project memory: low/avg band 0..5 dBm
    # for a BLE Sticker DUT (BLE radio typical 0 dBm).
    defaults = {
        "AVGMXLIM":     20.0,
        "AVGMNLIM":    -20.0,
        "PEAKLIM":       6.0,
        "payload":       "PRBS9",
        "packet_length": 37,
        "eut_tx_power": "AUTO",
    }
    if override is not None:
        for k in ("AVGMXLIM", "AVGMNLIM", "PEAKLIM"):
            if k in override:
                v = float(override[k])
                if not -50.0 <= v <= 30.0:
                    raise ValueError(f"{k} must be -50..+30 dBm, got {v}")
        if "payload" in override and override["payload"] not in (
                "PRBS9", "10101010", "11110000"):
            raise ValueError(f"payload must be PRBS9/10101010/11110000, "
                             f"got {override['payload']!r}")
        if "packet_length" in override:
            pl = int(override["packet_length"])
            if not 2 <= pl <= 255:
                raise ValueError(f"packet_length must be 2..255, got {pl}")
        if "eut_tx_power" in override and override["eut_tx_power"] not in (
                "AUTO", "1", "2", "3"):
            raise ValueError(f"eut_tx_power must be AUTO/1/2/3, "
                             f"got {override['eut_tx_power']!r}")
        return {**defaults, **override}
    if non_interactive:
        return defaults

    # CLI mode — ask the operator, one line per field.
    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "LEOP — Output Power per-test params (SPEC §3.2)")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, f"  Hit ENTER to accept the [default] in brackets.")
    avghigh = _ask_float("AVGMXLIM (avg high limit, dBm)",
                          defaults["AVGMXLIM"], -50.0, 30.0,
                          non_interactive, cli_input)
    avglow = _ask_float("AVGMNLIM (avg low limit, dBm)",
                         defaults["AVGMNLIM"], -50.0, 30.0,
                         non_interactive, cli_input)
    peak = _ask_float("PEAKLIM (peak-to-avg limit, dB)",
                       defaults["PEAKLIM"], None, None,
                       non_interactive, cli_input)
    payload = _ask_choice("Payload (PRBS9/10101010/11110000)",
                          defaults["payload"],
                          [("PRBS9", "PRBS9"), ("10101010", "10101010"),
                           ("11110000", "11110000")],
                          non_interactive, cli_input)
    pkt_len = _ask_int("Packet length (bytes)",
                        defaults["packet_length"], 2, 255,
                        non_interactive, cli_input)
    tx_power = _ask_choice("EUT Tx Power (AUTO/1/2/3)",
                            defaults["eut_tx_power"],
                            [("AUTO", "AUTO"), ("1", "1"), ("2", "2"),
                             ("3", "3")],
                            non_interactive, cli_input)
    return {
        "AVGMXLIM": avghigh,
        "AVGMNLIM": avglow,
        "PEAKLIM": peak,
        "payload": payload,
        "packet_length": pkt_len,
        "eut_tx_power": tx_power,
    }


def prompt_leicd_params(
    *,
    override: Optional[dict] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> dict:
    """SPEC §3.3 — LEICD (Carrier Frequency Offset & Drift, TX)."""
    defaults = {
        "MXPOSLIM":      150000.0,
        "MXNEGLIM":     -150000.0,
        "DFTBLELIM":      50000.0,
        "INITDFTBLERATE": 20000.0,
        "DFTBLERATE":     50000.0,
        "payload":        "10101010",
        "packet_length":  37,
    }
    if override is not None:
        for k in ("MXPOSLIM", "MXNEGLIM", "DFTBLELIM",
                  "INITDFTBLERATE", "DFTBLERATE"):
            if k in override:
                v = float(override[k])
                if not -500000.0 <= v <= 500000.0:
                    raise ValueError(f"{k} must be -500k..+500k Hz, got {v}")
        if "payload" in override and override["payload"] not in (
                "10101010", "11111111"):
            raise ValueError(f"LEICD payload must be 10101010 (BLE/2LE) or "
                             f"11111111 (BLR8), got {override['payload']!r}")
        if "packet_length" in override:
            pl = int(override["packet_length"])
            if not 2 <= pl <= 255:
                raise ValueError(f"packet_length must be 2..255, got {pl}")
        return {**defaults, **override}
    if non_interactive:
        return defaults

    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "LEICD — Carrier Frequency Offset & Drift (SPEC §3.3)")
    _cli_print(non_interactive, "=" * 60)
    mxpos = _ask_float("MXPOSLIM (positive offset limit, Hz)",
                        defaults["MXPOSLIM"], 0.0, 500000.0,
                        non_interactive, cli_input)
    mxneg = _ask_float("MXNEGLIM (negative offset limit, Hz)",
                        defaults["MXNEGLIM"], -500000.0, 0.0,
                        non_interactive, cli_input)
    dftlim = _ask_float("DFTBLELIM (packet drift limit, Hz)",
                         defaults["DFTBLELIM"], 0.0, 500000.0,
                         non_interactive, cli_input)
    initrate = _ask_float("INITDFTBLERATE (initial drift rate, Hz/50us)",
                           defaults["INITDFTBLERATE"], 0.0, 500000.0,
                           non_interactive, cli_input)
    dfrate = _ask_float("DFTBLERATE (drift rate, Hz/50us)",
                         defaults["DFTBLERATE"], 0.0, 500000.0,
                         non_interactive, cli_input)
    payload = _ask_choice("Payload (10101010/11111111)",
                          defaults["payload"],
                          [("10101010", "10101010"), ("11111111", "11111111")],
                          non_interactive, cli_input)
    pkt_len = _ask_int("Packet length (bytes)",
                        defaults["packet_length"], 2, 255,
                        non_interactive, cli_input)
    return {
        "MXPOSLIM": mxpos,
        "MXNEGLIM": mxneg,
        "DFTBLELIM": dftlim,
        "INITDFTBLERATE": initrate,
        "DFTBLERATE": dfrate,
        "payload": payload,
        "packet_length": pkt_len,
    }


def prompt_lemi_params(
    *,
    override: Optional[dict] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> dict:
    """SPEC §3.4 — LEMI (Modulation Characteristics, TX).

    Note: TOGGLE=CONT runs both 10101010 and 11110000 per BLE spec
    (default). TOGGLE=ONCE runs each once.
    """
    defaults = {
        "F1AVGMIN":  185000.0,
        "F1AVGMAX":  250000.0,
        "F2MAXLIM":  250000.0,
        "F1F2MAX":    0.95,
        "TOGGLE":    "CONT",
        "payload":   "BOTH",  # both 10101010 + 11110000 per spec
    }
    if override is not None:
        for k in ("F1AVGMIN", "F1AVGMAX", "F2MAXLIM"):
            if k in override:
                v = float(override[k])
                if not 0.0 <= v <= 500000.0:
                    raise ValueError(f"{k} must be 0..500k Hz, got {v}")
        if "F1F2MAX" in override:
            v = float(override["F1F2MAX"])
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"F1F2MAX must be 0..1, got {v}")
        if "TOGGLE" in override and override["TOGGLE"] not in (
                "ONCE", "CONT"):
            raise ValueError(f"TOGGLE must be ONCE/CONT, got {override['TOGGLE']!r}")
        if "payload" in override and override["payload"] not in (
                "10101010", "11110000", "BOTH"):
            raise ValueError(f"payload must be 10101010/11110000/BOTH, "
                             f"got {override['payload']!r}")
        return {**defaults, **override}
    if non_interactive:
        return defaults

    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "LEMI — Modulation Characteristics (SPEC §3.4)")
    _cli_print(non_interactive, "=" * 60)
    f1min = _ask_float("F1AVGMIN (f1avg min, Hz)",
                        defaults["F1AVGMIN"], 0.0, 500000.0,
                        non_interactive, cli_input)
    f1max = _ask_float("F1AVGMAX (f1avg max, Hz)",
                        defaults["F1AVGMAX"], 0.0, 500000.0,
                        non_interactive, cli_input)
    f2max = _ask_float("F2MAXLIM (f2max limit, Hz)",
                        defaults["F2MAXLIM"], 0.0, 500000.0,
                        non_interactive, cli_input)
    f1f2 = _ask_float("F1F2MAX (f1/f2 ratio max, 0..1)",
                       defaults["F1F2MAX"], 0.0, 1.0,
                       non_interactive, cli_input)
    toggle = _ask_choice("TOGGLE (ONCE/CONT)",
                          defaults["TOGGLE"],
                          [("ONCE", "ONCE"), ("CONT", "CONT")],
                          non_interactive, cli_input)
    payload = _ask_choice("Payload (10101010/11110000/BOTH)",
                          defaults["payload"],
                          [("10101010", "10101010"),
                           ("11110000", "11110000"),
                           ("BOTH", "BOTH")],
                          non_interactive, cli_input)
    return {
        "F1AVGMIN": f1min,
        "F1AVGMAX": f1max,
        "F2MAXLIM": f2max,
        "F1F2MAX": f1f2,
        "TOGGLE": toggle,
        "payload": payload,
    }


def prompt_less_params(
    *,
    override: Optional[dict] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> dict:
    """SPEC §3.5 — LESS (Receiver Sensitivity, RX) per-test params.

    Includes the opt-in sensitivity sweep sub-prompt (BLE_MSM Ch.7).
    The sweep is OFF by default — the operator explicitly opts in.
    """
    defaults = {
        "TXPWR":        -70.0,
        "FERLIM":        30.8,
        "NUMPKTS":     1500,
        "packet_length": 37,
        "DIRTYTX":      "OFF",
        "sweep": {
            "enabled": False,
            "start_dbm": -90.0,
            "stop_dbm":  -60.0,
            "step_db":     2.0,
            "fer_lim":    30.8,
            "offset_db":   0.0,
        },
    }
    if override is not None:
        v = {**defaults, **override}
        if "TXPWR" in v:
            if not -90.0 <= float(v["TXPWR"]) <= 0.0:
                raise ValueError(f"TXPWR must be -90..0 dBm, got {v['TXPWR']}")
        if "FERLIM" in v:
            if not 0.1 <= float(v["FERLIM"]) <= 100.0:
                raise ValueError(f"FERLIM must be 0.1..100, got {v['FERLIM']}")
        if "NUMPKTS" in v:
            n = int(v["NUMPKTS"])
            if not 1 <= n <= 10000:
                raise ValueError(f"NUMPKTS must be 1..10000, got {n}")
        if "packet_length" in v:
            pl = int(v["packet_length"])
            if not 2 <= pl <= 255:
                raise ValueError(f"packet_length must be 2..255, got {pl}")
        if "DIRTYTX" in v and v["DIRTYTX"] not in ("ON", "OFF"):
            raise ValueError(f"DIRTYTX must be ON/OFF, got {v['DIRTYTX']!r}")
        if v.get("sweep", {}).get("enabled"):
            sw = v["sweep"]
            if not -90.0 <= float(sw["start_dbm"]) <= 0.0:
                raise ValueError(f"sweep.start_dbm must be -90..0 dBm")
            if not -90.0 <= float(sw["stop_dbm"]) <= 0.0:
                raise ValueError(f"sweep.stop_dbm must be -90..0 dBm")
            if float(sw["start_dbm"]) > float(sw["stop_dbm"]):
                raise ValueError(f"sweep.start_dbm must be <= stop_dbm")
            if not 1.0 <= float(sw["step_db"]) <= 10.0:
                raise ValueError(f"sweep.step_db must be 1..10 dB")
        return v
    if non_interactive:
        return defaults

    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "LESS — Receiver Sensitivity (SPEC §3.5)")
    _cli_print(non_interactive, "=" * 60)
    txpwr = _ask_float("TXPWR (EUT Rx power level, dBm)",
                        defaults["TXPWR"], -90.0, 0.0,
                        non_interactive, cli_input)
    ferlim = _ask_float("FERLIM (PER limit, %)",
                         defaults["FERLIM"], 0.1, 100.0,
                         non_interactive, cli_input)
    numpkt = _ask_int("NUMPKTS (per Ch.5 step 11)",
                       defaults["NUMPKTS"], 1, 10000,
                       non_interactive, cli_input)
    pkt_len = _ask_int("Packet length (bytes)",
                        defaults["packet_length"], 2, 255,
                        non_interactive, cli_input)
    dirty = _ask_choice("Dirty TX (ON/OFF)",
                         defaults["DIRTYTX"],
                         [("ON", "ON"), ("OFF", "OFF")],
                         non_interactive, cli_input)
    sweep_enabled = _ask_yes_no("Enable sensitivity sweep (BLE_MSM Ch.7)?",
                                 False, non_interactive, cli_input)
    sweep = {"enabled": False}
    if sweep_enabled:
        _cli_print(non_interactive, "  -- Sensitivity sweep parameters --")
        sw_start = _ask_float("Sweep start RF level (dBm)",
                               defaults["sweep"]["start_dbm"], -90.0, 0.0,
                               non_interactive, cli_input)
        sw_stop = _ask_float("Sweep stop RF level (dBm)",
                              defaults["sweep"]["stop_dbm"], -90.0, 0.0,
                              non_interactive, cli_input)
        sw_step = _ask_float("Sweep step (dB)",
                              defaults["sweep"]["step_db"], 1.0, 10.0,
                              non_interactive, cli_input)
        sw_fer = _ask_float("Sweep FER limit (%)",
                             defaults["sweep"]["fer_lim"], 0.1, 100.0,
                             non_interactive, cli_input)
        sw_off = _ask_float("Sweep path-loss offset (dB)",
                             defaults["sweep"]["offset_db"], None, None,
                             non_interactive, cli_input)
        sweep = {
            "enabled": True,
            "start_dbm": sw_start,
            "stop_dbm":  sw_stop,
            "step_db":   sw_step,
            "fer_lim":   sw_fer,
            "offset_db": sw_off,
        }
    return {
        "TXPWR": txpwr,
        "FERLIM": ferlim,
        "NUMPKTS": numpkt,
        "packet_length": pkt_len,
        "DIRTYTX": dirty,
        "sweep": sweep,
    }


def prompt_lemp_params(
    *,
    override: Optional[dict] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> dict:
    """SPEC §3.6 — LEMP (Maximum Input Signal, RX). Opt 27 required."""
    defaults = {
        "TXPWR":        -10.0,
        "FERLIM":         1.0,
        "NUMPKTS":     1500,
        "packet_length": 37,
    }
    if override is not None:
        v = {**defaults, **override}
        if "TXPWR" in v:
            if not -90.0 <= float(v["TXPWR"]) <= 0.0:
                raise ValueError(f"TXPWR must be -90..0 dBm, got {v['TXPWR']}")
        if "FERLIM" in v:
            if not 0.1 <= float(v["FERLIM"]) <= 100.0:
                raise ValueError(f"FERLIM must be 0.1..100, got {v['FERLIM']}")
        if "NUMPKTS" in v:
            n = int(v["NUMPKTS"])
            if not 1 <= n <= 10000:
                raise ValueError(f"NUMPKTS must be 1..10000, got {n}")
        if "packet_length" in v:
            pl = int(v["packet_length"])
            if not 2 <= pl <= 255:
                raise ValueError(f"packet_length must be 2..255, got {pl}")
        return v
    if non_interactive:
        return defaults

    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "LEMP — Maximum Input Signal (SPEC §3.6, Opt 27)")
    _cli_print(non_interactive, "=" * 60)
    txpwr = _ask_float("TXPWR (EUT Rx power level, dBm)",
                        defaults["TXPWR"], -90.0, 0.0,
                        non_interactive, cli_input)
    ferlim = _ask_float("FERLIM (PER limit, %)",
                         defaults["FERLIM"], 0.1, 100.0,
                         non_interactive, cli_input)
    numpkt = _ask_int("NUMPKTS",
                       defaults["NUMPKTS"], 1, 10000,
                       non_interactive, cli_input)
    pkt_len = _ask_int("Packet length (bytes)",
                        defaults["packet_length"], 2, 255,
                        non_interactive, cli_input)
    return {
        "TXPWR": txpwr,
        "FERLIM": ferlim,
        "NUMPKTS": numpkt,
        "packet_length": pkt_len,
    }


def prompt_lepri_params(
    *,
    override: Optional[dict] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> dict:
    """SPEC §3.7 — LEPRI (PER Report Integrity, RX). Opt 27 required.

    NUMPKTS must be EVEN when PKTNUMMODE=FIXED (per RPM p.10-50).
    """
    defaults = {
        "PKTNUMMODE":  "RANDOM",
        "NUMPKTS":     100,   # even
        "NUMCYC":        3,
        "TXPWR":       -30.0,
        "LOWPERLIM":    50.0,
        "HIGHPERLIM":   60.5,
    }
    if override is not None:
        v = {**defaults, **override}
        if "PKTNUMMODE" in v and v["PKTNUMMODE"] not in ("RANDOM", "FIXED"):
            raise ValueError(f"PKTNUMMODE must be RANDOM/FIXED, "
                             f"got {v['PKTNUMMODE']!r}")
        if "NUMPKTS" in v:
            n = int(v["NUMPKTS"])
            if not 10 <= n <= 10000:
                raise ValueError(f"NUMPKTS must be 10..10000, got {n}")
            if v.get("PKTNUMMODE", "RANDOM") == "FIXED" and n % 2 != 0:
                raise ValueError(f"NUMPKTS must be even in FIXED mode, got {n}")
        if "NUMCYC" in v:
            n = int(v["NUMCYC"])
            if not 1 <= n <= 5:
                raise ValueError(f"NUMCYC must be 1..5, got {n}")
        if "TXPWR" in v:
            if not -90.0 <= float(v["TXPWR"]) <= 0.0:
                raise ValueError(f"TXPWR must be -90..0 dBm, got {v['TXPWR']}")
        return v
    if non_interactive:
        return defaults

    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "LEPRI — PER Report Integrity (SPEC §3.7, Opt 27)")
    _cli_print(non_interactive, "=" * 60)
    mode = _ask_choice("PKTNUMMODE (RANDOM/FIXED)",
                        defaults["PKTNUMMODE"],
                        [("RANDOM", "RANDOM"), ("FIXED", "FIXED")],
                        non_interactive, cli_input)
    numpkt = _ask_int("NUMPKTS (must be even in FIXED mode)",
                       defaults["NUMPKTS"], 10, 10000,
                       non_interactive, cli_input)
    if mode == "FIXED" and numpkt is not None and numpkt % 2 != 0:
        numpkt = numpkt + 1
    numcyc = _ask_int("NUMCYC (1..5)",
                       defaults["NUMCYC"], 1, 5,
                       non_interactive, cli_input)
    txpwr = _ask_float("TXPWR (EUT Rx power level, dBm)",
                        defaults["TXPWR"], -90.0, 0.0,
                        non_interactive, cli_input)
    lowlim = _ask_float("LOWPERLIM (low PER limit, %)",
                         defaults["LOWPERLIM"], 0.0, 100.0,
                         non_interactive, cli_input)
    hilim = _ask_float("HIGHPERLIM (high PER limit, %)",
                        defaults["HIGHPERLIM"], 0.0, 100.0,
                        non_interactive, cli_input)
    return {
        "PKTNUMMODE": mode,
        "NUMPKTS": numpkt,
        "NUMCYC": numcyc,
        "TXPWR": txpwr,
        "LOWPERLIM": lowlim,
        "HIGHPERLIM": hilim,
    }


def prompt_leps_params(
    *,
    override: Optional[dict] = None,
    preflight: Optional[Preflight] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> dict:
    """SPEC §3.8 — LEPS (Tx Power Stability / CTE, TX). Opt 37 required.

    Returns a dict with CTESLOT, NUMANTMODE, NUMANT, SWPATLEN,
    REFPWRLIM, SLOTPWRLIM, LECTETIME, packet_length.

    If Opt 37 is not present on the bench and the operator did not
    override, returns `{"_skip": True, "reason": "Opt 37 missing"}` so
    `run_campaign` can drop LEPS with a clear message.
    """
    defaults = {
        "CTESLOT":    "2US",   # 2US TRUE per RPM
        "NUMANTMODE": "AUTO",  # AUTO default per RPM p.12-17
        "NUMANT":     4,       # used only when NUMANTMODE=MANUAL
        "SWPATLEN":   2,
        "REFPWRLIM":  1.5,
        "SLOTPWRLIM": 2.0,
        "LECTETIME":  20,
        "packet_length": 0,    # 0 per scpi.meas_ble_cap_cte docstring
    }
    if override is not None:
        v = {**defaults, **override}
        if v.get("_skip"):
            return v
        if "CTESLOT" in v and v["CTESLOT"] not in ("1US", "2US"):
            raise ValueError(f"CTESLOT must be 1US/2US, got {v['CTESLOT']!r}")
        if "NUMANTMODE" in v and v["NUMANTMODE"] not in ("AUTO", "MANUAL"):
            raise ValueError(f"NUMANTMODE must be AUTO/MANUAL, "
                             f"got {v['NUMANTMODE']!r}")
        if "NUMANT" in v:
            n = int(v["NUMANT"])
            if not 2 <= n <= 75:
                raise ValueError(f"NUMANT must be 2..75, got {n}")
        if "SWPATLEN" in v:
            n = int(v["SWPATLEN"])
            if not 2 <= n <= 75:
                raise ValueError(f"SWPATLEN must be 2..75, got {n}")
        if "LECTETIME" in v:
            n = int(v["LECTETIME"])
            if not 2 <= n <= 20:
                raise ValueError(f"LECTETIME must be 2..20 (units of 8 us), got {n}")
        if "packet_length" in v:
            pl = int(v["packet_length"])
            if not (pl == 0 or 2 <= pl <= 255):
                raise ValueError(f"packet_length must be 0 or 2..255 (LEPS), got {pl}")
        return v
    # Opt 37 check (SPEC §3.8): bench is currently NOT option-37-
    # equipped per project memory; the prompt must skip LEPS with a
    # clear message if option 37 is absent.
    if preflight is not None and not preflight.has_opt("37"):
        if non_interactive:
            return {"_skip": True,
                    "reason": "LEPS requires MT8852B Opt 37 (BLE CTE / 2LE CTE) "
                              "which is not installed on this bench."}
        _cli_print(non_interactive, "")
        _cli_print(non_interactive, "  ! LEPS skipped: Opt 37 (BLE CTE / 2LE CTE) "
                                    "is not installed on this bench.")
        _cli_print(non_interactive, "  ! See bench OPTSTATUS above. To enable LEPS, "
                                    "install Opt 37 and re-run.")
        return {"_skip": True,
                "reason": "Opt 37 missing on bench"}
    if non_interactive:
        return defaults

    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "LEPS — Tx Power Stability (CTE) (SPEC §3.8, Opt 37)")
    _cli_print(non_interactive, "=" * 60)
    cte = _ask_choice("CTESLOT (1US/2US)",
                       defaults["CTESLOT"],
                       [("1US", "1US"), ("2US", "2US")],
                       non_interactive, cli_input)
    antmode = _ask_choice("NUMANTMODE (AUTO/MANUAL)",
                           defaults["NUMANTMODE"],
                           [("AUTO", "AUTO"), ("MANUAL", "MANUAL")],
                           non_interactive, cli_input)
    ant = defaults["NUMANT"]
    if antmode == "MANUAL":
        ant = _ask_int("NUMANT (2..75)",
                        defaults["NUMANT"], 2, 75,
                        non_interactive, cli_input)
    swpatlen = _ask_int("SWPATLEN (switching pattern length, 2..75)",
                         defaults["SWPATLEN"], 2, 75,
                         non_interactive, cli_input)
    refpwrl = _ask_float("REFPWRLIM (reference power ratio limit, dB)",
                          defaults["REFPWRLIM"], None, None,
                          non_interactive, cli_input)
    slotpwrl = _ask_float("SLOTPWRLIM (slot power ratio limit, dB)",
                           defaults["SLOTPWRLIM"], None, None,
                           non_interactive, cli_input)
    lectime = _ask_int("LECTETIME (CTE time, 2..20 units of 8 us)",
                        defaults["LECTETIME"], 2, 20,
                        non_interactive, cli_input)
    pkt_len = _ask_int("Packet length (0 or 2..255; 0 for LEPS)",
                        defaults["packet_length"], 0, 255,
                        non_interactive, cli_input)
    if pkt_len == 0 or pkt_len is None:
        pkt_len = 0
    return {
        "CTESLOT": cte,
        "NUMANTMODE": antmode,
        "NUMANT": ant,
        "SWPATLEN": swpatlen,
        "REFPWRLIM": refpwrl,
        "SLOTPWRLIM": slotpwrl,
        "LECTETIME": lectime,
        "packet_length": pkt_len,
    }


PROMPT_PER_TEST = {
    "LEOP":  prompt_leop_params,
    "LEICD": prompt_leicd_params,
    "LEMI":  prompt_lemi_params,
    "LESS":  prompt_less_params,
    "LEMP":  prompt_lemp_params,
    "LEPRI": prompt_lepri_params,
    "LEPS":  prompt_leps_params,
}


def prompt_no_control_confirm(
    *,
    override: Optional[bool] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> bool:
    """SPEC §9 Q5 — warning-confirmed prompt for EUT Control = NONE.

    Per BLE_MSM §6-1: "Test scripts cannot be run from the Bluetooth
    low energy measurement software if the EUT does not have an
    available Direct Test Mode connection." The MCP can only poll
    `*INS?` and read whatever ORESULT the vendor app drives — there is
    no automated HCI path.

    Returns True if the operator wants to proceed in watch-only mode,
    False to abort the campaign.
    """
    if override is not None:
        return bool(override)
    if non_interactive:
        # Default: refuse. The caller can pass override=True if they
        # really want to run unattended.
        return False
    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "  ! WARNING: EUT Control = No Control.")
    _cli_print(non_interactive, "  ! Script testing is disabled in this mode (BLE_MSM §6-1).")
    _cli_print(non_interactive, "  ! The MCP cannot issue HCI_Reset / HCI_LE_Transmitter_Test.")
    _cli_print(non_interactive, "  ! You must drive the DUT from the vendor app, and the MCP "
                                "will only poll *INS? and read whatever ORESULT comes back.")
    return _ask_yes_no("Proceed in watch-only mode?", False,
                       non_interactive, cli_input)


# ----------------------------------------------------------------------------
# Final confirmation prompt — emitted by run_campaign when
# `force_final_confirm=True` (set by the MCP tool `bt_run_full_suite`).
#
# This closes the loophole where an AI client pre-fills every
# `bt_run_full_suite` kwarg and skips every question, so the operator
# never sees the plan. Even with all params supplied, this prompt fires
# once with a summary of what is about to run, and the operator has to
# explicitly choose "Yes, run all" before any session.start() /
# MEASBLECAPX command is issued.
#
# Envelope shape mirrors the other NeedInput envelopes (same
# `state="awaiting_input"` + `prompt: {id, question, choices, default,
# help}`). The `plan` field is informational — the chat agent can show
# it to the operator alongside the question.
# ----------------------------------------------------------------------------
FINAL_CONFIRM_CHOICES = [
    {"label": "Yes, run all", "value": "yes"},
    {"label": "Cancel",       "value": "no"},
]


def prompt_final_confirm(
    plan: dict,
    *,
    override: Optional[bool] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
) -> bool:
    """Non-skippable final confirmation gate before any test runs.

    `plan` is a free-form summary dict (eut_interface, tests, channels,
    capture_type, packet_length, run_label, etc.) — informational
    only, used in the envelope's `help` text and printed in CLI mode.

    Returns True if the operator confirmed, False to abort.

    Library callers (run_ble_full_suite.py, batch scripts) pass
    `override=True` to skip this gate in unattended mode. The MCP tool
    `bt_run_full_suite` does NOT pass `override`, so this gate always
    fires there (and `run_campaign` is invoked with
    `force_final_confirm=True`, which is what causes this function to
    be reached in the first place).
    """
    if override is not None:
        return bool(override)

    # Build the human-readable summary from `plan`.
    eut_iface = plan.get("eut_interface", "?")
    tests = plan.get("tests") or []
    channels = plan.get("channels") or []
    capture_type = plan.get("capture_type", "?")
    pkt_len = plan.get("packet_length", "?")
    run_label = plan.get("run_label", "") or "(timestamp only)"

    summary_lines = [
        f"  EUT interface   : {eut_iface}",
        f"  Tests           : {', '.join(tests) if tests else '(none)'}",
        f"  Channels        : {', '.join(str(c) for c in channels) if channels else '(none)'}",
        f"  Capture type    : {capture_type}",
        f"  Packet length   : {pkt_len}",
        f"  Run label       : {run_label}",
    ]
    summary = "\n".join(summary_lines)

    if non_interactive:
        # In MCP mode (non_interactive=True), emit the awaiting_input
        # envelope. The chat agent surfaces it; the operator's choice
        # becomes the `final_confirm` override on the next call.
        help_text = (
            "All campaign parameters have been collected. "
            "Review the plan below and confirm to start the BLE TX/RX "
            "test campaign. This is the LAST gate before any test "
            "executes on the bench.\n" + summary
        )
        raise NeedInput({
            "ok": True,
            "state": "awaiting_input",
            "prompt": {
                "id": "final_confirm",
                "question": "Run the BLE test campaign now?",
                "choices": FINAL_CONFIRM_CHOICES,
                "default": "no",   # refuse by default — operator must opt in
                "help": help_text,
                "plan": plan,
            },
        })

    # CLI mode (non_interactive=False).
    _cli_print(non_interactive, "")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, "Final confirmation — about to run:")
    _cli_print(non_interactive, "=" * 60)
    _cli_print(non_interactive, summary)
    _cli_print(non_interactive, "=" * 60)
    return _ask_yes_no("Run the BLE test campaign now?", False,
                       non_interactive, cli_input)


# ----------------------------------------------------------------------------
# Per-test parameter prompts (for the spec §3 cases that need a value
# other than the script-3 default). For the v1, we only prompt for the
# core ones (data_rate, channels, packet_length). Per-test deeper
# overrides are exposed via the `test_params` dict on the MCP tool.
# ----------------------------------------------------------------------------
TEST_PARAM_FIELDS = {
    "LEOP":  ["AVGMXLIM", "AVGMNLIM", "PEAKLIM", "packet_length"],
    "LEICD": ["MXPOSLIM", "MXNEGLIM", "DFTBLELIM", "packet_length"],
    "LEMI":  ["F1AVGMIN", "F1AVGMAX", "F2MAXLIM", "F1F2MAX"],
    "LESS":  ["TXPWR", "FERLIM", "NUMPKTS", "packet_length", "DIRTYTX"],
    "LEMP":  ["TXPWR", "FERLIM", "NUMPKTS", "packet_length"],
    "LEPRI": ["PKTNUMMODE", "NUMPKTS", "NUMCYC", "TXPWR"],
    "LEPS":  ["CTESLOT", "NUMANTMODE", "SWPATLEN", "REFPWRLIM", "SLOTPWRLIM",
              "packet_length", "LECTETIME"],
}


def apply_test_params(sess: MT8852BSession,
                      test_id: str,
                      params: dict) -> None:
    """Apply per-test param overrides to the bench's preloaded script.

    The state machine already trusts the preloaded script 3 defaults
    (per project_8852b_v68_three_bug_fix.md — re-flushing SCPTCFG /
    LEMICFG.TOGGLE between iterations was breaking v6). This function
    only writes the *limit* SCPI fields (LEOPCFG / LESSCFG / etc.),
    never SCPTCFG, so we don't disturb the working recipe.
    """
    if not params:
        return
    script = sess.script
    if test_id == "LEOP":
        for k in ("AVGMXLIM", "AVGMNLIM", "PEAKLIM"):
            if k in params:
                sess.instr.write(scpi.leopcfg(script, k, params[k]))
    elif test_id == "LEICD":
        for k in ("MXPOSLIM", "MXNEGLIM", "DFTBLELIM",
                  "INITDFTBLERATE", "DFTBLERATE"):
            if k in params:
                sess.instr.write(scpi.leicdcfg(script, k, params[k]))
    elif test_id == "LEMI":
        for k in ("F1AVGMIN", "F1AVGMAX", "F2MAXLIM", "F1F2MAX"):
            if k in params:
                sess.instr.write(scpi.lemicfg(script, k, params[k]))
    elif test_id == "LESS":
        for k in ("TXPWR", "FERLIM", "NUMPKTS", "DIRTYTX"):
            if k in params:
                sess.instr.write(scpi.lesscfg(script, k, params[k]))
    elif test_id == "LEMP":
        for k in ("TXPWR", "FERLIM", "NUMPKTS"):
            if k in params:
                sess.instr.write(scpi.lempcfg(script, k, params[k]))
    elif test_id == "LEPRI":
        for k in ("PKTNUMMODE", "NUMPKTS", "NUMCYC", "TXPWR",
                  "LOWPERLIM", "HIGHPERLIM"):
            if k in params:
                sess.instr.write(scpi.lepricfg(script, k, params[k]))
    elif test_id == "LEPS":
        for k in ("CTESLOT", "NUMANTMODE", "SWPATLEN", "REFPWRLIM",
                  "SLOTPWRLIM", "LECTETIME"):
            if k in params:
                sess.instr.write(scpi.lepscfg_via_scpi(script, k, params[k]))


# ----------------------------------------------------------------------------
# Apply per-test SCPI for LEPS (we extend the scpi module inline since
# the spec adds new LEPSCFG helpers).
# ----------------------------------------------------------------------------
def _lepscfg_via_scpi(script: int, field: str, value) -> str:
    return f"LEPSCFG {script},{field},{value}"

# Attach as a shim onto the scpi module so we don't have to extend
# scpi.py in this round (per spec, the existing `le*cfg(script, field,
# value)` builders are sufficient; we just route LEPS through a
# dedicated helper).
scpi.lepscfg_via_scpi = _lepscfg_via_scpi  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------
# Campaign driver — the per-call entry point the MCP tool uses.
# Stateless across calls; the operator's previous answers are echoed
# in `completed` / `remaining` so a chat agent can resume.
# ----------------------------------------------------------------------------
CAMPAIGN_PROMPT_ORDER = [
    "eut_interface",
    "tests",
    "capture_type",
    "channels",
    "packet_length",
    "run_label",
]


def run_campaign(
    sess: MT8852BSession,
    *,
    eut_interface: Optional[str] = None,
    eut_baud: Optional[int] = None,
    eut_handshake: Optional[str] = None,
    tests: Optional[list[str]] = None,
    test_params: Optional[dict] = None,
    channels: Optional[list[int]] = None,
    capture_type: Optional[str] = None,
    packet_length: Optional[int] = None,
    report_dir: Optional[str] = None,
    run_label: Optional[str] = None,
    non_interactive: bool = False,
    cli_input: Callable[[str], str] = input,
    log_file_handler: Optional[logging.Handler] = None,
    force_final_confirm: bool = False,
) -> dict:
    """Drive the full BLE TX/RX campaign.

    `force_final_confirm=True` causes `prompt_final_confirm()` to be
    emitted immediately before `_execute_campaign()` — i.e. after every
    campaign parameter has been collected but BEFORE any
    `session.start()` / `MEASBLECAPX` is sent to the bench. The MCP tool
    `bt_run_full_suite` sets this unconditionally; library callers
    (run_ble_full_suite.py, batch scripts) leave it False so they can
    run unattended. The prompt fires regardless of whether the AI
    pre-filled every `override` kwarg — closing the "AI pre-fills every
    param and the human never sees anything" loophole.

    Returns one of:
      - {"ok": True,  "state": "running", "report_dir": "...",
         "started": "...", "results": [...]} on completion
      - {"ok": True,  "state": "awaiting_input", "prompt": {...}}
         on the first missing required param
      - {"ok": False, "error": "..."} on failure

    `non_interactive=True` makes the function fail (return
    awaiting_input with `error: "non-interactive mode"`? No — it
    returns the awaiting_input envelope with default values filled
    in. The chat agent can override them and re-call.
    """
    preflight: Optional[Preflight] = None
    if sess.instr.connected:
        try:
            preflight = run_preflight(sess)
        except Exception as e:
            log.warning("preflight failed: %s", e)

    # Apply EUT interface first (this is the "load-bearing" one — the
    # other tests need a working EUT link before they can produce
    # valid ORESULT data).
    try:
        iface, baud, handshake = prompt_eut_interface(
            preflight,
            override=eut_interface,
            non_interactive=non_interactive,
            cli_input=cli_input,
        )
    except NeedInput as e:
        return _add_session_state(e.envelope, completed=[], remaining=list(CAMPAIGN_PROMPT_ORDER))

    if iface == "NONE":
        # No Control mode — per spec §2.2 row 6, the bench can't issue
        # HCI_Reset / HCI_LE_Transmitter_Test. The MCP can only poll
        # *INS? and read whatever ORESULT the vendor app drives.
        # Per SPEC §9 Q5: explicit warning-confirmed prompt before
        # proceeding in watch-only mode. In non-interactive mode the
        # operator's choice of eut_interface=NONE IS the explicit
        # acknowledgment, so we proceed without re-prompting.
        if non_interactive:
            log.warning("EUT Control = NONE — running unattended in "
                        "watch-only mode (no re-confirmation in "
                        "non-interactive mode)")
        else:
            try:
                proceed = prompt_no_control_confirm(
                    non_interactive=non_interactive,
                    cli_input=cli_input,
                )
            except NeedInput as e:
                return _add_session_state(e.envelope,
                                          completed=["eut_interface"],
                                          remaining=["no_control_confirm",
                                                     "tests", "capture_type",
                                                     "channels", "packet_length",
                                                     "run_label", "test_params"])
            if not proceed:
                return {"ok": False,
                        "error": ("EUT Control = NONE was not confirmed; "
                                  "aborting.")}
            log.warning("EUT Control = NONE — running in watch-only mode")
    else:
        try:
            actual = sess.set_eut_interface(
                iface, baud, handshake, skip_if_ready=True)
            log.info("EUT interface set to %s (skip_if_ready=True)", actual)
        except Exception as e:
            return {"ok": False, "error": f"set_eut_interface failed: {e}",
                    "preflight": preflight.to_dict() if preflight else None}

    # Prompt for test selection
    try:
        chosen_tests = prompt_test_selection(
            override=tests,
            preflight=preflight,
            non_interactive=non_interactive,
            cli_input=cli_input,
        )
    except NeedInput as e:
        return _add_session_state(e.envelope, completed=["eut_interface"],
                                  remaining=[p for p in CAMPAIGN_PROMPT_ORDER
                                             if p != "eut_interface"])

    # Data rate
    try:
        rate = prompt_data_rate(
            override=capture_type,
            default="BLE",
            non_interactive=non_interactive,
            cli_input=cli_input,
        )
    except NeedInput as e:
        return _add_session_state(e.envelope, completed=["eut_interface", "tests"],
                                  remaining=[p for p in CAMPAIGN_PROMPT_ORDER
                                             if p not in ("eut_interface", "tests")])

    # Channels
    try:
        ch_list = prompt_channels(
            override=channels,
            default=list(DEFAULT_CHANNELS),
            non_interactive=non_interactive,
            cli_input=cli_input,
        )
    except NeedInput as e:
        return _add_session_state(e.envelope, completed=["eut_interface", "tests", "capture_type"],
                                  remaining=[p for p in CAMPAIGN_PROMPT_ORDER
                                             if p not in ("eut_interface", "tests", "capture_type")])

    # Packet length
    try:
        pkt_len = prompt_packet_length(
            override=packet_length,
            default=37,
            non_interactive=non_interactive,
            cli_input=cli_input,
        )
    except NeedInput as e:
        return _add_session_state(e.envelope,
                                  completed=["eut_interface", "tests", "capture_type", "channels"],
                                  remaining=[p for p in CAMPAIGN_PROMPT_ORDER
                                             if p not in ("eut_interface", "tests", "capture_type", "channels")])

    # Run label
    try:
        label_suffix = prompt_report_dir(
            override=run_label,
            non_interactive=non_interactive,
            cli_input=cli_input,
        )
    except NeedInput as e:
        return _add_session_state(e.envelope,
                                  completed=["eut_interface", "tests", "capture_type",
                                             "channels", "packet_length"],
                                  remaining=["run_label"])

    # ----------------------------------------------------------------
    # Per-test parameter collection (SPEC §3.2-§3.8)
    #
    # For each chosen test, call the matching prompt_<test>_params()
    # unless the operator has already supplied the params via the
    # `test_params` dict on the MCP tool. LEPS is skipped when Opt 37
    # is missing (the prompt returns {"_skip": True, "reason": ...}).
    #
    # The state machine for "have we collected all the params" is the
    # `test_params` dict itself: the caller passes back whatever they
    # collected so far, and we resume from there. If a prompt raises
    # NeedInput we return the envelope plus a `completed` list showing
    # which test-param keys are already collected.
    # ----------------------------------------------------------------
    test_params = dict(test_params or {})
    dropped_tests: list[str] = []
    per_test_remaining = [t for t in chosen_tests if t not in test_params]
    for tid in per_test_remaining:
        prompt_fn = PROMPT_PER_TEST.get(tid)
        if prompt_fn is None:
            continue
        try:
            params = prompt_fn(
                override=test_params.get(tid),
                preflight=preflight,
                non_interactive=non_interactive,
                cli_input=cli_input,
            )
        except NeedInput as e:
            completed_keys = [f"test_params:{t}" for t in chosen_tests
                              if t in test_params and t != tid]
            return _add_session_state(
                e.envelope,
                completed=(["eut_interface", "tests", "capture_type",
                            "channels", "packet_length", "run_label"]
                           + completed_keys),
                remaining=[f"test_params:{tid}"],
            )
        if isinstance(params, dict) and params.get("_skip"):
            log.warning("skipping %s: %s", tid, params.get("reason", "?"))
            dropped_tests.append(tid)
            continue
        test_params[tid] = params

    # Drop LEPS (or any other Opt-gated test) from the run.
    if dropped_tests:
        chosen_tests = [t for t in chosen_tests if t not in dropped_tests]
    if not chosen_tests:
        return {"ok": False,
                "error": f"All selected tests were skipped: {dropped_tests}",
                "preflight": preflight.to_dict() if preflight else None}

    # Apply per-test params to bench (limit-only SCPI; never SCPTCFG).
    for tid, params in test_params.items():
        if tid not in TEST_CATALOG:
            continue
        try:
            apply_test_params(sess, tid, params)
        except Exception as e:
            log.warning("apply_test_params(%s) failed: %s", tid, e)

    # ----------------------------------------------------------------
    # Non-skippable final confirmation gate.
    #
    # When `force_final_confirm=True` (set by the MCP tool
    # `bt_run_full_suite`), surface the full plan to the operator and
    # require an explicit "Yes, run all" answer before proceeding.
    # This fires AFTER all per-test params are collected but BEFORE
    # _execute_campaign (no session.start() / MEASBLECAPX yet), so the
    # operator gets one last look at the actual plan — including the
    # case where the AI pre-filled every parameter on the first call.
    # ----------------------------------------------------------------
    if force_final_confirm:
        plan = {
            "eut_interface": iface,
            "eut_baud": baud,
            "eut_handshake": handshake,
            "tests": chosen_tests,
            "channels": ch_list,
            "capture_type": rate,
            "packet_length": pkt_len,
            "run_label": label_suffix or "",
        }
        try:
            confirmed = prompt_final_confirm(
                plan,
                non_interactive=non_interactive,
                cli_input=cli_input,
            )
        except NeedInput as e:
            return _add_session_state(
                e.envelope,
                completed=(["eut_interface", "tests", "capture_type",
                            "channels", "packet_length", "run_label"]
                           + [f"test_params:{t}" for t in chosen_tests
                              if t in test_params]),
                remaining=["final_confirm"],
            )
        if not confirmed:
            return {"ok": False,
                    "error": ("Final confirmation was not given; "
                              "campaign aborted before any test ran."),
                    "plan": plan}

    # Now run the campaign.
    return _execute_campaign(
        sess, preflight, chosen_tests, rate, ch_list, pkt_len,
        label_suffix, report_dir, log_file_handler,
    )


def _execute_campaign(
    sess: MT8852BSession,
    preflight: Optional[Preflight],
    tests: list[str],
    capture_type: str,
    channels: list[int],
    pkt_len: int,
    label_suffix: str,
    report_dir_override: Optional[str],
    log_file_handler: Optional[logging.Handler],
) -> dict:
    """Run the per-test loop and save the report."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if label_suffix:
        run_label = f"ble_full_{label_suffix}_{ts}"
    else:
        run_label = f"ble_full_{ts}"

    if report_dir_override:
        out_dir = Path(report_dir_override)
    else:
        # The legacy CLI writes to <project_root>/reports/<run_label>;
        # we mirror that here. From an MCP context we don't always know
        # the project root, so fall back to cwd/reports.
        try:
            project_root = Path(__file__).resolve().parent.parent
        except Exception:
            project_root = Path.cwd()
        out_dir = project_root / "reports" / run_label
    out_dir.mkdir(parents=True, exist_ok=True)

    # Persist the preflight snapshot (spec §6)
    if preflight is not None:
        (out_dir / "bench_preflight.json").write_text(
            json.dumps(preflight.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # Attach the bench.log handler to the PARENT mcp_8852b logger so
    # that traffic from `mcp_8852b.state_machine` and
    # `mcp_8852b.instrument` propagates up via the standard logger
    # hierarchy and gets captured. The parent level must be DEBUG so
    # child log.debug() calls (one per SCPI write/query) pass the
    # logger filter. Reviewer R5 issue #1.
    _attached_loggers: list[logging.Logger] = []
    if log_file_handler is not None:
        parent = logging.getLogger("mcp_8852b")
        parent.setLevel(logging.DEBUG)
        parent.addHandler(log_file_handler)
        _attached_loggers.append(parent)
        for child in ("mcp_8852b.instrument", "mcp_8852b.state_machine",
                      "mcp_8852b.interactive"):
            logging.getLogger(child).propagate = True

    started = datetime.now().isoformat(timespec="seconds")
    t0 = time.monotonic()
    records: list[TestRecord] = []
    errors: list[str] = []
    for test_id in tests:
        for ch in channels:
            info = TEST_CATALOG[test_id]
            log.info("running %s ch=%d payload=%s",
                     test_id, ch, info["payload"])
            try:
                rec = run_one_test(sess, test_id, ch,
                                   capture_type=capture_type,
                                   packet_length=pkt_len)
            except Exception as e:
                log.error("  test failed: %s", e)
                rec = TestRecord(
                    test_id=test_id,
                    test_name=info["name"],
                    section=info["section"],
                    channel=ch,
                    capture_type=capture_type,
                    packet_length=pkt_len,
                    payload=info["payload"],
                    mod_type=info["mod_type"],
                    raw="",
                    valid=False,
                    fields=[],
                    pass_fail=None,
                    elapsed_s=0.0,
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    errlst_before="",
                    errlst_after=str(e),
                )
                errors.append(f"{test_id} ch{ch}: {e}")
            records.append(rec)
    ended = datetime.now().isoformat(timespec="seconds")
    duration_s = time.monotonic() - t0

    ctx = {
        "run_label": run_label,
        "started": started,
        "ended": ended,
        "duration_s": duration_s,
        "instrument": sess.instr.idn() if sess.instr.connected else "",
        "eut": sess.eut.to_dict() if sess.eut else {},
        "plan": {
            "tests": tests,
            "channels": channels,
            "capture_type": capture_type,
            "packet_length": pkt_len,
        },
    }
    if preflight is not None:
        ctx["preflight"] = preflight.to_dict()

    save_report(records, out_dir, ctx)

    # Detach the log handler so subsequent runs don't double-log
    for lg in _attached_loggers:
        try:
            lg.removeHandler(log_file_handler)
        except Exception:
            pass

    return {
        "ok": True,
        "state": "running",
        "report_dir": str(out_dir),
        "run_label": run_label,
        "started": started,
        "ended": ended,
        "duration_s": duration_s,
        "errors": errors,
        "results": [
            {
                "test_id": r.test_id,
                "channel": r.channel,
                "valid": r.valid,
                "pass_fail": r.pass_fail,
                "raw": r.raw,
            }
            for r in records
        ],
    }


def _add_session_state(envelope: dict, completed: list[str],
                        remaining: list[str]) -> dict:
    """Add a 'session' key to an awaiting_input envelope so a stateless
    caller can resume. The envelope is the dict that NeedInput carries."""
    envelope.setdefault("session", {})
    envelope["session"]["completed"] = list(completed)
    envelope["session"]["remaining"] = list(remaining)
    return envelope


# ----------------------------------------------------------------------------
# bench.log handler — captures pyvisa traffic (mcp_8852b logger) to a
# per-run log file. The MCP tool attaches this in run_campaign().
# ----------------------------------------------------------------------------
class _BenchLogHandler(logging.Handler):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self._fh = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("w", encoding="utf-8")
        except Exception as e:
            log.warning("could not open bench.log %s: %s", self.path, e)

    def emit(self, record: logging.LogRecord) -> None:
        if self._fh is None:
            return
        try:
            msg = self.format(record)
            self._fh.write(msg + "\n")
            self._fh.flush()
        except Exception:
            pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def make_bench_log_handler(out_dir: Path) -> _BenchLogHandler:
    """Return a logging handler that writes mcp_8852b logs to
    `<out_dir>/bench.log`. Attach it to the `mcp_8852b` logger.
    """
    h = _BenchLogHandler(out_dir / "bench.log")
    h.setLevel(logging.DEBUG)
    h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    return h
