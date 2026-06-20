"""JS220 instrument handle — `joulescope` Python API wrapper.

A thin layer that owns the `Device` instance and serializes all
configuration + statistics work behind a small surface. The MCP
tools (server.py) talk to this class only — they never call
`joulescope.scan()` or `parameter_set()` directly.

Key responsibilities:
  - connect / disconnect lifecycle (USB hot-plug, partial state)
  - configuration (parameter_set with validation via scpi.py)
  - statistics stream collection (background callbacks collect into
    a list at `reduction_frequency` Hz)
  - one-shot capture (turn source on, sleep, average the stats, turn
    source off)
  - state-machine enforcement (no start() unless IDLE, etc.)

Threading note: the joulescope driver delivers statistics callbacks
on a USB processing thread. We protect the stats list with a
`threading.Lock`. All other driver state is single-threaded.
"""
from __future__ import annotations
import time
import logging
import threading
from typing import Optional, Callable, Any, TYPE_CHECKING

# The `joulescope` driver is Windows-only (Cython backend). Importing
# it eagerly at module load pulls in the Cython extension even on
# machines where the bench is not plugged in. We defer the import
# until the first JS220 method that actually needs the driver.
# Type-only imports stay in TYPE_CHECKING so the rest of the module
# type-checks cleanly without paying the import cost.
if TYPE_CHECKING:
    from joulescope.v1.device import Device  # noqa: F401

from . import scpi
from .config import CONFIG
from .state_machine import State, StateMachine, StateError

log = logging.getLogger(__name__)


class JS220Error(RuntimeError):
    pass


def _joulescope_scan():
    """Lazy import of `joulescope.scan` — pulls in the Cython driver
    on first call only. Imported here (and not at module top) so a
    clean `import mcp_js220.server` does not require joulescope to
    be installed (e.g. CI / verification scripts)."""
    from joulescope import scan
    return scan


class JS220:
    """Owns one open session to a single JS220."""

    def __init__(self):
        self._device: Optional["Device"] = None
        self._idn_info: Optional[dict] = None
        self._sm = StateMachine()
        # Statistics collection state
        self._stats_lock = threading.Lock()
        self._stats: list[dict] = []
        self._stats_callback_id = 0
        self._last_streaming_summary: Optional[dict] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._device is not None and self._device.is_open

    @property
    def streaming(self) -> bool:
        return self._sm.is_(State.STREAMING)

    @property
    def state(self) -> State:
        return self._sm.state

    @property
    def idn_info(self) -> Optional[dict]:
        return self._idn_info

    def list_devices(self) -> list[str]:
        """Return the list of device_path strings scan() sees right now."""
        scan = _joulescope_scan()
        return [d.device_path for d in scan()]

    def connect(self, device_path: Optional[str] = None) -> dict:
        """Open the first matching device (or all of them if no path given).

        Args:
            device_path: optional case-insensitive substring match against
                the device's path (e.g. "005742") or full path
                (e.g. "u/js220/005742"). None = use scan()[0].

        Returns:
            idn info dict with model, serial, firmware versions, and the
            device_path that was actually selected.
        """
        if self.connected:
            # Idempotent
            return self._idn_info or {}

        scan = _joulescope_scan()
        devices = scan()
        if not devices:
            raise JS220Error("no JS220 devices found (USB not connected?)")

        target = None
        if device_path:
            needle = device_path.lower()
            for d in devices:
                if needle in d.device_path.lower() or needle in str(d).lower():
                    target = d
                    break
            if target is None:
                raise JS220Error(
                    f"no device matched {device_path!r}; "
                    f"available: {[d.device_path for d in devices]}"
                )
        else:
            target = devices[0]

        log.info("Opening JS220: %s", target)
        # If 'defaults' not specified the device will try to preserve state.
        target.open(mode="defaults")
        self._device = target
        self._idn_info = self._device.info() or {}
        self._sm.transition(State.IDLE)
        log.info("Connected: %s", self._idn_info)
        return {
            "device_path": self._device.device_path,
            "idn": self._idn_info,
        }

    def disconnect(self) -> None:
        """Close the device. Safe to call from any state (force-stops first)."""
        if self._device is None:
            self._sm.transition(State.DISCONNECTED)
            return
        # Stop streaming and unregister callbacks first so the
        # USB processing thread doesn't try to touch a half-closed
        # device on the way out.
        try:
            if self._sm.is_(State.STREAMING):
                try:
                    self._device.stop()
                except Exception:
                    pass
            if self._stats:
                self._unregister_stats_callback()
        except Exception as e:
            log.warning("cleanup on disconnect raised: %s", e)
        try:
            self._device.close()
        except Exception as e:
            log.warning("close() raised: %s", e)
        self._device = None
        self._idn_info = None
        self._sm.transition(State.DISCONNECTED)

    def reset(self) -> None:
        """Disconnect + reconnect. Clears any error state."""
        was_connected = self.connected
        path = self._device.device_path if was_connected and self._device else None
        if was_connected:
            self.disconnect()
        # The 'mode="defaults"' on open() already re-asserts factory
        # settings. A full USB cycle isn't necessary for soft reset.
        if path:
            self.connect(path)
        else:
            self.connect()
        # connect() already transitioned DISCONNECTED -> IDLE; the
        # state machine forbids IDLE -> IDLE so we don't transition
        # again here. If we were already in ERROR, force a clean slate.

    # ------------------------------------------------------------------
    # Configuration — wraps parameter_set with validation
    # ------------------------------------------------------------------
    def _check_connected(self) -> None:
        if not self.connected:
            raise JS220Error("not connected; call js220_connect first")

    def set_source(self, value: str) -> str:
        self._check_connected()
        v = scpi.set_source(value)
        self._device.parameter_set("source", v)
        return v

    def set_current_range(self, value: str) -> str:
        self._check_connected()
        v = scpi.set_current_range(value)
        self._device.parameter_set("i_range", v)
        return v

    def set_sampling_frequency(self, value: str) -> str:
        self._check_connected()
        v = scpi.set_sampling_frequency(value)
        self._device.parameter_set("sampling_frequency", v)
        return v

    def set_reduction_frequency(self, value: str) -> str:
        self._check_connected()
        v = scpi.set_reduction_frequency(value)
        self._device.parameter_set("reduction_frequency", v)
        return v

    def set_io_voltage(self, value: str) -> str:
        self._check_connected()
        v = scpi.set_io_voltage(value)
        self._device.parameter_set("io_voltage", v)
        return v

    def set_sensor_power(self, state: str) -> str:
        self._check_connected()
        v = scpi.set_sensor_power(state)
        self._device.parameter_set("sensor_power", v)
        return v

    def clear_accumulators(self) -> None:
        """Reset the charge/energy counters (driver-side, sensor-side)."""
        self._check_connected()
        self._device.statistics_accumulators_clear()

    def parameter_get(self, name: str) -> Any:
        self._check_connected()
        return self._device.parameter_get(name)

    # ------------------------------------------------------------------
    # Streaming + statistics collection
    # ------------------------------------------------------------------
    def _on_stats(self, data: dict) -> None:
        with self._stats_lock:
            self._stats.append(data)
            # The driver delivers ~reduction_frequency Hz; 10k samples
            # is enough for ~17 minutes at 10 Hz before we cap. Beyond
            # that the user should use js220_run_power_profile which
            # writes to disk.
            if len(self._stats) > 10_000:
                # Drop oldest so we don't OOM
                self._stats = self._stats[-10_000:]

    def _register_stats_callback(self) -> None:
        if self._stats_callback_id == 0:
            self._device.statistics_callback_register(self._on_stats)
            self._stats_callback_id = 1

    def _unregister_stats_callback(self) -> None:
        if self._stats_callback_id != 0:
            try:
                self._device.statistics_callback_unregister(self._on_stats)
            except Exception:
                pass
            self._stats_callback_id = 0

    def start(self) -> None:
        """Turn the sensor on, clear accumulators, register stats cbk, start()."""
        self._check_connected()
        self._sm.require(State.IDLE)
        # Clear accumulators so a fresh capture starts at zero
        self._device.statistics_accumulators_clear()
        with self._stats_lock:
            self._stats = []
        # Configure defaults the user may have forgotten to set
        if self._device.parameter_get("sampling_frequency") != CONFIG.default_sampling_frequency:
            self._device.parameter_set("sampling_frequency", CONFIG.default_sampling_frequency)
        if self._device.parameter_get("reduction_frequency") != CONFIG.default_reduction_frequency:
            self._device.parameter_set("reduction_frequency", CONFIG.default_reduction_frequency)
        if self._device.parameter_get("i_range") != CONFIG.default_current_range:
            self._device.parameter_set("i_range", CONFIG.default_current_range)
        # Register callback BEFORE start so we don't lose the first tick
        self._register_stats_callback()
        # Turn on
        self._device.parameter_set("source", scpi.source_on())
        # Start streaming
        self._device.start()
        self._sm.transition(State.STREAMING)
        # Warmup: discard the first ~0.5 s of stats to let ranges settle
        warmup_until = time.monotonic() + CONFIG.stream_warmup_s
        while time.monotonic() < warmup_until:
            time.sleep(0.05)
        with self._stats_lock:
            # Drop warmup samples so the user's first read is post-settle
            n = len(self._stats)
            keep_from = max(0, n - 50)  # keep at most last 50 warmup samples
            if keep_from:
                self._stats = self._stats[keep_from:]

    def stop(self) -> None:
        """Turn the sensor off and stop streaming. Safe to call from IDLE too."""
        if self._device is None:
            return
        if self._sm.is_(State.STREAMING):
            try:
                self._device.stop()
            except Exception as e:
                log.warning("stop() raised: %s", e)
            try:
                self._device.parameter_set("source", scpi.source_off())
            except Exception as e:
                log.warning("source off raised: %s", e)
            self._sm.transition(State.IDLE)
        self._unregister_stats_callback()

    def collect_for(self, duration_s: float) -> list[dict]:
        """Run streaming for `duration_s` and return the captured stats list.

        This is a blocking helper used by the one-shot measure tools
        and `js220_capture_window`. It manages start/stop internally
        so callers don't have to.
        """
        if duration_s <= 0:
            raise ValueError(f"duration_s must be > 0, got {duration_s}")
        if not self.connected:
            raise JS220Error("not connected")
        if self._sm.is_(State.STREAMING):
            # If the user already started, just collect for the window
            start_time = time.monotonic()
        else:
            self.start()
            start_time = time.monotonic()
        # Sleep for the requested duration
        end_time = start_time + duration_s
        while time.monotonic() < end_time:
            time.sleep(min(0.05, end_time - time.monotonic()))
        # Capture a snapshot of the stats list
        with self._stats_lock:
            snapshot = list(self._stats)
        # If we started the stream, stop it
        if self._sm.is_(State.STREAMING) and not self.streaming_was_user_started():
            self.stop()
        return snapshot

    def streaming_was_user_started(self) -> bool:
        """Helper for collect_for — see server.py for the user-managed case."""
        return False  # collect_for always starts/stops itself

    def get_stats_snapshot(self) -> list[dict]:
        """Return a copy of the current stats buffer without disturbing streaming."""
        with self._stats_lock:
            return list(self._stats)

    def clear_stats_buffer(self) -> None:
        with self._stats_lock:
            self._stats = []
