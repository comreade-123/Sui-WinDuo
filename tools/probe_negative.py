"""Guided negative-angle probe.

Walks you through two board poses so we can inspect what the firmware
reports at a large negative angle. A bar makes the value readable at a
glance.

Usage:
  .venv\\Scripts\\python.exe tools\\probe_negative.py --port COM3
"""
import argparse
import json
import re
import sys
import time

import serial


def bar(value, lo=-40.0, hi=40.0, width=41):
    span = hi - lo
    pos = int(round((value - lo) / span * (width - 1)))
    pos = max(0, min(width - 1, pos))
    chars = ["-"] * width
    chars[pos] = "#"
    return "".join(chars)


def read_for(ser, pat, seconds, label, show_bar=True):
    t0 = time.time()
    last = 0.0
    rows = []
    while time.time() - t0 < seconds:
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
        if "virt" not in d:
            continue
        now = time.time() - t0
        if now - last < 0.4:
            continue
        last = now
        rows.append((float(d["pitch"]), float(d["base"]), float(d["virt"]), float(d["angle"])))
        if show_bar:
            v = rows[-1][2]
            print("  %6.1fs  pitch=%7.2f  virt=%7.2f  angle=%7.2f  |%s|"
                  % (now, rows[-1][0], v, rows[-1][3], bar(v)))
    if not rows:
        print("  %s: no data" % label)
        return None
    vs = sorted(r[2] for r in rows)
    print("  -> %s: virt median %.2f, min %.2f, max %.2f"
          % (label, vs[len(vs) // 2], vs[0], vs[-1]))
    return rows


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

    ser.write(b"mode=debug\n")
    time.sleep(0.4)

    print("=" * 80)
    print("NEGATIVE ANGLE PROBE")
    print("=" * 80)

    print()
    print("POSE 1  Put the board FLAT on the desk (screen closed / level).")
    print("        Hold it there for 6 seconds.")
    p1 = read_for(ser, pat, 6, "POSE 1 flat")

    print()
    print("POSE 2  Now tilt the board the OTHER way, as far as it goes,")
    print("        until it is standing up / leaning past vertical.")
    print("        Hold it there for 8 seconds. This is the pose that")
    print("        produced the 'goes back to 0' symptom.")
    p2 = read_for(ser, pat, 8, "POSE 2 far negative")

    ser.close()

    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    if p1:
        v = sorted(r[2] for r in p1)
        print("flat pose     : virt %8.2f .. %8.2f" % (v[0], v[-1]))
    if p2:
        v = sorted(r[2] for r in p2)
        print("far pose      : virt %8.2f .. %8.2f" % (v[0], v[-1]))
        if v[0] < -10.0 and v[-1] > 10.0:
            print("=> virt swings across zero inside one pose: the pitch reference")
            print("   is flipping, which is what makes the reading snap back to 0.")
        elif v[-1] < -5.0:
            print("=> solid negative reading: firmware chain is fine at this angle.")
        elif v[0] > -5.0:
            print("=> never reached a large negative angle; tilt further.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
