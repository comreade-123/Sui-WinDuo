"""Live signed-angle chain tracer.

Prints the firmware's internal chain so a sign problem can be located:
    pitch  = absolute pitch angle (deg)
    base   = baseline captured at boot calibration
    virt   = signed relative angle before the output low-pass (HINGE_SIGN applied)
    angle  = what the firmware actually reports

Run it, rotate the board in BOTH directions, and watch which column loses
the sign.

Usage:
  .venv\\Scripts\\python.exe tools\\trace_angle_chain.py --port COM3 --seconds 25
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

    ser.write(b"mode=debug\n")          # make sure debug fields are emitted
    time.sleep(0.4)

    print("=" * 78)
    print("rotate the board in BOTH directions while this runs "
          "(%.0f seconds)" % args.seconds)
    print("=" * 78)
    print("%8s | %8s %8s %8s | %8s   %s" % ("t(s)", "pitch", "base", "virt", "angle", "note"))
    print("-" * 78)

    t0 = time.time()
    last = 0.0
    try:
        while time.time() - t0 < args.seconds:
            line = ser.readline()
            m = pat.search(line.decode("utf-8", "ignore")) if b"}" in line else None
            if not m:
                continue
            try:
                d = json.loads(m.group(0))
            except ValueError:
                continue
            if "angle" not in d or "virt" not in d:
                continue

            now = time.time() - t0
            if now - last < 0.35:
                continue
            last = now

            angle = float(d["angle"])
            virt = float(d["virt"])
            pitch = float(d.get("pitch", 0.0))
            base = float(d.get("base", 0.0))

            note = ""
            if virt < -1.0 and angle > -1.0:
                note = "SIGN LOST between virt and angle"
            elif virt > 1.0 and angle < 1.0:
                note = "SIGN LOST between virt and angle"
            elif abs(pitch - base) > 5.0 and abs(virt) < 1.0:
                note = "virt stuck near zero -> check HINGE_SIGN / baseline"
            print("%8.1f | %8.2f %8.2f %8.2f | %8.2f   %s"
                  % (now, pitch, base, virt, angle, note))
    finally:
        ser.close()

    print("-" * 78)
    print("legend: virt should equal (pitch - base) * HINGE_SIGN,")
    print("        angle is virt after the output low-pass -> they must share sign.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
