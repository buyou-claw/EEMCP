"""MT8852B instrument handle — pyvisa wrapper.

Single owner of the pyvisa transport. Higher-level code (state_machine,
MCP tools) talks only to this class — never to pyvisa directly.

Highlights vs the previous PoC:
  - Uses the *real* Anritsu command set (flat named commands), not
    made-up colon-separated SCPI.
  - `wait_test_complete()` polls `*INS?` for the CMP bit — that's the
    documented way to know a test/script has finished.
  - `read_oresult()` parses the comma-separated ORESULT response and
    returns a typed dict keyed by the Anritsu field names.
"""
from __future__ import annotations
import time
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import pyvisa

from . import scpi
from .config import CONFIG

log = logging.getLogger(__name__)


class MT8852BError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# INS register bit definitions (Ch. 2, Table 2-7)
#
# Anritsu uses integer-style bit numbering: bit 0 = LSB. So
# bit 0 (0x01) = CON, bit 1 (0x02) = DIS, bit 2 (0x04) = CMP, etc.
# (The "*INS? returns 5 means bits 0 and 2 are set" example in the
# manual — 5 = 0x05 = CON + CMP — confirms this.)
# ----------------------------------------------------------------------------
INS_CON  = 0x01   # bit 0 — connected
INS_DIS  = 0x02   # bit 1 — disconnected
INS_CMP  = 0x04   # bit 2 — script/test complete (the one we want)
INS_INQ  = 0x08   # bit 3 — inquiry complete
INS_OVT  = 0x10   # bit 4 — over-temperature warning
INS_LEFG = 0x20   # bit 5 — BLE frame generator stopped

#: bits we treat as "test done"
INS_DONE_MASK = INS_CMP | INS_LEFG


# ----------------------------------------------------------------------------
# ORESULT response parsing
# ----------------------------------------------------------------------------
@dataclass
class OResult:
    """Parsed ORESULT response block.

    For `ORESULT TEST,0,LEOP` the response `LEOP0,TRUE,-3.43,-2.40,-4.64,0.12,0,30,PASS`
    becomes:
        header       = "LEOP"
        ext_code     = 0
        valid        = True
        fields       = ["-3.43", "-2.40", "-4.64", "0.12", "0", "30"]
        pass_fail    = "PASS"
        raw          = "LEOP0,TRUE,-3.43,-2.40,-4.64,0.12,0,30,PASS"

    The field order is test-specific — see mcp_8852b/NOTES.md §10 for
    per-test field maps. Tests like LEOP have known positions; the AI
    should consult the manual for fields it doesn't recognise.
    """
    header: str
    ext_code: int
    valid: bool
    fields: list[str] = field(default_factory=list)
    pass_fail: Optional[str] = None
    raw: str = ""


_HEADER_RE = re.compile(r"^([A-Z0-9]+?)(\d+)$")


def parse_oresult_block(line: str) -> OResult:
    """Parse a single `<header><ext>,<data...>` block from ORESULT.

    Example input:
        "LEOP0,TRUE,-3.43,-2.40,-4.64,0.12,0,30,PASS"
        "OP0,TRUE,1.61,1.94,1.53,2.02,PASS"
    """
    raw = line.strip()
    if not raw:
        raise MT8852BError("empty ORESULT response")
    parts = raw.split(",")
    m = _HEADER_RE.match(parts[0])
    if not m:
        raise MT8852BError(f"cannot parse ORESULT header {parts[0]!r}")
    header = m.group(1)
    ext_code = int(m.group(2))
    body = parts[1:]
    valid_s = body[0].strip().upper() if body else ""
    if valid_s not in ("TRUE", "FALSE"):
        raise MT8852BError(f"ORESULT missing valid flag: {raw!r}")
    valid = valid_s == "TRUE"
    pass_fail: Optional[str] = None
    # The last token is the PASS/FAIL flag when present.
    if len(body) >= 2:
        last = body[-1].strip().upper()
        if last in ("PASS", "FAIL"):
            pass_fail = last
            body = body[1:-1]
        else:
            body = body[1:]
    return OResult(
        header=header, ext_code=ext_code, valid=valid,
        fields=[s.strip() for s in body],
        pass_fail=pass_fail, raw=raw,
    )


def parse_oresult_script(text: str) -> dict[str, OResult]:
    """Parse a full SCRIPT response into a dict keyed by header.

    The script response is many blocks back-to-back, comma-separated:
        `OP0,...,PC0,...,MI0,...,LEOP0,...,...`
    We have to split on each known header. Heuristic: split by walking
    the string and re-finding header tokens.
    """
    # All valid ORESULT test headers (Ch. 15). Sorted by length descending
    # so we don't match e.g. "OP" inside "LEOP".
    HEADERS = (
        "LEOP2LECTE", "LEICDBLECTE", "LEICD2LECTE", "LEPSBLECTE1US",
        "LEPS2LECTE1US", "LEPSBLECTE2US", "LEPS2LECTE2US",
        "LEOPBLECTE", "LEOP2M", "LEOPLR8", "LEOPLR2",
        "LEICD2M", "LEICDLR8", "LEMI2M", "LEMILR8", "LEPRILR8",
        "LEMP2M", "LEOP", "LEICD", "LEMI", "LESS2M", "LESSLR8",
        "LESSLR2", "LEPRI2M", "LEPRI", "LEMP", "LESS",
        "LEPRILR2",
        "OP", "PC", "EPC", "MI", "IC", "CD", "SS", "MS", "MP",
        "ERP", "ECM", "EDP", "EGT", "EST", "EBS", "EFS", "EMP",
    )
    text = text.strip()
    # Build a regex that matches any header + ext-code followed by a comma.
    hdr_pat = "|".join(f"({h})(\\d+)" for h in HEADERS)
    splitter = re.compile(rf"(?:^|,)({hdr_pat})(?=,)")
    # Find all matches (start positions) and split.
    matches = list(splitter.finditer(text))
    if not matches:
        raise MT8852BError(f"could not find any ORESULT header in {text[:200]!r}")
    out: dict[str, OResult] = {}
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].lstrip(",").strip()
        # Re-extract header+code for the dict key.
        hm = re.match(r"^([A-Z0-9]+?)(\d+),", block)
        if not hm:
            continue
        try:
            parsed = parse_oresult_block(block)
        except MT8852BError:
            continue
        key = hm.group(1)
        # Last occurrence wins (rare edge case).
        out[key] = parsed
    return out


# ----------------------------------------------------------------------------
# The main instrument class
# ----------------------------------------------------------------------------
class MT8852B:
    """One open session to a single MT8852B."""

    def __init__(self, resource: Optional[str] = None):
        self.resource = resource or CONFIG.visa_resource
        self.rm: Optional[pyvisa.ResourceManager] = None
        self.instr = None
        self._connected = False

    # --- connection lifecycle ---
    def connect(self, reset: bool = True) -> str:
        if self._connected:
            return self.idn()
        log.info("Opening VISA session to %s (backend=%s)",
                 self.resource, CONFIG.visa_library)
        self.rm = pyvisa.ResourceManager(CONFIG.visa_library)
        self.instr = self.rm.open_resource(self.resource)
        self.instr.timeout = CONFIG.timeout_ms
        # Anritsu replies are \n-terminated; the default `\n` reader
        # strips it. Set both ends to LF so writes / reads are consistent.
        self.instr.read_termination = "\n"
        self.instr.write_termination = "\n"
        self._connected = True
        # Drain the GPIB output buffer. After an interrupted session
        # (timeout, hard kill, etc.) the 8852B has the last query's
        # response still queued. *RST does NOT clear this — only reading
        # does. We read with a tight per-read timeout in a loop until
        # pyvisa errors with a timeout, meaning the buffer is empty.
        try:
            self._drain_bus()
        except Exception as e:
            log.warning("drain_bus at connect failed: %s", e)
        # Reset the instrument. INS register's CMP bit isn't cleared by
        # *CLS; *RST is the only thing that does it (and gets us to a
        # known state). Gated by `reset` so callers that want to
        # perform a VISA session recovery (open fresh handle, drain,
        # verify IDN) can suppress the *RST until they're ready.
        if reset:
            try:
                self.instr.write(scpi.reset())
                time.sleep(CONFIG.reset_settle_s)
            except Exception as e:
                log.warning("*RST at connect failed: %s", e)
            # Drain again — *RST may have queued one more response.
            try:
                self._drain_bus()
            except Exception:
                pass
        # Confirm identity.
        for attempt in range(5):
            try:
                cand = self.instr.query(scpi.idn()).strip()
                if cand.startswith(("ANRITSU", "Anritsu")):
                    log.info("Connected: %s", cand)
                    return cand
                log.warning("stale IDN response on attempt %d: %r",
                            attempt + 1, cand)
            except Exception as e:
                log.warning("IDN query attempt %d failed: %s", attempt + 1, e)
                time.sleep(0.3)
        return "unverified"

    def _drain_bus(self, max_reads: int = 16,
                   read_timeout_ms: int = 250) -> None:
        """Read raw bytes until the GPIB output buffer is empty.

        Each call to `read_raw()` returns at most one response. With a
        tight timeout, a buffer-empty condition raises VisaIOError,
        which is what we use as the stop signal. Bounded by max_reads
        so we don't loop forever if the bus is somehow always busy.
        """
        old = self.instr.timeout
        self.instr.timeout = read_timeout_ms
        try:
            for _ in range(max_reads):
                try:
                    data = self.instr.read_raw()
                    log.debug("drained: %r", data)
                except pyvisa.VisaIOError:
                    return
        finally:
            self.instr.timeout = old

    def disconnect(self) -> None:
        if self.instr is not None:
            try:
                self.instr.close()
            except Exception:
                pass
        if self.rm is not None:
            try:
                self.rm.close()
            except Exception:
                pass
        self.instr = None
        self.rm = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.instr is not None

    # --- raw I/O ---
    def write(self, cmd: str) -> None:
        if not self.connected:
            raise MT8852BError("not connected")
        log.debug(">> %s", cmd)
        self.instr.write(cmd)

    def query(self, cmd: str, timeout: Optional[float] = None) -> str:
        """Send `cmd` and read back one line of response.

        Args:
            cmd: SCPI command (with trailing '?').
            timeout: optional override for this call's VISA read timeout
                     (seconds). Useful for diagnosing "is this query
                     stuck or is the 8852B not responding?"
        """
        if not self.connected:
            raise MT8852BError("not connected")
        log.debug(">> %s", cmd)
        if timeout is not None:
            prev = self.instr.timeout
            self.instr.timeout = int(timeout * 1000)
        try:
            resp = self.instr.query(cmd)
        finally:
            if timeout is not None:
                self.instr.timeout = prev
        s = resp.strip()
        log.debug("<< %s", s[:120] + ("..." if len(s) > 120 else ""))
        return s

    def query_int(self, cmd: str) -> int:
        return int(self.query(cmd))

    # --- identity / reset / housekeeping ---
    def idn(self) -> str:
        return self.query(scpi.idn())

    def reset(self) -> str:
        self.write(scpi.reset())
        # *RST takes a couple of seconds; let it settle before any
        # further commands. Reuse the same settle window as connect.
        time.sleep(CONFIG.reset_settle_s)
        return "*RST"

    def clear_status(self) -> None:
        self.write(scpi.clear_status())

    def boot_status(self) -> str:
        """Returns the last self-test result (PASS/FAIL). Useful right
        after connect to confirm the unit is healthy."""
        return self.query(scpi.boot_status())

    def system_error(self) -> str:
        return self.query(scpi.system_error())

    # --- operation mode ---
    def set_opmode(self, mode: str, test: Optional[str] = None) -> None:
        """`OPMD SCRIPT` or `OPMD STEST,<test>` — set the test mode.
        The official Anritsu driver clears the status registers with
        `*CLS` immediately before this; we do the same in the library
        so any stale ESR/INR bits from the EUT init don't cause the
        post-RUN `*ESR?` check to see a spurious command error."""
        self.write(scpi.clear_status())
        self.write(scpi.opmd(mode, test))

    def opmode(self) -> str:
        return self.query(scpi.opmd_query())

    # --- script selection ---
    def select_script(self, script: int) -> None:
        """`SCPTSEL <n>` — pick which script (1..10) MEASBLECAP[X] uses
        for its limit parameters.

        Per Anritsu Ch. 16 §16-4: "the BLE Tx test measurement is
        performed using the limit parameters set in the selected script".
        Skipping this leaves the test running against whatever was
        previously selected — usually a stale classic-BT script that
        doesn't have BLE limits, which surfaces as ORESULT valid=FALSE
        even on a healthy bench.
        """
        self.write(scpi.scptsel(script))

    def selected_script(self) -> int:
        return self.query_int(scpi.scptsel_query())

    # --- capture config ---
    def set_capture_type(self, capture_type: str) -> None:
        self.write(scpi.set_ble_cap_type(capture_type))

    def set_range(self, range_setting: str = "AUTO") -> None:
        self.write(scpi.syscfg_range(range_setting))

    def cfg_capture(self, channel: int, ext_trig: str = "RF") -> None:
        self.write(scpi.cfg_ble_cap(channel, ext_trig))

    def set_packet_length(self, script: int, length: int) -> None:
        self.write(scpi.le_pkt_len(script, length))

    def set_packet_mode(self, script: int, mode: str) -> None:
        self.write(scpi.le_pkt_mode(script, mode))

    def set_script_pkt_type(self, script: int, packet_type: str,
                            state: bool) -> None:
        self.write(scpi.lescptcfg_pkt_type(script, packet_type, state))

    def set_script_test(self, script: int, test: str,
                        state: str | bool) -> None:
        """`SCPTCFG <script>,<test>,<ON|OFF>` — enable or disable a
        single test in a script. The official Anritsu BLE Measurement
        Software sends this for every BLE test (LEOP / LEICD / LEMI /
        LESS / LEPRI / LEMP / LEPS) during the initial connect
        sequence; without these ON, `OPMD SCRIPT` runs against an
        empty test list and ORESULT comes back valid=FALSE."""
        self.write(scpi.scptcfg(script, test, state))

    # --- EUT (DUT) interface ---
    def eut_interface(self) -> str:
        """Query the current EUT control interface (e.g. `RS232`).
        Strips the `SYSCFG EUTSRCE,` prefix the 8852B returns."""
        raw = self.query(scpi.eut_src_query())
        # Response shape: "SYSCFG EUTSRCE,RS232" — keep just the value.
        return raw.split(",", 1)[-1].strip()

    def set_eut_interface(self, interface: str,
                          baud: Optional[int] = None,
                          handshake: Optional[str] = None,
                          script: int = 3) -> str:
        """Configure how the MT8852B talks to the DUT — mirror the
        official Anritsu BLE Measurement Software (v1.20.012) Connect
        flow as captured by NI Spy 17.

        `interface` is one of: RS232, USB, BLE2WIRE, USBBLE2WIRE, USBADAPTOR.
        For RS232 also pass `baud` (default 115200) and optionally
        `handshake`. Per manual §5-16, valid handshake values are
        only `NONE` and `RTS/CTS` (we accept `RTSCTS` as an alias).
        Software flow control (XON/XOFF) is NOT supported by the
        8852B EUT link.

        `script` (1..10) is the script number the LESCPTCFG / SCPTCFG /
        SCPTRST / SCPTTSTGP sequence applies to. The official GUI uses
        **script 3** by default (the lab's MT8852B SN 001446004 has
        scripts 3..10 user-configurable; scripts 1..2 are read-only
        preloads). Pass a different number only if the operator has
        provisioned a different script.

        The full official Connect flow (in order, from the NI Spy
        capture):

            *IDN?
            OPTSTATUS?
            OPMD SCRIPT
            SCPTSEL <script>
            *OPC?
            SYSCFG EUTSRCE, <interface>           ← note the SPACE
            SYSCFG EUTRS232,<baud>                ← RS232 only
            SYSCFG CONFIG,RANGE,AUTO
            [SYSCFG USBADAPTOR,PORT,A]            ← USBADAPTOR only
            SYSCFG EUTHANDSHAKE,<mode>
            LESCPTCFG <script>,LEPKTTYPE,BLE,TRUE
            SCPTCFG <script>,LEOP,ON              ← enable each test
            SCPTCFG <script>,LEICD,ON
            SCPTCFG <script>,LEMI,ON
            SCPTCFG <script>,LESS,ON
            SCPTCFG <script>,LEPRI,ON
            SCPTCFG <script>,LEMP,ON
            SCPTCFG <script>,LEPS,ON
            SYSCFG EUTHANDSHAKE,<mode>            ← user-visible final value
            *RST                                 ← and we're back to factory

        The five steps that were missing from earlier versions of
        this code — and that the bench required before EUTADDR would
        come up — are:
          1. SYSCFG CONFIG,RANGE,AUTO              (set RF range)
          2. LESCPTCFG <script>,LEPKTTYPE,BLE,TRUE (declare LE packet type)
          3. SCPTCFG <script>,<test>,ON            (populate the script)
          4. SCPTRST <script>                      (reset the script)
          5. SCPTTSTGP <script>,STDTSTS/EDRTSTS,OFF (clear non-BLE groups)

        Without these, the 8852B silently accepts EUTSRCE but never
        brings the EUT up over HCI (EUTADDR stays 0). The exact
        timing matters — empirically verified on SN 001446004:
        removing any of the sleeps below re-introduces the silent
        EUT init failure.

        Returns the new interface name as a sanity-check echo.
        """
        if interface == "RS232":
            # Baud and handshake must be set BEFORE EUTSRCE — the SRCE
            # command triggers the link init using these values.
            self.write(scpi.eut_rs232(baud or 115200))
            if handshake:
                self.write(scpi.eut_handshake(handshake))
        # Range must be set before EUTSRCE so the RF path is configured
        # when the 8852B starts talking to the DUT.
        self.write(scpi.syscfg_range("AUTO"))
        self.write(scpi.eut_src(interface))
        # Step 2-5: declare the LE packet type, populate the script's
        # test list, then reset and clear non-BLE test groups. The
        # official Anritsu driver does this all in one block.
        time.sleep(0.5)
        # LESCPTCFG <script>,LEPKTTYPE,BLE,TRUE  — declares the LE PHY
        # the script will use. Without this the script test list is
        # empty even after SCPTCFG.
        self.write(scpi.lescptcfg_pkt_type(script, "BLE", True))
        time.sleep(0.1)
        # SCPTCFG <script>,<test>,ON  — populate the script with the
        # full BLE test family. The official GUI enables all 7 BLE
        # tests up front (LEOP, LEICD, LEMI, LESS, LEPRI, LEMP, LEPS).
        for test in ("LEOP", "LEICD", "LEMI", "LESS", "LEPRI", "LEMP", "LEPS"):
            self.write(scpi.scptcfg(script, test, "ON"))
            time.sleep(0.05)
        # SCPTRST <script>  — reset the (now-populated) script.
        # Without this the script processor keeps stale state from the
        # previous session, including stale CMP/INS bits.
        self.write(scpi.scptrst(script))
        time.sleep(0.2)
        # SCPTTSTGP <script>,<group>,OFF  — clear non-BLE test groups.
        # These two ON/OFF toggles are what the official driver uses to
        # make sure only the BLE test family is active in the script.
        self.write(scpi.scpttstgp(script, "STDTSTS", "OFF"))
        time.sleep(0.05)
        self.write(scpi.scpttstgp(script, "EDRTSTS", "OFF"))
        time.sleep(0.05)
        # Block until the 8852B finishes the script reset + group config.
        # *OPC? returns "1" when the command queue is drained.
        try:
            self.query(scpi.opc(), timeout=5.0)
        except Exception as e:
            log.warning("*OPC? after SCPTRST/SCPTTSTGP failed: %s", e)
        time.sleep(0.3)
        # *** The critical "trigger" — official GUI pattern ***
        # Per NI Spy capture of the working bench, the official Anritsu
        # BLE Measurement Software issues *RST at the END of the
        # initial-setup block (after LESCPTCFG + SCPTCFG + SCPTRST +
        # SCPTTSTGP), not at the start. This *RST is what actually
        # triggers the 8852B to bring up the HCI link with the
        # configured (LESCPTCFG/LEPKTTYPE + SCPTCFG) settings applied.
        # Without this final *RST, the 8852B silently accepts the config
        # commands but ERRLST shows DD=0C (Internal HCI Error =
        # CMD_DISALLOWED) and EUTADDR stays 0.
        self.write(scpi.reset())
        time.sleep(CONFIG.reset_settle_s)
        # Re-apply the EUTSRCE setting because *RST clears it. The
        # NI Spy capture shows the official GUI does NOT re-issue
        # EUTSRCE here — it's already applied via the 8852B's
        # non-volatile config. We don't either; the caller's
        # `set_eut_interface()` returns and the state machine then
        # configures the test.
        # Echo back what the unit now reports (catches typos / option
        # not-installed errors).
        return self.eut_interface()

    def eut_reset(self) -> None:
        """Send HCI_Reset to the DUT (only meaningful if a valid
        EUT source is configured and the DUT responds to HCI)."""
        self.write(scpi.eut_reset())

    # --- script processor (the part that was missing!) ---
    def scptrst(self, script: int | str) -> None:
        """`SCPTRST <script>` — reset a script to its default values.
        Per manual §4-15 the script number (1..10) or `ALL` is required;
        the no-parameter form is rejected by the firmware as a command
        error. The official Anritsu driver emits this right after
        `SYSCFG EUTSRCE,<iface>` so the EUT HCI init is (re)triggered."""
        self.write(scpi.scptrst(script))

    def set_test_group(self, script: int, group: str, on: bool) -> None:
        """`SCPTTSTGP <script>,<group>,<ON|OFF>` — enable or disable a
        test group within the selected script. Per manual §4-15 the
        script number is required."""
        self.write(scpi.scpttstgp(script, group, "ON" if on else "OFF"))

    def opc(self, timeout_s: float = 5.0) -> str:
        """`*OPC?` — block until the 8852B's command queue drains. The
        official driver emits this after every multi-step setup block
        (SCPTRST, SCPTSEL, SCPTTSTGP, SERVULP, RUN). Returns `1` on
        success. Raises on timeout."""
        return self.query(scpi.opc(), timeout=timeout_s)

    def esr(self) -> int:
        """`*ESR?` — read the Event Status Register. The official driver
        reads this after RUN to catch command / execution / device-
        dependent errors. Non-zero = something failed.

          bit 0 (0x01) OPC    — operation complete
          bit 2 (0x04) QYE    — query error
          bit 3 (0x08) DDE    — device-dependent error
          bit 4 (0x10) EXE    — execution error
          bit 5 (0x20) CME    — command error
          bit 7 (0x80) PON    — power-on

        Reading ESR clears it.
        """
        return self.query_int(scpi.esr_query())

    # --- ULP test setup (the form the official driver uses) ---
    def servulp(self, meas: str, payload: int, mod_type: str,
                packet_length: int) -> None:
        """`SERVULP <meas>,<payload>,<mod>,<pktlen>,` — configure the
        next single-shot BLE TX measurement. The official Anritsu driver
        uses this command (not `MEASBLECAPX`); both work, but SERVULP
        is what the firmware is set up to react to for EUT HCI
        coordination."""
        self.write(scpi.servulp(meas, payload, mod_type, packet_length))

    def servulpx(self, meas: str, payload: int, mod_type: str,
                 packet_length: int) -> None:
        """`SERVULPX` — extended ULP form for CTE / 2LE / Coded PHY."""
        self.write(scpi.servulpx(meas, payload, mod_type, packet_length))

    def servulpx2(self, meas: str, payload: int, mod_type: str,
                  packet_length: int, cte_type: str = "AOA",
                  cte_time: int = 20) -> None:
        """`SERVULPX2` — extended ULP form with explicit CTE params."""
        self.write(scpi.servulpx2(meas, payload, mod_type, packet_length,
                                  cte_type, cte_time))

    # --- ULPDUTTXTEST — official BLE Measurement Software wire format ---
    def ulpdut_txtest(self, channel: int, length: int,
                      pattern: str) -> None:
        """`ULPDUTTXTEST <channel>,<length>,<pattern>` — start a BLE TX
        test using the official Anritsu BLE Measurement Software
        (v1.20.012) wire format. NI Spy capture of the working bench
        shows the official driver uses this command (NOT MEASBLECAPX
        and NOT SERVULP) for the actual TX trigger."""
        self.write(scpi.ulpdut_txtest(channel, length, pattern))

    def ulpdut_enhtxtest(self, channel: int, length: int,
                         pattern: str, capture_type: str = "BLE") -> None:
        """`ULPDUTENHTXTEST <channel>,<length>,<pattern>, <BLE|2LE|BLR>`
        — enhanced ULP TX test with explicit PHY selection. This is
        what the official GUI issues after `ULPDUTTXTEST`."""
        self.write(scpi.ulpdut_enhtxtest(channel, length, pattern,
                                        capture_type))

    # --- per-test limit configuration (optional) ---
    def set_leop_limit(self, script: int, field: str, value) -> None:
        self.write(scpi.leopcfg(script, field, value))

    def set_leicd_limit(self, script: int, field: str, value) -> None:
        self.write(scpi.leicdcfg(script, field, value))

    def set_lemi_limit(self, script: int, field: str, value) -> None:
        self.write(scpi.lemicfg(script, field, value))

    def set_less_limit(self, script: int, field: str, value) -> None:
        self.write(scpi.lesscfg(script, field, value))

    # --- run / abort ---
    def run(self) -> None:
        self.write(scpi.run())

    def abort(self) -> None:
        self.write(scpi.abort())

    def abort_cap(self) -> None:
        self.write(scpi.abort_ble_cap())

    # --- measure (one-shot TX capture + run) ---
    def meas_ble_cap(self, meas: str, mod_type: str = "NA",
                     packet_length: int = 37) -> None:
        """Configure the next capture as a single TX measurement, then
        the next `run()` triggers it. Equivalent to issuing
        MEASBLECAPX <meas>,<mod>,<sync>,<len>."""
        self.write(scpi.meas_ble_cap_ext(meas, mod_type, packet_length))

    # --- completion polling ---
    def ins(self) -> int:
        """Read the INS register. Bit 4 (CMP, value 16) = test/script done."""
        return self.query_int(scpi.ins_query())

    def is_test_complete(self) -> bool:
        """Returns True if the CMP bit (and/or LEFG) is set."""
        return bool(self.ins() & INS_DONE_MASK)

    def wait_test_complete(self, timeout_s: float = 30.0,
                           poll_s: float = 0.1,
                           require_transition: bool = False) -> bool:
        """Poll `*INS?` until CMP bit set (transition or level), or timeout.

        The MT8852B blocks the parser while a test/script is running, so
        a `*INS?` query will only return once the test finishes (or after
        the per-query timeout, which we set to the test timeout).

        Args:
            require_transition: when True, CMP must transition from 0→1
                during the wait. This protects against a stale CMP bit
                left from a prior session (the 8852B's *RST does not
                clear it). The caller should snapshot CMP before RUN and
                pass it as `require_transition`.
        """
        deadline = time.monotonic() + timeout_s
        old = self.instr.timeout
        self.instr.timeout = int((timeout_s + 5.0) * 1000)
        # If we need a transition, wait for CMP=0 first. The 8852B
        # clears CMP when RUN is processed, so it should drop shortly
        # after the test starts.
        saw_zero = not require_transition
        try:
            while True:
                try:
                    ins_val = self.ins()
                    cmp_now = bool(ins_val & INS_CMP)
                    if require_transition:
                        if cmp_now and saw_zero:
                            return True
                        if not cmp_now:
                            saw_zero = True
                    else:
                        if cmp_now:
                            return True
                    if time.monotonic() > deadline:
                        return False
                except pyvisa.VisaIOError:
                    return False
                time.sleep(poll_s)
        finally:
            self.instr.timeout = old

    # --- result reading ---
    def read_oresult(self, test: str, ext_code: int = 0) -> OResult:
        """Read a single test's ORESULT response, parsed."""
        raw = self.query(scpi.oresult_test(test, ext_code))
        return parse_oresult_block(raw)

    def read_oresult_script(self, ext_code: int = 0) -> dict[str, OResult]:
        """Read the whole script's ORESULT, parsed into a dict."""
        raw = self.query(scpi.oresult_script(ext_code))
        return parse_oresult_script(raw)