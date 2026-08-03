"""Cheap USBTMC bulk-IN recovery attempt.

Per memory entry feedback_usbtmc_setonly_query.md:
  - viClear does NOT recover the IN pipe (documented).
  - Real recovery = disable+re-enable USB device in Device Manager
    OR physically unplug+replug the USB cable.

This script tries the cheap-but-documented-failed path first, just so
we have an error to show. It will NOT fix a hard wedge.
"""
import sys
sys.path.insert(0, "E:/other/ai_project/EEMCP")

import pyvisa
from mcp_MSO54.config import CONFIG

rm = pyvisa.ResourceManager()
print(f"Resources seen by pyvisa:")
for r in rm.list_resources():
    print(f"  {r}")

print(f"\nAttempting to open {CONFIG.visa_resource} ...")
try:
    instr = rm.open_resource(CONFIG.visa_resource)
    print(f"  open: OK (session {instr.session})")
except Exception as e:
    print(f"  open: FAIL  {type(e).__name__}: {e}")
    sys.exit(2)

print(f"\nTrying viClear (per Tek docs, does NOT recover bulk-IN)...")
try:
    instr.clear()
    print(f"  viClear: OK")
except Exception as e:
    print(f"  viClear: FAIL  {type(e).__name__}: {e}")

print(f"\nTrying *IDN? ...")
try:
    idn = instr.query("*IDN?")
    print(f"  *IDN? -> {idn.strip()}")
except Exception as e:
    print(f"  *IDN? -> FAIL  {type(e).__name__}: {e}")

print(f"\nClosing...")
try:
    instr.close()
except Exception as e:
    print(f"  close: FAIL  {type(e).__name__}: {e}")

print(f"\nIf *IDN? failed above, the USBTMC bulk-IN is wedged.")
print(f"Recovery: disable+re-enable the USB device in Device Manager,")
print(f"          OR unplug+replug the USB cable. viClear does not fix it.")
