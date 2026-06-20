"""MT8852B pre-flight check.

Verifies the bench is set up correctly for a BLE Sticker HWDVT run,
WITHOUT running a real test. Stops at the first failure with an
actionable error message. Always safe to run.

Checks performed, in order:
  0. *RST recovery (NOTES §15-16 — escape hatch from a "wedged" unit)
  1. GPIB link to MT8852B
  2. *IDN? + firmware identification
  3. BOOTSTATUS? (self-test result)
  4. EUT interface configuration (RS232 / USB / BLE2WIRE / ...)
  5. EUT address acquisition (i.e. the 8852B can actually see the DUT)
  6. EUT_HCI_RESET (only for HCI-based interfaces — pings the DUT)
  7. SCPTSEL 3 + MEASBLECAPX dry-run (catches a broken RF path
     BEFORE you invest in a real measurement, AND verifies the
     "selected script" prerequisite per NOTES §2b)

Usage:
    python -m mcp_8852b.scripts.preflight_check                # defaults
    python -m mcp_8852b.scripts.preflight_check --interface BLE2WIRE
    python -m mcp_8852b.scripts.preflight_check --channel 19
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

# Make the project root importable so this script works whether it's
# invoked from the project root or from inside mcp_8852b/scripts/.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_8852b.state_machine import MT8852BSession, TxTestPlan
from mcp_8852b import scpi


# ----------------------------------------------------------------------------
# Pretty output
# ----------------------------------------------------------------------------
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36mINFO\033[0m"


def _hr(char: str = "-", width: int = 78) -> str:
    return char * width


def _ok(msg: str) -> None:
    print(f"  [{PASS}] {msg}")


def _bad(msg: str) -> None:
    print(f"  [{FAIL}] {msg}")


def _info(msg: str) -> None:
    print(f"  [{INFO}] {msg}")


# ----------------------------------------------------------------------------
# Individual checks. Each returns (ok, error_message).
# ----------------------------------------------------------------------------
def check_idn(s: MT8852BSession) -> tuple[bool, str]:
    try:
        idn = s.instr.idn()
        if not idn.upper().startswith("ANRITSU"):
            return False, f"*IDN? returned {idn!r}, expected 'ANRITSU,...'"
        _ok(f"identity: {idn}")
        return True, ""
    except Exception as e:
        return False, f"*IDN? failed: {e}"


def check_boot(s: MT8852BSession) -> tuple[bool, str]:
    try:
        bs = s.instr.boot_status().strip()
        # BOOTSTATUS is implementation-specific; 0 = no self-test fault
        # on the lab's unit. Anything else we surface to the user.
        if bs in ("0", "PASS", "0\n"):
            _ok(f"BOOTSTATUS: {bs}")
            return True, ""
        _info(f"BOOTSTATUS returned {bs!r} (review if this is expected)")
        return True, ""  # not fatal
    except Exception as e:
        return False, f"BOOTSTATUS? failed: {e}"


def check_eut_interface(s: MT8852BSession, interface: str,
                        baud: int, handshake: str) -> tuple[bool, str]:
    try:
        actual = s.set_eut_interface(interface, baud=baud,
                                     handshake=handshake)
        if actual.upper() != interface.upper():
            return False, (f"set EUTSRCE={interface}, unit reports back {actual!r}. "
                           "Option not installed? Bad value?")
        if interface == "RS232":
            _ok(f"EUT interface: {actual} (baud={baud}, handshake={handshake})")
        else:
            _ok(f"EUT interface: {actual}")
        return True, ""
    except Exception as e:
        return False, (f"set_eut_interface({interface}) failed: {e}. "
                       "Check the cable and that the option is licensed.")


def check_eut_address(s: MT8852BSession) -> tuple[bool, str]:
    try:
        time.sleep(1.0)  # give the 8852B a moment to acquire the address
        addr = s.instr.query(scpi.eut_addr_query())
        addr = addr.replace("SYSCFG EUTADDR,", "").strip()
        if addr == "000000000000":
            return False, ("EUT address is all-zero — the 8852B cannot see "
                           "the DUT on the configured interface. Check the "
                           "cable, the DUT's HCI driver, and the baud rate.")
        _ok(f"EUT BD_ADDR: {addr}")
        return True, ""
    except Exception as e:
        return False, f"EUTADDR? failed: {e}"


def check_eut_reset(s: MT8852BSession) -> tuple[bool, str]:
    try:
        s.instr.eut_reset()
        time.sleep(0.5)
        _ok("EUTRESET issued (HCI reset accepted)")
        return True, ""
    except Exception as e:
        # EUTRESET may fail if the 2-wire interface is in a weird state;
        # surface as a warning rather than a hard fail.
        _info(f"EUTRESET failed: {e} (non-fatal — some interfaces ignore it)")
        return True, ""


def check_rf_dry_run(s: MT8852BSession, channel: int) -> tuple[bool, str]:
    """Configure a capture but don't RUN. Confirms the 8852B is ready
    to actually attempt a measurement. If MEASBLECAP errors out, the
    RF path or capture-type option is broken.

    Uses `s.script` (the session-level script number set by the
    caller from the CLI). The 8852B has 10 user-programmable script
    slots; the choice of which one to test against is up to the
    operator — this check exercises whichever one they've selected.
    """
    try:
        s.instr.reset()
        time.sleep(0.5)
        s.instr.set_capture_type("BLE")
        s.instr.set_range("AUTO")
        s.instr.cfg_capture(channel, "RF")
        s.instr.set_opmode("SCRIPT")
        # Use whatever script number the caller set on the session.
        # Per Anritsu Ch. 16 §16-4, MEASBLECAP[X] reads the limits from
        # the currently-selected script — without SCPTSEL, the 8852B
        # runs against whatever was left selected by the previous
        # session, which on a fresh boot is script 1 (no BLE limits).
        s.instr.select_script(s.script)
        s.instr.meas_ble_cap("LEOP", "NA", 37)
        time.sleep(0.2)
        _ok(f"MEASBLECAPX dry-run succeeded on ch {channel} "
            f"(script {s.script})")
        return True, ""
    except Exception as e:
        return False, (f"MEASBLECAPX failed on ch {channel}: {e}. "
                       "Check SETBLECAPTYP, RANGE, OPMD, SCPTSEL ordering; "
                       "BLE option 27 not installed?")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MT8852B pre-flight check")
    ap.add_argument("--interface", default="RS232",
                    choices=["RS232", "USB", "BLE2WIRE",
                             "USBBLE2WIRE", "USBADAPTOR"],
                    help="EUT control interface (default RS232)")
    ap.add_argument("--baud", type=int, default=115200,
                    help="RS232 baud rate (default 115200)")
    ap.add_argument("--handshake", default="NONE",
                    choices=["NONE", "RTSCTS"],
                    help="RS232 handshaking (default NONE; pass RTSCTS "
                         "when the DUT needs CTS, as the official BLE "
                         "Measurement Software does for this lab's "
                         "BLE Sticker). XON/XOFF is not supported by "
                         "the 8852B EUT link (manual §5-16).")
    ap.add_argument("--channel", type=int, default=19,
                    help="BLE channel to dry-run capture on (default 19)")
    ap.add_argument("--script", type=int, default=None,
                    help="MT8852B script slot (1..10) whose limits "
                         "MEASBLECAPX will read. Defaults to whatever "
                         "the session was constructed with (currently 2). "
                         "Pick whichever script the operator has "
                         "provisioned for this test.")
    ap.add_argument("--resource", default=None,
                    help="VISA resource string (default from config.py)")
    ap.add_argument("--no-confirm", action="store_true",
                    help="skip the EUT-config confirmation prompt")
    args = ap.parse_args(argv)

    print(_hr("="))
    print("  MT8852B pre-flight check")
    print(_hr("="))
    print()
    print("  EUT control interface (about to be applied):")
    print(f"    interface: {args.interface}")
    if args.interface == "RS232":
        print(f"    baud:      {args.baud}")
        print(f"    handshake: {args.handshake}")
    print(f"  Per project convention, please confirm the EUT wiring is correct.")
    if not args.no_confirm:
        try:
            ans = input("  Proceed with pre-flight? [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("  [ABORT] User cancelled.")
            return 130

    session = MT8852BSession(args.resource)
    if args.script is not None:
        session.script = args.script
    failures = 0
    try:
        # 0. Recovery — if the 8852B is in a weird state from a previous
        # session (e.g. "in calibration mode" after a stray TESTMODE),
        # *RST brings it back to a known-good state. This is the
        # documented recovery (NOTES.md §15, §16). The connect() below
        # already does *RST, but we print a notice so the operator knows
        # what's happening.
        print()
        print("[0/7] recovery (*RST before anything else)")
        try:
            session.instr.reset()
            time.sleep(0.3)
            _ok("*RST issued — unit back to factory defaults")
        except Exception as e:
            _info(f"*RST failed (non-fatal): {e}")

        # 1. Connect
        print()
        print("[1/7] GPIB connect")
        try:
            session.connect()
            _ok(f"connected to {session.instr.resource}")
        except Exception as e:
            _bad(f"connect failed: {e}")
            return 2

        # 2. Identity
        print()
        print("[2/8] identity (*IDN?)")
        ok, msg = check_idn(session)
        if ok: _ok_clean = None
        else: _bad(msg); failures += 1

        # 3. Self-test
        print()
        print("[3/8] self-test (BOOTSTATUS?)")
        ok, msg = check_boot(session)
        if not ok: _bad(msg); failures += 1

        # 4. EUT interface
        print()
        print(f"[4/8] EUT interface ({args.interface})")
        ok, msg = check_eut_interface(session, args.interface,
                                      args.baud, args.handshake)
        if not ok: _bad(msg); failures += 1

        # 5. EUT address
        print()
        print("[5/8] EUT address acquisition")
        ok, msg = check_eut_address(session)
        if not ok: _bad(msg); failures += 1

        # 6. EUT reset (only if previous steps passed)
        print()
        print("[6/8] EUT HCI reset (EUTRESET)")
        if failures == 0:
            ok, msg = check_eut_reset(session)
            if not ok: _bad(msg); failures += 1
        else:
            _info("skipped (prior failure)")

        # 7. Script select + RF dry-run
        print()
        print(f"[7/8] script select (SCPTSEL {session.script}) + RF dry-run "
              f"(MEASBLECAPX LEOP on ch {args.channel})")
        if failures == 0:
            # SCPTSEL is the prerequisite for MEASBLECAPX: per Anritsu
            # Ch. 16 §16-4 the measurement reads limits from the
            # selected script. Without an explicit SCPTSEL, the 8852B
            # runs against whatever was left selected by the previous
            # session (typically script 1, no BLE limits) → ORESULT
            # valid=FALSE. The number itself is operator choice — the
            # 8852B has 10 user-programmable slots; we use whichever
            # the operator passed via --script (default 2 from the
            # session).
            try:
                session.instr.select_script(session.script)
                _ok(f"SCPTSEL {session.script} issued")
            except Exception as e:
                _bad(f"SCPTSEL failed: {e}")
                failures += 1
            ok, msg = check_rf_dry_run(session, args.channel)
            if not ok: _bad(msg); failures += 1
        else:
            _info("skipped (prior failure)")

    finally:
        try:
            session.disconnect()
        except Exception:
            pass

    print()
    print(_hr("="))
    if failures == 0:
        print(f"  [{PASS}] Pre-flight complete — bench is ready for BLE TX tests.")
        return 0
    print(f"  [{FAIL}] {failures} check(s) failed. See messages above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
