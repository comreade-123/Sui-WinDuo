"""Monotonic-accumulation test.

Turn the board steadily in ONE direction and watch the reported angle.
It should keep accumulating (e.g. -30, -60, -90, -120 ...) instead of
snapping back to a positive value around +/-180.

Usage:
  .venv\\Scripts\\python.exe tools\\test_accumulation.py --port COM3 --seconds 25
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
    ap.add_argument("--seconds", type=float, default=25.0)
    args = ap.parse_args()

    pat = re.compile(r"\{[^}]*\}")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as exc:
        print("cannot open %s: %s" % (args.port, exc))
        return 2

    ser.write(b"mode=debug\n")
    time.sleep(0.4)

    print("=" * 72)
    print("MONOTONIC ACCUMULATION TEST")
    print("=" * 72)
    print("turn the board slowly and steadily in ONE direction, e.g. the")
    print("direction that gives NEGATIVE readings, and keep going past 180 deg")
    print("(%.0f seconds)" % args.seconds)
    print()
    print("%7s | %9s %9s | %s" % ("t(s)", "angle", "virt", "note"))
    print("-" * 72)

    t0 = time.time()
    last = 0.0
    prev = None
    vals = []
    jumps = []
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
            note = ""
            if prev is not None:
                delta = ang - prev
                if abs(delta) > 30.0:
                    jumps.append((now, prev, ang, delta))
                    note = "JUMP %+.1f" % delta
            prev = ang
            if now - last >= 0.4:
                last = now
                print("%7.1f | %9.2f %9s | %s"
                      % (now, ang, ("%.2f" % float(d["virt"])) if "virt" in d else "-", note))
    finally:
        ser.close()

    print("-" * 72)
    if not vals:
        print("no data")
        return 1
    print("angle range : %8.2f .. %8.2f" % (min(vals), max(vals)))
    print("samples     : %d" % len(vals))
    print("big jumps   : %d" % len(jumps))
    for t, a, b, d in jumps[:6]:
        print("   at %6.1fs  %8.2f -> %8.2f  (%+.1f)" % (t, a, b, d))
    print()
    lo, hi = min(vals), max(vals)
    if hi - lo > 200.0 and abs(lo) > 150.0:
        print("=> accumulated past 180 deg without wrapping: FIX CONFIRMED")
    elif len(jumps) == 0 and abs(lo) > 90.0:
        print("=> went beyond 90 deg with no sign jump: looks good")
    elif len(jumps) > 0:
        print("=> sign jumps still present; inspect the listed jumps above")
    else:
        print("=> travel was small; rotate further past 180 deg to prove the fix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
