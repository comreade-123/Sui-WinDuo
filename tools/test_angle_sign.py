"""Signed-angle verification with generous timing.

Follow the four prompts. Each prompt gives you plenty of time to settle,
and live readings are printed so you can see the effect of your own hand
movement as it happens.

Usage:
  .venv\\Scripts\\python.exe tools\\test_angle_sign.py --port COM3
"""
import argparse
import json
import re
import sys
import time

import serial


def collect(ser, pat, seconds, label, tick=1.0):
    """Read for N seconds; print a live line every `tick` seconds; return stats."""
    t0 = time.time()
    last = t0
    vals = []
    while time.time() - t0 < seconds:
        line = ser.readline()
        if b"}" in line:
            m = pat.search(line.decode("utf-8", "ignore"))
            if m:
                try:
                    d = json.loads(m.group(0))
                except ValueError:
                    d = None
                if d and "angle" in d:
                    vals.append(float(d["angle"]))
        now = time.time()
        if now - last >= tick and vals:
            last = now
            recent = vals[-20:]
            avg = sum(recent) / len(recent)
            print("    [%4.1fs] angle = %8.2f" % (now - t0, avg))
    if not vals:
        print("  %-26s no data" % label)
        return None, None, None
    srt = sorted(vals)
    med = srt[len(srt) // 2]
    print("  %-26s median %8.2f   min %7.2f   max %7.2f   n=%d"
          % (label, med, srt[0], srt[-1], len(vals)))
    return med, srt[0], srt[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    pat = re.compile(r"\{[^}]*\}")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as exc:
        print("cannot open %s: %s" % (args.port, exc))
        return 2

    print("=" * 62)
    print("signed angle test -- follow the four prompts")
    print("=" * 62)

    print()
    print("STEP 1/4  Put the board down FLAT and let go. Do not touch it.")
    print("          Measuring for 8 seconds...")
    base, bmin, bmax = collect(ser, pat, 8, "flat, untouched")

    print()
    print("STEP 2/4  Rotate about 45 deg toward DIRECTION A, then HOLD still.")
    print("          (start when you are ready -- measuring 8 seconds)")
    da, amin, amax = collect(ser, pat, 8, "direction A held")

    print()
    print("STEP 3/4  Rotate back the other way, about 45 deg, and HOLD still.")
    print("          (measuring 8 seconds)")
    db, bmin2, bmax2 = collect(ser, pat, 8, "direction B held")

    print()
    print("STEP 4/4  Put it back FLAT and let go again.")
    print("          (measuring 6 seconds -- checks for drift)")
    back, rmin, rmax = collect(ser, pat, 6, "flat again")

    ser.close()

    print()
    print("=" * 62)
    print("VERDICT")
    print("=" * 62)
    if None in (base, da, db, back):
        print("not enough data")
        return 1

    print("flat baseline      : %8.2f" % base)
    print("direction A        : %8.2f   (delta %+8.2f)" % (da, da - base))
    print("direction B        : %8.2f   (delta %+8.2f)" % (db, db - base))
    print("flat again         : %8.2f   (drift %+8.2f)" % (back, back - base))
    print()

    span = abs(da - db)
    if span < 20.0:
        print("WARNING: only %.1f deg of travel was measured. Rotate more so the" % span)
        print("         sign test is meaningful.")
    pos = "A" if da > db else "B"
    print("direction %s -> POSITIVE => perspective stretch ON" % pos)
    print("direction %s -> negative => stays clear" % ("B" if pos == "A" else "A"))
    print()
    if abs(back - base) <= 5.0:
        print("drift check: OK (returned to within %.1f deg of baseline)" % abs(back - base))
    else:
        print("drift check: %.1f deg offset after returning flat -- gyro bias or"
              % abs(back - base))
        print("             calibration may need redoing (long-press the button).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
