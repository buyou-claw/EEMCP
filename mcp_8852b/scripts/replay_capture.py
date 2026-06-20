"""Replay the official Anritsu BLE Measurement Software's exact Connect + init
sequence (from Capture.nitrace) against the MT8852B.

This is the v4 replay that addresses the four failure modes identified
by a colleague who specialises in the MT8852B firmware (FW 4.20.000):

    D — Timing/OPC sync missing (ROOT CAUSE of the original failure)
        The official GUI does NOT use a fixed inter-command sleep. It
        sends *OPC? and BLOCKS on the response after every hardware-
        mutating command. We do the same here: any command in the
        HARDWARE_MUTATING set is followed by *OPC? and a 5s wait for
        "1" before the next command goes out.

    A — Missing implicit GUI commands (likely contributing cause)
        NI Spy captures GPIB traffic, but the official GUI's
        SYNCMEAS LE and EUTSTART may be filtered out. Both are
        added after EUTHANDSHAKE,NONE so the 8852B is in the right
        radio + EUT-listen state before *RST.

    B — HCI UART controller hard-lockout (recovery only)
        If the 8852B's EUT UART controller is locked, no init sequence
        will bring the link up. The HCI_LOCKOUT_DETECTOR below checks
        for the symptom (3× EUTADDR=0 polls in a row) and raises a
        specific error so the user knows to power-cycle the 8852B
        instead of us thrashing the hardware. (v3: no longer short-
        circuits; the TEST_BLOCK's BLETSTS,ON+RUN is the actual unlock
        and must run before we declare a lockout.)

    C — VISA session cleanup (aggravator)
        On exit we run EUTSTOP + *RST + *CLS + viClose to release the
        session cleanly.

v3 changes over v2:
  - TEST_BLOCK rewritten to match official GUI's Capture.txt calls
    134-160 exactly (BLETSTS,ON + RUN, not ULPDUTTXTEST).
  - Post-init HCILockoutError no longer early-returns — the TEST_BLOCK
    must run because that's where the cache-unlock lives.
  - Post-test probe added: re-queries EUTADDR/EUTLEFEAT/EUTVER/*INS?
    after the RUN trigger so we can see whether the cache finally
    populates.

v4 changes over v3 (the "now we know the unstick pair" release):
  - INIT_BLOCK now includes SCPTRST 3 + SCPTTSTGP 3,STDTSTS,OFF +
    SCPTTSTGP 3,EDRTSTS,OFF before *RST — the missing "unstick" pair
    from the decompiled MX885203A driver and the official library's
    set_eut_interface(). v3's TEST_BLOCK had BLETSTS,ON+RUN but the
    init never armed the script, so the cache stayed empty.
  - SYNCMEAS LE / EUTSTART removed (the official library doesn't
    send them; they were NI-Spy-filter guesses and likely noise).
  - ULPDUTTSTEND? (query form, timed out) → ULPDUTTSTEND (set form
    with response "ULPDUTTSTEND,0"); the response is drained by the
    new WRITE_THEN_DRAIN logic so the next query doesn't mis-align.

Run:
    python -m mcp_8852b.scripts.replay_capture --skip-test
    python -m mcp_8852b.scripts.replay_capture               # init + test
    python -m mcp_8852b.scripts.replay_capture --query-pace-ms 200
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_8852b.instrument import MT8852B
from mcp_8852b.config import CONFIG


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36mINFO\033[0m"


# ---------------------------------------------------------------------------
# Hardware-mutating commands: every one of these touches the 8852B's UART,
# BLE radio, or RF path. After writing any of these, we MUST send *OPC? and
# block for the response before issuing the next command — otherwise we
# overwrite state before it has landed.
# ---------------------------------------------------------------------------
HARDWARE_MUTATING = frozenset({
    "OPMD", "SCPTSEL", "SCPTCFG", "SCPTNM", "LESCPTCFG",
    "SCPTRST", "SCPTTSTGP", "SYSCAL", "SYSCFG", "FILTER",
    "FIXEDOFF", "PATHTBL", "PATHOFF", "SERVRXONBNC", "SYNCMEAS",
    "SYNCMEAS",       # explicit duplicate so the membership test is robust
    "EUTSTART",       # implicit GUI command we are inserting
    "ULPDUTTXTEST", "ULPDUTENHTXTEST", "ULPDUTTSTEND",
    "RUN", "ABORTCAP", "CONT", "SERVSDATA", "SERVRDATA",
    "*RST",
})


# ---------------------------------------------------------------------------
# Set-form commands that produce a 1-line response on the VISA output
# buffer (so the next query would mis-align). We must drain the response
# immediately after the write, before the next query goes out.
#
# Per Capture.txt call 144: 'W ULPDUTTSTEND → R ULPDUTTSTEND,0' — the
# 8852B returns the current test-end status as a response. The query
# form (ULPDUTTSTEND?) is NOT supported and times out, so we use the
# set form and drain.
# ---------------------------------------------------------------------------
WRITE_THEN_DRAIN = frozenset({
    "ULPDUTTSTEND",
})


def is_query(cmd: str) -> bool:
    return "?" in cmd


def is_hardware_mutating(cmd: str) -> bool:
    """True if this command writes a hardware register / radio / UART state."""
    head = cmd.split()[0].split("?")[0].rstrip(",")
    return head in HARDWARE_MUTATING or head.startswith("SYSCFG") \
        or head.startswith("LESCPTCFG") or head.startswith("SCPTCFG") \
        or head.startswith("SCPTTSTGP") or head.startswith("SCPTRST") \
        or head.startswith("SCPTNM")


# ---------------------------------------------------------------------------
# The exact init block (everything from OPTSTATUS? up to and including *RST)
# in the order NI Spy captured. We add SYNCMEAS LE + EUTSTART after
# EUTHANDSHAKE,NONE because NI Spy may have filtered them out of the trace.
# ---------------------------------------------------------------------------
INIT_BLOCK = [
    "*IDN?",
    "OPTSTATUS?",
    "OPMD SCRIPT",
    "SCPTSEL 3",
    "*OPC?",
    # ---- preflight EUTSRCE/EUTRS232 (the GUI sends these twice) ----
    "SYSCFG EUTSRCE, RS232",
    "SYSCFG EUTRS232,115200",
    "SYSCFG CONFIG,RANGE,AUTO",
    "SCPTSEL 3",
    "SYSCFG EUTSRCE, RS232",
    "SYSCFG EUTRS232,115200",
    "SYSCFG USBADAPTOR,PORT,A",
    "SYSCFG EUTHANDSHAKE,RTS/CTS",
    "SYSCFG CONFIG,RANGE,AUTO",
    "OPTSTATUS?",
    "OPTSTATUS?",
    "LESCPTCFG 3,LEPKTTYPE,BLE,TRUE",
    # ---- populate scripts 3..10 (the GUI populates all 8 user scripts) ----
    "SCPTCFG? 1",
    "SCPTCFG? 2",
    "SCPTNM? 3",
    "SCPTCFG? 3",
    *[f"SCPTCFG 3,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    "SCPTNM? 4",
    "SCPTCFG? 4",
    *[f"SCPTCFG 4,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    "SCPTNM? 5",
    "SCPTCFG? 5",
    *[f"SCPTCFG 5,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    "SCPTNM? 6",
    "SCPTCFG? 6",
    *[f"SCPTCFG 6,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    "SCPTNM? 7",
    "SCPTCFG? 7",
    *[f"SCPTCFG 7,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    "SCPTNM? 8",
    "SCPTCFG? 8",
    *[f"SCPTCFG 8,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    "SCPTNM? 9",
    "SCPTCFG? 9",
    *[f"SCPTCFG 9,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    "SCPTNM? 10",
    "SCPTCFG? 10",
    *[f"SCPTCFG 10,{t},ON" for t in ("LEOP","LEICD","LEMI","LESS","LEPRI","LEMP","LEPS")],
    # ---- revert handshake to NONE ----
    "SYSCFG EUTHANDSHAKE,NONE",
    # ---- v6: SCPTRST/SCPTTSTGP trio removed (v4 unstick pair deleted
    #        as Bug A). The decompiled MX885203A driver DOES emit these
    #        but the official Capture.txt does NOT — the GUI only uses
    #        them inside set_eut_interface() during a fresh connect,
    #        NOT in the post-*RST trigger block. With them present the
    #        v4/v5 replay hung the EUT HCI state machine and the bench
    #        never published EUTLEFEAT to the GPIB cache.
    "*OPC?",
    # ---- *RST at the end of the setup block (the critical trigger) ----
    "*RST",
    "*OPC?",
]


# v3 per-test trigger block — matches the official GUI's exact post-*RST
# sequence from Capture.txt calls 134-160.
#
# Two key changes from v2:
#   (1) SCPTTSTGP 3, BLETSTS,ON — this is what the decompiled MX885203A
#       driver does, and what the GUI's Capture.txt shows (call 157).
#       Without it the 8852B's EUT HCI state machine stays "stuck" and
#       EUTADDR never gets exposed to the GPIB cache (the symptom we
#       have been chasing for three sessions).
#   (2) RUN (script-mode) instead of ULPDUTTXTEST (single-shot DTM).
#       The official GUI's per-test trigger is script-mode, not DTM.
#       ULPDUTTXTEST bypasses the script-run path that updates the
#       EUT state cache and the test-result registers.
#
# Also stripped the noise commands (SERVRXONBNC, SYNCMEAS, SYSCAL, FIXEDOFF,
# PATHTBL, PATHOFF, ABORTCAP, SERVSDATA, SERVRDATA, FILTER, ULPDUTENHTXTEST,
# the second ULPDUTTXTEST, etc.) that v2 had but the GUI does not send —
# the GUI does ONE clean RUN after configuring script 3 with BLETSTS,ON.
TEST_BLOCK = [
    # ---- Wait for *RST to complete (GUI call 135) ----
    "*OPC?",
    # ---- Activate SCRIPT mode + select script 3 (GUI calls 137-138) ----
    "OPMD SCRIPT",
    "*OPC?",
    # ---- EUT config (GUI calls 140-143) ----
    "SYSCFG EUTSRCE, RS232",
    "SYSCFG EUTRS232,115200",
    "SYSCFG CONFIG,RANGE,AUTO",
    "SCPTSEL 3",
    # ---- End any in-flight test + read back status (GUI call 144) ----
    #     Set form, not query form. The 8852B returns "ULPDUTTSTEND,0"
    #     which the loop will drain via WRITE_THEN_DRAIN.
    "ULPDUTTSTEND",
    "*OPC?",
    # ---- Re-do EUT config (GUI calls 146-149) ----
    "SYSCFG EUTSRCE, RS232",
    "SYSCFG EUTRS232,115200",
    "SYSCFG EUTHANDSHAKE,NONE",
    "SYSCFG CONFIG,RANGE,AUTO",
    # ---- Clear status + re-activate script (GUI calls 150-154) ----
    "*CLS",
    "OPMD SCRIPT",
    "SCPTSEL 3",
    "*OPC?",
    # ---- Toggle test groups — THE CRITICAL BLETSTS,ON (GUI calls 155-157) ----
    "SCPTTSTGP 3, STDTSTS,OFF",
    "SCPTTSTGP 3, EDRTSTS,OFF",
    "SCPTTSTGP 3, BLETSTS,ON",
    # ---- Trigger script-mode test (GUI call 158) ----
    "RUN",
    "*OPC?",
]


class HCILockoutError(RuntimeError):
    """Raised when the MT8852B's EUT UART controller is hard-locked.

    Symptom: 3× consecutive EUTADDR=0 polls while the 8852B is otherwise
    responsive (IDN, OPTSTATUS, SCPTSEL all return normally). EUTLEFEAT
    and EUTVER time out. ERRLST shows no error (because the firmware
    only reports GPIB-level errors, not UART hardware faults).

    Recovery: full power-cycle of the MT8852B (off 10s, on, wait ~60s
    for self-test). *RST, *CLS, EUTRESET, and EUTSRCE toggling do NOT
    clear this state on FW 4.20.000.
    """


def _euthci_lockout_check(m: MT8852B, attempts: int = 3) -> str:
    """Return the BD_ADDR if the EUT link is up, or raise HCILockoutError
    if the EUT UART controller appears to be hard-locked.

    We poll EUTADDR `attempts` times. If every single poll returns
    000000000000 AND EUTLEFEAT/EUTVER time out, the 8852B is in the
    lockout state described above.
    """
    addr_zero_count = 0
    last_addr = ""
    for i in range(attempts):
        try:
            r = m.query("SYSCFG? EUTADDR", timeout=2.0)
            last_addr = (r.replace("SYSCFG EUTADDR,", "")
                          .replace("SYSCFG? EUTADDR", "").strip())
        except Exception as e:
            last_addr = f"<query failed: {e}>"
        print(f"    EUTADDR poll #{i+1}: {last_addr}")
        if last_addr and last_addr != "000000000000":
            return last_addr
        addr_zero_count += 1
        time.sleep(0.5)

    # Try EUTLEFEAT to confirm — if THAT times out, it's a lockout.
    try:
        m.query("SYSCFG? EUTLEFEAT", timeout=2.0)
    except Exception as e:
        raise HCILockoutError(
            "EUTADDR=0 for 3 consecutive polls AND EUTLEFEAT timed out — "
            "the MT8852B's EUT UART controller is hard-locked. "
            "Power-cycle the MT8852B (off 10s, on, wait ~60s for self-test). "
            f"(last error: {e})"
        ) from e

    # EUTLEFEAT responded but EUTADDR stays 0 — softer failure
    raise HCILockoutError(
        "EUTADDR=0 for 3 consecutive polls (EUTLEFEAT responds). "
        "Init sequence did not bring up the HCI link. Likely causes: "
        "(a) EUT DUT not powered, (b) RS232 cable swapped, "
        "(c) baud/handshake mismatch, or (d) HCI lockout already cleared "
        "by the EUTLEFEAT check but EUTADDR still pending. Check the DUT."
    )


def _send(m: MT8852B, cmd: str, opc_timeout: float = 5.0) -> str:
    """Send a command, blocking on *OPC? if it mutates hardware state.

    Queries are sent and the response is read back. Writes that touch
    hardware registers are followed by *OPC? and blocked on the response
    so we never overrun the 8852B's command queue.
    """
    if is_query(cmd):
        return m.query(cmd, timeout=opc_timeout).strip()
    m.write(cmd)
    if is_hardware_mutating(cmd):
        # Block until the 8852B drains its command queue.
        try:
            m.query("*OPC?", timeout=opc_timeout)
        except Exception as e:
            print(f"      [WARN] *OPC? after {cmd} failed: {e}")
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replay official init sequence (v2)")
    ap.add_argument("--query-pace-ms", type=int, default=200,
                    help="extra delay between queries (ms); default 200")
    ap.add_argument("--write-pace-ms", type=int, default=50,
                    help="extra delay between writes (ms); default 50")
    ap.add_argument("--skip-test", action="store_true",
                    help="only run the init block, not the test trigger")
    ap.add_argument("--resource", default=None)
    args = ap.parse_args(argv)

    m = MT8852B(args.resource)
    failures = 0
    try:
        # ----- VISA session recovery prologue -----
        # If a prior process left the VISA session in a corrupted state
        # (interrupted read_raw, killed mid-write, etc.), pyvisa raises
        # access violations on the next call. "Rebooting the GUI" fixes
        # it because that closes + reopens the GPIB session. We do the
        # same here: nuke any stale session and start fresh.
        print("VISA session recovery: closing any stale handle ...")
        try:
            m.disconnect()
        except Exception:
            pass
        time.sleep(0.2)
        print("Opening fresh VISA session + draining GPIB buffer ...")
        m.connect(reset=False)  # see Fix D — no *RST yet
        for _ in range(8):
            try:
                m._drain_bus(max_reads=4, read_timeout_ms=150)
                break
            except Exception:
                time.sleep(0.1)
        # Verify IDN
        try:
            idn = m.idn().strip()
            if not idn.startswith(("ANRITSU", "Anritsu")):
                raise RuntimeError(f"unexpected IDN: {idn!r}")
            print(f"  [{PASS}] IDN = {idn}")
        except Exception as e:
            print(f"  [{FAIL}] IDN failed: {e}")
            return 1
        print()

        print("=" * 70)
        print("REPLAY v6: official init sequence (Capture.nitrace + Capture.txt)")
        print("  Hardware-mutating commands are followed by *OPC? blocking wait.")
        print("=" * 70)
        for i, cmd in enumerate(INIT_BLOCK):
            tag = f"[{i+1:3d}/{len(INIT_BLOCK)}]"
            try:
                if is_query(cmd):
                    r = m.query(cmd, timeout=5.0)
                    print(f"  {tag} Q  {cmd:50s} → {r[:80]!r}")
                    time.sleep(args.query_pace_ms / 1000.0)
                else:
                    # Fix B: settle after *RST so the 8852B finishes
                    # its internal reset before the next command lands.
                    if cmd.strip().upper() == "*RST":
                        time.sleep(CONFIG.reset_settle_s)
                    m.write(cmd)
                    # Fix C: drain any 1-line response produced by a
                    # set-form command BEFORE the *OPC? (or the next
                    # query), so the 8852B's output buffer stays
                    # aligned with our read protocol.
                    if cmd in WRITE_THEN_DRAIN:
                        try:
                            m._drain_bus(max_reads=4, read_timeout_ms=200)
                            print(f"  {tag} W  {cmd:50s} (drained response)")
                        except Exception as e:
                            print(f"  {tag} W  {cmd:50s} (drain failed: {e})")
                    if is_hardware_mutating(cmd):
                        # Block on *OPC?
                        try:
                            m.query("*OPC?", timeout=5.0)
                            print(f"  {tag} W* {cmd:50s} (*OPC? returned 1)")
                        except Exception as e:
                            print(f"  {tag} W* {cmd:50s} (*OPC? failed: {e})")
                            failures += 1
                    elif cmd not in WRITE_THEN_DRAIN:
                        print(f"  {tag} W  {cmd:50s}")
                    time.sleep(args.write_pace_ms / 1000.0)
            except Exception as e:
                print(f"  {tag} {FAIL} {cmd}: {e}")
                failures += 1

        # ----- EUTSRCE recovery toggle (s/w-level "restart" of EUT link) -----
        print()
        print("=" * 70)
        print("EUTSRCE recovery toggle (RS232 → MANUAL → RS232)")
        print("=" * 70)
        for toggle in ("MANUAL", "RS232"):
            m.write(f"SYSCFG EUTSRCE,{toggle}")
            if is_hardware_mutating(f"SYSCFG EUTSRCE,{toggle}"):
                try:
                    m.query("*OPC?", timeout=5.0)
                except Exception as e:
                    print(f"  [WARN] *OPC? after EUTSRCE,{toggle} failed: {e}")
            time.sleep(args.write_pace_ms / 1000.0)
        print(f"  [{PASS}] EUTSRCE toggle complete")

        # ----- HCI lockout detector (does NOT short-circuit anymore) -----
        print()
        print("=" * 70)
        print("Post-init probe (informational — TEST_BLOCK still runs even if this fails)")
        print("=" * 70)
        try:
            addr = _euthci_lockout_check(m, attempts=3)
            print(f"  [{PASS}] EUT link is up: BD_ADDR = {addr}")
        except HCILockoutError as e:
            print(f"  [{FAIL}] {e}")
            failures += 1
            # v3: do NOT return early. The TEST_BLOCK has the unlock
            # (SCPTTSTGP 3, BLETSTS,ON + RUN) and the post-test probe
            # will tell us whether the cache finally populates.

        try:
            feat = m.query("SYSCFG? EUTLEFEAT", timeout=3.0)
            print(f"  EUTLEFEAT = {feat.strip()}")
        except Exception as e:
            print(f"  EUTLEFEAT timeout: {e}")

        try:
            err = m.query("ERRLST", timeout=3.0)
            print(f"  ERRLST    = {err.strip()}")
        except Exception as e:
            print(f"  ERRLST err: {e}")

        if args.skip_test:
            print()
            print("=" * 70)
            print(f"[{('PASS' if failures == 0 else 'FAIL')}] Init done; --skip-test, no test run")
            print("=" * 70)
            return 0 if failures == 0 else 1

        print()
        print("=" * 70)
        print("REPLAY v5: per-test trigger block (BLETSTS,ON + RUN + CMP-poll + ORESULT)")
        print("=" * 70)
        for i, cmd in enumerate(TEST_BLOCK):
            tag = f"[{i+1:3d}/{len(TEST_BLOCK)}]"
            try:
                if is_query(cmd):
                    r = m.query(cmd, timeout=8.0)
                    print(f"  {tag} Q  {cmd:50s} → {r[:80]!r}")
                    time.sleep(args.query_pace_ms / 1000.0)
                else:
                    # Fix B: settle after *RST so the 8852B finishes
                    # its internal reset before the next command lands.
                    if cmd.strip().upper() == "*RST":
                        time.sleep(CONFIG.reset_settle_s)
                    m.write(cmd)
                    # Fix C: drain any 1-line response produced by a
                    # set-form command BEFORE the *OPC? (or the next
                    # query). Without this the *OPC? mis-aligns with
                    # the 8852B's output buffer and times out, leaving
                    # the next read to pick up the stale "ULPDUTTSTEND,0".
                    if cmd in WRITE_THEN_DRAIN:
                        try:
                            m._drain_bus(max_reads=4, read_timeout_ms=200)
                            print(f"  {tag} W  {cmd:50s} (drained ULPDUTTSTEND response)")
                        except Exception as e:
                            print(f"  {tag} W  {cmd:50s} (drain failed: {e})")
                    if is_hardware_mutating(cmd):
                        try:
                            m.query("*OPC?", timeout=5.0)
                            print(f"  {tag} W* {cmd:50s} (*OPC? returned 1)")
                        except Exception as e:
                            print(f"  {tag} W* {cmd:50s} (*OPC? failed: {e})")
                            failures += 1
                    elif cmd not in WRITE_THEN_DRAIN:
                        print(f"  {tag} W  {cmd:50s}")
                    time.sleep(args.write_pace_ms / 1000.0)
            except Exception as e:
                print(f"  {tag} {FAIL} {cmd}: {e}")
                failures += 1

        # ----- Post-RUN: wait for CMP bit (the v5 critical fix) -----
        # Per Capture.txt idx 116-172: after *OPC? returns 1 immediately
        # (idx 115), the GUI polls *ESR? + *INS? for ~110ms waiting for
        # the CMP bit (0x04) to set. *INS? transitions from 42 to 46
        # (= 42 | CMP) when the test finishes. WITHOUT THIS WAIT, the
        # 8852B has not yet published EUTADDR / EUTLEFEAT to the GPIB
        # cache, so the post-test probe reads 0 and the result
        # queries return "no result yet".
        print()
        print("=" * 70)
        print("Post-RUN: poll *INS? for CMP bit (test complete, ~110ms)")
        print("=" * 70)
        deadline = time.monotonic() + 30.0
        ins_val = 0
        last_ins_str = ""
        while time.monotonic() < deadline:
            try:
                last_ins_str = m.query("*INS?", timeout=3.0)
                # *INS? returns just the integer (e.g. "42", "46"); strip
                ins_val = int(last_ins_str.strip(), 0)
                if ins_val & 0x04:  # CMP bit
                    print(f"  [{PASS}] CMP set after "
                          f"{30.0 - (deadline - time.monotonic()):.2f}s "
                          f"(*INS? = {ins_val} = 0x{ins_val:02X})")
                    break
                else:
                    print(f"  t={30.0 - (deadline - time.monotonic()):.2f}s  "
                          f"*INS? = {ins_val}")
            except Exception as e:
                print(f"  *INS? err: {e}")
            time.sleep(0.1)
        else:
            print(f"  [{FAIL}] CMP not set after 30s (last *INS? = {last_ins_str!r})")
            failures += 1

        # Re-issue OPMD SCRIPT (per Capture.txt idx 173) — switches the
        # 8852B into "result mode" so the next XRESULT/ORESULT queries
        # return the test data, not the live state.
        print()
        print("Re-issuing OPMD SCRIPT (Capture.txt idx 173, result mode)")
        m.write("OPMD SCRIPT")
        m.write("*CLS")
        try:
            m.query("*OPC?", timeout=5.0)
        except Exception:
            pass

        # ----- Post-test probe: did the RUN trigger populate the EUT cache? -----
        print()
        print("=" * 70)
        print("Post-test probe (after CMP set + result mode)")
        print("=" * 70)
        # EUTLEFEAT is the real health check. EUTADDR=0 is expected &
        # normal for BLE/RS232.
        try:
            feat = m.query("SYSCFG? EUTLEFEAT", timeout=3.0)
            feat_clean = (feat.replace("SYSCFG EUTLEFEAT,", "")
                             .replace("SYSCFG? EUTLEFEAT", "").strip())
            if (feat_clean and feat_clean != "0000000000000000"
                    and feat_clean.upper() != "0"):
                print(f"  [{PASS}] EUTLEFEAT: {feat_clean}")
            else:
                print(f"  [{FAIL}] EUTLEFEAT still zero/empty: {feat_clean!r}")
                failures += 1
        except Exception as e:
            print(f"  [{FAIL}] EUTLEFEAT query failed: {e}")
            failures += 1

        try:
            addr = m.query("SYSCFG? EUTADDR", timeout=3.0)
            addr_clean = (addr.replace("SYSCFG EUTADDR,", "")
                           .replace("SYSCFG? EUTADDR", "").strip())
            print(f"  [INFO] EUTADDR = {addr_clean!r} (0 is normal for BLE)")
        except Exception as e:
            print(f"  EUTADDR query failed: {e}")

        try:
            err = m.query("ERRLST", timeout=3.0)
            err_clean = (err.replace("ERRLST", "")
                            .replace("ERRLST?", "").strip())
            if not err_clean or err_clean.upper() in ("\"\"", "''"):
                print(f"  [PASS] ERRLST empty")
            else:
                print(f"  [WARN] ERRLST = {err_clean!r}")
        except Exception as e:
            print(f"  ERRLST query failed: {e}")

        # ----- Read ORESULT for each BLE test (v5: the part v4 was missing) -----
        print()
        print("=" * 70)
        print("ORESULT readback (v5 — what the GUI does at idx 227-243)")
        print("=" * 70)
        for t in ("LEOP", "LEICD", "LEMI", "LESS", "LEMP", "LEPRI"):
            try:
                r = m.query(f"ORESULT TEST,0,{t}", timeout=5.0)
                print(f"  {t:6s} → {r}")
            except Exception as e:
                print(f"  {t:6s} err: {e}")
                failures += 1

    except HCILockoutError as e:
        print(f"\n[{FAIL}] {e}")
        failures += 1
    finally:
        # Session cleanup (fix C)
        try:
            print()
            print("=" * 70)
            print("Cleanup: EUTSTOP + *RST + *CLS + viClose")
            print("=" * 70)
            try:
                m.write("EUTSTOP")
            except Exception:
                pass
            try:
                m.write("*RST")
            except Exception:
                pass
            try:
                m.write("*CLS")
            except Exception:
                pass
        finally:
            try:
                m.disconnect()
            except Exception:
                pass

    print()
    print("=" * 70)
    if failures == 0:
        print(f"[{PASS}] Replay completed with no errors.")
        return 0
    print(f"[{FAIL}] {failures} command(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
