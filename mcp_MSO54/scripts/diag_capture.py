"""Re-test CURVE? on CH1 in a FRESH connection (after the previous run
left the bulk-IN in a partially-wedged state from CH2/3/4 preamble TMO)."""
from __future__ import annotations
import sys
import time

sys.path.insert(0, "E:/other/ai_project/EEMCP")

from mcp_MSO54.instrument import MSO54, MSO54Error


def main() -> int:
    s = MSO54()
    try:
        s.connect()
    except Exception as e:
        print(f"CONNECT FAIL: {e}")
        return 1

    try:
        print(f"idn  : {s.idn()}")
        print(f"acq  : {s.q_acq_state()}  numacq={s.q_acq_numacq()}")
        print(f"CH1  : display={s.q_channel_display(1)} scale={s.q_channel_scale(1)} V/div")
        print(f"reclen: {s.q_record_length()}")
        print()
        for stop in (50, 500, 1000):
            try:
                t0 = time.time()
                wfm = s.read_curve("CH1", start=1, stop=stop, width=1)
                dt = time.time() - t0
                v = wfm["values"]
                print(f"  CURVE? CH1 stop={stop:5d}: OK in {dt:.3f}s  "
                      f"npts={wfm['npoints']}  vmin={min(v):+.4f}  "
                      f"vmax={max(v):+.4f}  vmean={sum(v)/len(v):+.4f}")
            except Exception as e:
                print(f"  CURVE? CH1 stop={stop:5d}: FAIL  {type(e).__name__}: {e}")
                break
    finally:
        s.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
