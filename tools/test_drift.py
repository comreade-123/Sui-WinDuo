"""Drift benchmark for gyro-only mode.

Hold the board perfectly still and measure how fast the reported angle
creeps. That creep rate is the entire cost of dropping the accelerometer.

Usage:
  .venv\\Scripts\\python.exe tools\\test_drift.py --port COM3 --seconds 60
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
    ap.add_argument("--seconds", type=float, default=60.0)
    args = ap.parse_args()

    pat = re.compile(r"\{[^}]*\}")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as exc:
        print("cannot open %s: %s" % (args.port, exc))
        return 2

    print("=" * 70)
    print("GYRO-ONLY DRIFT BENCHMARK")
    print("=" * 70)
    print("put the board down and DO NOT touch it for %.0f seconds" % args.seconds)
    print("(this is the only cost of dropping the accelerometer)")
    print()

    t0 = time.time()
    samples = []          # (t, angle)
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
            now = time.time() - t0
            samples.append((now, float(d["angle"])))
            if now - last >= 5.0:
                last = now
                print("  [%5.1fs] angle = %9.2f" % (now, samples[-1][1]))
    finally:
        ser.close()

    print()
    print("=" * 70)
    if len(samples) < 20:
        print("not enough samples")
        return 1

    first = samples[0]
    lastv = samples[-1]
    dt = lastv[0] - first[0]
    drift = lastv[1] - first[1]
    rate = drift / dt if dt > 0 else 0.0
    vals = [s[1] for s in samples]

    # 拟合线性漂移率更稳健（首末点易受抖动影响）
    n = len(samples)
    sx = sum(s[0] for s in samples)
    sy = sum(s[1] for s in samples)
    sxx = sum(s[0] * s[0] for s in samples)
    sxy = sum(s[0] * s[1] for s in samples)
    denom = n * sxx - sx * sx
    slope = ((n * sxy - sx * sy) / denom) if abs(denom) > 1e-9 else 0.0

    print("samples      : %d over %.1f s" % (n, dt))
    print("first / last : %8.2f -> %8.2f" % (first[1], lastv[1]))
    print("range        : %8.2f .. %8.2f  (span %.2f)" % (min(vals), max(vals),
                                                          max(vals) - min(vals)))
    print("total drift  : %+8.2f deg" % drift)
    print("drift rate   : %+8.3f deg/s  (%.2f deg/min, least-squares fit)"
          % (slope, slope * 60.0))
    print()
    abs_rate = abs(slope) * 60.0
    if abs_rate < 1.0:
        print("=> EXCELLENT: under 1 deg/min. Gyro-only is perfectly usable.")
    elif abs_rate < 5.0:
        print("=> GOOD: %.1f deg/min. Fine for session use; long-press to recalibrate"
              % abs_rate)
        print("   if it bothers you after a long session.")
    elif abs_rate < 20.0:
        print("=> ACCEPTABLE but noticeable: %.1f deg/min. Consider re-running the"
              % abs_rate)
        print("   boot calibration while the board is truly still and level.")
    else:
        print("=> POOR: %.1f deg/min suggests the gyro bias estimate is off." % abs_rate)
        print("   Re-check that the board is motionless during the 3 s boot calibration,")
        print("   and that the button is wired (long-press re-calibrates).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
