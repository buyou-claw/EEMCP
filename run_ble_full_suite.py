"""Interactive BLE TX/RX test suite for the MT8852B — CLI entry point.

This is the THIN WRAPPER that drives the same `run_one_test` /
`save_report` / `run_preflight` plumbing the new `bt_run_full_suite`
MCP tool uses. The test catalog, the per-test executor, the report
writer, and the prompt engine all live in `mcp_8852b/` so the CLI
output and the MCP-tool output produce identical files.

Reference docs:
  * BLE_Measurement_Software_Operation_Manual.txt
  * Remote_Programming_Manual.txt
  * App Note 13000-00323 (HCI vs 2-Wire)
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_8852b.instrument import MT8852B, MT8852BError
from mcp_8852b.state_machine import MT8852BSession
from mcp_8852b.report import (
    TEST_CATALOG, TestRecord, run_one_test, save_report,
    DEFAULT_CHANNELS,
)
from mcp_8852b.interactive import (
    run_preflight, prompt_test_selection, prompt_data_rate,
    prompt_channels, prompt_packet_length, prompt_eut_interface,
    make_bench_log_handler, NeedInput,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("ble_full_suite")


# ---------------------------------------------------------------------------
# Backwards-compat prompt wrappers — the spec §3 prompt_xxx() functions
# used to live here. We re-export them as thin shims that call the
# canonical versions in mcp_8852b.interactive. Anything in the test
# suite that imported prompt_test_selection etc. from this module
# still works.
# ---------------------------------------------------------------------------
def prompt_test_selection_cli() -> list:
    """CLI-only test selection prompt (no overrides, no preflight)."""
    return prompt_test_selection(non_interactive=False)


def prompt_data_rate_cli() -> str:
    return prompt_data_rate(non_interactive=False)


def prompt_channels_cli() -> list:
    return prompt_channels(non_interactive=False)


def prompt_packet_length_cli() -> int:
    return prompt_packet_length(non_interactive=False)


# Keep the old prompt_data_rate signature for any direct callers
def prompt_data_rate_legacy() -> str:
    """Legacy shape: 1/2/3 input -> BLE/2LE/BLR. Kept for back-compat."""
    return prompt_data_rate(non_interactive=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BLE TX/RX test suite for the MT8852B. "
                    "Per BLE Measurement Software Operation Manual §1-3.",
    )
    parser.add_argument(
        "--tests", default=None,
        help="comma-separated test IDs, e.g. 'LEOP,LEICD,LEMI,LESS' "
             "(default: prompt). Use 'all' for the full catalog.",
    )
    parser.add_argument(
        "--rate", default=None, choices=("1", "2", "3"),
        help="data rate: 1=BLE 1Mbps, 2=2LE 2Mbps, 3=BLR (default: prompt).",
    )
    parser.add_argument(
        "--channels", default=None,
        help="comma-separated channels 0..39, or 'low'/'mid'/'high'/'all' "
             "(default: prompt).",
    )
    parser.add_argument(
        "--pkt-len", type=int, default=None,
        help="packet length in bytes 2..255 (default: prompt).",
    )
    parser.add_argument(
        "--eut-iface", default=None,
        help="EUT interface override (RS232/USB/BLE2WIRE/USBBLE2WIRE/"
             "USBADAPTOR/NONE). Default: prompt with preflight default.",
    )
    parser.add_argument(
        "--eut-baud", type=int, default=None,
        help="RS232 baud rate (1200..460800). Default: 115200.",
    )
    parser.add_argument(
        "--eut-handshake", default=None, choices=("NONE", "RTSCTS"),
        help="RS232 handshake. Default: NONE.",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="don't prompt; use defaults or CLI args.",
    )
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  MT8852B BLE Test Suite - interactive runner")
    print("  per BLE Measurement Software Operation Manual §1-3")
    print("=" * 60)

    # ---- 1. operator prompts (or CLI args) ----
    # Test selection
    if args.tests is not None:
        if args.tests.strip().lower() == "all":
            tests = list(TEST_CATALOG.keys())
        else:
            tests = [t.strip().upper() for t in args.tests.split(",")
                     if t.strip().upper() in TEST_CATALOG]
        if not tests:
            tests = ["LEOP", "LEICD", "LEMI", "LESS"]
    elif args.non_interactive:
        tests = ["LEOP", "LEICD", "LEMI", "LESS"]
    else:
        tests = prompt_test_selection(non_interactive=False)

    # Data rate
    if args.rate is not None:
        capture_type = {"1": "BLE", "2": "2LE", "3": "BLR"}[args.rate]
    elif args.non_interactive:
        capture_type = "BLE"
    else:
        capture_type = prompt_data_rate(non_interactive=False)

    # Channels
    if args.channels is not None:
        a = args.channels.strip().lower()
        if a == "all":
            channels = [0, 19, 39]
        elif a == "low":
            channels = [0]
        elif a == "mid":
            channels = [19]
        elif a == "high":
            channels = [39]
        else:
            try:
                channels = [int(x) for x in args.channels.replace(" ", "").split(",")]
            except ValueError:
                channels = [0, 19, 39]
    elif args.non_interactive:
        channels = [0, 19, 39]
    else:
        channels = prompt_channels(non_interactive=False)

    # Packet length
    if args.pkt_len is not None:
        if 2 <= args.pkt_len <= 255:
            pkt_len = args.pkt_len
        else:
            print(f"  ! pkt-len {args.pkt_len} out of range, defaulting to 37")
            pkt_len = 37
    elif args.non_interactive:
        pkt_len = 37
    else:
        pkt_len = prompt_packet_length(non_interactive=False)

    print()
    print(f"  plan: tests={tests}  rate={capture_type}  "
          f"channels={channels}  pkt_len={pkt_len}")

    # ---- 2. connect to bench ----
    print()
    print("=" * 60)
    print("Connecting to MT8852B...")
    print("=" * 60)
    m = MT8852B()
    idn = m.connect(reset=False)
    print(f"  {idn}")

    sess = MT8852BSession()
    sess.instr = m
    sess.connect()

    # ---- preflight (for the bench.log / bench_preflight.json files) ----
    preflight = None
    try:
        preflight = run_preflight(sess)
    except Exception as e:
        log.warning("preflight failed: %s", e)

    # ---- EUT interface (default-aware prompt) ----
    # Resolve the EUT interface using the same path the MCP tool uses,
    # so CLI runs produce a bench_preflight.json and bench.log identical
    # to MCP runs.
    try:
        iface, baud, handshake = prompt_eut_interface(
            preflight,
            override=args.eut_iface,
            non_interactive=args.non_interactive,
        )
    except NeedInput as e:
        # Shouldn't happen in CLI mode (non_interactive=False => stdin
        # prompt; non_interactive=True => defaults). Be defensive.
        print(f"  ! EUT interface prompt failed: {e}")
        iface, baud, handshake = "RS232", 115200, "NONE"
    # CLI overrides for baud/handshake
    if args.eut_baud is not None:
        baud = args.eut_baud
    if args.eut_handshake is not None:
        handshake = args.eut_handshake

    # Set up report dir + bench.log handler before running tests.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_label = f"ble_full_{ts}"
    out_dir = _PROJECT_ROOT / "reports" / run_label
    out_dir.mkdir(parents=True, exist_ok=True)
    bench_handler = make_bench_log_handler(out_dir)
    # Attach to the parent mcp_8852b logger so child loggers
    # (mcp_8852b.state_machine, mcp_8852b.instrument) propagate up
    # and get captured. The parent level must be DEBUG so the
    # child log.debug() calls (one per SCPI write/query in
    # mcp_8852b.instrument) actually pass the logger filter — the
    # root logger defaults to WARNING which would drop them.
    parent = logging.getLogger("mcp_8852b")
    parent.setLevel(logging.DEBUG)
    parent.addHandler(bench_handler)
    for child in ("mcp_8852b.instrument", "mcp_8852b.state_machine",
                  "mcp_8852b.interactive"):
        logging.getLogger(child).propagate = True

    if preflight is not None:
        (out_dir / "bench_preflight.json").write_text(
            json.dumps(preflight.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    try:
        # EUT init
        try:
            sess.set_eut_interface(
                iface, baud, handshake, skip_if_ready=True)
        except MT8852BError as e:
            log.warning("set_eut_interface failed: %s — continuing without EUT init", e)

        # ---- 3. run all (test, channel) pairs ----
        t0 = time.monotonic()
        started = datetime.now().isoformat(timespec="seconds")
        records: list = []
        for test_id in tests:
            for ch in channels:
                print()
                print(f"--- {test_id}  ch={ch}  payload={TEST_CATALOG[test_id]['payload']} ---")
                try:
                    rec = run_one_test(
                        sess, test_id, ch,
                        capture_type=capture_type,
                        packet_length=pkt_len,
                    )
                except MT8852BError as e:
                    log.error("  test failed: %s", e)
                    rec = TestRecord(
                        test_id=test_id,
                        test_name=TEST_CATALOG[test_id]["name"],
                        section=TEST_CATALOG[test_id]["section"],
                        channel=ch,
                        capture_type=capture_type,
                        packet_length=pkt_len,
                        payload=TEST_CATALOG[test_id]["payload"],
                        mod_type=TEST_CATALOG[test_id]["mod_type"],
                        raw="",
                        valid=False,
                        fields=[],
                        pass_fail=None,
                        elapsed_s=0.0,
                        timestamp=datetime.now().isoformat(timespec="seconds"),
                        errlst_before="",
                        errlst_after=str(e),
                    )
                records.append(rec)
                print(f"  raw = {rec.raw}")
                print(f"  valid={rec.valid}  pass={rec.pass_fail}  "
                      f"elapsed={rec.elapsed_s:.1f}s")
        ended = datetime.now().isoformat(timespec="seconds")
        duration_s = time.monotonic() - t0

        # ---- 4. save report ----
        ctx = {
            "run_label": run_label,
            "started": started,
            "ended": ended,
            "duration_s": duration_s,
            "instrument": idn,
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
        print()
        print("=" * 60)
        print(f"Report written to: {out_dir}")
        print("=" * 60)

        def _pf(r):
            if r.pass_fail in ("PASS", "FAIL"):
                return r.pass_fail
            if r.raw and ",PASS" in r.raw:
                return "PASS"
            if r.raw and ",FAIL" in r.raw:
                return "FAIL"
            return "INVALID" if not r.valid else "UNKNOWN"
        pf_list = [_pf(r) for r in records]
        n_pass = pf_list.count("PASS")
        n_fail = pf_list.count("FAIL")
        n_inv = sum(1 for v in pf_list if v in ("INVALID", "UNKNOWN"))
        print(f"  {len(records)} runs  |  PASS={n_pass}  FAIL={n_fail}  invalid/unknown={n_inv}")

    finally:
        try:
            sess.disconnect()
        except Exception:
            pass
        # Detach the bench.log handler from the parent mcp_8852b logger
        # so subsequent runs don't double-log.
        try:
            parent.removeHandler(bench_handler)
        except Exception:
            pass
        bench_handler.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
