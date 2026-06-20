"""Report writer + per-test executor for the BLE TX/RX campaign.

This module is the single source of truth for:

  - The BLE test catalog (which tests the Anritsu BLE Measurement
    Software can run, with their default payload / mod_type / units).
  - The per-test executor `run_one_test` that drives a single
    (test, channel) iteration through the state machine.
  - The report writer `save_report` that emits
    `report.md` + `summary.json` + `data.csv` into
    `reports/ble_full_<run_label>_<ts>/`.

It is shared by:
  - `run_ble_full_suite.py` (the legacy CLI runner) — thin wrapper
    that adds interactive prompts on top.
  - `mcp_8852b.interactive` — drives the per-test executor from the
    new `bt_run_full_suite` MCP tool.

The state machine's three v6.8 fixes (wait_complete's transition
detection, read_result's OPMD SCRIPT + *CLS + *OPC? block, and
set_eut_interface's skip_if_ready=True default) are kept intact — this
file only CONSUMES those methods, it does not replace them.
"""
from __future__ import annotations
import csv
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .state_machine import (
    MT8852BSession, TxTestPlan, MT8852BError,
)

log = logging.getLogger("mcp_8852b.report")


# ----------------------------------------------------------------------------
# Test catalog (BLE Measurement Software Operation Manual p.1-4 §1-3)
# ----------------------------------------------------------------------------
# `payload` is what the bench should expect on the air per test, per the
# manual notes cited in `run_ble_full_suite.py`. `script_test` is the
# SCPTCFG name. `requires_opt` lists the Anritsu options the test needs;
# if any are missing the preflight will grey the test out.
# ----------------------------------------------------------------------------
TEST_CATALOG = {
    "LEOP": {
        "name": "Output Power",
        "section": "BLE_MSM §1-3 (TX) / RPM §10 LEOPCFG p.10-38",
        "unit": "dBm",
        "payload": "PRBS9",
        "script_test": "BLEOP",
        "mod_type": "NA",
        "rx": False,
        "requires_opt": [],
    },
    "LEICD": {
        "name": "Carrier Frequency Offset & Drift",
        "section": "BLE_MSM §1-3 (TX) / RPM §10 LEICDCFG p.10-42",
        "unit": "Hz",
        "payload": "10101010",
        "script_test": "BLEICD",
        "mod_type": "NA",
        "rx": False,
        "requires_opt": [],
    },
    "LEMI": {
        "name": "Modulation Index",
        "section": "BLE_MSM §1-3 (TX) / RPM §10 LEMICFG p.10-43",
        "unit": "Hz",
        "payload": "10101010",
        "script_test": "BLEMI",
        "mod_type": "MOD10101010",
        "rx": False,
        "requires_opt": [],
    },
    "LESS": {
        "name": "Receiver Sensitivity (PER)",
        "section": "BLE_MSM §1-3 (RX) / RPM §10 LESSCFG p.10-48",
        "unit": "%",
        "payload": "PRBS9",
        "script_test": "BLESS",
        "mod_type": "NA",
        "rx": True,
        "requires_opt": [],
    },
    "LEMP": {
        "name": "Maximum Input Signal",
        "section": "BLE_MSM §1-3 (RX) / RPM §10 LEMPCFG p.10-52",
        "unit": "%",
        "payload": "PRBS9",
        "script_test": "BLEMP",
        "mod_type": "NA",
        "rx": True,
        "requires_opt": ["27"],
    },
    "LEPRI": {
        "name": "PER Report Integrity",
        "section": "BLE_MSM §1-3 (RX) / RPM §10 LEPRICFG p.10-50",
        "unit": "%",
        "payload": "PRBS9",
        "script_test": "BLEPRI",
        "mod_type": "NA",
        "rx": True,
        "requires_opt": ["27"],
    },
    "LEPS": {
        "name": "Tx Power Stability (CTE)",
        "section": "BLE_MSM §1-3 (TX) / RPM §10 LEPSCFG p.10-46 — Opt 37 only",
        "unit": "dB",
        "payload": "PRBS9",
        "script_test": "BLEPS",
        "mod_type": "NA",
        "rx": False,
        "requires_opt": ["37"],
    },
}

#: default channel set the lab uses when no channels are explicitly
#: chosen (low / mid / high).
DEFAULT_CHANNELS = [0, 19, 39]


# ----------------------------------------------------------------------------
# Per-test result record
# ----------------------------------------------------------------------------
@dataclass
class TestRecord:
    test_id: str         # "LEOP", "LEICD", ...
    test_name: str
    section: str         # doc reference
    channel: int
    capture_type: str
    packet_length: int
    payload: str
    mod_type: str
    raw: str             # full ORESULT response line
    valid: bool
    fields: list
    pass_fail: Optional[str]
    elapsed_s: float
    timestamp: str
    errlst_before: str
    errlst_after: str


# ----------------------------------------------------------------------------
# Per-test execution
# ----------------------------------------------------------------------------
def run_one_test(
    sess: MT8852BSession,
    test_id: str,
    channel: int,
    capture_type: str,
    packet_length: int,
) -> TestRecord:
    """Run a single (test, channel) iteration using the state machine.

    Per the bench's preloaded script 3 (per the v6 path), the bench's
    default payload is already correct for each test (PRBS9 for LEOP/
    LESS, 10101010 for LEICD, 10101010/11110000 for LEMI). We trust
    the bench's preloaded config here because re-flushing SCPTCFG /
    LEMICFG.TOGGLE between every iteration was causing the v6 test
    path to break (see project_8852b_v68_three_bug_fix.md).

    Calls (in order):
      1. `sess.configure_ble_tx(plan)` — recipe (sets the bench up).
      2. `sess.start()` — sends RUN.
      3. `sess.wait_complete(...)` — blocks on the CMP 1→0→1 transition
         (v6.6 fix; see `state_machine.py:505-529`).
      4. `sess.read_result()` — re-issues OPMD SCRIPT + *CLS + *OPC?
         before ORESULT (v6.8 fix; see `state_machine.py:564-574`).

    Raises MT8852BError on timeout or HCI failure. Caller catches and
    records as an INVALID row.
    """
    if test_id not in TEST_CATALOG:
        raise MT8852BError(f"unknown test_id {test_id!r}; "
                          f"valid: {list(TEST_CATALOG)}")
    info = TEST_CATALOG[test_id]

    err_before = sess._read_errlst()

    plan = TxTestPlan(
        capture_type=capture_type,
        channel=channel,
        meas=test_id,
        mod_type=info["mod_type"],
        packet_length=packet_length,
    )

    t0 = time.monotonic()
    sess.configure_ble_tx(plan)
    sess.start()
    ok = sess.wait_complete(timeout_s=30.0, poll_s=0.1)
    if not ok:
        raise MT8852BError(
            f"{test_id} ch{channel}: wait_complete timed out")
    oresult = sess.read_result()
    dt = time.monotonic() - t0

    err_after = sess._read_errlst()

    return TestRecord(
        test_id=test_id,
        test_name=info["name"],
        section=info["section"],
        channel=channel,
        capture_type=capture_type,
        packet_length=packet_length,
        payload=info["payload"],
        mod_type=info["mod_type"],
        raw=oresult.raw,
        valid=oresult.valid,
        fields=list(oresult.fields),
        pass_fail=oresult.pass_fail,
        elapsed_s=dt,
        timestamp=datetime.now().isoformat(timespec="seconds"),
        errlst_before=err_before,
        errlst_after=err_after,
    )


# ----------------------------------------------------------------------------
# Report generation
# ----------------------------------------------------------------------------
def _pf(r: TestRecord) -> str:
    """Robust PASS/FAIL/INVALID/UNKNOWN extraction.

    The OResult parser only sees the trailing token as PASS/FAIL, but
    LEMI has a trailing '100.00%' toggle percentage, so the parser
    returns pass_fail=None. Fall back to substring match on the raw
    ORESULT line.
    """
    if r.pass_fail in ("PASS", "FAIL"):
        return r.pass_fail
    if r.raw and ",PASS" in r.raw:
        return "PASS"
    if r.raw and ",FAIL" in r.raw:
        return "FAIL"
    if not r.valid:
        return "INVALID"
    return "UNKNOWN"


def save_report(records: list, out_dir: Path, ctx: dict) -> None:
    """Write data.csv + summary.json + report.md into `out_dir`.

    Args:
        records: list of TestRecord from `run_one_test`.
        out_dir: target directory (will be created).
        ctx: dict of campaign context with keys:
             run_label, started, ended, duration_s, instrument, eut,
             plan, (optional) preflight.

    The output schema is the same one the lab's existing
    `run_ble_full_suite.py` produces — CLI runs and MCP runs
    produce identical files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- data.csv ----
    csv_path = out_dir / "data.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "test_id", "test_name", "channel", "capture_type",
            "packet_length", "payload", "mod_type",
            "valid", "pass_fail", "value_main", "unit",
            "n_failed", "n_packets", "elapsed_s", "timestamp", "raw",
        ])
        for r in records:
            unit = TEST_CATALOG.get(r.test_id, {}).get("unit", "")
            value_main = r.fields[0] if r.fields else ""
            n_fail = ""
            n_pkt = ""
            if r.test_id in ("LEOP", "LEICD", "LEMI") and len(r.fields) >= 7:
                n_fail = r.fields[-3]
                n_pkt = r.fields[-2]
            elif r.test_id == "LESS" and len(r.fields) >= 3:
                n_fail = r.fields[0]  # FER
                n_pkt = r.fields[1]
            w.writerow([
                r.test_id, r.test_name, r.channel, r.capture_type,
                r.packet_length, r.payload, r.mod_type,
                r.valid, r.pass_fail, value_main, unit,
                n_fail, n_pkt, f"{r.elapsed_s:.2f}", r.timestamp, r.raw,
            ])

    # ---- summary.json ----
    pf_list = [_pf(r) for r in records]
    n_total = len(records)
    n_pass = pf_list.count("PASS")
    n_fail = pf_list.count("FAIL")
    n_invalid = sum(1 for v in pf_list if v in ("INVALID", "UNKNOWN"))
    summary = {
        "run_label": ctx.get("run_label", ""),
        "started":   ctx.get("started", ""),
        "ended":     ctx.get("ended", ""),
        "duration_s": ctx.get("duration_s", 0.0),
        "instrument": ctx.get("instrument", ""),
        "eut":       ctx.get("eut", {}),
        "plan":      ctx.get("plan", {}),
        "totals": {
            "tests_run": n_total,
            "pass": n_pass,
            "fail": n_fail,
            "invalid_or_unknown": n_invalid,
        },
        "results": [asdict(r) for r in records],
    }
    if "preflight" in ctx:
        summary["preflight"] = ctx["preflight"]
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ---- report.md ----
    def _verdict(r: TestRecord) -> tuple[str, str]:
        if r.pass_fail == "PASS":
            return "PASS", "PASS"
        if r.pass_fail == "FAIL":
            return "FAIL", "FAIL"
        if r.raw and ",PASS" in r.raw:
            return "PASS", "PASS"
        if r.raw and ",FAIL" in r.raw:
            return "FAIL", "FAIL"
        if not r.valid:
            return "invalid", "INVALID"
        return "?", "UNKNOWN"

    pass_fail_list = [_verdict(r)[1] for r in records]
    n_pass_md = sum(1 for v in pass_fail_list if v == "PASS")
    n_fail_md = sum(1 for v in pass_fail_list if v == "FAIL")
    n_inv = sum(1 for v in pass_fail_list if v in ("INVALID", "UNKNOWN"))

    lines = []
    lines.append(f"# {ctx.get('run_label', 'BLE Test Run')}")
    lines.append("")
    lines.append(f"- **Started**: {ctx.get('started', '?')}")
    lines.append(f"- **Ended**: {ctx.get('ended', '?')}")
    lines.append(f"- **Duration**: {ctx.get('duration_s', 0.0):.1f} s")
    lines.append(f"- **Instrument**: {ctx.get('instrument', '?')}")
    eut = ctx.get("eut", {})
    lines.append(f"- **EUT**: {eut.get('interface','?')} "
                 f"@ {eut.get('baud','?')} baud, "
                 f"handshake={eut.get('handshake','?')}")
    plan = ctx.get("plan", {})
    lines.append(f"- **Capture type**: {plan.get('capture_type', '?')}")
    lines.append(f"- **Packet length**: {plan.get('packet_length', '?')} bytes")
    lines.append(f"- **Channels**: {plan.get('channels', '?')}")
    lines.append(f"- **Tests**: {plan.get('tests', '?')}")
    lines.append("")
    lines.append("## Per-test / per-channel results")
    lines.append("")
    lines.append("| Test | Channel | Payload | Value | Unit | PASS/FAIL | Elapsed |")
    lines.append("|---|---:|---|---:|---|:---:|---:|")
    for r in records:
        value = r.fields[0] if r.fields else "-"
        verdict, _ = _verdict(r)
        unit = TEST_CATALOG.get(r.test_id, {}).get("unit", "")
        lines.append(
            f"| {r.test_id} ({r.test_name}) | {r.channel} | {r.payload} | "
            f"{value} | {unit} | {verdict} | "
            f"{r.elapsed_s:.1f} s |"
        )
    lines.append("")
    lines.append("## Per-test documentation reference")
    lines.append("")
    for tid in sorted({r.test_id for r in records}):
        info = TEST_CATALOG.get(tid, {})
        lines.append(f"- **{tid}** - {info.get('name','?')} - {info.get('section','?')}")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    if n_inv:
        lines.append(f"- **WARNING: {n_inv} result(s) marked `valid=FALSE`** - "
                     "check bench state (DUT HCI lockout, missing packets).")
    if n_fail_md:
        lines.append(f"- **FAIL: {n_fail_md} result(s) FAIL** - see `data.csv` per-row for raw data.")
    if n_pass_md == n_total and n_total:
        lines.append(f"- **PASS: all {n_pass_md} / {n_total} results PASS**.")
    elif n_pass_md:
        lines.append(f"- **{n_pass_md} / {n_total} results PASS**, {n_fail_md} FAIL, {n_inv} invalid/uncertain.")
    lines.append("")
    lines.append("## Files in this report folder")
    lines.append("")
    lines.append("| File | Purpose |")
    lines.append("|---|---|")
    lines.append("| `data.csv` | Per-test/per-channel raw rows |")
    lines.append("| `summary.json` | Machine-readable summary + per-test envelopes |")
    lines.append("| `report.md` | This report |")
    lines.append("| `bench.log` | pyvisa traffic captured during the run |")
    lines.append("| `bench_preflight.json` | bench snapshot taken before tests began |")
    lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
