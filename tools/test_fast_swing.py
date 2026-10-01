"""Fast-swing test: does a negative angle survive a quick rotation?

Watches the reported angle while you swing the board quickly into the
negative direction and hold it there. Prints each time the sign changes
and reports how long the negative reading lasted.

Usage:
  .venv\\Scripts\\python.exe tools\\test_fast_swing.py --port COM3
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

    ser.write(b"mode=debug\n")
    time.sleep(0.4)

    print("=" * 70)
    print("FAST-SWING TEST")
    print("=" * 70)
    print("1) swing the board QUICKLY into the negative direction")
    print("2) keep holding it there")
    print("(%.0f seconds -- sign changes are printed as they happen)" % args.seconds)
    print()

    t0 = time.time()
    last_sign = None
    seg_start = t0
    segments = []          # (sign, duration, min, max)
    seg_min = None
    seg_max = None
    samples = 0

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
            samples += 1
            sign = "-" if ang < -0.5 else ("+" if ang > 0.5 else "0")
            if seg_min is None or ang < seg_min:
                seg_min = ang
            if seg_max is None or ang > seg_max:
                seg_max = ang
            if sign != last_sign:
                now = time.time()
                if last_sign is not None:
                    segments.append((last_sign, now - seg_start, seg_min, seg_max))
                    print("  [%5.2fs] sign %s -> %s   (previous segment: %.2fs, %.1f..%.1f)"
                          % (now - t0, last_sign, sign, now - seg_start, seg_min, seg_max))
                last_sign = sign
                seg_start = now
                seg_min = seg_max = ang
    finally:
        if last_sign is not None:
            segments.append((last_sign, time.time() - seg_start, seg_min, seg_max))
        ser.close()

    print()
    print("=" * 70)
    print("RESULT: %d samples, %d segments" % (samples, len(segments)))
    print("=" * 70)
    neg = [s for s in segments if s[0] == "-"]
    pos = [s for s in segments if s[0] == "+"]
    for sign, dur, lo, hi in segments:
        label = {"-": "negative", "+": "positive", "0": "near zero"}[sign]
        print("  %-10s %6.2fs   range %8.2f .. %8.2f" % (label, dur, lo, hi))
    print()
    if not neg:
        print("no negative reading was captured -- swing further or faster")
    else:
        longest = max(s[1] for s in neg)
        deepest = min(s[2] for s in neg)
        print("longest negative hold : %.2f s" % longest)
        print("deepest negative angle: %.2f deg" % deepest)
        if longest < 1.0:
            print("=> negative reading still collapses quickly; the accelerometer")
            print("   reference is probably still pulling it back.")
        else:
            print("=> negative reading holds. Fix confirmed.")
    if len(neg) > 3:
        print("note: %d separate negative segments -- the sign is flipping back and"
              % len(neg))
        print("      forth, which suggests noise around zero or a loose mount.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
