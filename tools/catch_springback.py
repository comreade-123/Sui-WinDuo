"""Capture a fast spring-back motion and look for a discontinuous jump.

Reproduces the "screen springs back, -10 snaps to 0" symptom by logging
the full chain at high rate and flagging any sample-to-sample step larger
than a threshold.

Usage:
  .venv\\Scripts\\python.exe tools\\catch_springback.py --port COM3 --seconds 20
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
    ap.add_argument("--step", type=float, default=2.0,
                    help="flag any step larger than this many degrees")
    args = ap.parse_args()

    pat = re.compile(r"\{[^}]*\}")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as exc:
        print("cannot open %s: %s" % (args.port, exc))
        return 2

    ser.write(b"mode=debug\n")
    time.sleep(0.4)

    print("=" * 76)
    print("SPRING-BACK CAPTURE")
    print("=" * 76)
    print("open the screen to a small negative angle, then let the hinge")
    print("spring back to zero -- repeat a few times during %.0f seconds" % args.seconds)
    print()
    print("%7s | %9s %9s %9s | %s" % ("t(s)", "angle", "virt", "gyro", "note"))
    print("-" * 76)

    t0 = time.time()
    last_print = 0.0
    prev_angle = None
    prev_t = None
    flagged = []
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

            now = time.time() - t0
            angle = float(d["angle"])
            virt = d.get("virt")
            gyro = d.get("gyro")

            note = ""
            if prev_angle is not None:
                step = angle - prev_angle
                dt = (now - prev_t) if prev_t is not None else 0.0
                if abs(step) > args.step:
                    rate = (step / dt) if dt > 1e-6 else 0.0
                    note = "STEP %+.1f (dt=%.3fs, %.0f deg/s)" % (step, dt, rate)
                    flagged.append((now, prev_angle, angle, step, dt, rate))
            prev_angle = angle
            prev_t = now

            if now - last_print >= 0.25 or note:
                last_print = now
                print("%7.2f | %9.2f %9s %9s | %s"
                      % (now, angle,
                         ("%.2f" % float(virt)) if virt is not None else "-",
                         ("%.2f" % float(gyro)) if gyro is not None else "-",
                         note))
    finally:
        ser.close()

    print("-" * 76)
    print("flagged steps: %d" % len(flagged))
    for t, a, b, step, dt, rate in flagged[:15]:
        print("  %6.2fs  %8.2f -> %8.2f   step %+7.2f  dt %.3fs  rate %+8.0f deg/s"
              % (t, a, b, step, dt, rate))
    print()
    if flagged:
        big = max(abs(f[3]) for f in flagged)
        fast = max(abs(f[5]) for f in flagged)
        print("largest single-sample step : %.2f deg" % big)
        print("fastest implied rate       : %.0f deg/s" % fast)
        if fast > 400:
            print("=> this is a genuinely fast motion (spring-back). The firmware")
            print("   follows it, so the jump is physical, not a computation bug.")
        else:
            print("=> the step is too large for the reported speed: likely a real")
            print("   discontinuity in the angle chain. Look at the rows above.")
    else:
        print("no step exceeded %.1f deg; if you still see a snap, describe the" % args.step)
        print("exact numbers and I will lower the threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
