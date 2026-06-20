"""pytest configuration — sys.path setup + shared fixtures.

Mirrors verify_preflight.py's sys.path manipulation so the pytest
collection sees mcp_34465a / mcp_8852b / mcp_js220 as importable
packages.
"""
from __future__ import annotations
import sys
from pathlib import Path

# Make sure we import the local packages, not anything else.
# Self-locate the project root via __file__ (no hard-coded bench path).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
