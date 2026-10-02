"""Measure CPU load of the glass overlay (objective before/after).

Launches duo_glass.py with a given set of options, samples the process CPU
time from the OS, and reports average CPU usage plus frame counters.

Usage:
  .venv\\Scripts\\python.exe tools/bench_overlay_cpu.py
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

PY = str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe")
GLASS = str(Path(__file__).resolve().parent.parent / "pc" / "duo_glass.py")


def cpu_times(pid):
    """Return (user, kernel) CPU seconds for a pid using 'wmic'-free approach."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Id %d).TotalProcessorTime.TotalSeconds" % pid],
            capture_output=True, text=True, timeout=15)
        txt = out.stdout.strip().replace(",", ".")
        return float(txt) if txt else None
    except Exception:
        return None


def run_case(label, extra_args, seconds):
    cmd = [PY, GLASS, "--port", "COM3"] + extra_args
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    time.sleep(3.0)                      # let it settle (calibration + first paints)

    c0 = cpu_times(proc.pid)
    wall0 = time.time()
    time.sleep(seconds)
    c1 = cpu_times(proc.pid)
    wall = time.time() - wall0

    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=8)
    except Exception:
        proc.kill()
        out = ""

    paints = re.findall(r"paint ([\d.]+)ms/帧\((\d+)\)", out)
    caps = re.findall(r"capture ([\d.]+)ms/帧\((\d+)\)", out)
    total_paints = sum(int(n) for _, n in paints)
    total_caps = sum(int(n) for _, n in caps)

    cpu = (c1 - c0) if (c0 is not None and c1 is not None) else None
    pct = (cpu / wall * 100.0) if cpu is not None else float("nan")
    print("%-34s | CPU %6.1f%% | paints %4d | captures %3d | %.1fs"
          % (label, pct, total_paints, total_caps, wall))
    return pct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()

    print("=" * 78)
    print("Glass overlay CPU benchmark (board should be connected; COM3)")
    print("=" * 78)
    print("%-34s | %-9s | %-12s | %-14s |" % ("configuration", "CPU", "redraws", "captures"))
    print("-" * 78)
    print("NOTE: the overlay window will appear and disappear repeatedly.")
    print()

    run_case("baseline (scale=1.0, 3Hz, old path)", ["--capture-scale", "1.0",
                                                     "--refresh-hz", "3"], args.seconds)
    run_case("optimised (scale=0.5, 2Hz)", ["--capture-scale", "0.5",
                                            "--refresh-hz", "2"], args.seconds)
    run_case("light (scale=0.35, 1Hz)", ["--capture-scale", "0.35",
                                         "--refresh-hz", "1"], args.seconds)
    print()
    print("CPU%% is the overlay process only, measured over the sampling window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
