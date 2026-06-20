"""34465A instrument handle — pyvisa wrapper.

A thin layer that owns the pyvisa ResourceManager and the open session.
The DMM is fundamentally simpler than the MT8852B: each measurement is
a single shot, so there is no session state machine. The class only needs
to manage connect/disconnect, raw SCPI, and a typed set of measurement
helpers (one per function) that return a float.

Higher-level code (MCP tools) should not call pyvisa directly; use the
methods here so we have a single place to handle timeouts and error
mapping.
"""
from __future__ import annotations
import time
import logging
from typing import Optional

import pyvisa

from . import scpi
from .config import CONFIG

log = logging.getLogger(__name__)


class DMM34465AError(RuntimeError):
    pass


class DMM34465A:
    """Owns one open session to a single 34465A."""

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
        # 34465A appends \n to replies; make sure we strip it.
        self.instr.read_termination = "\n"
        self.instr.write_termination = "\n"
        self._connected = True
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

    # --- raw SCPI ---
    def write(self, cmd: str) -> None:
        if not self.connected:
            raise DMM34465AError("not connected")
        log.debug(">> %s", cmd)
        self.instr.write(cmd)

    def query(self, cmd: str) -> str:
        if not self.connected:
            raise DMM34465AError("not connected")
        log.debug(">> %s", cmd)
        resp = self.instr.query(cmd)
        log.debug("<< %s", resp.strip())
        return resp.strip()

    def query_float(self, cmd: str) -> float:
        return float(self.query(cmd))

    # --- commonly used commands ---
    def idn(self) -> str:
        return self.query(scpi.idn())

    def reset(self) -> None:
        self.write(scpi.reset())
        # *RST is fast on a DMM, but let the relays/input mux settle.
        time.sleep(CONFIG.reset_settle_s)

    def clear_status(self) -> None:
        self.write(scpi.clear_status())

    def abort(self) -> None:
        self.write(scpi.abort())

    def system_error(self) -> tuple[int, str]:
        resp = self.query(scpi.system_error())
        # Typical format: -113,"Undefined header"
        if "," in resp:
            code_s, msg = resp.split(",", 1)
            return int(code_s), msg.strip('"')
        try:
            return int(resp), ""
        except ValueError:
            return 0, resp

    # --- configuration ---
    def conf(self, function: str) -> None:
        self.write(scpi.conf(function))

    def set_nplc(self, function: str, nplc: float) -> None:
        self.write(scpi.set_nplc(function, nplc))

    def set_resolution(self, function: str, digits: float) -> None:
        self.write(scpi.set_resolution(function, digits))

    def set_auto_zero(self, function: str, mode: str) -> None:
        self.write(scpi.set_auto_zero(function, mode))

    def set_continuity_threshold(self, ohms: float) -> None:
        self.write(scpi.continuity_threshold(ohms))

    def set_beep(self, state: str) -> None:
        self.write(scpi.beep(state))

    # --- measurement helpers (return a float) ---
    def measure(self, function: str) -> float:
        """One-shot measurement: configure + trigger + fetch.

        The 34465A auto-ranges when no range is given. Returns the reading
        as a float (or str for special forms — see `measure_diode`).
        """
        return float(self.query(scpi.meas_one_shot(function)))

    def measure_volt_dc(self) -> float:    return self.measure("VOLT:DC")
    def measure_volt_ac(self) -> float:    return self.measure("VOLT:AC")
    def measure_curr_dc(self) -> float:    return self.measure("CURR:DC")
    def measure_curr_ac(self) -> float:    return self.measure("CURR:AC")
    def measure_res(self) -> float:        return self.measure("RES")
    def measure_fres(self) -> float:       return self.measure("FRES")
    def measure_freq(self) -> float:       return self.measure("FREQ")
    def measure_per(self) -> float:        return self.measure("PER")
    def measure_cont(self) -> float:       return self.measure("CONT")
    def measure_cap(self) -> float:        return self.measure("CAP")

    def measure_diode(self) -> float:
        # Diode returns volts; float is the right shape.
        return self.measure("DIOD")

    def measure_temp_rtd(self) -> float:   return self._measure_with_extended_timeout("TEMP:RTD")
    def measure_temp_ther(self) -> float:  return self._measure_with_extended_timeout("TEMP:THER")
    def measure_temp_tc(self) -> float:    return self._measure_with_extended_timeout("TEMP:TC")

    def _measure_with_extended_timeout(self, function: str) -> float:
        """Run a one-shot measurement with `temperature_timeout_ms`.

        Temperature probes on the 34465A wait on a valid transducer
        reading; with the probe disconnected the instrument holds the
        bus, so we lengthen the pyvisa read timeout for this query.
        """
        if not self.connected:
            raise DMM34465AError("not connected")
        old = self.instr.timeout
        self.instr.timeout = CONFIG.temperature_timeout_ms
        try:
            return float(self.query(scpi.meas_one_shot(function)))
        finally:
            self.instr.timeout = old
