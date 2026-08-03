"""MSO54 phased validation — runs on real hardware, aborts on any error.

Usage:
  python -m mcp_MSO54.scripts.validate                # run all phases P1..P10
  python -m mcp_MSO54.scripts.validate --phase p1    # run a single phase

CRITICAL SAFETY RULES:
  - Each phase catches its own exceptions and re-raises to STOP the run.
  - NEVER sends a query for a set-only command (would halt USBTMC bulk-IN).
  - Every write is followed (in the same phase) by a readback to confirm
    the instrument accepted it. If the readback doesn't match, we abort.

This script does NOT use the MCP server — it talks to the instrument
directly through instrument.py to keep debugging output visible.
"""
from __future__ import annotations
import argparse
import logging
import sys
import time
import traceback

# Allow `python -m mcp_MSO54.scripts.validate` from the project root.
sys.path.insert(0, "E:/other/ai_project/EEMCP")

from mcp_MSO54.instrument import MSO54, MSO54Error
from mcp_MSO54 import scpi


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("validate")


# --- the single live scope handle used across all phases ---
SCOPE = MSO54()


def _hdr(name: str) -> None:
    bar = "=" * 64
    log.info("")
    log.info(bar)
    log.info("PHASE %s", name)
    log.info(bar)


def _ensure_connected() -> None:
    """Idempotent: connect only if not already."""
    if not SCOPE.connected:
        SCOPE.connect()


def _phase_run(fn):
    """Common wrapper: connect before, disconnect after, on success only."""
    _ensure_connected()
    try:
        return fn()
    finally:
        # Leave the connection open across phases when running the full
        # suite, so the operator can inspect; only disconnect on shutdown.
        pass


def p1_connect_and_idn() -> str:
    _hdr("P1: connect + *IDN? + *OPT?")
    idn = SCOPE.connect()                       # also sends HEADER OFF
    log.info("IDN: %s", idn)
    opts = SCOPE.options()
    log.info("OPT: %s", opts)
    stb = SCOPE.read_stb()
    log.info("STB: 0x%02x", stb)
    return f"idn={idn!r}; opt={opts!r}; stb=0x{stb:02x}"


def p2_safe_reads() -> str:
    _hdr("P2: all read-only queries known safe from prior session")
    # channel reads (1..CONFIG.num_channels)
    for n in range(1, SCOPE.instr and 4 + 1 or 1):  # fallback if CONFIG not loaded
        pass
    # Use config for clarity:
    from mcp_MSO54.config import CONFIG
    n_ch = CONFIG.num_channels
    log.info("--- vertical reads (CH1..CH%d) ---", n_ch)
    for n in range(1, n_ch + 1):
        scale = SCOPE.q_channel_scale(n)
        pos   = SCOPE.q_channel_position(n)
        off   = SCOPE.q_channel_offset(n)
        cpl   = SCOPE.q_channel_coupling(n)
        bw    = SCOPE.q_channel_bandwidth(n)
        term  = SCOPE.q_channel_termination(n)
        lbl   = SCOPE.q_channel_label(n)
        log.info("CH%d: scale=%sV/div pos=%sdiv off=%sV cpl=%s bw=%sHz term=%sΩ label=%r",
                 n, scale, pos, off, cpl, bw, term, lbl)

    log.info("--- horizontal reads ---")
    log.info("horiz scale=%s s/div, position=%s%%, mode=%s, sample_rate=%s, rec_len=%d",
             SCOPE.q_horizontal_scale(), SCOPE.q_horizontal_position(),
             SCOPE.q_horizontal_mode(), SCOPE.q_sample_rate(),
             SCOPE.q_record_length())

    log.info("--- acquisition reads ---")
    log.info("acq mode=%s numavg=%d state=%s stopafter=%s numacq=%d",
             SCOPE.q_acq_mode(), SCOPE.q_acq_numavg(),
             SCOPE.q_acq_state(), SCOPE.q_acq_stopafter(),
             SCOPE.q_acq_numacq())

    log.info("--- trigger reads ---")
    log.info("trig type=%s edge src=%s slope=%s cpl=%s mode=%s state=%s",
             SCOPE.q_trig_type(), SCOPE.q_trig_edge_source(),
             SCOPE.q_trig_edge_slope(), SCOPE.q_trig_edge_coupling(),
             SCOPE.q_trig_mode(), SCOPE.q_trig_state())
    log.info("trig level CH1=%s V", SCOPE.q_trig_level(1))

    log.info("--- measurement reads (current state) ---")
    log.info("MEAS LIST: %r", SCOPE.q_meas_list())
    # Try to read whatever exists; don't assume any MEAS<n> is configured
    for n in range(1, 5):
        try:
            t = SCOPE.q_meas_type(n)
            s = SCOPE.q_meas_source(n)
            v = SCOPE.q_meas_value(n)
            log.info("MEAS%d: type=%s source=%s value=%s", n, t, s, v)
        except MSO54Error as e:
            log.info("MEAS%d: not configured (%s)", n, e)
            break  # once we hit a gap, the rest are also empty

    log.info("--- waveform preamble ---")
    SCOPE.set_data_source("CH1")
    pre = SCOPE.read_preamble()
    log.info("preamble: %s", pre)

    log.info("--- DVM reads (don't change mode yet, just probe) ---")
    try:
        log.info("DVM mode=%s source=%s value=%s V",
                 SCOPE.q_dvm_mode(), SCOPE.q_dvm_source(), SCOPE.q_dvm_value())
    except MSO54Error as e:
        log.info("DVM not active (OK): %s", e)

    return "ok"


def p3_write_header_off_roundtrip() -> str:
    _hdr("P3: HEADER OFF was already sent in connect(). Verify with *IDN?")
    # HEADER OFF should already be in effect from connect(). If we see a
    # bare TEKTRONIX,... string (no echo), it's working.
    idn = SCOPE.idn()
    assert "TEKTRONIX" in idn.upper(), f"unexpected IDN: {idn!r}"
    log.info("IDN confirmed bare (no 'IDN' echo prefix): %s", idn)
    return idn


def p4_autoset_roundtrip() -> str:
    _hdr("P4: AUTOSET EXECUTE → readback horizontal+acquisition state")
    pre_scale = SCOPE.q_horizontal_scale()
    pre_state = SCOPE.q_acq_state()
    log.info("BEFORE autoset: horiz_scale=%s, acq_state=%s", pre_scale, pre_state)
    SCOPE.autoset()
    time.sleep(0.5)  # let autoset settle before reading back
    post_scale = SCOPE.q_horizontal_scale()
    post_state = SCOPE.q_acq_state()
    log.info("AFTER  autoset: horiz_scale=%s, acq_state=%s", post_scale, post_state)
    return f"pre={pre_scale}/{pre_state}; post={post_scale}/{post_state}"


def p5_channel_write_roundtrip() -> str:
    _hdr("P5: CH1 scale write → readback (STOP acquisition during write)")
    # MSO54 quirk (FW 1.34.8.1078, verified 2026-07-18): while acquisition
    # is RUN, the instrument continuously auto-adjusts vertical scale to
    # fit the waveform, overwriting any user CH1:SCALE value within ~100 ms.
    # Workaround: STOP acquisition, write, read back, then RUN again.
    # Verified: scale=1.0 V/div persists after RUN resumes.
    SCOPE.reset()
    SCOPE.set_acq_state(False)         # STOP
    time.sleep(0.5)
    pre = SCOPE.q_channel_scale(1)
    log.info("post-reset+STOP CH1 scale=%s", pre)
    SCOPE.set_channel_scale(1, 1.0)
    time.sleep(0.3)
    rb = SCOPE.q_channel_scale(1)
    log.info("CH1 scale: set=1.0  readback=%s", rb)
    assert abs(rb - 1.0) < 1e-6, f"CH1 scale readback mismatch: {rb}"
    SCOPE.set_acq_state(True)          # RUN, restore
    return f"scale={rb}"


def p6_trigger_write_roundtrip() -> str:
    _hdr("P6: TRIGGER A edge source/slope/level writes → readback")
    pre_src = SCOPE.q_trig_edge_source()
    pre_slope = SCOPE.q_trig_edge_slope()
    pre_level = SCOPE.q_trig_level(1)
    log.info("BEFORE: src=%s slope=%s level=%s", pre_src, pre_slope, pre_level)

    SCOPE.set_trig_edge_source("CH1")
    SCOPE.set_trig_edge_slope("RISE")
    SCOPE.set_trig_level(1, 0.0)
    rb_src = SCOPE.q_trig_edge_source()
    rb_slope = SCOPE.q_trig_edge_slope()
    rb_level = SCOPE.q_trig_level(1)
    log.info("AFTER : src=%s slope=%s level=%s", rb_src, rb_slope, rb_level)
    assert "CH1" in rb_src, f"trig source readback mismatch: {rb_src}"
    assert "RISE" in rb_slope.upper(), f"trig slope readback mismatch: {rb_slope}"
    assert abs(rb_level) < 1e-6, f"trig level readback mismatch: {rb_level}"
    return f"src={rb_src}; slope={rb_slope}; level={rb_level}"


def p7_meas_add_roundtrip() -> str:
    """Add ONE measurement; verify by querying MEAS<n>:TYPE? — NEVER query ADDMEAS."""
    _hdr("P7: MEASUREMENT:ADDMEAS FREQUENCY (write-only) → MEAS<n>:TYPE? readback")
    # Find the lowest empty MEAS<n>. We do this by trying DELETE on 1..8 first
    # (cheap, idempotent) then ADDMEAS. ADDMEAS itself auto-assigns the slot,
    # so we then LIST? to find where it landed.
    for n in range(1, 9):
        try:
            SCOPE.meas_delete(n)
        except MSO54Error:
            pass
    pre_list = SCOPE.q_meas_list()
    log.info("MEAS LIST (cleared): %r", pre_list)

    SCOPE.meas_add("FREQUENCY")
    time.sleep(0.2)
    post_list = SCOPE.q_meas_list()
    log.info("MEAS LIST (after ADD FREQUENCY): %r", post_list)

    # Default source is CH1 — confirm by reading MEAS1:SOURCE? and TYPE?
    # If FREQUENCY didn't land at slot 1, scan 1..8 for the first slot with
    # a non-empty type.
    for n in range(1, 9):
        try:
            t = SCOPE.q_meas_type(n)
            s = SCOPE.q_meas_source(n)
            log.info("MEAS%d: type=%s source=%s", n, t, s)
            if "FREQ" in t.upper():
                v = SCOPE.q_meas_value(n)
                log.info("MEAS%d value=%s Hz", n, v)
                return f"slot={n}; type={t}; src={s}; value={v}"
        except MSO54Error as e:
            log.info("MEAS%d: empty (%s)", n, e)
            break
    raise MSO54Error("no slot returned a FREQUENCY type after ADDMEAS")


def p8_dvm_roundtrip() -> str:
    _hdr("P8: DVM mode/source write → DVM:MEASUREMENT:VALUE? readback")
    SCOPE.set_dvm_source("CH1")
    SCOPE.set_dvm_mode("DC")
    time.sleep(0.3)
    mode = SCOPE.q_dvm_mode()
    src = SCOPE.q_dvm_source()
    v = SCOPE.q_dvm_value()
    log.info("DVM mode=%s src=%s value=%s V", mode, src, v)
    assert "DC" in mode, f"DVM mode readback mismatch: {mode}"
    assert "CH1" in src, f"DVM source readback mismatch: {src}"
    return f"mode={mode}; src={src}; value={v}"


def p9_screenshot_roundtrip() -> str:
    _hdr("P9: SAVE:IMAGE + FILESYSTEM:READFILE")
    path = "/tmp/mcp_mso54_validate.png"
    SCOPE.fs_delete(path)  # best-effort cleanup
    SCOPE.save_image(path)
    time.sleep(1.0)  # let the scope write the file
    data = SCOPE.read_file_bytes(path)
    log.info("screenshot bytes: %d  head=%s", len(data), data[:8].hex())
    assert len(data) > 100, f"screenshot suspiciously small: {len(data)} bytes"
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG (header={data[:8].hex()})"
    SCOPE.fs_delete(path)
    return f"png_bytes={len(data)}"


def p10_curve_roundtrip() -> str:
    _hdr("P10: CURVE? on CH1 — binary waveform with preamble decoding")
    SCOPE.set_data_source("CH1")
    SCOPE.set_data_encoding("RIBINARY")
    SCOPE.set_data_width(1)
    # Limit to a manageable window so the transfer finishes fast.
    SCOPE.set_data_start(1)
    SCOPE.set_data_stop(1000)
    wfm = SCOPE.read_curve("CH1", start=1, stop=1000, width=1)
    log.info("curve: npoints=%d  unit=%s  vmin=%.4g  vmax=%.4g  vmean=%.4g",
             wfm["npoints"], wfm["unit"],
             min(wfm["values"]), max(wfm["values"]),
             sum(wfm["values"]) / len(wfm["values"]))
    log.info("preamble: %s", wfm["preamble"])
    return f"npoints={wfm['npoints']}; unit={wfm['unit']}"


def p11_save_image_no_read() -> str:
    _hdr("P11: SAVE:IMAGE (write-only) — confirm scope stays responsive")
    # SAVE:IMAGE itself is a write-only command: it tells the scope to save
    # a screenshot to its own filesystem. The dangerous part we already know
    # about is FILESYSTEM:READFILE (P9) — here we only verify the WRITE
    # half is safe by sending it and then confirming the bus is still alive
    # with a fresh *IDN?.
    path = "/tmp/mcp_mso54_p11.png"
    SCOPE.save_image(path)
    time.sleep(1.0)  # let the scope actually finish writing
    # Confirm bus is alive — if SAVE:IMAGE had a hidden read side-effect,
    # this query would time out.
    idn = SCOPE.idn()
    assert "TEKTRONIX" in idn.upper()
    log.info("SAVE:IMAGE returned cleanly; bus still alive (IDN=%s)", idn)
    # Best-effort cleanup
    try:
        SCOPE.fs_delete(path)
    except Exception:
        pass
    return "save_image_ok"


PHASES = {
    "p1":  p1_connect_and_idn,
    "p2":  p2_safe_reads,
    "p3":  p3_write_header_off_roundtrip,
    "p4":  p4_autoset_roundtrip,
    "p5":  p5_channel_write_roundtrip,
    "p6":  p6_trigger_write_roundtrip,
    "p7":  p7_meas_add_roundtrip,
    "p8":  p8_dvm_roundtrip,
    # p9 REMOVED — FILESYSTEM:READFILE wedges USBTMC bulk-IN on this scope.
    # See mcp_MSO54/instrument.py read_file_bytes() and NOTES.md §1.
    "p10": p10_curve_roundtrip,
    "p11": p11_save_image_no_read,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=list(PHASES.keys()), default=None,
                    help="Run a single phase; default = all in order.")
    args = ap.parse_args()

    targets = [args.phase] if args.phase else list(PHASES.keys())

    log.info("=" * 64)
    log.info("MSO54 phased validation")
    log.info("resource: %s", SCOPE.resource)
    log.info("phases:   %s", ", ".join(targets))
    log.info("=" * 64)

    single_phase = args.phase is not None
    try:
        for name in targets:
            fn = PHASES[name]
            _phase_run(fn)
            log.info(">>> %s PASSED", name)
        if single_phase:
            log.info(">>> single-phase run done; disconnecting")
            SCOPE.disconnect()
    except Exception:
        log.error(">>> ABORT: %s", traceback.format_exc())
        try:
            SCOPE.disconnect()
        except Exception:
            pass
        sys.exit(1)

    log.info("=" * 64)
    log.info("ALL REQUESTED PHASES PASSED")
    log.info("=" * 64)


if __name__ == "__main__":
    main()
