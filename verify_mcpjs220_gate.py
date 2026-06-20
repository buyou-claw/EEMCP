"""Verify the MCP JS220 user-interaction gate.

Confirms the hard-coded rule:
  "After MCP startup, the user must be asked before any test runs."

The rule is enforced by removing @mcp.tool() from every atomic test
tool, leaving js220_preflight_capture / js220_run_preflight_capture as
the only MCP-callable capture entry points. Scripts that need batch /
CI mode still import the atomic Python functions directly from
mcp_js220.server — that path is preserved.

What this script checks (4 tests, no real instrument needed):
  1. Atomic test tools are NOT registered as MCP tools.
  2. js220_preflight_capture signature still has auto_confirm (propose
     bypass) and js220_run_preflight_capture signature still has the
     user_confirmed gate (default False, hard-gated).
  3. Library import channel is intact — scripts can still call atomic
     functions directly.
  4. The expected non-test tools (connection, status, preflight, read-
     only discovery) are still present in the MCP tool registry.

Run:  python verify_mcpjs220_gate.py
"""
from __future__ import annotations
import re
import sys
import inspect
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_js220 import server as srv  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}  {note}".rstrip())


def _registered_tool_names() -> set[str]:
    """Best-effort discovery of registered MCP tool names.

    Tries FastMCP's runtime tool manager first (authoritative); falls
    back to parsing the module source for @mcp.tool(...) decorators.
    Mirrors verify_mcp8852b_gate.py:_registered_tool_names().
    """
    registered: set[str] = set()
    tm = getattr(srv.mcp, "_tool_manager", None) or getattr(srv.mcp, "tool_manager", None)
    if tm is not None:
        tools_dict = getattr(tm, "_tools", None) or getattr(tm, "tools", None) or {}
        if isinstance(tools_dict, dict):
            registered = set(tools_dict.keys())
        else:
            # Some FastMCP versions store a flat list of Tool objects.
            for t in tools_dict:
                name = getattr(t, "name", None) or getattr(t, "key", None)
                if name:
                    registered.add(name)
    if registered:
        return registered
    src = inspect.getsource(srv)
    for m in re.finditer(r"@mcp\.tool\b[^\n]*\ndef\s+(\w+)\s*\(", src):
        registered.add(m.group(1))
    return registered


# Atomic write/measure tools removed from the MCP surface. These are
# only reachable via `from mcp_js220.server import ...` now.
ATOMIC_TOOLS = {
    "js220_configure_for_test",
    "js220_set_source",
    "js220_set_current_range",
    "js220_set_sampling_frequency",
    "js220_set_reduction_frequency",
    "js220_set_io_voltage",
    "js220_clear_accumulators",
    "js220_start",
    "js220_stop",
    "js220_collect",
    "js220_measure_current_avg",
    "js220_measure_voltage_avg",
    "js220_measure_power_avg",
    "js220_measure_current_peak",
    "js220_measure_power_peak",
    "js220_capture_window",
    "js220_run_power_profile",
}

# Read-only / connection / preflight tools that MUST remain in the MCP
# tool registry. Mirrors the post-gate surface.
EXPECTED_MCP_TOOLS = {
    # Read-only configuration discovery
    "js220_get_capabilities",
    "js220_recommend_config",
    # Connection lifecycle
    "js220_list_devices",
    "js220_connect",
    "js220_disconnect",
    "js220_status",
    "js220_reset",
    # Preflight gate (the only capture entry points)
    "js220_preflight_capture",
    "js220_run_preflight_capture",
}


def test_1_atomic_tools_removed() -> None:
    name = "1. 原子测试工具不再注册为 MCP tool"
    try:
        registered = _registered_tool_names()
        leaked = ATOMIC_TOOLS & registered
        assert not leaked, (
            f"atomic tools leaked into MCP registry: {sorted(leaked)}. "
            "These should be reachable only via Python import "
            "(from mcp_js220.server import js220_measure_current_avg, ...)."
        )
        record(name, True,
               f"{len(ATOMIC_TOOLS)} atomic tools absent "
               f"({len(registered)} tools total registered)")
    except Exception:
        record(name, False, traceback.format_exc())


def test_2_preflight_gate_intact() -> None:
    name = "2. preflight 强制位仍在 (auto_confirm + user_confirmed)"
    try:
        # Step 1: auto_confirm param exists on js220_preflight_capture
        propose_sig = inspect.signature(srv.js220_preflight_capture)
        assert "auto_confirm" in propose_sig.parameters, (
            f"js220_preflight_capture missing auto_confirm: "
            f"{list(propose_sig.parameters.keys())}"
        )
        # auto_confirm default is False (must pause for user review)
        ac_param = propose_sig.parameters["auto_confirm"]
        assert ac_param.default is False, (
            f"auto_confirm default must be False (pause for user), got {ac_param.default!r}"
        )

        # Step 2: user_confirmed param exists on js220_run_preflight_capture
        run_sig = inspect.signature(srv.js220_run_preflight_capture)
        assert "user_confirmed" in run_sig.parameters, (
            f"js220_run_preflight_capture missing user_confirmed: "
            f"{list(run_sig.parameters.keys())}"
        )
        # user_confirmed default is False (hard gate)
        uc_param = run_sig.parameters["user_confirmed"]
        assert uc_param.default is False, (
            f"user_confirmed default must be False (hard gate), got {uc_param.default!r}"
        )
        record(name, True,
               f"auto_confirm=False + user_confirmed=False (gate enforced)")
    except Exception:
        record(name, False, traceback.format_exc())


def test_3_library_import_intact() -> None:
    name = "3. 库级 import 通道保留（脚本可调用原子函数）"
    try:
        # Both the gate tools and the removed-atomic tools must still be
        # callable as plain Python for the script path.
        all_required = ATOMIC_TOOLS | EXPECTED_MCP_TOOLS
        for fn_name in sorted(all_required):
            fn = getattr(srv, fn_name, None)
            assert callable(fn), f"{fn_name} is not importable / not callable"
        record(name, True,
               f"{len(all_required)} functions importable + callable")
    except Exception:
        record(name, False, traceback.format_exc())


def test_4_expected_tools_present() -> None:
    name = "4. 连接/读状态/发现/preflight 类仍在 MCP 注册表"
    try:
        registered = _registered_tool_names()
        missing = EXPECTED_MCP_TOOLS - registered
        assert not missing, f"expected MCP tools missing: {sorted(missing)}"
        record(name, True,
               f"all {len(EXPECTED_MCP_TOOLS)} expected tools present "
               f"({len(registered)} total registered)")
    except Exception:
        record(name, False, traceback.format_exc())


def main() -> int:
    print("=" * 72)
    print("MCP JS220 gate verification (4 tests, no real instruments)")
    print("=" * 72)
    test_1_atomic_tools_removed()
    test_2_preflight_gate_intact()
    test_3_library_import_intact()
    test_4_expected_tools_present()
    print("=" * 72)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"Summary: {passed}/{total} tests passed")
    print("=" * 72)
    if passed == total:
        print("GO")
        return 0
    print("NO-GO")
    return 1


if __name__ == "__main__":
    sys.exit(main())