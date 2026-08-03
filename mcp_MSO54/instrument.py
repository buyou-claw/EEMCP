"""MSO54 instrument handle — pyvisa wrapper (the ONLY file that imports pyvisa).

A thin layer that owns the pyvisa ResourceManager and the open USBTMC
session to a single Tektronix 5 Series MSO. Higher-level code (the MCP
tools in server.py) must never touch pyvisa directly — it goes through
the typed helpers here so timeouts, error mapping, and — critically —
the write/query separation live in exactly one place.

  ┌─────────────────────────────────────────────────────────────────────┐
  │ USBTMC LOCKUP RULE (see NOTES.md §1, and scpi.py header)             │
  │ On the bench MSO54, sending a `?` for a set-only / action command    │
  │ halts the USBTMC bulk-IN endpoint. Writes keep working but every     │
  │ read returns VI_ERROR_IO, and NOTHING short of a scope power-cycle   │
  │ recovers it (viClear "succeeds" but the IN pipe stays dead).         │
  │                                                                       │
  │ Defense in depth here:                                               │
  │   • query() REFUSES any string that does not end in '?'.             │
  │   • write() REFUSES any string that ends in '?'.                     │
  │   • All command strings come from scpi.py, never ad-hoc.             │
  └─────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations
import time
import logging
from typing import Optional

import pyvisa

from . import scpi
from .config import CONFIG

log = logging.getLogger(__name__)


class MSO54Error(RuntimeError):
    pass


class MSO54:
    """Owns one open USBTMC session to a single MSO54."""

    def __init__(self, resource: Optional[str] = None):
        self.resource = resource or CONFIG.visa_resource
        self.rm: Optional[pyvisa.ResourceManager] = None
        self.instr = None
        self._connected = False

    # --- connection lifecycle ---
    def connect(self) -> str:
        if self._connected:
            return self.idn()
        log.info("Opening VISA session to %s", self.resource)
        self.rm = pyvisa.ResourceManager()  # default backend (NI-VISA)
        self.instr = self.rm.open_resource(self.resource)
        self.instr.timeout = CONFIG.timeout_ms
        self.instr.read_termination = "\n"
        self.instr.write_termination = "\n"
        self._connected = True
        # HEADER OFF so query replies are bare values, not "CMD ARG;" echoes.
        self.write(scpi.header_off())
        idn = self.idn()
        log.info("Connected: %s", idn)
        return idn

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

    # --- raw SCPI (write/query strictly separated) ---
    def write(self, cmd: str) -> None:
        """Send a command that expects NO reply.

        Guards against the USBTMC lockup: refuses anything ending in '?'.
        A query sent through write() would leave a reply stranded in the
        output queue; but more importantly this keeps the two paths from
        ever being confused at the call site.
        """
        if not self.connected:
            raise MSO54Error("not connected")
        if cmd.rstrip().endswith("?"):
            raise MSO54Error(
                f"refusing to write() a query string: {cmd!r} — use query()"
            )
        log.debug(">> %s", cmd)
        self.instr.write(cmd)

    def query(self, cmd: str) -> str:
        """Send a query and read the reply.

        HARD GUARD (the whole reason this class exists): refuse any string
        that is not a genuine query ending in '?'. This is the single
        checkpoint that makes the "never query a set-only command" rule
        impossible to violate by accident.
        """
        if not self.connected:
            raise MSO54Error("not connected")
        if not cmd.rstrip().endswith("?"):
            raise MSO54Error(
                f"refusing to query() a non-query string: {cmd!r} — "
                "querying a set-only command halts the USBTMC bus (NOTES.md §1)"
            )
        log.debug(">> %s", cmd)
        resp = self.instr.query(cmd)
        log.debug("<< %s", resp.strip())
        return resp.strip()

    def query_float(self, cmd: str) -> float:
        return float(self.query(cmd))

    # --- universal / control ---
    def idn(self) -> str:
        return self.query(scpi.idn())

    def opc(self) -> str:
        return self.query(scpi.opc())

    def options(self) -> str:
        return self.query(scpi.options())

    def all_events(self) -> str:
        """Drain the event/error queue (safe query — has a reply)."""
        return self.query(scpi.all_events())

    def read_stb(self) -> int:
        """Read the Status Byte via the USBTMC control transfer.

        This is a *control-endpoint* read (READ_STATUS_BYTE), NOT a bulk
        read — it does not touch the bulk-IN pipe and so is safe even when
        the bulk-IN endpoint is wedged. Used as a lightweight health probe.
        """
        if not self.connected:
            raise MSO54Error("not connected")
        return int(self.instr.read_stb())

    def reset(self) -> None:
        self.write(scpi.reset())
        time.sleep(CONFIG.reset_settle_s)

    def clear_status(self) -> None:
        self.write(scpi.clear_status())

    def factory(self) -> None:
        self.write(scpi.factory())
        time.sleep(CONFIG.reset_settle_s)

    def autoset(self) -> None:
        self.write(scpi.autoset())
        time.sleep(CONFIG.reset_settle_s)

    # --- vertical / channel (writes) ---
    def set_channel_display(self, n: int, on: bool) -> None:
        self.write(scpi.set_channel_display(n, on))

    def set_channel_scale(self, n: int, volts_per_div: float) -> None:
        self.write(scpi.set_channel_scale(n, volts_per_div))

    def set_channel_position(self, n: int, divs: float) -> None:
        self.write(scpi.set_channel_position(n, divs))

    def set_channel_offset(self, n: int, volts: float) -> None:
        self.write(scpi.set_channel_offset(n, volts))

    def set_channel_coupling(self, n: int, coupling: str) -> None:
        self.write(scpi.set_channel_coupling(n, coupling))

    def set_channel_bandwidth(self, n: int, hz) -> None:
        self.write(scpi.set_channel_bandwidth(n, hz))

    def set_channel_termination(self, n: int, ohms: float) -> None:
        self.write(scpi.set_channel_termination(n, ohms))

    def set_channel_label(self, n: int, text: str) -> None:
        self.write(scpi.set_channel_label(n, text))

    # --- vertical / channel (queries) ---
    def q_channel_display(self, n: int) -> str:  return self.query(scpi.q_channel_display(n))
    def q_channel_scale(self, n: int) -> float:  return self.query_float(scpi.q_channel_scale(n))
    def q_channel_position(self, n: int) -> float: return self.query_float(scpi.q_channel_position(n))
    def q_channel_offset(self, n: int) -> float: return self.query_float(scpi.q_channel_offset(n))
    def q_channel_coupling(self, n: int) -> str: return self.query(scpi.q_channel_coupling(n))
    def q_channel_bandwidth(self, n: int) -> float: return self.query_float(scpi.q_channel_bandwidth(n))
    def q_channel_termination(self, n: int) -> float: return self.query_float(scpi.q_channel_termination(n))
    def q_channel_label(self, n: int) -> str:    return self.query(scpi.q_channel_label(n))

    # --- horizontal (writes) ---
    def set_horizontal_scale(self, sec_per_div: float) -> None:
        self.write(scpi.set_horizontal_scale(sec_per_div))

    def set_horizontal_position(self, percent: float) -> None:
        self.write(scpi.set_horizontal_position(percent))

    def set_horizontal_mode(self, mode: str) -> None:
        self.write(scpi.set_horizontal_mode(mode))

    def set_sample_rate(self, sa_per_s: float) -> None:
        self.write(scpi.set_sample_rate(sa_per_s))

    def set_record_length(self, points: int) -> None:
        self.write(scpi.set_record_length(points))

    # --- horizontal (queries) ---
    def q_horizontal_scale(self) -> float:    return self.query_float(scpi.q_horizontal_scale())
    def q_horizontal_position(self) -> float: return self.query_float(scpi.q_horizontal_position())
    def q_horizontal_mode(self) -> str:       return self.query(scpi.q_horizontal_mode())
    def q_sample_rate(self) -> float:         return self.query_float(scpi.q_sample_rate())
    def q_record_length(self) -> int:         return int(float(self.query(scpi.q_record_length())))

    # --- acquisition (writes) ---
    def set_acq_mode(self, mode: str) -> None:
        self.write(scpi.set_acq_mode(mode))

    def set_acq_numavg(self, n: int) -> None:
        self.write(scpi.set_acq_numavg(n))

    def set_acq_state(self, run: bool) -> None:
        self.write(scpi.set_acq_state(run))

    def set_acq_stopafter(self, mode: str) -> None:
        self.write(scpi.set_acq_stopafter(mode))

    # --- acquisition (queries) ---
    def q_acq_mode(self) -> str:      return self.query(scpi.q_acq_mode())
    def q_acq_numavg(self) -> int:    return int(float(self.query(scpi.q_acq_numavg())))
    def q_acq_state(self) -> str:     return self.query(scpi.q_acq_state())
    def q_acq_stopafter(self) -> str: return self.query(scpi.q_acq_stopafter())
    def q_acq_numacq(self) -> int:    return int(float(self.query(scpi.q_acq_numacq())))

    # --- trigger (writes) ---
    def set_trig_type(self, t: str) -> None:
        self.write(scpi.set_trig_type(t))

    def set_trig_edge_source(self, source: str) -> None:
        self.write(scpi.set_trig_edge_source(source))

    def set_trig_edge_slope(self, slope: str) -> None:
        self.write(scpi.set_trig_edge_slope(slope))

    def set_trig_edge_coupling(self, coupling: str) -> None:
        self.write(scpi.set_trig_edge_coupling(coupling))

    def set_trig_level(self, n: int, volts: float) -> None:
        self.write(scpi.set_trig_level(n, volts))

    def set_trig_mode(self, mode: str) -> None:
        self.write(scpi.set_trig_mode(mode))

    def trig_force(self) -> None:
        self.write(scpi.trig_force())

    # --- trigger (queries) ---
    def q_trig_type(self) -> str:          return self.query(scpi.q_trig_type())
    def q_trig_edge_source(self) -> str:   return self.query(scpi.q_trig_edge_source())
    def q_trig_edge_slope(self) -> str:    return self.query(scpi.q_trig_edge_slope())
    def q_trig_edge_coupling(self) -> str: return self.query(scpi.q_trig_edge_coupling())
    def q_trig_level(self, n: int) -> float: return self.query_float(scpi.q_trig_level(n))
    def q_trig_mode(self) -> str:          return self.query(scpi.q_trig_mode())
    def q_trig_state(self) -> str:         return self.query(scpi.q_trig_state())

    # --- automatic measurements ---
    def meas_add(self, meas_type: str) -> None:
        """Add a measurement. SET-ONLY — never queried."""
        self.write(scpi.meas_add(meas_type))

    def meas_delete(self, n: int) -> None:
        self.write(scpi.meas_delete(n))

    def meas_set_source(self, n: int, source: str) -> None:
        self.write(scpi.meas_set_source(n, source))

    def meas_set_type(self, n: int, meas_type: str) -> None:
        self.write(scpi.meas_set_type(n, meas_type))

    def q_meas_list(self) -> str:         return self.query(scpi.q_meas_list())
    def q_meas_type(self, n: int) -> str: return self.query(scpi.q_meas_type(n))
    def q_meas_source(self, n: int) -> str: return self.query(scpi.q_meas_source(n))
    def q_meas_value(self, n: int) -> float: return self.query_float(scpi.q_meas_value(n))
    def q_meas_mean(self, n: int) -> float:  return self.query_float(scpi.q_meas_mean(n))
    def q_meas_min(self, n: int) -> float:   return self.query_float(scpi.q_meas_min(n))
    def q_meas_max(self, n: int) -> float:   return self.query_float(scpi.q_meas_max(n))
    def q_meas_stddev(self, n: int) -> float: return self.query_float(scpi.q_meas_stddev(n))

    # --- waveform transfer (CURVE) ---
    def set_data_source(self, source: str) -> None:
        self.write(scpi.set_data_source(source))

    def set_data_encoding(self, enc: str) -> None:
        self.write(scpi.set_data_encoding(enc))

    def set_data_width(self, bytes_per_point: int) -> None:
        self.write(scpi.set_data_width(bytes_per_point))

    def set_data_start(self, point: int) -> None:
        self.write(scpi.set_data_start(point))

    def set_data_stop(self, point: int) -> None:
        self.write(scpi.set_data_stop(point))

    def q_data_source(self) -> str:   return self.query(scpi.q_data_source())
    def q_data_encoding(self) -> str: return self.query(scpi.q_data_encoding())

    def read_preamble(self) -> dict:
        """Read the WFMOUTPRE scaling block needed to convert raw samples."""
        return {
            "npoints": int(float(self.query(scpi.q_wfm_npoints()))),
            "xincr": self.query_float(scpi.q_wfm_xincr()),
            "xzero": self.query_float(scpi.q_wfm_xzero()),
            "ymult": self.query_float(scpi.q_wfm_ymult()),
            "yzero": self.query_float(scpi.q_wfm_yzero()),
            "yoff": self.query_float(scpi.q_wfm_yoff()),
            "xunit": self.query(scpi.q_wfm_xunit()).strip('"'),
            "yunit": self.query(scpi.q_wfm_yunit()).strip('"'),
        }

    def read_curve(self, source: str, start: int = 1, stop: Optional[int] = None,
                   width: int = 1) -> dict:
        """Acquire one waveform and return scaled samples.

        Configures DATA:* for a binary transfer, reads the CURVE? block
        with an extended timeout, then applies the preamble scaling:
            volts = (raw - YOFF) * YMULT + YZERO
            t[i]  = XZERO + i * XINCR
        Returns {"source", "unit", "times", "values", "npoints", "preamble"}.
        """
        if not self.connected:
            raise MSO54Error("not connected")
        self.set_data_source(source)
        self.set_data_encoding("RIBINARY")   # signed big-endian integers
        self.set_data_width(width)
        self.set_data_start(start)
        if stop is None:
            stop = self.q_record_length()
        self.set_data_stop(stop)

        pre = self.read_preamble()

        old = self.instr.timeout
        self.instr.timeout = CONFIG.curve_timeout_ms
        try:
            datatype = "b" if width == 1 else "h"  # 8-bit vs 16-bit signed
            raw = self.instr.query_binary_values(
                scpi.curve(), datatype=datatype, is_big_endian=True,
                container=list,
            )
        finally:
            self.instr.timeout = old

        yoff, ymult, yzero = pre["yoff"], pre["ymult"], pre["yzero"]
        xzero, xincr = pre["xzero"], pre["xincr"]
        values = [(r - yoff) * ymult + yzero for r in raw]
        times = [xzero + i * xincr for i in range(len(values))]
        return {
            "source": source,
            "unit": pre["yunit"],
            "times": times,
            "values": values,
            "npoints": len(values),
            "preamble": pre,
        }

    # --- DVM (option SUP5-DVM) ---
    def set_dvm_source(self, source: str) -> None:
        self.write(scpi.set_dvm_source(source))

    def set_dvm_mode(self, mode: str) -> None:
        self.write(scpi.set_dvm_mode(mode))

    def q_dvm_mode(self) -> str:    return self.query(scpi.q_dvm_mode())
    def q_dvm_source(self) -> str:  return self.query(scpi.q_dvm_source())
    def q_dvm_value(self) -> float: return self.query_float(scpi.q_dvm_value())

    # --- screenshot / filesystem ---
    def save_image(self, instrument_path: str) -> None:
        self.write(scpi.save_image(instrument_path))

    def fs_delete(self, instrument_path: str) -> None:
        self.write(scpi.fs_delete(instrument_path))

    def read_file_bytes(self, instrument_path: str) -> bytes:
        """DISABLED on the USBTMC transport — see below.

        Attempting this method on the bench MSO54 via USB wedged the USBTMC
        bulk-IN endpoint THREE times in a row (2026-07-18). Only USB-port
        re-enumeration (and on earlier attempts a full scope reboot) recovered
        it. The combination of "long binary payload + read_termination='\\n' +
        arbitrary block response" is not survivable on this firmware over USB.

        Recovery: USB port reset → bulk-IN recovers (no scope reboot needed).
        See memory/feedback_usbtmc_setonly_query.md and NOTES.md §1.

        To re-enable this path, you must do so via a transport where the
        bulk-IN lockup cannot occur (LAN socket: TCPIP0::...::4000::SOCKET).
        Do NOT re-enable on USB without first proving the path is safe.
        """
        raise MSO54Error(
            "read_file_bytes() is disabled on USBTMC — the path wedges the "
            "MSO54's bulk-IN endpoint. Use the LAN socket transport, or have "
            "the operator pull the screenshot off the scope manually. "
            "See NOTES.md §1."
        )
        # --- Original implementation, kept for reference / LAN port. ---
        # if not self.connected:
        #     raise MSO54Error("not connected")
        #
        # old_timeout = self.instr.timeout
        # self.instr.timeout = CONFIG.curve_timeout_ms
        # try:
        #     self.instr.write(scpi.fs_readfile(instrument_path))
        #     hdr = self._read_exact(1)
        #     if hdr != b"#":
        #         raise MSO54Error(
        #             f"FILESYSTEM:READFILE: expected '#' header, got {hdr!r}"
        #         )
        #     digit_count_b = self._read_exact(1)
        #     n_digits = int(digit_count_b.decode("ascii"))
        #     length_b = self._read_exact(n_digits)
        #     payload_len = int(length_b.decode("ascii"))
        #     payload = self._read_exact(payload_len)
        #     try:
        #         _ = self.instr.read_raw()
        #     except Exception:
        #         pass
        #     return payload
        # finally:
        #     self.instr.timeout = old_timeout
        if not self.connected:
            raise MSO54Error("not connected")

        old_timeout = self.instr.timeout
        self.instr.timeout = CONFIG.curve_timeout_ms
        try:
            self.instr.write(scpi.fs_readfile(instrument_path))

            # 1. Read the leading '#'
            hdr = self._read_exact(1)
            if hdr != b"#":
                raise MSO54Error(
                    f"FILESYSTEM:READFILE: expected '#' header, got {hdr!r}"
                )

            # 2. Read the single byte that says how many digits follow
            digit_count_b = self._read_exact(1)
            try:
                n_digits = int(digit_count_b.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as e:
                raise MSO54Error(
                    f"FILESYSTEM:READFILE: bad digit-count byte "
                    f"{digit_count_b!r}: {e}"
                )
            if not (1 <= n_digits <= 9):
                raise MSO54Error(
                    f"FILESYSTEM:READFILE: implausible digit count {n_digits}"
                )

            # 3. Read that many ASCII digits = payload length
            length_b = self._read_exact(n_digits)
            try:
                payload_len = int(length_b.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as e:
                raise MSO54Error(
                    f"FILESYSTEM:READFILE: bad length bytes {length_b!r}: {e}"
                )

            # 4. Read exactly payload_len bytes (the file contents)
            payload = self._read_exact(payload_len)

            # 5. Best-effort drain of trailing '\n' (Tek 5-series adds one)
            try:
                tail = self.instr.read_raw()  # no size arg -> reads until terminator or 1 read
                # Discard the trailing LF/CR — file content is already complete.
                _ = tail
            except Exception:
                pass

            return payload
        finally:
            self.instr.timeout = old_timeout

    def _read_exact(self, n: int) -> bytes:
        """Read EXACTLY n bytes, looping until the buffer is full.

        pyvisa's `read_raw(size=n)` is not guaranteed to return all n bytes
        in one call — it can short-read. We loop on the remaining count.
        """
        if n <= 0:
            return b""
        buf = bytearray()
        while len(buf) < n:
            chunk = self.instr.read_raw(size=n - len(buf))
            if not chunk:
                # No more data; bailing out rather than spinning.
                raise MSO54Error(
                    f"short read: wanted {n} bytes, got {len(buf)} so far"
                )
            buf.extend(chunk)
        return bytes(buf)
