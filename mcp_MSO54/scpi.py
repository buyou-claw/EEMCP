"""MSO54 (Tektronix 5 Series MSO) SCPI command builders.

Every command is a Python function returning the string to send. This
centralizes syntax and — critically for this instrument — enforces a hard
separation between QUERIES and WRITES.

  ┌─────────────────────────────────────────────────────────────────────┐
  │ USBTMC LOCKUP RULE (see NOTES.md §1)                                  │
  │ Never send a `?` for a set-only / action command. On the bench MSO54  │
  │ this halts the USBTMC bulk-IN endpoint: writes keep working but every │
  │ read returns VI_ERROR_IO, and NI-VISA's viClear cannot recover it —   │
  │ only a scope power-cycle (or USB-port re-enumeration) clears it.      │
  │                                                                       │
  │ Therefore: functions here that end in `?` are the ONLY strings ever   │
  │ passed to instrument.query(). Everything else is write-only.          │
  │ instrument.query() additionally guards on the trailing `?`.           │
  └─────────────────────────────────────────────────────────────────────┘

Command tree verified against the bench unit (FV:1.34.8.1078) on
2026-07-18. `MEASUrement:MEAS<n>:UNITS?` was found NOT to answer on this
firmware (it wedges the bus) — do not add it; derive units from the
measurement type via config.MEAS_UNIT instead.
"""
from __future__ import annotations
from typing import Literal


Slope = Literal["RISE", "FALL", "EITHER"]
Coupling = Literal["DC", "AC", "DCREJ", "HFREJ", "LFREJ", "NOISEREJ"]
TrigMode = Literal["AUTO", "NORMAL"]
AcqMode = Literal["SAMPLE", "PEAKDETECT", "HIRES", "AVERAGE", "ENVELOPE"]
Encoding = Literal["ASCII", "RIBINARY", "RPBINARY", "FASTEST"]


# ---------------------------------------------------------------------------
# Universal / instrument control  (WRITES)
# ---------------------------------------------------------------------------
def reset() -> str:
    """Reset to *RST defaults."""
    return "*RST"


def clear_status() -> str:
    return "*CLS"


def factory() -> str:
    """Full factory reset (heavier than *RST)."""
    return "FACTORY"


def header_off() -> str:
    """Strip the 'CMD ARG;' echo from query replies so we get bare values."""
    return "HEADER OFF"


def autoset() -> str:
    """Auto-scale vertical/horizontal/trigger for a stable display."""
    return "AUTOSET EXECUTE"


# ---------------------------------------------------------------------------
# Universal / instrument control  (QUERIES)
# ---------------------------------------------------------------------------
def idn() -> str:
    return "*IDN?"


def opc() -> str:
    """Operation-complete query — returns 1 when prior overlapped cmds finish."""
    return "*OPC?"


def options() -> str:
    return "*OPT?"


def all_events() -> str:
    """Drain the event/error queue: returns 'code,"msg";code,"msg";...'."""
    return "ALLEV?"


def event_query() -> str:
    """Single oldest event code (integer)."""
    return "EVENT?"


def analog_bandwidth() -> str:
    return "CONFIGURATION:ANALOG:BANDWIDTH?"


# ---------------------------------------------------------------------------
# Vertical / channel
# ---------------------------------------------------------------------------
def _ch(n: int) -> str:
    if n < 1 or n > 8:
        raise ValueError(f"channel must be 1..8, got {n}")
    return f"CH{n}"


# --- writes ---
def set_channel_display(n: int, on: bool) -> str:
    """Turn a channel's waveform on/off (5 Series global display state)."""
    return f"DISPLAY:GLOBAL:{_ch(n)}:STATE {'ON' if on else 'OFF'}"


def set_channel_scale(n: int, volts_per_div: float) -> str:
    if volts_per_div <= 0:
        raise ValueError("volts_per_div must be > 0")
    return f"{_ch(n)}:SCALE {volts_per_div:g}"


def set_channel_position(n: int, divs: float) -> str:
    return f"{_ch(n)}:POSITION {divs:g}"


def set_channel_offset(n: int, volts: float) -> str:
    return f"{_ch(n)}:OFFSET {volts:g}"


def set_channel_coupling(n: int, coupling: Literal["DC", "AC", "DCREJECT"]) -> str:
    return f"{_ch(n)}:COUPLING {coupling}"


def set_channel_bandwidth(n: int, hz: float | str) -> str:
    """Bandwidth limit in Hz, or 'FULL'."""
    val = hz if isinstance(hz, str) else f"{hz:g}"
    return f"{_ch(n)}:BANDWIDTH {val}"


def set_channel_termination(n: int, ohms: float) -> str:
    """Input termination — 50 or 1e6 (1 MΩ)."""
    return f"{_ch(n)}:TERMINATION {ohms:g}"


def set_channel_label(n: int, text: str) -> str:
    safe = text.replace('"', "")
    return f'{_ch(n)}:LABEL:NAME "{safe}"'


def set_channel_ext_atten(n: int, ratio: float) -> str:
    return f"{_ch(n)}:PROBEFUNC:EXTATTEN {ratio:g}"


# --- queries ---
def q_channel_display(n: int) -> str:  return f"DISPLAY:GLOBAL:{_ch(n)}:STATE?"
def q_channel_selected(n: int) -> str: return f"SELECT:{_ch(n)}?"
def q_channel_scale(n: int) -> str:    return f"{_ch(n)}:SCALE?"
def q_channel_position(n: int) -> str: return f"{_ch(n)}:POSITION?"
def q_channel_offset(n: int) -> str:   return f"{_ch(n)}:OFFSET?"
def q_channel_coupling(n: int) -> str: return f"{_ch(n)}:COUPLING?"
def q_channel_bandwidth(n: int) -> str: return f"{_ch(n)}:BANDWIDTH?"
def q_channel_termination(n: int) -> str: return f"{_ch(n)}:TERMINATION?"
def q_channel_label(n: int) -> str:    return f"{_ch(n)}:LABEL:NAME?"
def q_channel_ext_atten(n: int) -> str: return f"{_ch(n)}:PROBEFUNC:EXTATTEN?"
def q_channel_probe_type(n: int) -> str: return f"{_ch(n)}:PROBE:ID:TYPE?"


# ---------------------------------------------------------------------------
# Horizontal
# ---------------------------------------------------------------------------
# --- writes ---
def set_horizontal_scale(sec_per_div: float) -> str:
    if sec_per_div <= 0:
        raise ValueError("sec_per_div must be > 0")
    return f"HORIZONTAL:SCALE {sec_per_div:g}"


def set_horizontal_position(percent: float) -> str:
    """Trigger position as a percentage (0..100) of the record."""
    if not 0 <= percent <= 100:
        raise ValueError("position percent must be 0..100")
    return f"HORIZONTAL:POSITION {percent:g}"


def set_horizontal_mode(mode: Literal["AUTO", "MANUAL"]) -> str:
    return f"HORIZONTAL:MODE {mode}"


def set_sample_rate(sa_per_s: float) -> str:
    """Set sample rate (MANUAL mode)."""
    return f"HORIZONTAL:MODE:SAMPLERATE {sa_per_s:g}"


def set_record_length(points: int) -> str:
    return f"HORIZONTAL:MODE:RECORDLENGTH {int(points)}"


# --- queries ---
def q_horizontal_mode() -> str:        return "HORIZONTAL:MODE?"
def q_horizontal_scale() -> str:       return "HORIZONTAL:SCALE?"
def q_horizontal_position() -> str:    return "HORIZONTAL:POSITION?"
def q_sample_rate() -> str:            return "HORIZONTAL:SAMPLERATE?"
def q_record_length() -> str:          return "HORIZONTAL:RECORDLENGTH?"


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------
# --- writes ---
def set_acq_mode(mode: AcqMode) -> str:
    return f"ACQUIRE:MODE {mode}"


def set_acq_numavg(n: int) -> str:
    return f"ACQUIRE:NUMAVG {int(n)}"


def set_acq_state(run: bool) -> str:
    return f"ACQUIRE:STATE {'RUN' if run else 'STOP'}"


def set_acq_stopafter(mode: Literal["RUNSTOP", "SEQUENCE"]) -> str:
    return f"ACQUIRE:STOPAFTER {mode}"


# --- queries ---
def q_acq_mode() -> str:        return "ACQUIRE:MODE?"
def q_acq_numavg() -> str:      return "ACQUIRE:NUMAVG?"
def q_acq_state() -> str:       return "ACQUIRE:STATE?"
def q_acq_stopafter() -> str:   return "ACQUIRE:STOPAFTER?"
def q_acq_numacq() -> str:      return "ACQUIRE:NUMACQ?"


# ---------------------------------------------------------------------------
# Trigger (A event, edge — the common path)
# ---------------------------------------------------------------------------
# --- writes ---
def set_trig_type(t: Literal["EDGE", "WIDTH", "TIMEOUT", "RUNT", "WINDOW", "LOGIC"]) -> str:
    return f"TRIGGER:A:TYPE {t}"


def set_trig_edge_source(source: str) -> str:
    """source e.g. 'CH1', 'CH2', 'LINE', 'AUX'."""
    return f"TRIGGER:A:EDGE:SOURCE {source}"


def set_trig_edge_slope(slope: Slope) -> str:
    return f"TRIGGER:A:EDGE:SLOPE {slope}"


def set_trig_edge_coupling(coupling: Coupling) -> str:
    return f"TRIGGER:A:EDGE:COUPLING {coupling}"


def set_trig_level(n: int, volts: float) -> str:
    return f"TRIGGER:A:LEVEL:{_ch(n)} {volts:g}"


def set_trig_mode(mode: TrigMode) -> str:
    return f"TRIGGER:A:MODE {mode}"


def trig_force() -> str:
    return "TRIGGER FORCE"


# --- queries ---
def q_trig_type() -> str:           return "TRIGGER:A:TYPE?"
def q_trig_edge_source() -> str:    return "TRIGGER:A:EDGE:SOURCE?"
def q_trig_edge_slope() -> str:     return "TRIGGER:A:EDGE:SLOPE?"
def q_trig_edge_coupling() -> str:  return "TRIGGER:A:EDGE:COUPLING?"
def q_trig_level(n: int) -> str:    return f"TRIGGER:A:LEVEL:{_ch(n)}?"
def q_trig_mode() -> str:           return "TRIGGER:A:MODE?"
def q_trig_state() -> str:          return "TRIGGER:STATE?"


# ---------------------------------------------------------------------------
# Automatic measurements
# ---------------------------------------------------------------------------
# --- writes ---
def meas_add(meas_type: str) -> str:
    """Add a new measurement of the given type. SET-ONLY — never query this."""
    return f"MEASUREMENT:ADDMEAS {meas_type}"


def meas_delete(n: int) -> str:
    return f"MEASUREMENT:DELETE \"MEAS{int(n)}\""


def meas_set_source(n: int, source: str) -> str:
    return f"MEASUREMENT:MEAS{int(n)}:SOURCE {source}"


def meas_set_type(n: int, meas_type: str) -> str:
    return f"MEASUREMENT:MEAS{int(n)}:TYPE {meas_type}"


# --- queries ---
def q_meas_list() -> str:           return "MEASUREMENT:LIST?"
def q_meas_type(n: int) -> str:     return f"MEASUREMENT:MEAS{int(n)}:TYPE?"
def q_meas_source(n: int) -> str:   return f"MEASUREMENT:MEAS{int(n)}:SOURCE?"


def q_meas_value(n: int) -> str:
    """Current-acquisition mean = the 'live' measurement value.

    Verified working on FV:1.34.8.1078. Do NOT use MEAS<n>:UNITS? (wedges
    the bus) — derive the unit from the type via config.MEAS_UNIT.
    """
    return f"MEASUREMENT:MEAS{int(n)}:RESULTS:CURRENTACQ:MEAN?"


def q_meas_mean(n: int) -> str:  return f"MEASUREMENT:MEAS{int(n)}:RESULTS:ALLACQS:MEAN?"
def q_meas_min(n: int) -> str:   return f"MEASUREMENT:MEAS{int(n)}:RESULTS:ALLACQS:MINIMUM?"
def q_meas_max(n: int) -> str:   return f"MEASUREMENT:MEAS{int(n)}:RESULTS:ALLACQS:MAXIMUM?"
def q_meas_stddev(n: int) -> str: return f"MEASUREMENT:MEAS{int(n)}:RESULTS:ALLACQS:STDDEV?"
def q_meas_pop(n: int) -> str:   return f"MEASUREMENT:MEAS{int(n)}:RESULTS:ALLACQS:POPULATION?"


# ---------------------------------------------------------------------------
# Waveform transfer (CURVE)
# ---------------------------------------------------------------------------
# --- writes ---
def set_data_source(source: str) -> str:
    return f"DATA:SOURCE {source}"


def set_data_encoding(enc: Encoding) -> str:
    return f"DATA:ENCDG {enc}"


def set_data_width(bytes_per_point: int) -> str:
    return f"DATA:WIDTH {int(bytes_per_point)}"


def set_data_start(point: int) -> str:
    return f"DATA:START {int(point)}"


def set_data_stop(point: int) -> str:
    return f"DATA:STOP {int(point)}"


# --- queries ---
def q_data_source() -> str:     return "DATA:SOURCE?"
def q_data_encoding() -> str:   return "DATA:ENCDG?"
def curve() -> str:             return "CURVE?"

# Waveform preamble (scaling) — all verified queries.
def q_wfm_npoints() -> str: return "WFMOUTPRE:NR_PT?"
def q_wfm_xincr() -> str:   return "WFMOUTPRE:XINCR?"
def q_wfm_xzero() -> str:   return "WFMOUTPRE:XZERO?"
def q_wfm_ymult() -> str:   return "WFMOUTPRE:YMULT?"
def q_wfm_yzero() -> str:   return "WFMOUTPRE:YZERO?"
def q_wfm_yoff() -> str:    return "WFMOUTPRE:YOFF?"
def q_wfm_xunit() -> str:   return "WFMOUTPRE:XUNIT?"
def q_wfm_yunit() -> str:   return "WFMOUTPRE:YUNIT?"


# ---------------------------------------------------------------------------
# DVM (option SUP5-DVM — present on the bench unit)
# ---------------------------------------------------------------------------
# --- writes ---
def set_dvm_source(source: str) -> str:
    return f"DVM:SOURCE {source}"


def set_dvm_mode(mode: Literal["OFF", "DC", "ACRMS", "ACDCRMS", "FREQUENCY"]) -> str:
    return f"DVM:MODE {mode}"


# --- queries ---
def q_dvm_mode() -> str:        return "DVM:MODE?"
def q_dvm_source() -> str:      return "DVM:SOURCE?"
def q_dvm_value() -> str:       return "DVM:MEASUREMENT:VALUE?"


# ---------------------------------------------------------------------------
# Screenshot / filesystem
# ---------------------------------------------------------------------------
# --- writes ---
def save_image(instrument_path: str) -> str:
    """Save a screenshot to a path on the instrument's own filesystem."""
    return f'SAVE:IMAGE "{instrument_path}"'


def fs_delete(instrument_path: str) -> str:
    return f'FILESYSTEM:DELETE "{instrument_path}"'


def fs_readfile(instrument_path: str) -> str:
    """Stream a file's bytes back. Response is a raw binary block (no `?`
    form of its own — this IS the query the instrument answers with data)."""
    return f'FILESYSTEM:READFILE "{instrument_path}"'
