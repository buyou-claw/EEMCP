"""Joulescope JS220 MCP Server package.

An MCP server that exposes the Joulescope JS220 precision energy
analyzer as atomic tools for Claude to control via natural language.
Pairs naturally with the existing `mcp_34465a` (DMM) and `mcp_8852b`
(BT tester) packages for the CMT BLE Sticker HWDVT line:

    mcp_34465a  -> DC voltage / current / resistance
    mcp_8852b   -> BT RF power / freq / sensitivity
    mcp_js220   -> current, voltage, power, energy (high-rate streaming)

The JS220 connects only over USB and uses the private joulescope
Python API (NOT SCPI / pyvisa). The package wraps the public
`joulescope.scan`, `Joulescope.open`/`close`, `parameter_set`/`get`,
`start`/`stop`, `statistics_callback_register`, and `read` calls into
a uniform tool surface.
"""
__version__ = "0.1.0"
