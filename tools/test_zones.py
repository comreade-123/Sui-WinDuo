"""Zone-transition probe.

The firmware uses the accelerometer to hold the reading near zero and
above, and pure gyro integration once the angle goes below a threshold.
This test shows the hand-over: it prints the zone, flags any sudden step,
and reports how far the negative side was able to travel.

Usage:
  .venv\\Scripts\\python.exe tools\\test_zones.py --port COM3 --seconds 25
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
    ap.add_argument("--step", type=float, default=8.0)
    args = ap.parse_args()

    pat = re.compile(r"\{[^}]*\}")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as exc:
        print("cannot open %s: %s" % (args.port, exc))
        return 2

    ser.write(b"mode=debug\n")
    time.sleep(0.4)
    ser.reset_input_buffer()

    print("=" * 78)
    print("ZONE TRANSITION PROBE")
    print("=" * 78)
    print("1) first turn toward POSITIVE readings and hold a moment")
    print("   (accelerometer holds it -> should be rock steady)")
    print("2) then swing the OTHER way into negative territory and keep going")
    print("   (firmware switches to pure gyro -> should be smooth, no sign snap)")
    print()
    print("%7s | %9s %9s | %-11s | %s" % ("t(s)", "angle", "gyro", "zone", "note"))
    print("-" * 78)

    t0 = time.time()
    last_print = 0.0
    prev = None
    prev_t = None
    flagged = []
    logs = []
    zone_switches = []
    last_zone = None
    vals = []

    try:
        while time.time() - t0 < args.seconds:
            line = ser.readline().decode("utf-8", "ignore").strip()
            if line.startswith("#"):
                if "正值区" in line or "负值区" in line:
                    logs.append("%5.1fs %s" % (time.time() - t0, line))
                continue
            if not line.startswith("{"):
                continue
            m = pat.search(line)
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
            gyro = float(d.get("gyro", 0.0))
            vals.append(angle)
            zone = "ACCEL(>=0)" if angle >= -2.0 else "GYRO(<0)"
            if zone != last_zone:
                if last_zone is not None:
                    zone_switches.append((now, last_zone, zone, angle))
                last_zone = zone

            note = ""
            if prev is not None:
                step = angle - prev
                dt = (now - prev_t) if prev_t is not None else 0.0
                if abs(step) > args.step:
                    note = "STEP %+.1f (dt=%.3fs)" % (step, dt)
                    flagged.append((now, prev, angle, step, dt))
            prev, prev_t = angle, now

            if now - last_print >= 0.3 or note:
                last_print = now
                print("%7.2f | %9.2f %9.2f | %-11s | %s" % (now, angle, gyro, zone, note))
    finally:
        ser.close()

    print("-" * 78)
    print("zone switches : %d" % len(zone_switches))
    for t, a, b, ang in zone_switches[:8]:
        print("   %6.2fs  %s -> %s  at angle %.2f" % (t, a, b, ang))
    print("flagged steps : %d" % len(flagged))
    for t, a, b, step, dt in flagged[:10]:
        print("   %6.2fs  %8.2f -> %8.2f  step %+7.2f  dt %.3fs" % (t, a, b, step, dt))
    print("firmware zone logs:")
    for l in logs[:8]:
        print("   " + l)
    print()
    if not vals:
        print("no data")
        return 1
    print("angle range : %8.2f .. %8.2f" % (min(vals), max(vals)))
    print()
    if flagged:
        print("=> steps detected; inspect them above before calling it fixed.")
    elif min(vals) < -20.0:
        print("=> clean: travelled to %.1f deg on the negative side with no step >%.0f deg,"
              % (min(vals), args.step))
        print("   and the zone hand-over produced no jump. Confirmed.")
    else:
        print("=> no steps, but the negative side only reached %.1f deg; swing further"
              % min(vals))
        print("   to exercise the hand-over properly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
