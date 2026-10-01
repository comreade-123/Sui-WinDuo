"""Stability check: hold the board still and confirm the angle does not drift.

The previous build accumulated without bound and ran off to -1000 deg.
This test proves the bounded build stays put.

Usage:
  .venv\\Scripts\\python.exe tools\\test_stability.py --port COM3 --seconds 20
"""
import argparse
import json
import re
import sys
import time

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=20.0)
    args = ap.parse_args()

    pat = re.compile(r"\{[^}]*\}")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as exc:
        print("cannot open %s: %s" % (args.port, exc))
        return 2

    print("=" * 68)
    print("STABILITY CHECK -- put the board down and DO NOT touch it")
    print("=" * 68)
    print("measuring %.0f seconds ..." % args.seconds)
    print()

    t0 = time.time()
    vals = []
    last = 0.0
    try:
        while time.time() - t0 < args.seconds:
            line = ser.readline()
            if b"}" not in line:
                continue
            m = pat.search(line.decode("utf-8", "ignore"))
            if not m:
                continue
            try:
                d = json.loads(m.group(0))
            except ValueError:
                continue
            if "angle" not in d:
                continue
            ang = float(d["angle"])
            vals.append(ang)
            now = time.time() - t0
            if now - last >= 2.0:
                last = now
                print("  [%5.1fs] angle = %8.2f   status=%s mode=%s"
                      % (now, ang, d.get("status"), d.get("mode")))
    finally:
        ser.close()

    print()
    print("=" * 68)
    print("RESULT")
    print("=" * 68)
    if not vals:
        print("no data")
        return 1
    first = vals[0]
    last_v = vals[-1]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    drift = last_v - first
    print("samples      : %d" % len(vals))
    print("first / last : %8.2f  ->  %8.2f" % (first, last_v))
    print("range        : %8.2f .. %8.2f   (span %.2f)" % (lo, hi, span))
    print("drift        : %+8.2f deg over %.0f s" % (drift, args.seconds))
    print()
    if abs(drift) > 30.0 or span > 60.0:
        print("=> STILL UNSTABLE: angle is running away again.")
    elif abs(drift) > 5.0:
        print("=> mostly stable but drifting %.1f deg; gyro bias may need recalibration"
              % drift)
    else:
        print("=> STABLE: drift %.2f deg over %.0f s, span %.2f deg. Fix confirmed."
              % (drift, args.seconds, span))
    return 0


if __name__ == "__main__":
    sys.exit(main())
