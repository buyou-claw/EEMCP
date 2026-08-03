# mcp_MSO54 — Notes from the bench

Lessons learned building this package. Read before changing anything
that touches the bus.

> **Looking for measurement how-to (ripple, noise voltage, etc.)?**
> See [`MEASUREMENT_GUIDE.md`](./MEASUREMENT_GUIDE.md) — this file is
> about the bus protocol; that file is about what to set, why, and
> what to expect back.

---

## §1 USBTMC bulk-IN lockup — three triggers, one recovery

**Status:** confirmed on bench MSO54, FW 1.34.8.1078, on 2026-07-18.
Cost: two scope reboots and one USB-port reset in a single afternoon.

Once wedged: writes still go out, but every read returns `VI_ERROR_IO`
(or `VI_ERROR_TMO` once the queue fills up). `viClear`, `inst.clear()`,
buffer flush, and physical USB replug all FAILED to recover until
the bulk-IN endpoint itself was reset (see recovery below).

### Trigger #1 — `?` on a set-only command

`MEASUREMENT:ADDMEAS?` halts USBTMC bulk-IN. `ADDMEAS` is action-only;
appending `?` produces no valid response on the wire and the firmware
enters an invalid state. The hard guard in `instrument.MSO54.query()`
(refuses any string not ending in `?`) prevents this — **do not
remove that guard**.

### Trigger #2 — `?` on a firmware-unsupported query

`MEASUREMENT:MEAS<n>:UNITS?` does NOT answer on this firmware (times
out). Worse: a partial wedge where STB goes `0x10 → 0x0`. The unit
is derived from the measurement type via `config.MEAS_UNIT` instead —
**do not "fix" this by adding the query back**.

### Trigger #3 — long binary READFILE with `read_termination='\n'`

`FILESYSTEM:READFILE` returns an IEEE 488.2 arbitrary-block payload
(`#N<digits><bytes>`). PNG data has no `\n`, so `read_raw()` blocks
until the 30 s timeout — and the timeout itself wedges USBTMC. This
was reproduced THREE times in a row with progressively better parsers
(raw read, then explicit `#N...` header parser). Both wedged.

**The path is unsafe on this firmware over USB.** `read_file_bytes()`
in `instrument.py` is intentionally disabled (raises
`MSO54Error`). To re-enable: move to the LAN socket transport
(`TCPIP0::...::4000::SOCKET`) and verify the path is safe there
first. Do NOT re-enable on USB.

### Trigger #4 — `WFMOUTPRE:*` queries on display=OFF channels

`WFMOUTPRE:NR_PT?`, `WFMOUTPRE:YMULT?`, etc. all return *something* for
display=ON channels but **silently hang on display=OFF channels**
(because there's no waveform to scale). A query like
`DATA:SOURCE CH2; :WFMOUTPRE:NR_PT?` will sit there until the
per-query timeout (10 s default) — and in our experience that timeout
is enough to leave the bulk-IN pipe in a partially-wedged state where
text SCPI queries still work but the next binary CURVE? read fails
with `VI_ERROR_IO`. Recovered only via USB-port reset.

**Workaround:** before sending any `WFMOUTPRE:*` query or `CURVE?`,
check `CH<n>:DISPLAY?` first and skip OFF channels. `scripts/diag_capture.py`
demonstrates the symptom; `scripts/generate_report.py` now filters
OFF channels out by default (see `--no-skip-off-channels` to override).

### Recovery — USB port reset, not scope reboot

1. Unplug and replug the USB cable (or disable+re-enable in Device
   Manager — right-click the Tektronix USB device → Disable, then
   Enable).
2. Wait ~2 s for NI-VISA to re-enumerate.
3. Open a fresh pyvisa session — `*IDN?` works again.

This worked on 2026-07-18 after the third wedge. Only fall back to a
scope power-cycle if USB-port reset fails. The recovery is also saved
in memory as `feedback_usbtmc_setonly_query.md`.

---

## §2 MSO54 auto-scale override (FW 1.34.8.1078)

**Symptom:** `set_channel_scale()` succeeds in the sense that no error
is returned, but the next `q_channel_scale()` reports the original
(signal-derived) value. The user's setting was silently overwritten.

**Cause:** while `ACQUIRE:STATE` is RUN, the scope continuously
auto-adjusts vertical scale to fit the waveform. The auto loop runs
in firmware and has higher priority than a single SCPI write.

**Workaround (verified):** STOP acquisition before writing scale.

```python
scope.set_acq_state(False)   # STOP
scope.set_channel_scale(1, 1.0)   # write persists
scope.set_acq_state(True)    # RUN (scale stays at 1.0 until next auto event)
```

`scope_set_channel_scale()` in `server.py` does NOT do this
automatically — the operator must call `scope_acq_stop()` first
because changing acquisition state is itself a destructive op.

**Side note:** turning the channel OFF with `set_channel_display(n, False)`
also worked in isolation but did NOT survive a `*RST` cycle, so STOP
acquisition is the robust workaround.

---

## §3 Filesystem path conventions (PARTIALLY resolved)

The Tek 5 Series MSO programmer guide does not document filesystem
paths cleanly, but **the `/tmp/` prefix is mapped to the scope's
Windows-style temp directory** (e.g. `C:/Users/Lenovo/AppData/Local/Temp/`
on the bench unit). This was confirmed by `SAVE:IMAGE /tmp/test.png`
on 2026-07-18: the scope silently translated it to the Windows path
and wrote the PNG successfully. The MCP server's `scope_save_image`
still requires the operator to pull the file off the scope manually
because `FILESYSTEM:READFILE` is the unsafe path (see §1), not
`SAVE:IMAGE`.

If a screenshot read-back tool is added in the future:
- The path translation is handled by the scope, so `SAVE:IMAGE /tmp/x.png`
  Just Works on this firmware.
- `FILESYSTEM:READFILE` remains unsafe on USBTMC. To retrieve bytes
  programmatically, use the LAN socket transport (`TCPIP0::...::4000::SOCKET`)
  and re-verify the path is safe there.

---

## §4 DVM probe discipline

`DVM:MEASUREMENT:VALUE?` returns the **instantaneous** DVM reading
(0.001–0.01 s integration), not a one-shot triggered value. For
long-period measurements, prefer adding an automatic measurement
(`MEASUREMENT:ADDMEAS`) and reading its
`MEAS<n>:RESULTS:ALLACQS:MEAN?` instead.

---

## §5 `HEADER OFF` is mandatory

`connect()` sends `HEADER OFF` so query replies come back as bare
values (`TEKTRONIX,MSO54,...`) instead of `"*IDN TEKTRONIX,MSO54,..."`.
Without it, every `query_float()` would explode on the leading `"*IDN`
prefix. Do not remove the `header_off()` call from `connect()`.

---

## §6 Probe-only commands (verified safe to query)

These queries were probed during P2 / validation and confirmed to
answer on this firmware. Adding them to `scpi.py` is safe; just be
aware the list below is the proven set on this firmware, not a
universal "all MSO54 queries" list.

```
*IDN?, *OPT?, *OPC?, ALLEV?, EVENT?,
CONFIGURATION:ANALOG:BANDWIDTH?,
DISPLAY:GLOBAL:CH<n>:STATE?, SELECT:CH<n>?,
CH<n>:SCALE?, CH<n>:POSITION?, CH<n>:OFFSET?,
CH<n>:COUPLING?, CH<n>:BANDWIDTH?, CH<n>:TERMINATION?,
CH<n>:LABEL:NAME?, CH<n>:PROBEFUNC:EXTATTEN?, CH<n>:PROBE:ID:TYPE?,
HORIZONTAL:MODE?, HORIZONTAL:SCALE?, HORIZONTAL:POSITION?,
HORIZONTAL:SAMPLERATE?, HORIZONTAL:RECORDLENGTH?,
ACQUIRE:MODE?, ACQUIRE:NUMAVG?, ACQUIRE:STATE?,
ACQUIRE:STOPAFTER?, ACQUIRE:NUMACQ?,
TRIGGER:A:TYPE?, TRIGGER:A:EDGE:SOURCE?, TRIGGER:A:EDGE:SLOPE?,
TRIGGER:A:EDGE:COUPLING?, TRIGGER:A:LEVEL:CH<n>?, TRIGGER:A:MODE?,
TRIGGER:STATE?,
MEASUREMENT:LIST?, MEASUREMENT:MEAS<n>:TYPE?, MEASUREMENT:MEAS<n>:SOURCE?,
MEASUREMENT:MEAS<n>:RESULTS:CURRENTACQ:MEAN?,
MEASUREMENT:MEAS<n>:RESULTS:ALLACQS:MEAN?,
MEASUREMENT:MEAS<n>:RESULTS:ALLACQS:MINIMUM?,
MEASUREMENT:MEAS<n>:RESULTS:ALLACQS:MAXIMUM?,
MEASUREMENT:MEAS<n>:RESULTS:ALLACQS:STDDEV?,
MEASUREMENT:MEAS<n>:RESULTS:ALLACQS:POPULATION?,
DATA:SOURCE?, DATA:ENCDG?,
WFMOUTPRE:NR_PT?, WFMOUTPRE:XINCR?, WFMOUTPRE:XZERO?,
WFMOUTPRE:YMULT?, WFMOUTPRE:YZERO?, WFMOUTPRE:YOFF?,
WFMOUTPRE:XUNIT?, WFMOUTPRE:YUNIT?,
CURVE?,
DVM:MODE?, DVM:SOURCE?, DVM:MEASUREMENT:VALUE?
```

### Known-UNSAFE queries (do NOT add)

- `MEASUREMENT:ADDMEAS?` — set-only, halts USBTMC bulk-IN
- `MEASUREMENT:MEAS<n>:UNITS?` — times out, partial wedge
- `FILESYSTEM:READFILE` with binary data + `read_termination='\n'` — halts USBTMC bulk-IN
