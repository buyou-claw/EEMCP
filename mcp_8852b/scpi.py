"""MT8852B command builders.

The MT8852B does NOT use standard colon-separated SCPI. Its GPIB/TCP
command set is flat named commands with whitespace-separated keywords
and comma-separated parameters, e.g.

    SETBLECAPTYP BLE
    CFGBLECAP 0,RF
    MEASBLECAP LEOP,NA,71764129

The exact grammar is in the Anritsu "Bluetooth Test Set MT8852B Remote
Programming Manual" (part M-W3969AE-8.0, 8th Ed), which lives in
`reference/extracted/_all_pages.txt` after extraction. Key chapters:

  Ch. 4  General GPIB commands: *IDN, *RST, OPMD, SYSCFG, LEPKTLEN,
         LEPKTMODE, BOOTSTATUS?, ERRLST, *INS?, *CLS, ...
  Ch. 14 RUN / ABORT
  Ch. 15 ORESULT (read results) and XRESULT (extended results)
  Ch. 16 BLE measurement: SETBLECAPTYP, CFGBLECAP, MEASBLECAP,
         MEASBLECAPX (with packet length), MEASBLECAPX2 (with CTE),
         LESCPTCFG, ABORTCAP, plus per-test config: LEOPCFG, LEICDCFG,
         LEMICFG, LESSCFG, LEPRICFG, LEMPCFG

This file is the single source of truth for every command syntax. No
I/O happens here — `instrument.py` owns the pyvisa transport and calls
these builders.

Result data layout returned by ORESULT (see Ch. 15, §15-2):

    ORESULT TEST,0,LEOP          <-- query
    LEOP0,TRUE,1.32,1.32,1.32,0.19,0,2,PASS    <-- response

The body is comma-separated: `<header><ext-code>,<results_valid>,...data...,<pass/fail>`.
Test headers and their data fields are listed in the manual (Ch. 15).
"""
from __future__ import annotations
from typing import Literal, Optional


# ----------------------------------------------------------------------------
# Standard IEEE-488.2 / instrument control
# ----------------------------------------------------------------------------
def idn() -> str:
    """`*IDN?` — returns `Anritsu,MT8852B,<serial>,<firmware>`."""
    return "*IDN?"


def reset() -> str:
    """`*RST` — full reset. Note: invalidates any pending results (sets
    the "Results Valid" flag to FALSE)."""
    return "*RST"


def clear_status() -> str:
    """`*CLS` — clear status registers (ESR, INR, EETF, etc.)."""
    return "*CLS"


def opc() -> str:
    """`*OPC?` — block until prior command completes. Used by the official
    Anritsu driver between every multi-step setup block. We emit it
    after SCPTRST, after SCPTTSTGP, after SCPTSEL, after SERVULP, and
    after RUN. Each `*OPC?` returns `1` when the 8852B has fully drained
    its command queue."""
    return "*OPC?"


def esr_query() -> str:
    """`*ESR?` — read Event Status Register (decimal byte). The official
    driver reads this after RUN to detect command / execution / device-
    dependent errors. Non-zero bits mean:

      bit 0 (0x01) OPC    — operation complete (always 0 here, *OPC? is used)
      bit 2 (0x04) QYE    — query error
      bit 3 (0x08) DDE    — device-dependent error
      bit 4 (0x10) EXE    — execution error
      bit 5 (0x20) CME    — command error
      bit 7 (0x80) PON    — power-on
    """
    return "*ESR?"


def ins_query() -> str:
    """`*INS?` — read Instrument Status Register. Bit 2 (CMP) = test/script
    complete. Bit 0 (CON) = connected. Bit 5 (INQ) = inquiry complete.
    Returns a decimal byte."""
    return "*INS?"


def system_error() -> str:
    """Read the error-state latch (`ERRLST`). Returns a 38-character hex
    string packing connection / HCI / DSP state. Clears the latch on read.

    Per Anritsu Remote Programming Manual §4-2, the response shape is
    `ABCCDDEFGHIIJKK!LLLLLLL!MMMMMMM!NNNNNNN!OOOOOOO!` where each
    field is a diagnostic indicator. The field widths matter — note
    `J` is one char, `KK` is two:

      A  Connection Already Exists      (0/1)
      B  EUT Test Mode State            (0=enabled, 1=not enabled)
      CC EUT HCI Error                  (00=OK, else 2-hex HCI error code,
                                         EUT controlled via RS232)
      DD Internal HCI Error             (00=OK, else 2-hex error code)
      E  Internal Sync Error            (0/1)
      F  EUT Sync Error                 (0/1, EUT HCI sync via RS232)
      G  EUT Hardware Error             (0/1, EUT-reported HCI hw error)
      H  Request Failed (system busy)   (0/1)
      II DSP Status                     (00=OK, 01..23 = specific errors —
                                         01=searching channel, 02=searching
                                         sync word, 06=incorrect packet, etc.)
      J  EUT BT Address                 (0=OK, 1=No EUT BD_ADDR set in
                                         MANUAL mode)
      KK HCI Comm Status                (00=OK, 01=unknown HCI command,
                                         02=no connection, 03=hardware
                                         failure, 04=paging timeout,
                                         05=connection timeout,
                                         06=unsupp feat param,
                                         07=conn ended by user,
                                         08=low resource, 09=power off,
                                         10=local host ended,
                                         11=unsupp remote feat)
      LLLLLLL EUT reported last opcode  (hex)
      MMMMMMM EUT reported last event   (hex)
      NNNNNNN Internal last opcode      (hex)
      OOOOOOO Internal last event      (hex)

    This is the most useful single diagnostic for a broken HCI link —
    fields CC/DD/E/F/G/J/KK tell you *why* the link is broken, and
    LLLLLLL/MMMMMMM show the last HCI opcode/event the EUT/8852B saw.
    """
    return "ERRLST"


def boot_status() -> str:
    """Startup self-test result. Run right after power-on to confirm the
    unit is healthy."""
    return "BOOTSTATUS?"


# ----------------------------------------------------------------------------
# Operation mode
# ----------------------------------------------------------------------------
OPMD_SCRIPT = "SCRIPT"        # script-driven tests
OPMD_STEST  = "STEST"         # single-test mode (with optional test param)


def opmd(mode: str, test: Optional[str] = None) -> str:
    """Set operation mode. BLE testing uses `SCRIPT`.

    Args:
        mode: SCRIPT (full script with all configured tests), or STEST
              (single test, requires `test` parameter).
        test: optional single-test name for STEST (e.g. "LEOP").
    """
    if mode == "SCRIPT":
        return "OPMD SCRIPT"
    if mode == "STEST":
        if not test:
            raise ValueError("STEST mode requires a test parameter")
        return f"OPMD STEST,{test}"
    raise ValueError(f"unknown opmd mode {mode!r}; use SCRIPT or STEST")


def opmd_query() -> str:
    return "OPMD?"


# ----------------------------------------------------------------------------
# SCPTSEL — select active script (Ch. 4, §4-15)
# ----------------------------------------------------------------------------
def scptsel(script: int) -> str:
    """`SCPTSEL <n>` — select the active script (1..10).

    The MT8852B holds up to 10 scripts. Scripts 1 and 2 are predefined
    (BT/EDR); scripts 3..10 are user-configurable for BLE. MEASBLECAP[X]
    uses the limit parameters of the selected script (per Ch. 16 §16-4),
    so this MUST be set before MEASBLECAPX or the test runs against
    whatever script was last selected.

    The MT8852B's *OPC example (Ch. 4) pairs `OPMD SCRIPT; SCPTSEL 3`
    every time, so the official recipe always sets both together.
    """
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"SCPTSEL {script}"


def scptsel_query() -> str:
    return "SCPTSEL?"


def scptrst(script: int | str) -> str:
    """`SCPTRST <script>` — reset a script to its default values.

    Per the Remote Programming Manual §4-15, this command REQUIRES a
    script number (1..10) or the literal `ALL` to reset every script
    at once. The earlier `SCPTRST` (no parameter) form does not match
    the manual and is rejected by the firmware as a command error.

    The official Anritsu driver emits this right after
    `SYSCFG EUTSRCE,<iface>` so the chosen script's state machine
    is cleared and the EUT HCI re-init is (re)triggered.
    """
    if isinstance(script, str):
        if script.upper() != "ALL":
            raise ValueError(f"script string must be 'ALL', got {script!r}")
        return "SCPTRST ALL"
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10 or 'ALL', got {script}")
    return f"SCPTRST {script}"


# Test groups the script processor can enable/disable. Per manual
# §4-15 the four valid groups are STDTSTS / EDRTSTS / BLETSTS / ALLTSTS;
# ALLTSTS toggles every test in the script at once.
SCPT_TST_GROUPS = ("BLETSTS", "STDTSTS", "EDRTSTS", "ALLTSTS")


def scpttstgp(script: int, group: str, state: str) -> str:
    """`SCPTTSTGP <script>,<group>,<state>` — enable or disable a test
    group within the selected script. Sent between EUT init and the
    measurement to select which test family (BLE / classic / EDR) is
    active. Without these three calls (BLETSTS ON, STDTSTS OFF,
    EDRTSTS OFF) the 8852B's script processor keeps the previously-
    selected group, which after a classic-BT session leaves the BLE
    test set disabled.

    Per the Remote Programming Manual §4-15, the wire format is:

        SCPTTSTGP<ws><script number><,><testgroup><,> <state>

    e.g. `SCPTTSTGP 4,STDTSTS,ON`. The script number is REQUIRED —
    the earlier no-script form `SCPTTSTGP,GROUP,STATE` does not match
    the manual and is rejected by the firmware.

    Args:
        script: 1..10 — the script the group state applies to. Use the
            same number as `scptsel(script)` for the active session.
        group: `BLETSTS` (BLE tests) / `STDTSTS` (basic-rate tests) /
            `EDRTSTS` (EDR tests) / `ALLTSTS` (every test in script).
        state: `ON` | `OFF`.
    """
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    if group not in SCPT_TST_GROUPS:
        raise ValueError(f"group must be one of {SCPT_TST_GROUPS}, got {group!r}")
    state_u = state.strip().upper()
    if state_u not in ("ON", "OFF"):
        raise ValueError(f"state must be ON or OFF, got {state!r}")
    return f"SCPTTSTGP {script},{group},{state_u}"


# SCPTCFG — enable/disable a specific test within a script. Per manual
# §4-15 this also requires a script number, and the per-test valid
# tokens are OP, PC, MI, IC, CD, SS, MS, MP, ERP, ECM, EDP, EBS, EFS,
# EMP, EGT, EST, EPC, LEOP, LEMI, LEICD, LESS, LEMP, LEPRI, LEPS,
# STDTSTS, EDRTSTS, BLETSTS, PCTSTS, ALLTSTS.
SCPT_TESTS = (
    "OP", "PC", "EPC", "MI", "IC", "CD", "SS", "MS", "MP",
    "ERP", "ECM", "EDP", "EBS", "EFS", "EMP", "EGT", "EST",
    "LEOP", "LEMI", "LEICD", "LESS", "LEMP", "LEPRI", "LEPS",
    "STDTSTS", "EDRTSTS", "BLETSTS", "PCTSTS", "ALLTSTS",
)


def scptcfg(script: int, test: str, state: str | bool) -> str:
    """`SCPTCFG <script>,<test>,<state>` — enable or disable a single
    test in a script. The official BLE Measurement Software sends this
    for every (LEOP / LEICD / LEMI / LESS / LEPRI / LEMP / LEPS) test
    right after `LESCPTCFG <script>,LEPKTTYPE,BLE,TRUE` during the
    initial connect sequence. Without these ON, `OPMD SCRIPT` /
    `SCPTSEL` selects the script but the test list inside stays empty
    and ORESULT comes back with valid=FALSE.

    Per manual §4-15: scripts 1..2 are read-only. Scripts 3..10 are
    user-configurable; this command only writes to 3..10. The official
    GUI operates on script 3 by default.

    Args:
        script: 1..10. Wire form is `SCPTCFG <n>,...`; the parser only
            accepts 3..10 as the leading number for writes.
        test: one of the SCPTCFG tokens (LEOP, LEICD, LEMI, LESS,
            LEPRI, LEMP, LEPS, ...).
        state: `ON` / `OFF` (strings) or a bool.
    """
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    test_u = test.strip().upper()
    if test_u not in SCPT_TESTS:
        raise ValueError(
            f"test must be one of {SCPT_TESTS}, got {test!r}")
    if isinstance(state, bool):
        state_u = "ON" if state else "OFF"
    else:
        state_u = state.strip().upper()
        if state_u not in ("ON", "OFF"):
            raise ValueError(f"state must be ON/OFF (or bool), got {state!r}")
    return f"SCPTCFG {script},{test_u},{state_u}"


def scptcfg_query(script: int) -> str:
    """`SCPTCFG? <n>` — query the test list of a script (returns
    space-separated tokens like `LEOP,LEICD,LEMI`)."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"SCPTCFG? {script}"


# ----------------------------------------------------------------------------
# SYSCFG — system configuration (range, range units, GPIB addr, etc.)
# ----------------------------------------------------------------------------
def syscfg_range(range_setting: str) -> str:
    """Set RF input range. Pass `AUTO` for auto-range, or a fixed range
    string like `1`, `2`, `3`. Anritsu validates the value against the
    installed options; `AUTO` is safe everywhere."""
    if range_setting != "AUTO" and not range_setting.replace(".", "").isdigit():
        raise ValueError(f"range must be AUTO or numeric, got {range_setting!r}")
    return f"SYSCFG CONFIG,RANGE,{range_setting}"


def syscfg_query(field: str = "RANGE") -> str:
    """Query a SYSCFG sub-field. Defaults to RANGE."""
    return f"SYSCFG? CONFIG,{field}"


# ----------------------------------------------------------------------------
# BLE capture configuration
# ----------------------------------------------------------------------------
BleCaptureType = Literal["BLE", "2LE", "BLR"]
"""`BLE` = 1 Msymbol/s uncoded, `2LE` = 2 Msymbol/s, `BLR` = LE coded (S=2 or S=8)."""


def set_ble_cap_type(capture_type: str) -> str:
    """Set the BLE packet capture type. One of BLE / 2LE / BLR."""
    if capture_type not in ("BLE", "2LE", "BLR"):
        raise ValueError(f"capture_type must be BLE / 2LE / BLR, got {capture_type!r}")
    return f"SETBLECAPTYP {capture_type}"


def cfg_ble_cap(channel: int, ext_trig: Literal["RF", "EXT"] = "RF") -> str:
    """Configure the BLE capture: channel (0..39) and trigger source.

    `RF` triggers off the received RF signal (normal for BLE tests).
    `EXT` triggers off the EXT BNC input (used when an external trigger
    is wired into the MT8852B back panel)."""
    if not 0 <= channel <= 39:
        raise ValueError(f"channel must be 0..39, got {channel}")
    if ext_trig not in ("RF", "EXT"):
        raise ValueError(f"ext_trig must be RF or EXT, got {ext_trig!r}")
    return f"CFGBLECAP {channel},{ext_trig}"


# ----------------------------------------------------------------------------
# MEASBLECAP — perform a TX measurement (single-capture form)
# ----------------------------------------------------------------------------
BleMeas = Literal["LEOP", "LEOP2M", "LEOPLR8",
                  "LEICD", "LEICD2M", "LEICDLR8",
                  "LEMI", "LEMI2M", "LEMILR8"]
"""BLE TX measurements supported by MEASBLECAP. See Ch. 16 §16-4.

Modulation characteristics (LEMI*) need `mod_type`. For power/CFO tests
use `NA`."""

BleModType = Literal["NA", "MOD10101010", "MOD11110000", "MOD11111111"]
"""Modulation pattern for modulation tests. NA for power / CFO. The
MT8852B distinguishes BLE/2LE from BLR8 patterns (11111111 for BLR8)."""

BleSyncword = Literal["71764129"]
"""Default BLE sync word. Anritsu exposes this as a 32-bit hex value but
in practice you use the default."""


def meas_ble_cap(meas: str, mod_type: str = "NA",
                 syncword: str = "71764129") -> str:
    """Trigger a single-shot BLE TX capture + measurement.

    The MT8852B waits for a packet matching `CFGBLECAP`'s channel/trigger,
    then runs the chosen test using the limit set in the selected script.

    Notes:
      - Output power (LEOP*) requires PRBS9 payload on the DUT.
      - Carrier drift (LEICD*) requires 10101010 (BLE/2LE) or 11111111 (BLR8).
      - Modulation (LEMI*) requires 10101010 OR 11110000 (BLE/2LE), or
        11111111 (BLR8).
    """
    return f"MEASBLECAP {meas},{mod_type},{syncword}"


def meas_ble_cap_ext(meas: str, mod_type: str = "NA", packet_length: int = 37,
                     syncword: str = "71764129") -> str:
    """Same as `meas_ble_cap` but lets you set the packet length (2..255)."""
    if not 2 <= packet_length <= 255:
        raise ValueError(f"packet_length must be 2..255, got {packet_length}")
    return f"MEASBLECAPX {meas},{mod_type},{syncword},{packet_length}"


def meas_ble_cap_cte(meas: str, mod_type: str = "NA", packet_length: int = 37,
                     cte_type: Literal["AOA", "AOD1", "AOD2"] = "AOA",
                     cte_time: int = 20,
                     syncword: str = "71764129") -> str:
    """Same as `meas_ble_cap_ext` but with CTE (Constant Tone Extension)
    parameters. Requires option 37 (AoA/AoD). `cte_time` is 2..20 (each
    unit = 8 µs). For Tx-power-stability tests (LEPSBLECTE1US, etc.),
    pass `packet_length=0` (the MT8852B ignores it in that case)."""
    if not 2 <= packet_length <= 255 and packet_length != 0:
        raise ValueError(f"packet_length must be 2..255 or 0 for LEPS*, got {packet_length}")
    if not 2 <= cte_time <= 20:
        raise ValueError(f"cte_time must be 2..20, got {cte_time}")
    return f"MEASBLECAPX2 {meas},{mod_type},{syncword},{packet_length},{cte_type},{cte_time}"


# ----------------------------------------------------------------------------
# ULPDUTTXTEST — official wire format used by BLE Measurement Software
# ----------------------------------------------------------------------------
# The official Anritsu BLE Measurement Software (v1.20.012) drives the
# 8852B with `ULPDUTTXTEST` / `ULPDUTENHTXTEST` for the actual TX
# trigger, NOT `MEASBLECAPX` / `SERVULP`. NI Spy capture of the working
# bench (SN 001446004, FW 4.20.000) shows:
#
#     ULPDUTTXTEST 0,37,10101010
#     ULPDUTENHTXTEST 0,37,10101010, BLE
#
# Wire format: `ULPDUTTXTEST <channel>,<length>,<pattern>`
#   channel:  0..39 (BLE channel index)
#   length:   2..255 (packet payload length in bytes)
#   pattern:  hex payload pattern, e.g. `10101010` or `0F0F0F0F`
#
# Wire format (enhanced): `ULPDUTENHTXTEST <channel>,<length>,<pattern>,<BLE|2LE|BLR>`
#   same args + PHY mode
#
# Note the SPACE after the comma before BLE/2LE/BLR — same quote-mode
# quirk the official driver uses for `SYSCFG EUTSRCE, RS232`.

def ulpdut_txtest(channel: int, length: int, pattern: str) -> str:
    """`ULPDUTTXTEST <channel>,<length>,<pattern>` — start a BLE TX
    test using the official Anritsu driver wire format.

    Args:
        channel: BLE channel index 0..39.
        length: packet payload length 2..255 (bytes).
        pattern: payload pattern as a hex string, e.g. `"10101010"`
            (the official GUI uses `"10101010"` for CFO/LEOP, the
            PRBS9 pattern is what the DUT generates — the pattern here
            tells the 8852B what to *expect* from the air).
    """
    if not 0 <= channel <= 39:
        raise ValueError(f"channel must be 0..39, got {channel}")
    if not 2 <= length <= 255:
        raise ValueError(f"length must be 2..255, got {length}")
    pat = pattern.strip().upper()
    if not pat or not all(c in "0123456789ABCDEF" for c in pat):
        raise ValueError(f"pattern must be a hex string, got {pattern!r}")
    return f"ULPDUTTXTEST {channel},{length},{pat}"


def ulpdut_enhtxtest(channel: int, length: int, pattern: str,
                     capture_type: str = "BLE") -> str:
    """`ULPDUTENHTXTEST <channel>,<length>,<pattern>, <BLE|2LE|BLR>` —
    enhanced ULP TX test with explicit PHY selection.

    Same args as `ulpdut_txtest()` plus `capture_type`:
      - `BLE`  1 Msymbol/s uncoded PHY
      - `2LE`  2 Msymbol/s uncoded PHY
      - `BLR`  LE Coded PHY (S=2 or S=8, picked by the packet-length field)
    """
    valid_phy = ("BLE", "2LE", "BLR")
    if capture_type not in valid_phy:
        raise ValueError(
            f"capture_type must be one of {valid_phy}, got {capture_type!r}")
    base = ulpdut_txtest(channel, length, pattern)
    # Mirror the official wire format: comma + space before the PHY token.
    return f"{base}, {capture_type}"


# ----------------------------------------------------------------------------
# SERVULP — official Anritsu driver uses these (NOT MEASBLECAPX)
# ----------------------------------------------------------------------------
# The official MX885203A BLE Measurement Software drives the 8852B via
# SERVULP / SERVULPX / SERVULPX2 / SERVULPX2LE / SERVULPXLR8 commands
# (decoded from MT8852B_Driver.dll IL). MEASBLECAP[X] is documented in
# the Remote Programming Manual and ALSO works, but the official
# driver never uses it. We default to SERVULP because that's what the
# 8852B firmware is wired to react to for EUT HCI coordination.

def servulp(meas: str, payload: int, mod_type: str, packet_length: int) -> str:
    """`SERVULP <meas>,<payload>,<mod>,<pktlen>,` — configure the next
    single-shot BLE TX measurement, in the form the official Anritsu
    driver uses. `<payload>` is the PRBS pattern or `0` for power/CFO
    tests; `<mod>` is `NA`, `MOD10101010`, or `MOD11110000`; `<pktlen>`
    is the packet length in bytes (2..255).

    Note the trailing comma — the official driver appends an empty
    field (used as a separator placeholder for the 2LE/Coded variants).
    """
    if not 0 <= payload <= 0xFF:
        raise ValueError(f"payload must be 0..255, got {payload}")
    if not 2 <= packet_length <= 255:
        raise ValueError(f"packet_length must be 2..255, got {packet_length}")
    return f"SERVULP {meas},{payload},{mod_type},{packet_length},"


def servulpx(meas: str, payload: int, mod_type: str,
             packet_length: int) -> str:
    """`SERVULPX <meas>,<payload>,<mod>,<pktlen>,` — extended form used
    by the official driver for CTE tests (LEOP2M, LEOPLR8, etc.)."""
    if not 0 <= payload <= 0xFF:
        raise ValueError(f"payload must be 0..255, got {payload}")
    if not 2 <= packet_length <= 255:
        raise ValueError(f"packet_length must be 2..255, got {packet_length}")
    return f"SERVULPX {meas},{payload},{mod_type},{packet_length},"


def servulpx2(meas: str, payload: int, mod_type: str,
              packet_length: int, cte_type: str = "AOA",
              cte_time: int = 20) -> str:
    """`SERVULPX2 <meas>,<payload>,<mod>,<pktlen>,<AOA|AOD1|AOD2>,<2..20>`
    — extended form for 2LE + CTE tests (LEOP2LECTE, LEOP2M, etc.)."""
    if not 0 <= payload <= 0xFF:
        raise ValueError(f"payload must be 0..255, got {payload}")
    if not 2 <= packet_length <= 255:
        raise ValueError(f"packet_length must be 2..255, got {packet_length}")
    if cte_type not in ("AOA", "AOD1", "AOD2"):
        raise ValueError(f"cte_type must be AOA/AOD1/AOD2, got {cte_type!r}")
    if not 2 <= cte_time <= 20:
        raise ValueError(f"cte_time must be 2..20, got {cte_time}")
    return f"SERVULPX2 {meas},{payload},{mod_type},{packet_length},{cte_type},{cte_time}"


def abort_ble_cap() -> str:
    """Stop the in-progress BLE capture if it's stuck (no valid packet)."""
    return "ABORTCAP"


# ----------------------------------------------------------------------------
# Run / abort (Ch. 14)
# ----------------------------------------------------------------------------
def run() -> str:
    """Start the configured test or script.

    In `SCRIPT` op-mode this runs the whole BLE script. In `STEST` mode
    it runs the chosen single test. Waits for the INS CMP bit to set
    (pollable via `ins_query()`)."""
    return "RUN"


def abort() -> str:
    """Abort the running test or script. Immediate, no graceful wait."""
    return "ABORT"


# ----------------------------------------------------------------------------
# Packet length / mode (Ch. 4)
# ----------------------------------------------------------------------------
def le_pkt_len(script: int, length: int) -> str:
    """Set packet length for a script (1..10). Length 2..255 bytes."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    if not 2 <= length <= 255:
        raise ValueError(f"length must be 2..255, got {length}")
    return f"LEPKTLEN {script},{length}"


def le_pkt_mode(script: int, mode: Literal["AUTO", "MANUAL"]) -> str:
    """Set packet-length mode for a script. AUTO picks from payload;
    MANUAL uses `LEPKTLEN` value."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"LEPKTMODE {script},{mode}"


def lescptcfg_pkt_type(script: int, packet_type: str, state: bool) -> str:
    """Enable/disable a packet type for every test in a script.
    packet_type: BLE, 2LE, LR8, LR2, BLECTE, 2LECTE."""
    valid = ("BLE", "2LE", "LR8", "LR2", "BLECTE", "2LECTE")
    if packet_type not in valid:
        raise ValueError(f"packet_type must be one of {valid}, got {packet_type!r}")
    state_s = "TRUE" if state else "FALSE"
    return f"LESCPTCFG {script},LEPKTTYPE,{packet_type},{state_s}"


# ----------------------------------------------------------------------------
# Test-specific configuration (Ch. 13)
# ----------------------------------------------------------------------------
# These set pass/fail limits. The MT8852B firmware defaults are usually
# fine; we expose them so the AI can tighten them per-DUT if needed.

def leopcfg(script: int, field: str, value) -> str:
    """LEOPCFG — set a BLE output-power limit field for a script.

    Common fields: AVGLIMLOW, AVGLIMHI (avg-power pass window in dBm);
    PKTTOLERANCE (per-packet tolerance in dB)."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"LEOPCFG {script},{field},{value}"


def leicdcfg(script: int, field: str, value) -> str:
    """LEICDCFG — set a BLE carrier-drift limit field. Common fields:
    DFTBLELIM (default drift limit in Hz, e.g. 50000), NUMPKTS, etc."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"LEICDCFG {script},{field},{value}"


def lemicfg(script: int, field: str, value) -> str:
    """LEMICFG — set a BLE modulation limit. Common fields:
    F1AVGMIN (Hz), F1AVGMAX, F2MAXLIM."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"LEMICFG {script},{field},{value}"


def lesscfg(script: int, field: str, value) -> str:
    """LESSCFG — set a BLE sensitivity limit. Common fields:
    FERLIM (% PER limit), LFREQSEL (low-frequency select)."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"LESSCFG {script},{field},{value}"


def lepricfg(script: int, field: str, value) -> str:
    """LEPRICFG — set a BLE PER-report-integrity limit."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"LEPRICFG {script},{field},{value}"


def lempcfg(script: int, field: str, value) -> str:
    """LEMPCFG — set a BLE max-input-power limit."""
    if not 1 <= script <= 10:
        raise ValueError(f"script must be 1..10, got {script}")
    return f"LEMPCFG {script},{field},{value}"


# ----------------------------------------------------------------------------
# ORESULT — read test results (Ch. 15)
# ----------------------------------------------------------------------------
def oresult_script(ext_code: int = 0) -> str:
    """Read the full script's summary results as one comma-stream of
    `<header><ext>,<data>` blocks.

    Easier to read per-test via `oresult_test`, but this gives you
    everything in one round trip."""
    return f"ORESULT SCRIPT,{ext_code}"


def oresult_test(test: str, ext_code: int = 0) -> str:
    """Read a single test's summary results.

    Response: `<header><ext-code>,<results_valid>,<...data...>,<pass/fail>`.

    Args:
        test: one of OP, PC, MI, IC, CD, SS, MS, MP, LEOP, LEOP2M,
              LEOPLR8, LEICD, LEICD2M, LEICDLR8, LEMI, LEMI2M,
              LEMILR8, LESS, LESS2M, LESSLR8, LESSLR2, LEPRI, LEPRI2M,
              LEPRILR8, LEPRILR2, LEMP, LEMP2M, LEPSBLECTE1US, etc.
        ext_code: extension code (0 = standard). 1 = initial drift
                  (LEICD), 1 = all-slot results (LEPS*BLECTE).
    """
    return f"ORESULT TEST,{ext_code},{test}"


def xresult(test: str, stage: str, ext_code: Optional[int] = None) -> str:
    """Read extended results for a stage. `stage` examples:
    HOPOFFL, HOPOFFM, HOPOFFH (low/mid/high fixed channel),
    HOPONL, HOPONM, HOPONH, HOPONALL, HOPONANY (hopping modes).
    """
    if ext_code is None:
        return f"XRESULT {test},{stage}"
    return f"XRESULT {test},{stage},{ext_code}"


# ----------------------------------------------------------------------------
# Quick state queries
# ----------------------------------------------------------------------------
def status_query() -> str:
    """The MT8852B has no single 'state' query; this is for parity with
    other instruments. Use `*INS?` instead."""
    return "*INS?"


def scriptmode_query(script: int) -> str:
    """Returns the configured script mode (STANDARD, NULLPKT, etc.)."""
    return f"SCRIPTMODE? {script}"


# ----------------------------------------------------------------------------
# EUT (DUT) interface — how the MT8852B controls the device under test
# ----------------------------------------------------------------------------
# Per Anritsu App Note PN: 13000-00323 §"Method 1", the MT8852B talks to
# the EUT over a separate control port (front panel "EUT Control"). The
# physical interface is set with SYSCFG EUTSRCE,<source>:
#
#   RS232        — UART HCI (4-wire UART, 8N1, default baud 115200)
#   USB          — USB HCI (DUT enumerates as a Bluetooth HCI device)
#   BLE2WIRE     — Anritsu proprietary 2-wire (CC debug-style) for BLE
#                  controllers that don't expose standard HCI
#   USBBLE2WIRE  — USB-to-2Wire adapter (USB on PC side, 2-wire on DUT)
#   USBADAPTOR   — USB-to-serial adapter (RS232-style HCI over USB CDC)
#   INQUIRY      — classic BT inquiry (not for BLE)
#   MANUAL       — manually-entered BD_ADDR (not for BLE)
#
# For BLE testing the only meaningful choices are the first five.
# When EUTSRCE is set to anything other than MANUAL, the 8852B also
# "initialises the EUT for tests" (per manual Ch. 5) — this typically
# includes sending HCI_Reset and putting the controller in a state
# ready to accept HCI_LE_Transmitter_Test.
EutInterface = Literal["RS232", "USB", "BLE2WIRE", "USBBLE2WIRE", "USBADAPTOR"]
"""BLE-compatible EUT interface choices. INQUIRY / MANUAL are NOT
valid for BLE testing (per the MT8852B manual Ch. 5)."""


def eut_src(interface: str) -> str:
    """`SYSCFG EUTSRCE, <source>` — pick the EUT control interface.

    The MT8852B uses this to decide how to talk to the DUT for the
    HCI_LE_Transmitter_Test / HCI_LE_Receiver_Test commands. Pick the
    one that matches the physical cable plugged into the front-panel
    "EUT Control" port.

    Note the SPACE after the comma. NI Spy capture of the official
    BLE Measurement Software (v1.20.012) shows `SYSCFG EUTSRCE, RS232`
    (with the space). The no-space form `SYSCFG EUTSRCE,RS232` is
    also accepted by the firmware, but mirroring the official wire
    format avoids the parser ever tripping on a quote-mode quirk.
    """
    valid = ("RS232", "USB", "BLE2WIRE", "USBBLE2WIRE", "USBADAPTOR")
    if interface not in valid:
        raise ValueError(
            f"EUT interface must be one of {valid}, got {interface!r}. "
            "INQUIRY and MANUAL are not valid for BLE testing."
        )
    return f"SYSCFG EUTSRCE, {interface}"


def eut_src_query() -> str:
    return "SYSCFG? EUTSRCE"


def eut_rs232(baud: int) -> str:
    """`SYSCFG EUTRS232,<baud>` — RS232 HCI baud rate.

    The DUT's HCI UART must be configured to the same baud. Per the
    Remote Programming Manual §5-17, the 8852B supports these baud
    rates (and ONLY these — 921600 is NOT a documented option):

        1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200,
        230400, 460800

    Many BLE stacks default to 115200 8N1.
    """
    valid_bauds = (1200, 2400, 4800, 9600, 19200, 38400,
                   57600, 115200, 230400, 460800)
    if baud not in valid_bauds:
        raise ValueError(f"EUTRS232 baud must be one of {valid_bauds}, got {baud}")
    return f"SYSCFG EUTRS232,{baud}"


def eut_rs232_query() -> str:
    return "SYSCFG? EUTRS232"


def eut_handshake(mode: str) -> str:
    """`SYSCFG EUTHANDSHAKE,<mode>` — RS232 HCI hardware flow control.

    Per the Remote Programming Manual §5-16 the 8852B accepts ONLY:
      - `NONE`     handshaking disabled
      - `RTS/CTS`  RTS / CTS hardware handshaking

    Software flow control (XON/XOFF) is not supported by the EUT
    RS232 HCI link — the manual is explicit ("The RS232 HCI link does
    not at present support … handshaking", §5-17 remarks).

    We accept the all-letter form `RTSCTS` as an alias and translate
    to the canonical `RTS/CTS` the firmware expects.
    """
    canonical = {
        "NONE":     "NONE",
        "RTSCTS":   "RTS/CTS",
        "RTS/CTS":  "RTS/CTS",
    }
    if mode not in canonical:
        raise ValueError(
            "EUTHANDSHAKE must be NONE or RTSCTS (alias for RTS/CTS); "
            f"XON/XOFF is not supported by the 8852B EUT link, got {mode!r}")
    return f"SYSCFG EUTHANDSHAKE,{canonical[mode]}"


def eut_handshake_query() -> str:
    """`SYSCFG? EUTHANDSHAKE` — read the active RS232 HCI flow-control mode."""
    return "SYSCFG? EUTHANDSHAKE"


def eut_addr_query() -> str:
    """Query the EUT's BD_ADDR (12 hex chars). Returns zeros if the
    8852B has not yet acquired the address (i.e. hasn't run an inquiry
    or the BLE2WIRE initialisation)."""
    return "SYSCFG? EUTADDR"


def eut_reset() -> str:
    """`EUTRESET` — send HCI_Reset to the DUT. Useful between tests to
    put the controller back into a known state."""
    return "EUTRESET"


# ----------------------------------------------------------------------------
# EUT diagnostics — read HCI / connection state without running a test
# ----------------------------------------------------------------------------
# These queries probe what the 8852B knows about the DUT. Together they
# paint a picture of "is the HCI link up, what features does the DUT
# advertise, what was the last thing it said".

def eut_le_feat_query() -> str:
    """`SYSCFG? EUTLEFEAT` — read BLE-supported features of the EUT.

    Per Anritsu manual §5-15, the response is a 16-hex-char string of
    the LE features bitmap (Bluetooth HCI spec). All-zeros means the
    8852B has not been able to query the DUT (HCI link is not up)."""
    return "SYSCFG? EUTLEFEAT"


def eut_feat_query() -> str:
    """`SYSCFG? EUTFEAT` — read classic-BT supported features of the EUT.

    Per Anritsu manual §5-15, the response is a 16-hex-char string.
    All-zeros typically means the 8852B has not yet read the features
    (e.g. no HCI connection has been made)."""
    return "SYSCFG? EUTFEAT"


def eut_name_query() -> str:
    """`SYSCFG? EUTNAME` — read the EUT user-friendly name.

    Returns "not available" when the 8852B has not been able to read it
    from the DUT (i.e. no HCI connection)."""
    return "SYSCFG? EUTNAME"


def get_eut_feat() -> str:
    """`GETEUTFEAT` — actively query the DUT for its supported features
    over HCI. Per Anritsu manual §17-7, this is the only command that
    forces a fresh HCI_Read_Local_Extended_Features round-trip; the
    result becomes available via `eut_feat_query()`.

    Use this to differentiate "8852B has stale features" from "8852B
    has never been able to talk to the DUT"."""
    return "GETEUTFEAT"


# ----------------------------------------------------------------------------
# Convenience: full recipe for one BLE TX measurement
# ----------------------------------------------------------------------------
def recipe_ble_tx_test(meas: str,
                       packet_length: int = 37,
                       mod_type: str = "NA",
                       script: int = 2,
                       payload: int = 0) -> list[str]:
    """Return the list of commands to configure + run one BLE TX test,
    in the order the official Anritsu BLE Measurement Software
    (MX885203A) issues them (decoded from MT8852B_Driver.dll IL).

    The earlier version of this recipe used `MEASBLECAPX` directly and
    skipped the script-group setup — that left the 8852B's script
    processor in the wrong state and the EUT HCI never came up. The
    official driver interleaves `SCPTTSTGP` and `*OPC?` between every
    multi-step block; we do the same.

    Args:
        meas: test name (LEOP, LEICD, LEMI, LESS, LEPRI, LEMP, ...)
        packet_length: 2..255 (use 0 for LEPS* tests)
        mod_type: NA / MOD10101010 / MOD11110000
        script: 1..10; 2 = FULLTEST preloaded on the lab's 8852B
        payload: PRBS payload byte; 0 for power/CFO tests

    Caller flow after issuing every command in this list:
      1. Poll `*ESR?` — non-zero = cmd/exec/device error (RUN rejected)
      2. Poll `*INS?` for CMP bit (test/script complete)
      3. Read `ORESULT TEST,0,<meas>` to get the measurement
    """
    return [
        # Script processor setup (official's Script.RunScript flow).
        # NOTE: SETBLECAPTYP / SYSCFG CONFIG,RANGE / CFGBLECAP are NOT
        # in the official working sequence and must NOT be sent here —
        # including them between SCPTTSTGP and OPMD SCRIPT causes the
        # 8852B to fail with ESR=40 (DDE+CME) on the next RUN. If the
        # caller needs to override channel/range, send those BEFORE
        # EUTSRCE so the EUT HCI init runs against the right config.
        clear_status(),
        opmd("SCRIPT"),
        scptsel(script),
        opc(),                                # *OPC? — wait for SCPTSEL
        scpttstgp(script, "BLETSTS", "ON"),
        scpttstgp(script, "STDTSTS", "OFF"),
        scpttstgp(script, "EDRTSTS", "OFF"),
        opc(),                                # *OPC? — wait for SCPTTSTGP
        # Configure the ULP test (official's RunOutputPower flow)
        servulp(meas, payload, mod_type, packet_length),
        opc(),                                # *OPC? — wait for SERVULP
        # Start the test
        run(),
    ]


# ----------------------------------------------------------------------------
# Recipe helpers
# ----------------------------------------------------------------------------
DEFAULT_BLE_SCRIPT = 2
"""Default script number for BLE TX tests. The lab's MT8852B (SN
001446004) is provisioned with "SCRIPT 2: FULLTEST" (BLE TX/RX
set) by Anritsu's BLE Measurement Software (MX885203A). Scripts
1..2 are the pre-loaded ones; scripts 3..10 are user-configurable
per the MT8852B manual. Override with `bt_run_tx_test(...,
script=...)` if your lab uses a different default."""