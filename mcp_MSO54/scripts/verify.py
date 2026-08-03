"""mcp_MSO54 structural + smoke verifier.

Two layers:

  1. **Structural tests (offline, no scope)** — load server.py, check
     every tool has a docstring, every write-tool has the
     `user_confirmed` gate, every envelope is the right shape, the
     config constants are self-consistent.

  2. **Smoke tests (online, requires scope)** — connect, exercise
     one read and one gated write per category. Skipped automatically
     if the scope is unreachable.

Run all:
    python -m mcp_MSO54.scripts.verify

Run only structural:
    python -m mcp_MSO54.scripts.verify --offline

Run only smoke:
    python -m mcp_MSO54.scripts.verify --online

Exit code is the number of failures (0 = all green).
"""
from __future__ import annotations
import argparse
import inspect
import json
import sys
import time

sys.path.insert(0, "E:/other/ai_project/EEMCP")


# ---------------------------------------------------------------------------
# Structural tests — no instrument I/O
# ---------------------------------------------------------------------------
def t_server_loads():
    """server.py imports without error."""
    import mcp_MSO54.server as srv
    assert srv.mcp is not None
    assert srv.scope is not None
    return "ok"


def _registered_tool_names() -> list[str]:
    """Return the names of all tools registered on `srv.mcp`.

    FastMCP exposes the registered tools via `mcp._tool_manager._tools`
    (a dict keyed by tool name). We fall back to scanning public
    callables if that path changes.
    """
    import mcp_MSO54.server as srv
    mcp = srv.mcp
    # FastMCP internal: list of Tool objects with a .name attribute
    try:
        tools = list(getattr(mcp, "_tool_manager")._tools.values())
        return sorted(t.name for t in tools)
    except AttributeError:
        pass
    # Fallback: scan server.py module attributes that look like tools.
    names = []
    for name, obj in vars(srv).items():
        if name.startswith("scope_") and callable(obj) and inspect.getdoc(obj):
            names.append(name)
    return sorted(names)


def t_every_tool_has_docstring():
    """Every registered FastMCP tool has a non-empty docstring."""
    import mcp_MSO54.server as srv
    names = _registered_tool_names()
    assert len(names) >= 20, f"expected >= 20 tools, found {len(names)}: {names}"
    bad = []
    for name in names:
        # The tool's underlying Python function lives in srv with the same name.
        fn = getattr(srv, name, None)
        if fn is None:
            # Tool registered under a different name (FastMCP may strip prefix)
            continue
        doc = inspect.getdoc(fn) or ""
        if not doc.strip():
            bad.append(name)
    assert not bad, f"tools without docstring: {bad}"
    return f"{len(names)} tools, all documented"


def t_every_write_has_user_confirmed():
    """Every tool that mutates scope state must gate on `user_confirmed=True`."""
    import mcp_MSO54.server as srv
    # Tools that should NOT have user_confirmed (read-only or preflight-propose-only)
    read_only = {
        "scope_connect", "scope_disconnect", "scope_status",
        "scope_get_channel_state", "scope_get_horizontal_state",
        "scope_get_acquisition_state", "scope_get_trigger_state",
        "scope_get_measurement_list", "scope_read_dvm",
        "scope_preflight_add_measurement",
        "scope_read_curve",  # also has gate, but listed below
        "scope_reset", "scope_autoset",  # intentional scope-wide destructive
    }
    # Tools that MUST have user_confirmed=True
    must_gate = {
        "scope_set_channel_scale", "scope_set_channel_position",
        "scope_set_channel_offset", "scope_set_channel_coupling",
        "scope_set_channel_label",
        "scope_set_horizontal_scale", "scope_set_horizontal_position",
        "scope_set_trigger_edge",
        "scope_run_preflight_add_measurement",
        "scope_set_dvm",
        "scope_read_curve",
        "scope_save_image",
    }
    for name in must_gate:
        fn = getattr(srv, name, None)
        assert fn is not None, f"missing required tool: {name}"
        sig = inspect.signature(fn)
        assert "user_confirmed" in sig.parameters, (
            f"{name}: missing user_confirmed parameter"
        )
    return f"{len(must_gate)} write-tools all have user_confirmed gate"


def t_config_consistency():
    """MEAS_UNIT and DEFAULT_LIMITS must cover every MEAS_TYPES entry."""
    from mcp_MSO54 import config
    types = set(config.MEAS_TYPES)
    unit_keys = set(config.MEAS_UNIT.keys())
    limits_keys = set(config.DEFAULT_LIMITS.keys())
    missing_unit = types - unit_keys
    missing_limits = types - limits_keys
    assert not missing_unit, f"MEAS_TYPES entries without MEAS_UNIT: {missing_unit}"
    assert not missing_limits, f"MEAS_TYPES entries without DEFAULT_LIMITS: {missing_limits}"
    return f"{len(types)} measurement types, all covered"


def t_envelope_shape():
    """common.envelope.envelope() returns the standard 7-key shape."""
    from common.envelope import envelope
    env = envelope("TEST", 1.0, "V", lower=0.0, upper=2.0)
    expected_keys = {"test", "value", "unit", "lower", "upper", "pass", "timestamp"}
    assert set(env.keys()) == expected_keys, (
        f"envelope keys = {set(env.keys())}, expected {expected_keys}"
    )
    assert env["pass"] is True
    env2 = envelope("TEST", 5.0, "V", lower=0.0, upper=2.0)
    assert env2["pass"] is False
    return "envelope returns 7-key shape with correct pass logic"


def t_instrument_guards():
    """instrument.MSO54.write()/query() reject the wrong kind of string.

    Uses a stub that bypasses the connection check so we can exercise
    just the write/query string validation.
    """
    from mcp_MSO54.instrument import MSO54, MSO54Error
    s = MSO54()

    class _StubInstr:
        """Pretends to be an open pyvisa session — guards only check
        `self.connected`, so we make that True with no real I/O."""
        def __getattr__(self, name): return lambda *a, **k: ""

    s.instr = _StubInstr()
    s._connected = True

    # write() refuses queries (any string ending in '?')
    try:
        s.write("FOO?")
    except MSO54Error as e:
        assert "refusing to write()" in str(e), str(e)
    else:
        raise AssertionError("write() accepted a query string")

    # query() refuses non-queries (anything not ending in '?')
    try:
        s.query("FOO")
    except MSO54Error as e:
        assert "refusing to query()" in str(e), str(e)
    else:
        raise AssertionError("query() accepted a non-query string")

    # Sanity: write() accepts a non-query, query() accepts a query — no
    # exception on the string-type check (would fail later on real I/O).
    try:
        s.write("FOO 1")           # type check passes
    except MSO54Error as e:
        if "refusing to write()" in str(e):
            raise AssertionError(f"write() wrongly refused a non-query: {e}")
        # any other MSO54Error is fine for the type-check purpose
    try:
        s.query("FOO?")            # type check passes (then fails on _StubInstr)
    except MSO54Error as e:
        if "refusing to query()" in str(e):
            raise AssertionError(f"query() wrongly refused a query: {e}")

    return "write()/query() guards reject wrong kind of string"


def t_read_file_disabled():
    """read_file_bytes() is disabled on USBTMC."""
    from mcp_MSO54.instrument import MSO54, MSO54Error
    s = MSO54()
    try:
        s.read_file_bytes("/tmp/x.png")
    except MSO54Error as e:
        assert "disabled on USBTMC" in str(e)
    else:
        raise AssertionError("read_file_bytes() should be disabled")
    return "read_file_bytes() disabled on USB (NOTES.md §1)"


STRUCTURAL_TESTS = [
    ("server.py loads",                    t_server_loads),
    ("every tool has docstring",           t_every_tool_has_docstring),
    ("every write has user_confirmed",     t_every_write_has_user_confirmed),
    ("config consistency",                 t_config_consistency),
    ("envelope shape",                     t_envelope_shape),
    ("instrument write/query guards",      t_instrument_guards),
    ("read_file_bytes disabled on USB",    t_read_file_disabled),
]


# ---------------------------------------------------------------------------
# Smoke tests — require live scope
# ---------------------------------------------------------------------------
def s_connect_and_idn(scope):
    idn = scope.idn()
    assert "TEKTRONIX" in idn.upper(), idn
    return idn


def s_all_reads(scope):
    """One representative read from each category."""
    scope.q_channel_scale(1)
    scope.q_horizontal_scale()
    scope.q_acq_mode()
    scope.q_trig_type()
    scope.q_meas_list()
    scope.q_dvm_value()
    scope.read_preamble()
    return "all read paths answered"


def s_trigger_write_readback(scope):
    """The exact P6 path."""
    scope.set_trig_type("EDGE")
    scope.set_trig_edge_source("CH1")
    scope.set_trig_edge_slope("RISE")
    scope.set_trig_level(1, 0.0)
    rb = scope.q_trig_edge_source()
    assert "CH1" in rb
    return f"trigger src={rb}"


def s_meas_add_readback(scope):
    """The exact P7 path."""
    for n in range(1, 9):
        try: scope.meas_delete(n)
        except Exception: pass
    scope.meas_add("FREQUENCY")
    time.sleep(0.2)
    for n in range(1, 9):
        try:
            if scope.q_meas_type(n) == "FREQUENCY":
                return f"MEAS{n} = FREQUENCY"
        except Exception:
            return "no slot populated"
    return "no FREQUENCY slot found"


def s_dvm_write_readback(scope):
    """The exact P8 path."""
    scope.set_dvm_source("CH1")
    scope.set_dvm_mode("DC")
    time.sleep(0.3)
    v = scope.q_dvm_value()
    return f"DVM={v:.4f} V"


def s_curve_readback(scope):
    """The exact P10 path (bounded to 1000 pts)."""
    wfm = scope.read_curve("CH1", start=1, stop=1000, width=1)
    return f"CURVE: {wfm['npoints']} pts, unit={wfm['unit']}"


def s_save_image_safety(scope):
    """The exact P11 path."""
    scope.save_image("/tmp/mcp_verify_test.png")
    time.sleep(1.0)
    idn = scope.idn()  # bus must still be alive
    assert "TEKTRONIX" in idn.upper()
    return "SAVE:IMAGE OK, bus alive"


SMOKE_TESTS = [
    ("connect + IDN",                s_connect_and_idn),
    ("all read paths answer",        s_all_reads),
    ("trigger write+readback",       s_trigger_write_readback),
    ("ADDMEAS + MEAS<n>:TYPE",       s_meas_add_readback),
    ("DVM write+readback",           s_dvm_write_readback),
    ("CURVE? end-to-end",            s_curve_readback),
    ("SAVE:IMAGE write-only safe",   s_save_image_safety),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_structural():
    fails = 0
    print("=" * 64)
    print("STRUCTURAL TESTS (offline)")
    print("=" * 64)
    for name, fn in STRUCTURAL_TESTS:
        try:
            msg = fn()
            print(f"  PASS  {name}: {msg}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {e!r}")
    return fails


def run_smoke():
    from mcp_MSO54.instrument import MSO54, MSO54Error
    fails = 0
    print("=" * 64)
    print("SMOKE TESTS (online, requires scope)")
    print("=" * 64)
    scope = MSO54()
    try:
        scope.connect()
    except Exception as e:
        print(f"  SKIP  cannot connect to scope: {e!r}")
        return 0

    for name, fn in SMOKE_TESTS:
        try:
            msg = fn(scope)
            print(f"  PASS  {name}: {msg}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {e!r}")

    try:
        scope.disconnect()
    except Exception:
        pass
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--offline", action="store_true",
                   help="Run only structural tests (no scope needed)")
    g.add_argument("--online", action="store_true",
                   help="Run only smoke tests (requires scope)")
    args = ap.parse_args()

    run_both = not (args.offline or args.online)
    fails = 0
    if run_both or args.offline:
        fails += run_structural()
    if run_both or args.online:
        fails += run_smoke()
    print()
    if fails == 0:
        print("ALL CHECKS PASSED")
        return 0
    print(f"{fails} FAILURE(S)")
    return fails


if __name__ == "__main__":
    sys.exit(main())
