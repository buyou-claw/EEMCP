"""Verify the MCP 34465A user-interaction gate.

Confirms the hard-coded rule:
  "After MCP startup, the user must be asked before any test runs."

The rule is enforced by removing @mcp.tool() from every atomic measure
/ config / scan tool, leaving `dmm_preflight_measure` (propose) and
`dmm_run_preflight_measure` (execute) as the only MCP-callable test
entry points. The execute tool is hard-gated by `user_confirmed=True`
— `False` returns an error before any SCPI is sent.

Atomic tools (`dmm_measure_volt_dc`, `dmm_set_nplc`,
`dmm_scan_test_points`, etc.) remain as plain Python functions on
`mcp_34465a.server` for scripts and `verify_preflight.py` to import.
They are no longer decorated with @mcp.tool(), so FastMCP does not
register them. A chat agent that tries to call `dmm_measure_volt_dc`
via MCP gets a tool-not-found error.

What this script checks (4 tests, no real instrument needed):
  1. Atomic test / config / scan tools are NOT registered as MCP tools.
  2. `dmm_run_preflight_measure` keeps the `user_confirmed` gate.
  3. Library import channel is intact — scripts can still call the
     removed functions directly.
  4. The expected non-test tools (connection / status / reset +
     preflight propose + preflight run) are still present in the
     MCP tool registry.

Run:  python verify_mcp34465a_gate.py
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

from mcp_34465a import server as srv  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}  {note}".rstrip())


def _registered_tool_names() -> set[str]:
    """Best-effort discovery of registered MCP tool names.

    Tries FastMCP's runtime tool manager first (authoritative); falls
    back to parsing the module source for @mcp.tool(...) decorators.
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


# Atomic tools that were removed from the MCP surface. They are still
# importable from `mcp_34465a.server` for scripts, but they are NOT
# MCP-callable anymore.
ATOMIC_TOOLS = {
    # One-shot measurements (all 12 typed + 1 dispatcher)
    "dmm_measure_volt_dc",
    "dmm_measure_volt_ac",
    "dmm_measure_curr_dc",
    "dmm_measure_curr_ac",
    "dmm_measure_resistance",
    "dmm_measure_resistance_4w",
    "dmm_measure_frequency",
    "dmm_measure_period",
    "dmm_measure_continuity",
    "dmm_measure_diode",
    "dmm_measure_capacitance",
    "dmm_measure_temperature",
    "dmm_measure",
    # Configuration writes
    "dmm_set_nplc",
    "dmm_set_resolution",
    "dmm_set_continuity_threshold",
    "dmm_set_beep",
    # High-level batch scan
    "dmm_scan_test_points",
}

# The only MCP test entry points — both carry the user-interaction gate.
EXPECTED_MCP_TOOLS = {
    "dmm_connect",
    "dmm_disconnect",
    "dmm_status",
    "dmm_reset",
    "dmm_preflight_measure",
    "dmm_run_preflight_measure",
}


def test_1_atomic_tools_removed() -> None:
    name = "1. 原子测试/配置/scan 工具不再注册为 MCP tool"
    try:
        registered = _registered_tool_names()
        leaked = ATOMIC_TOOLS & registered
        assert not leaked, (
            f"atomic tools leaked into MCP registry: {sorted(leaked)}. "
            "These should be reachable only via Python import "
            "(from mcp_34465a.server import dmm_measure_volt_dc, ...)."
        )
        record(name, True,
               f"{len(ATOMIC_TOOLS)} atomic tools absent "
               f"({len(registered)} tools total registered)")
    except Exception:
        record(name, False, traceback.format_exc())


def test_2_run_preflight_user_confirmed_gate() -> None:
    name = "2. dmm_run_preflight_measure 保留 user_confirmed 强制位"
    try:
        sig = inspect.signature(srv.dmm_run_preflight_measure)
        params = sig.parameters
        assert "user_confirmed" in params, (
            "dmm_run_preflight_measure is missing the user_confirmed "
            "parameter — the MCP gate must be hard"
        )
        # Default should be False (force caller to set True explicitly).
        uc_param = params["user_confirmed"]
        assert uc_param.default is False, (
            f"dmm_run_preflight_measure.user_confirmed default must be "
            f"False (it is {uc_param.default!r}); a True default would "
            "let callers skip the gate by omission"
        )
        record(name, True, f"signature params: {list(params.keys())}")
    except Exception:
        record(name, False, traceback.format_exc())


def test_3_library_import_intact() -> None:
    name = "3. 库级 import 通道保留（脚本可调用原子函数）"
    try:
        for fn_name in sorted(ATOMIC_TOOLS | EXPECTED_MCP_TOOLS):
            fn = getattr(srv, fn_name, None)
            assert callable(fn), f"{fn_name} is not importable / not callable"
        record(name, True,
               f"{len(ATOMIC_TOOLS | EXPECTED_MCP_TOOLS)} functions "
               "importable + callable")
    except Exception:
        record(name, False, traceback.format_exc())


def test_4_expected_tools_present() -> None:
    name = "4. 连接/状态/重置 + 两条 preflight 工具仍在 MCP 注册表"
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
    print("MCP 34465A gate verification (4 tests, no real instruments)")
    print("=" * 72)
    test_1_atomic_tools_removed()
    test_2_run_preflight_user_confirmed_gate()
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