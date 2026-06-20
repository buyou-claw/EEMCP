"""Verify the MCP 8852B user-interaction gate.

Confirms the hard-coded rule:
  "After MCP startup, the user must be asked before any test runs."

The rule is enforced by (a) removing @mcp.tool() from every atomic test
tool, leaving bt_run_full_suite as the only MCP-callable entry point,
(b) stripping the non_interactive bypass from bt_run_full_suite, and
(c) forcing a final plan-confirmation gate so the operator sees the
plan even when the AI pre-fills every parameter on the first call.
Scripts that need batch / CI mode still import the atomic Python
functions directly from mcp_8852b.server — that path is preserved.

What this script checks (5 tests, no real instrument needed):
  1. Atomic test tools are NOT registered as MCP tools.
  2. bt_run_full_suite has no `non_interactive` parameter (forced gate).
  3. Library import channel is intact — scripts can still call atomic
     functions directly.
  4. The expected non-test tools (connection, diagnostics, read-only,
     bt_run_full_suite) are still present in the MCP tool registry.
  5. The final plan-confirmation gate is hard-enforced:
     (a) prompt_final_confirm is exported from mcp_8852b.interactive,
     (b) bt_run_full_suite's body calls run_campaign with
         force_final_confirm=True (no `False`/`override=` escape hatch
         in the source).

Run:  python verify_mcp8852b_gate.py
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

from mcp_8852b import server as srv  # noqa: E402
from mcp_8852b import interactive as iv  # noqa: E402

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


ATOMIC_TOOLS = {
    "bt_reset",
    "bt_set_eut_interface",
    "bt_run_tx_test",
    "bt_configure_ble_tx",
    "bt_start",
    "bt_wait_test_complete",
    "bt_abort",
}

EXPECTED_MCP_TOOLS = {
    "bt_connect",
    "bt_disconnect",
    "bt_status",
    "bt_eut_interface",
    "bt_diag_eut_link",
    "bt_boot_status",
    "bt_ins",
    "bt_read_oresult",
    "bt_read_script_result",
    "bt_preflight",
    "bt_run_full_suite",
}


def test_1_atomic_tools_removed() -> None:
    name = "1. 原子测试工具不再注册为 MCP tool"
    try:
        registered = _registered_tool_names()
        leaked = ATOMIC_TOOLS & registered
        assert not leaked, (
            f"atomic tools leaked into MCP registry: {sorted(leaked)}. "
            "These should be reachable only via Python import "
            "(from mcp_8852b.server import bt_run_tx_test, ...)."
        )
        record(name, True,
               f"{len(ATOMIC_TOOLS)} atomic tools absent "
               f"({len(registered)} tools total registered)")
    except Exception:
        record(name, False, traceback.format_exc())


def test_2_run_full_suite_no_bypass() -> None:
    name = "2. bt_run_full_suite 不再接受 non_interactive 旁路"
    try:
        sig = inspect.signature(srv.bt_run_full_suite)
        params = sig.parameters
        assert "non_interactive" not in params, (
            f"bt_run_full_suite still accepts non_interactive="
            f"{params.get('non_interactive')!r}; the MCP gate must be hard"
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
    name = "4. 连接/读状态/诊断类 + bt_run_full_suite 仍在 MCP 注册表"
    try:
        registered = _registered_tool_names()
        missing = EXPECTED_MCP_TOOLS - registered
        assert not missing, f"expected MCP tools missing: {sorted(missing)}"
        record(name, True,
               f"all {len(EXPECTED_MCP_TOOLS)} expected tools present "
               f"({len(registered)} total registered)")
    except Exception:
        record(name, False, traceback.format_exc())


def test_5_final_confirm_enforced() -> None:
    """Non-skippable final plan-confirmation gate (NOTES §17 follow-up).

    Two enforcement layers:
      (a) `prompt_final_confirm` exists on `mcp_8852b.interactive` with
          the expected signature (so library callers can also use it).
      (b) `bt_run_full_suite`'s body hard-codes
          `force_final_confirm=True` on the `run_campaign(...)` call.
          We parse the source for the call site rather than runtime-
          intercepting it (no real instrument required).
    """
    name = "5. 最终确认门 (force_final_confirm) 已硬性生效"
    try:
        # (a) prompt_final_confirm is exported and callable.
        fn = getattr(iv, "prompt_final_confirm", None)
        assert callable(fn), (
            "mcp_8852b.interactive.prompt_final_confirm is missing or "
            "not callable. The final plan-confirmation gate cannot fire."
        )
        sig = inspect.signature(fn)
        assert "plan" in sig.parameters, (
            f"prompt_final_confirm signature missing 'plan' arg: {sig}"
        )

        # (b) bt_run_full_suite body passes force_final_confirm=True
        #     unconditionally. Scan source for the run_campaign(...) call
        #     inside bt_run_full_suite; assert no False / no kw override
        #     that disables the gate.
        src = inspect.getsource(srv)
        # Extract the bt_run_full_suite body.
        m = re.search(
            r"def\s+bt_run_full_suite\s*\([^)]*\)\s*->\s*dict:.*?(?=\n(?:def|class|@mcp\.tool|if\s+__name__)|\Z)",
            src,
            re.DOTALL,
        )
        assert m, "could not locate bt_run_full_suite body in server.py"
        body = m.group(0)
        assert "force_final_confirm=" in body, (
            "bt_run_full_suite body never references "
            "force_final_confirm=; the gate is not wired up."
        )
        # The only call site should pass True. Find every occurrence.
        flags = re.findall(r"force_final_confirm\s*=\s*([A-Za-z_]+)", body)
        assert flags, (
            "bt_run_full_suite references force_final_confirm but the "
            "AST shape was unexpected — check the regex."
        )
        bad = [f for f in flags if f != "True"]
        assert not bad, (
            f"bt_run_full_suite body contains force_final_confirm={bad!r}; "
            "the MCP channel must always pass True. Library callers can "
            "still pass False to run_campaign directly."
        )

        record(name, True,
               f"prompt_final_confirm present; force_final_confirm=True "
               f"set on run_campaign(...) call ({len(flags)} occurrence(s))")
    except Exception:
        record(name, False, traceback.format_exc())


def main() -> int:
    print("=" * 72)
    print("MCP 8852B gate verification (5 tests, no real instruments)")
    print("=" * 72)
    test_1_atomic_tools_removed()
    test_2_run_full_suite_no_bypass()
    test_3_library_import_intact()
    test_4_expected_tools_present()
    test_5_final_confirm_enforced()
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
