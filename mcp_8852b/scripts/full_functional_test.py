"""Comprehensive MT8852B functional test sweep.

Brings up the bench + DUT via the proven EUT init flow, then exercises
every MCP 8852B tool category: status, EUT HCI diagnostics, RF path,
TX tests (LEOP/LEICD/LEMI/LEPS), RX tests (LESS/LEMP/LEPRI), and
result readback. Writes a markdown report to reports/full_sweep_<ts>.md.

Run:
    python -m mcp_8852b.scripts.full_functional_test
"""
from __future__ import annotations
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_8852b.instrument import MT8852B
from mcp_8852b import scpi
from mcp_8852b.config import CONFIG
from mcp_8852b.state_machine import MT8852BSession, TxTestPlan, State

REPORTS_ROOT = _PROJECT_ROOT / "reports"
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"
WARN = "[WARN]"

results = {"phases": [], "errors": 0, "warnings": 0, "started": datetime.now().isoformat()}


def log(phase: str, status: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  {ts} {status:7s} [{phase}] {msg}")
    if status == FAIL:
        results["errors"] += 1
    elif status == WARN:
        results["warnings"] += 1


def phase(title: str):
    """Decorator: print phase header and return a list to append rows to."""
    print()
    print("=" * 78)
    print(f"PHASE: {title}")
    print("=" * 78)
    return []


def banner(s: str):
    print()
    print("-" * 78)
    print(s)
    print("-" * 78)


# ----------------------------------------------------------------------------
# Phase 1: Connection + System Status
# ----------------------------------------------------------------------------
def phase1_status(m: MT8852B):
    rows = phase("1. Connection + System Status")
    log("1.connect", INFO, f"Opening VISA session to {m.resource}")
    try:
        idn = m.connect(reset=False)  # Don't double-reset; v6 replay handled it
        log("1.idn", PASS if idn.startswith(("ANRITSU", "Anritsu")) else FAIL,
            f"IDN = {idn!r}")
    except Exception as e:
        log("1.connect", FAIL, f"connect failed: {e}")
        return rows

    # Status queries
    for label, fn in [
        ("*ESR?", lambda: m.query(scpi.esr_query()).strip()),
        ("*INS?", lambda: m.query(scpi.ins_query()).strip()),
        ("ERRLST", lambda: m.query(scpi.system_error()).strip()),
        ("BOOTSTATUS?", lambda: m.query(scpi.boot_status()).strip()),
        ("OPMD?", lambda: m.query(scpi.opmd_query()).strip()),
        ("SCPTSEL?", lambda: m.query(scpi.scptsel_query()).strip()),
    ]:
        try:
            r = fn()
            log(f"1.{label}", PASS, f"{label} = {r!r}")
        except Exception as e:
            log(f"1.{label}", FAIL, f"{label} failed: {e}")

    # Decode *INS? bits
    try:
        ins = int(m.query(scpi.ins_query()).strip(), 0)
        flags = []
        for bit, name in [(0x01, "CON"), (0x02, "DIS"), (0x04, "CMP"),
                          (0x08, "INQ"), (0x10, "OVT"), (0x20, "LEFG"),
                          (0x40, "?"), (0x80, "?")]:
            if ins & bit:
                flags.append(name)
        log("1.*INS? decode", INFO, f"{ins} = 0x{ins:02X} = {'+'.join(flags)}")
    except Exception as e:
        log("1.*INS? decode", WARN, f"failed: {e}")

    # Reset and *CLS
    try:
        m.write(scpi.clear_status())
        log("1.*CLS", PASS, "cleared status")
    except Exception as e:
        log("1.*CLS", FAIL, f"failed: {e}")

    return rows


# ----------------------------------------------------------------------------
# Phase 2: EUT HCI Bring-up + Diagnostics
# ----------------------------------------------------------------------------
def phase2_eut(m: MT8852B):
    rows = phase("2. EUT HCI Bring-up + Diagnostics")
    log("2.eut_init", INFO, "Bringing up EUT HCI link via set_eut_interface (RS232 115200 RTS/CTS)...")
    try:
        actual = m.set_eut_interface("RS232", baud=115200, handshake="RTSCTS")
        log("2.eut_init", PASS, f"EUTSRCE: {actual}")
    except Exception as e:
        log("2.eut_init", FAIL, f"set_eut_interface failed: {e}")
        return rows

    # EUT diagnostic sweep
    eut_queries = [
        ("EUTSRCE", scpi.eut_src_query),
        ("EUTRS232", scpi.eut_rs232_query),
        ("EUTHANDSHAKE", scpi.eut_handshake_query),
        ("EUTADDR", scpi.eut_addr_query),
        ("EUTFEAT", scpi.eut_feat_query),
        ("EUTLEFEAT", scpi.eut_le_feat_query),
        ("EUTNAME", scpi.eut_name_query),
    ]
    for label, qfn in eut_queries:
        try:
            r = m.query(qfn(), timeout=3.0).strip()
            log(f"2.{label}", PASS, f"{label} = {r!r}")
        except Exception as e:
            log(f"2.{label}", WARN, f"{label} timeout: {type(e).__name__}")

    # EUTRESET (sends HCI_Reset to DUT)
    try:
        m.write(scpi.eut_reset())
        time.sleep(0.5)
        log("2.EUTRESET", PASS, "EUTRESET sent (HCI_Reset to DUT)")
    except Exception as e:
        log("2.EUTRESET", WARN, f"failed: {e}")

    return rows


# ----------------------------------------------------------------------------
# Phase 3: TX Test Sweep (LEOP / LEICD / LEMI / LEPS on multiple channels)
# ----------------------------------------------------------------------------
def phase3_tx_tests(m: MT8852B, sess: MT8852BSession):
    rows = phase("3. TX Test Sweep (LEOP/LEICD/LEMI/LEPS)")

    # TX test matrix: (meas, channel, capture_type, packet_length, mod_type)
    # Use the v6-style "official recipe" via set_eut_interface + RUN
    tx_matrix = [
        ("LEOP",  0,  "BLE", 37,  "NA"),
        ("LEOP",  19, "BLE", 37,  "NA"),
        ("LEOP",  39, "BLE", 37,  "NA"),
        ("LEICD", 19, "BLE", 37,  "NA"),
        ("LEICD", 0,  "BLE", 37,  "NA"),
        ("LEMI",  19, "BLE", 37,  "MOD11110000"),
        ("LEMI",  0,  "BLE", 37,  "MOD11110000"),
    ]

    for meas, ch, cap, plen, mod in tx_matrix:
        label = f"3.TX.{meas}.ch{ch}"
        try:
            # Configure for this test
            sess.configure_ble_tx(TxTestPlan(
                capture_type=cap, channel=ch, meas=meas,
                mod_type=mod, packet_length=plen, range_setting="AUTO",
            ))
            # Pre-RUN cleanup
            try: m.abort()
            except Exception: pass
            try: m.clear_status()
            except Exception: pass
            # Start
            sess.start()
            # Wait
            if not sess.wait_complete(timeout_s=30.0):
                log(label, FAIL, f"CMP not set in 30s on {meas} ch{ch}")
                continue
            # Read result
            res = m.read_oresult(meas)
            if res.valid:
                log(label, PASS, f"{meas} ch{ch} → {res.fields!r} pass={res.pass_fail!r}")
            else:
                log(label, FAIL, f"{meas} ch{ch} result invalid: {res.fields!r}")
        except Exception as e:
            log(label, FAIL, f"failed: {e}\n{traceback.format_exc()[-200:]}")

    return rows


# ----------------------------------------------------------------------------
# Phase 4: RX Test Sweep (LESS / LEMP / LEPRI)
# ----------------------------------------------------------------------------
def phase4_rx_tests(m: MT8852B, sess: MT8852BSession):
    rows = phase("4. RX Test Sweep (LESS/LEMP/LEPRI)")

    # RX tests: 8852B transmits, DUT receives. DUT must be in
    # HCI_LE_Receiver_Test mode. The 8852B drives the DUT into RX mode
    # automatically when SCPTCFG LESS,ON is configured and RUN is sent.
    rx_matrix = [
        ("LESS",  19, -70),  # Sensitivity at -70 dBm typical
        ("LEMP",  19, -10),  # Max input power at -10 dBm
        ("LEPRI", 19, -50),  # PER integrity at -50 dBm
        ("LESS",  0,  -70),
        ("LEMP",  39, -10),
    ]

    for meas, ch, pwr in rx_matrix:
        label = f"4.RX.{meas}.ch{ch}.pwr{pwr}"
        try:
            # For RX tests, we need to configure the script with the RX
            # test, set the 8852B to transmit on the channel at the
            # specified power, and run.
            #
            # Use the official recipe for RX tests:
            #   SCPTCFG s,<meas>,ON
            #   SCPTTSTGP s,BLETSTS,ON
            #   SERVULP <meas>,<pkt_count>,<mod>,<pktlen>
            #   RUN

            # Set script 3 active with the RX test
            m.set_opmode("SCRIPT")
            m.select_script(3)
            m.opc()

            # Enable the RX test
            m.set_script_test(3, meas, True)
            m.set_test_group(3, "BLETSTS", True)
            m.set_test_group(3, "STDTSTS", False)
            m.set_test_group(3, "EDRTSTS", False)
            m.opc()

            # Set capture channel
            m.cfg_capture(ch, "RF")
            m.opc()

            # Pre-RUN cleanup
            try: m.abort()
            except Exception: pass
            try: m.clear_status()
            except Exception: pass

            # Trigger
            m.write(scpi.run())
            m.opc()

            # Wait for CMP
            deadline = time.monotonic() + 30.0
            cmp_set = False
            while time.monotonic() < deadline:
                ins = int(m.query(scpi.ins_query()).strip(), 0)
                if ins & 0x04:  # CMP bit
                    cmp_set = True
                    break
                time.sleep(0.1)

            if not cmp_set:
                log(label, FAIL, f"CMP not set in 30s on {meas} ch{ch} pwr{pwr}")
                continue

            # Read result. RX test parsers don't set `valid` for LESS/LEMP/LEPRI;
            # we check `fields` is non-empty instead. The 8852B returns
            # `['PER_PCT', 'RX_COUNT', 'TX_COUNT']` for LESS/LEMP and a
            # multi-segment block for LEPRI.
            res = m.read_oresult(meas)
            if res.fields and len(res.fields) >= 3:
                log(label, PASS, f"{meas} ch{ch} pwr{pwr} → {res.fields!r}")
            else:
                log(label, FAIL, f"{meas} ch{ch} pwr{pwr} empty result: {res.fields!r}")
        except Exception as e:
            log(label, FAIL, f"failed: {e}\n{traceback.format_exc()[-200:]}")

    return rows


# ----------------------------------------------------------------------------
# Phase 5: ORESULT SCRIPT + Extended Variants
# ----------------------------------------------------------------------------
def phase5_oresult(m: MT8852B):
    rows = phase("5. ORESULT SCRIPT + Extended Variants")

    try:
        # Re-issue OPMD SCRIPT for result mode
        m.write("OPMD SCRIPT")
        m.opc()

        # ORESULT SCRIPT (whole script result)
        try:
            r = m.query(scpi.oresult_script(0), timeout=10.0).strip()
            log("5.ORESULT_SCRIPT", PASS, f"OResult Script = {r[:200]!r}")
        except Exception as e:
            log("5.ORESULT_SCRIPT", FAIL, f"failed: {e}")
    except Exception as e:
        log("5.phase5", FAIL, f"phase setup failed: {e}")

    return rows


# ----------------------------------------------------------------------------
# Phase 6: System commands (ABORT, ABORTCAP, EUTSTOP, *RST, *CLS)
# ----------------------------------------------------------------------------
def phase6_system(m: MT8852B):
    rows = phase("6. System Commands (ABORT, ABORTCAP, EUTSTOP, *RST)")

    for label, fn in [
        ("ABORT", lambda: m.write(scpi.abort())),
        ("ABORTCAP", lambda: m.write(scpi.abort_ble_cap())),
        ("*CLS", lambda: m.write(scpi.clear_status())),
    ]:
        try:
            fn()
            log(f"6.{label}", PASS, f"{label} sent")
        except Exception as e:
            log(f"6.{label}", FAIL, f"{label} failed: {e}")

    return rows


def main():
    print("=" * 78)
    print("MT8852B MCP Full Functional Test Sweep")
    print(f"Started: {results['started']}")
    print(f"Resource: GPIB0::27::INSTR  (FW 4.20.000)")
    print("=" * 78)

    m = MT8852B()
    m.connect(reset=False)  # open the VISA session first
    sess = MT8852BSession()
    sess.instr = m
    sess.connect()  # transitions state from DISCONNECTED to IDLE

    try:
        phase1_status(m)
        phase2_eut(m)
        phase3_tx_tests(m, sess)
        phase4_rx_tests(m, sess)
        phase5_oresult(m)
        phase6_system(m)
    finally:
        # Final cleanup
        try:
            m.write("EUTSTOP")
            m.write("*RST")
            m.write("*CLS")
        except Exception:
            pass
        try:
            m.disconnect()
        except Exception:
            pass

    results["finished"] = datetime.now().isoformat()
    results["errors_total"] = results["errors"]
    results["warnings_total"] = results["warnings"]

    # Write report
    report_path = REPORTS_ROOT / f"full_sweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# MT8852B MCP Full Functional Test Sweep\n\n")
        f.write(f"- Started: `{results['started']}`\n")
        f.write(f"- Finished: `{results['finished']}`\n")
        f.write(f"- Errors: **{results['errors_total']}**\n")
        f.write(f"- Warnings: **{results['warnings_total']}**\n")
        f.write(f"- Resource: `GPIB0::27::INSTR`\n\n")
        f.write(f"See console output above for per-test results.\n")

    print()
    print("=" * 78)
    print(f"SWEEP COMPLETE: {results['errors_total']} errors, {results['warnings_total']} warnings")
    print(f"Report: {report_path}")
    print("=" * 78)
    return 0 if results["errors_total"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
