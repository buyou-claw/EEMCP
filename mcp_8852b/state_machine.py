"""MT8852B session state machine.

The MT8852B BLE measurement has a real lifecycle (per Ch. 16, the
"Example BLE Measurement" recipe):

    1. SETBLECAPTYP <BLE|2LE|BLR>
    2. SYSCFG CONFIG,RANGE,<AUTO|range>
    3. CFGBLECAP <channel>,<RF|EXT>
    4. OPMD SCRIPT
    5. MEASBLECAP[X] <meas>,<mod>,<sync>[,<len>]
    6. RUN                                     <-- starts the test
    7. *INS?                                   <-- poll until CMP (bit 4) set
    8. ORESULT TEST,0,<meas>                   <-- read back the result

Anything outside that lifecycle (reading before RUN, re-configuring mid-
test, running without first MEASBLECAP, etc.) produces a runtime error
or, worse, silently wrong results. The state machine enforces the
ordering so a misbehaving caller can't drive the instrument into a bad
state.

States:

    DISCONNECTED ─connect─▶ IDLE
    IDLE ─configure_ble_tx─▶ CONFIGURED
    CONFIGURED ─start─▶ TESTING
    TESTING ─wait_complete─▶ COMPLETE
    COMPLETE ─read_oresult─▶ IDLE                (back to start of next test)
    COMPLETE ─abort─▶ IDLE                       (cancel)
    any ─error─▶ ERROR
"""
from __future__ import annotations
import logging
import threading
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

from .instrument import MT8852B, MT8852BError, OResult
from . import scpi

log = logging.getLogger(__name__)


class State(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    IDLE = "IDLE"
    CONFIGURED = "CONFIGURED"
    TESTING = "TESTING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


@dataclass
class EutConfig:
    """How the MT8852B is wired to the DUT (front-panel EUT Control
    port). Set once at session start, persists until changed.

    See scpi.EutInterface for the valid choices. The two-wire Anritsu
    interface (`BLE2WIRE`) is what most BLE Sticker designs use because
    they typically don't expose a standard HCI UART.
    """
    interface: str = "RS232"      # RS232 / USB / BLE2WIRE / USBBLE2WIRE / USBADAPTOR
    baud: int = 115200            # for RS232 only
    #: RS232 handshaking. Defaults to NONE per project preference;
    #: the official BLE Measurement Software v1.20.012 uses RTSCTS
    #: for the lab's BLE Sticker DUT, so pass `handshake="RTSCTS"`
    #: explicitly when wiring a DUT that needs CTS.
    handshake: str = "NONE"       # for RS232 only
    eut_address: str = ""         # 12-hex BD_ADDR; "" = not yet acquired

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "baud": self.baud,
            "handshake": self.handshake,
            "eut_address": self.eut_address,
        }


@dataclass
class TxTestPlan:
    """A configured BLE TX test that is not yet started."""
    capture_type: str = "BLE"     # BLE / 2LE / BLR
    channel: int = 0
    meas: str = "LEOP"            # LEOP / LEICD / LEMI / ...
    mod_type: str = "NA"          # for LEMI*: MOD10101010 / MOD11110000 / ...
    packet_length: int = 37
    range_setting: str = "AUTO"

    def to_dict(self) -> dict:
        return {
            "capture_type": self.capture_type,
            "channel": self.channel,
            "meas": self.meas,
            "mod_type": self.mod_type,
            "packet_length": self.packet_length,
            "range_setting": self.range_setting,
        }


@dataclass
class TxTestResult:
    """The outcome of one BLE TX test, in the standard envelope shape."""
    test: str                # "TX_POWER_DBM", "FREQ_DEV_KHZ", etc.
    header: str              # "LEOP", "LEICD", "LEMI", ...
    channel: int
    capture_type: str
    value: float             # the headline metric
    unit: str
    lower: Optional[float] = None
    upper: Optional[float] = None
    pass_: Optional[bool] = None
    raw_fields: list[str] = field(default_factory=list)
    raw_text: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "test": self.test,
            "header": self.header,
            "channel": self.channel,
            "capture_type": self.capture_type,
            "value": self.value,
            "unit": self.unit,
            "lower": self.lower,
            "upper": self.upper,
            "pass": self.pass_,
            "raw_fields": self.raw_fields,
            "raw": self.raw_text,
            "timestamp": self.timestamp,
        }


class MT8852BSession:
    """One MT8852B and its lifecycle state."""

    def __init__(self, resource: Optional[str] = None):
        self.instr = MT8852B(resource)
        self.state = State.DISCONNECTED
        self.plan: Optional[TxTestPlan] = None
        self.last_result: Optional[OResult] = None
        self.last_error: Optional[str] = None
        # How the 8852B talks to the DUT. Set with `set_eut_interface`.
        # BLE TX tests need a configured EUT source — otherwise the
        # 8852B can't tell the DUT to start transmitting.
        self.eut: EutConfig = EutConfig()
        # Active script number (1..10). Per the MT8852B Ch. 16 §16-4,
        # MEASBLECAP[X] uses "the limit parameters set in the selected
        # script", so the recipe MUST SCPTSEL before MEASBLECAPX. The
        # lab's MT8852B (SN 001446004) is provisioned with the BLE
        # Measurement Software's defaults on script 3 (per NI Spy
        # capture of the working bench — scripts 1..2 are read-only
        # preloads; 3..10 are user-configurable). Override via
        # `bt_run_tx_test(..., script=N)`.
        self.script: int = 3
        # The CMP bit is set whenever a test completes — including
        # from a PRIOR session. *RST does not clear it. We snapshot
        # CMP before RUN and only consider a transition as a valid
        # completion.
        self._cmp_at_run_start: bool = False
        # Per-instance reentrant lock serializing state mutations.
        # The MCP exposes `session` at module level, and a future
        # async / multi-client use could race the configure → start →
        # wait → read lifecycle. RLock allows the same thread to
        # re-enter (e.g. `read_result` calls `instr.read_oresult`
        # which may schedule cleanup that re-acquires the lock).
        self._lock = threading.RLock()

    # --- connection ---
    def connect(self) -> str:
        idn = self.instr.connect()
        self.state = State.IDLE
        return idn

    def disconnect(self) -> None:
        try:
            if self.state in (State.TESTING,):
                try:
                    self.instr.abort()
                except Exception as e:
                    log.warning("abort during disconnect failed: %s", e)
        finally:
            self.instr.disconnect()
            self.state = State.DISCONNECTED
            self.plan = None
            self.last_result = None

    # --- EUT (DUT) interface ---
    def set_eut_interface(self, interface: str,
                          baud: Optional[int] = None,
                          handshake: Optional[str] = None,
                          init_timeout_s: float = 10.0,
                          skip_if_ready: bool = True) -> str:
        """Configure how the 8852B talks to the DUT and wait for the
        EUT link to come up.

        Valid interfaces: RS232 / USB / BLE2WIRE / USBBLE2WIRE /
        USBADAPTOR. For RS232 also set `baud` (default 115200) and
        optionally `handshake` (NONE / RTSCTS — XON/XOFF is not
        supported by the 8852B EUT link per manual §5-16).

        The 8852B's `SYSCFG EUTSRCE,<source>` is the one command that
        actually "initialises the EUT for tests" (per manual Ch. 5).
        Internally that triggers:
          - 8852B internal register setup (Anritsu-vendor commands,
            tens of milliseconds)
          - HCI_Reset to the DUT (reboots the BT controller)
          - Read_Local_Version / Read_Local_Features / Read_BD_ADDR
          - Setting local name to "ANRITSU MT8852B..."
          - Configuring scan/page/encrypt/auth defaults
          - For CSR/Qualcomm controllers, vendor DTM setup

        On the lab's CSR DUT the full sequence takes ~10 seconds
        (see `mcp_8852b/reference/_ble_msm_samples/Internal Log.txt`, lines
        1064-1466). This method waits up to `init_timeout_s` for
        EUTADDR to become non-zero before returning, and also reads
        the ERRLST latch so any "EUT init failed" reasons get logged.

        The follow-on SCPTRST + SCPTTSTGP sequence uses `self.script`
        as the active script number — same one the next TX test will
        run against. Default 2 = lab's preloaded FULLTEST.

        Returns the new interface name (e.g. "RS232") as reported by
        the 8852B. Raises if the link does not come up in time.
        """
        try:
            # v6.8 fast-init: if the bench is already in a usable BLE
            # script state (OPMD=SCRIPT, SCPTSEL=3, BLETSTS=ON, LE pkt
            # length set), skip the full EUT init. The full init does a
            # *RST → SYSCFG EUTSRCE → HCI re-init sequence that hangs
            # the DUT in HCI CMD TIMEOUT lockout if EUT HCI is already
            # in a Tx-test state from the prior session. The official
            # GUI uses *RST every connection, but their workflow assumes
            # a fresh DUT — ours is often hot from a prior run.
            if skip_if_ready and self._bench_ble_ready():
                log.info("Bench already in BLE script mode; "
                         "skipping full EUT init (fast path)")
                self.eut = EutConfig(
                    interface=interface,
                    baud=baud or 115200,
                    handshake=handshake or "NONE",
                    eut_address="",
                )
                return interface
            actual = self.instr.set_eut_interface(
                interface, baud, handshake, script=self.script)
            self.eut = EutConfig(
                interface=actual,
                baud=baud or (self.eut.baud if self.eut.interface == "RS232" else 115200),
                handshake=handshake or self.eut.handshake,
                eut_address="",
            )
            # Wait for the 8852B to finish initialising the EUT
            # (HCI_Reset + BD_ADDR read + vendor setup). Poll EUTADDR
            # until it goes non-zero, or give up after `init_timeout_s`.
            # The 8852B also logs the last init error to ERRLST —
            # we read it before and after so the operator can see
            # *why* the link is dead, not just *that* it is.
            addr = self._wait_eut_ready(init_timeout_s)
            if addr:
                self.eut.eut_address = addr
                log.info("EUT link up: BD_ADDR=%s", addr)
            else:
                err = self._read_errlst()
                log.warning(
                    "EUT link did not come up within %.1fs. "
                    "EUTADDR=000000000000. ERRLST=%s",
                    init_timeout_s, err or "<empty>",
                )
            return actual
        except Exception as e:
            self.last_error = str(e)
            self.state = State.ERROR
            raise

    def _parse_eut_addr(self, raw: str) -> str:
        """Strip the `SYSCFG EUTADDR,` / `SYSCFG? EUTADDR` prefix and
        uppercase. Returns the 12-hex string, or '' if the response
        is empty.
        """
        addr = (raw.replace("SYSCFG EUTADDR,", "")
                   .replace("SYSCFG? EUTADDR", "")
                   .strip())
        return addr.upper() if addr else ""

    def read_eut_addr(self) -> str:
        """Single-shot read of the current EUTADDR (BD_ADDR) on the
        8852B. Returns the 12-hex string, '' if the bench has not
        acquired a BD_ADDR, or '' if the query raised.

        For a polling read that waits for the link to come up, use
        `_wait_eut_ready()`.
        """
        try:
            raw = self.instr.query(scpi.eut_addr_query())
            return self._parse_eut_addr(raw)
        except Exception as e:
            log.debug("EUTADDR read failed: %s", e)
            return ""

    def _wait_eut_ready(self, timeout_s: float) -> str:
        """Poll `SYSCFG? EUTADDR` until it returns a non-zero 12-hex
        BD_ADDR, or `timeout_s` elapses. Returns the BD_ADDR string
        (uppercased) or "" on timeout.

        8852B EUT init is slow on the CSR DUT (~10s end-to-end per
        the reference/_ble_msm_samples/ Internal Log), so the default 10s budget
        is tight. Caller can override via `init_timeout_s`.
        """
        deadline = time.monotonic() + timeout_s
        last_seen = ""
        while time.monotonic() < deadline:
            try:
                raw = self.instr.query(scpi.eut_addr_query())
                addr = self._parse_eut_addr(raw)
                if addr and addr != "000000000000":
                    return addr
                last_seen = addr
            except Exception as e:
                log.debug("EUTADDR query failed: %s", e)
            time.sleep(0.25)
        return ""

    def _read_errlst(self) -> str:
        """Read and clear the ERRLST latch. Returns the raw string.

        ERRLST is a 38-char hex string packing connection / HCI / DSP
        state — the most useful single diagnostic for a broken HCI
        link. Cleared on read, so the next read will only show errors
        that occurred after this one.
        """
        try:
            return self.instr.query(scpi.system_error()).strip()
        except Exception as e:
            return f"<ERRLST read failed: {e}>"

    def _bench_ble_ready(self) -> bool:
        """Probe the bench to see if it's already in a usable BLE script
        state from a prior session. Returns True if all of these hold:
          - OPMD = SCRIPT
          - SCPTSEL = <self.script>
          - SCPTTSTGP shows BLETSTS=ON (3rd field is ON)
          - LEPKTLEN <script> returns a non-empty integer length

        If True, the bench has been configured by a prior session
        (typically the official Anritsu GUI or a successful prior run)
        and we can skip the full EUT init that would re-trigger HCI
        and risk the HCI CMD TIMEOUT lockout.
        """
        try:
            opmd = self.instr.query("OPMD?", timeout=2.0).strip()
            if "SCRIPT" not in opmd.upper():
                return False
            sel = self.instr.query("SCPTSEL?", timeout=2.0).strip()
            if str(self.script) not in sel:
                return False
            tstgp = self.instr.query(
                f"SCPTTSTGP? {self.script}", timeout=2.0).strip()
            # Expect form "SCPTTSTGP n,STDTSTS,EDRTSTS,BLETSTS"
            # — last field must be ON
            if not tstgp.upper().endswith(",ON"):
                return False
            pktlen = self.instr.query(
                f"LEPKTLEN? {self.script}", timeout=2.0).strip()
            if not pktlen or "LEPKTLEN" not in pktlen.upper():
                return False
            return True
        except Exception as e:
            log.debug("_bench_ble_ready probe failed: %s", e)
            return False

    # --- configuration ---
    def configure_ble_tx(self, plan: TxTestPlan) -> None:
        """Apply the per-test configuration (everything except RUN).

        v6.2 fix — after several iterations, the only sequence that
        actually produces valid ORESULT data on the lab's 8852B (SN
        001446004, FW 4.20.000) is the **exact** TEST_BLOCK from
        `replay_capture.py` (which is itself a 1:1 replay of the
        official Anritsu GUI's NI Spy capture). Earlier "streamlined"
        versions of this method — that did the same thing in fewer
        commands — returned valid=FALSE on ORESULT, even though the
        bench state (SCPTSEL=3, SCPTTSTGP=OFF,OFF,ON) was identical.

        The 8852B's EUT HCI state machine is finicky. Specifically:
          - EUTHANDSHAKE must be set to NONE (not RTS/CTS) per the
            v6 recipe, even though the DUT uses RTS/CTS in the init
            path. The 8852B re-applies the non-volatile config after
            the EUTSRCE toggle.
          - OPMD SCRIPT must be sent TWICE (once for activation, once
            after *CLS) — the *CLS in between resets status registers
            that OPMD SCRIPT depends on.
          - ULPDUTTSTEND must be sent between blocks to drain any
            in-flight test (its set-form response "ULPDUTTSTEND,0" is
            drained via read_raw, not via *OPC?).
          - SCPTTSTGP n, BLETSTS, ON must be sent LAST (after
            STDTSTS,OFF and EDRTSTS,OFF) — the order matters per the
            official GUI capture.
          - The whole sequence must run as a single paced block (50ms
            between writes) — splitting it across method calls breaks
            the bus alignment and ORESULT reads garbage.

        To preserve the state-machine pattern (configure → start →
        wait → read) we run the whole TEST_BLOCK inside
        configure_ble_tx except for the final RUN, which start() sends.
        Between configure_ble_tx returning and start() being called,
        we do *not* do any queries or writes — the bus stays aligned.
        """
        if self.state not in (State.IDLE, State.CONFIGURED,
                              State.COMPLETE, State.ERROR):
            raise MT8852BError(f"cannot configure in state {self.state}")
        try:
            m = self.instr
            # v6.2 fix — match the v6 TEST_BLOCK (the exact sequence
            # from `replay_capture.py`) so the 8852B's EUT HCI state
            # machine transitions into the "result mode" we need.
            # Removing SETBLECAPTYP / CFGBLECAP from this block — they
            # were causing the v6 test path to break (the bench's
            # default channel + PHY are correct; channel changes can
            # be done OUTSIDE this block in a separate pre-test step
            # if needed).
            PACE = 0.05
            # ---- 1. Activate SCRIPT mode ----
            m.write("OPMD SCRIPT")
            time.sleep(PACE)
            try:
                m.query("*OPC?", timeout=5.0)
            except Exception as e:
                log.debug("*OPC? after OPMD SCRIPT failed (best-effort): %s", e)
            time.sleep(PACE)
            # ---- 2. EUT config re-arm (per Capture.txt calls 140-143) ----
            m.write("SYSCFG EUTSRCE, RS232")
            time.sleep(PACE)
            m.write("SYSCFG EUTRS232,115200")
            time.sleep(PACE)
            m.write("SYSCFG CONFIG,RANGE,AUTO")
            time.sleep(PACE)
            m.write("SCPTSEL 3")
            time.sleep(PACE)
            # ---- 3. End any in-flight test (per Capture.txt call 144) ----
            m.write("ULPDUTTSTEND")
            try:
                m._drain_bus(max_reads=4, read_timeout_ms=200)
            except Exception as e:
                log.debug("drain_bus after ULPDUTTSTEND failed (best-effort): %s", e)
            time.sleep(PACE)
            try:
                m.query("*OPC?", timeout=5.0)
            except Exception as e:
                log.debug("*OPC? after ULPDUTTSTEND failed (best-effort): %s", e)
            time.sleep(PACE)
            # ---- 4. Re-do EUT config + select script (calls 146-149) ----
            m.write("SYSCFG EUTSRCE, RS232")
            time.sleep(PACE)
            m.write("SYSCFG EUTRS232,115200")
            time.sleep(PACE)
            m.write("SYSCFG EUTHANDSHAKE,NONE")
            time.sleep(PACE)
            m.write("SYSCFG CONFIG,RANGE,AUTO")
            time.sleep(PACE)
            # ---- 5. Clear status + re-activate script (calls 150-154) ----
            m.write("*CLS")
            time.sleep(PACE)
            m.write("OPMD SCRIPT")
            time.sleep(PACE)
            m.write("SCPTSEL 3")
            time.sleep(PACE)
            try:
                m.query("*OPC?", timeout=5.0)
            except Exception as e:
                log.debug("*OPC? after re-activate script failed (best-effort): %s", e)
            time.sleep(PACE)
            # ---- 6. THE CRITICAL UNLOCK (calls 155-157) ----
            m.write(f"SCPTTSTGP {self.script}, STDTSTS,OFF")
            time.sleep(PACE)
            m.write(f"SCPTTSTGP {self.script}, EDRTSTS,OFF")
            time.sleep(PACE)
            m.write(f"SCPTTSTGP {self.script}, BLETSTS,ON")
            time.sleep(PACE)
            self.plan = plan
            self.last_result = None
            self.state = State.CONFIGURED
        except Exception as e:
            self.last_error = str(e)
            self.state = State.ERROR
            raise

    # --- run / wait / stop ---
    def start(self) -> None:
        """Send RUN. Transitions to TESTING.

        v6.6 fix — per Remote Programming Manual p.2-7 Table 2-7:
            "CMP — Script or test completion. This bit is cleared when a
             test or script has started and is set on its completion or
             termination."
        And p.2-7:
            "The INS register is read with the *INS? Command. It cannot
             be cleared by reading it or by the *CLS command."
        And p.3-10 (*RST):
            "GPIB Status registers and Input/Output queues are not cleared."

        Therefore: *CLS and *RST CANNOT clear the CMP bit. The ONLY thing
        that clears it is the RUN command itself. The wait_complete()
        path must detect the 1→0→1 *transition*, not just CMP==1.

        Snapshot the *INS? BEFORE RUN so wait_complete() can detect the
        RUN-driven 1→0 clear edge, then poll for the 0→1 set edge.
        """
        if self.state != State.CONFIGURED:
            raise MT8852BError(
                f"cannot start in state {self.state}; configure first")
        try:
            # Snapshot CMP BEFORE RUN. CMP cannot be cleared via *CLS/*RST
            # per RPM p.2-7 — only the RUN edge clears it. So we always
            # require a transition (require_transition=True) regardless
            # of the current state.
            try:
                pre_ins = self.instr.ins()
                self._cmp_at_run_start = bool(pre_ins & 0x04)
            except Exception as e:
                log.debug("pre-RUN *INS? snapshot failed (using safe default): %s", e)
                self._cmp_at_run_start = True  # safe default: require transition
            # Send RUN, then BLOCK on *OPC? (v6 path's idx 159 — confirms
            # the RUN was accepted, NOT that the test completed).
            self.instr.run()
            try:
                self.instr.query("*OPC?", timeout=10.0)
            except Exception as e:
                log.debug("*OPC? after RUN failed (best-effort): %s", e)
            self.state = State.TESTING
        except Exception as e:
            self.last_error = str(e)
            self.state = State.ERROR
            raise

    def wait_complete(self, timeout_s: float = 30.0,
                      poll_s: float = 0.1) -> bool:
        """Block until *INS? reports CMP transition 1→0→1 (or timeout).

        v6.6 — per RPM p.2-7, CMP is cleared only by the RUN edge. So
        we always require a transition: see CMP=0 (the RUN clear edge)
        then CMP=1 (the test-complete set edge). Without this, a stale
        CMP=1 from the prior test pre-RUN would short-circuit the wait
        and ORESULT would return zero/FALSE data.
        """
        if self.state != State.TESTING:
            raise MT8852BError(
                f"cannot wait_complete in state {self.state}; start first")
        ok = self.instr.wait_test_complete(
            timeout_s=timeout_s, poll_s=poll_s,
            require_transition=True,  # always — see docstring
        )
        if not ok:
            self.last_error = (
                f"test did not complete within {timeout_s:.1f}s "
                "(DUT may not be transmitting — check DTM and channel)")
            self.state = State.ERROR
            return False
        self.state = State.COMPLETE
        return True

    def stop(self) -> None:
        """Abort whatever's running and go back to IDLE."""
        if self.state in (State.TESTING, State.CONFIGURED):
            try:
                self.instr.abort()
            except Exception as e:
                log.warning("abort in stop() failed: %s", e)
        self.state = State.IDLE

    # --- result ---
    def read_result(self) -> OResult:
        """ORESULT TEST,0,<meas>. The plan's `meas` is the ORESULT header.

        v6.8 — RESTORED the OPMD SCRIPT + *CLS + *OPC? "result mode"
        re-issue. Empirical test (debug_sess_v6_6.py) confirms:
          - WITHOUT the re-issue, first ORESULT returns '46' (a stale
            *INS? value from wait_complete's last poll) → bus mis-align,
            subsequent ORESULTs return previous-test data shifted by one.
          - WITH the re-issue, ORESULT returns TRUE + real RF data.
        The OPC? consumes any lingering *INS? response in the queue
        and re-arms ORESULT to read the just-completed test.
        Per Capture.txt idx 173 (official GUI does this every time).
        """
        if self.state != State.COMPLETE:
            raise MT8852BError(
                f"cannot read in state {self.state}; "
                "wait_complete first")
        if self.plan is None:
            raise MT8852BError("no plan; cannot determine meas")
        m = self.instr
        # Result-mode switch — clears any *INS? residue from wait_complete
        # and re-issues OPMD SCRIPT so ORESULT reads the just-completed
        # test's data, not the live status.
        try:
            m.write("OPMD SCRIPT")
            time.sleep(0.05)
            m.write("*CLS")
            time.sleep(0.05)
            try:
                m.query("*OPC?", timeout=5.0)
            except Exception as e:
                log.debug("*OPC? in read_result failed (best-effort): %s", e)
        except Exception as e:
            log.warning("OPMD SCRIPT/*CLS in read_result failed: %s", e)
        r = m.read_oresult(self.plan.meas)
        self.last_result = r
        # Go back to IDLE so the next test can be configured.
        self.state = State.IDLE
        return r

    # --- utility ---
    def status(self) -> dict:
        return {
            "state": self.state.value,
            "connected": self.instr.connected,
            "plan": self.plan.to_dict() if self.plan else None,
            "eut": self.eut.to_dict(),
            "last_error": self.last_error,
        }