"""Entry point so `python -m mcp_MSO54` starts the MCP server."""
from .server import mcp

if __name__ == "__main__":
    mcp.run()
