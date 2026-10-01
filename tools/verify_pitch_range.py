#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_pitch_range.py -- 对固件 pitchFromAccel() 做量程/单调性的静态数值分析

被分析对象: firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino
            pitchFromAccel():
                const float horizontal = sqrtf(accel.y*accel.y + accel.z*accel.z);
                return atan2f(-accel.x, horizontal) * RAD_TO_DEG_F;

问题: atan2(-ax, sqrt(ay^2+az^2)) 的第二个参数恒为非负, 因此返回值恒在 [-90, +90]。
      当装置绕转轴转过 ±90 度以上时, 重力矢量在 X 轴上的投影会反向, 而水平分量
      sqrt(ay^2+az^2) 只保留幅值 (不区分符号), 于是读数**折返**:
      theta = +90+a 与 theta = +90-a 读到同一个值。

后果 (硬件侧必须知道):
      1) 复现: 传感器俯仰 -90..+90 之外的姿态, 加速度计无法区分;
      2) 关键: **从"加速度计能分辨"的一端走到"折返的一侧"时, 加速度计的读数会
         反向变化**, 从而把互补滤波往错误方向拉 —— 这里会表现为角度走不到上限。
      3) 固件的 "静止锚定" (STILL_BASE_TRACK) 会持续把基准拉向加速度计读数,
         因此只要装置停在折返区间, 基准会被慢慢拉偏。

本脚本给出可复现的数字证据 (无硬件, 纯数值), 并找出"单调可分辨区间"的边界。

用法:
    python tools/verify_pitch_range.py
    python tools/verify_pitch_range.py --json-out verification/pitch_range.json
退出码: 0 = 分析完成 (仅报告事实); 1 = 发现与固件注释/文档不一致。
"""

from __future__ import annotations

import argparse
import json
import math
import sys

RAD_TO_DEG_F = 57.29578


def pitch_from_accel(ax: float, ay: float, az: float) -> float:
    """逐行复刻固件的 pitchFromAccel()。"""
    horizontal = math.sqrt(ay * ay + az * az)
    return math.atan2(-ax, horizontal) * RAD_TO_DEG_F


def accel_for_sensor_pitch(theta_deg: float) -> tuple[float, float, float]:
    """给定"传感器俯仰"反推重力矢量在传感器坐标系下的分量 (单位 g)。

    约定: 转轴 = 传感器 Y 轴; 俯仰在 X-Z 平面内; theta=0 时 Z 轴与重力反向 (az=+1)。
    """
    th = math.radians(theta_deg)
    ax = -math.sin(th)
    az = math.cos(th)
    return ax, 0.0, az


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="pitchFromAccel 量程分析")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    print("=" * 78)
    print("固件 pitchFromAccel() 量程与单调性分析")
    print("  pitch = atan2(-ax, sqrt(ay^2 + az^2)) * 57.29578")
    print("=" * 78)

    # ---- 1) 逐点扫描: 真实俯仰 vs 加速度计读数
    rows = []
    for theta in range(-180, 181, 5):
        ax, ay, az = accel_for_sensor_pitch(theta)
        rows.append((theta, pitch_from_accel(ax, ay, az)))

    print("\n[1] 真实姿态 -> 加速度计读数 (每 15 度采样)")
    print(f"  {'真实俯仰':>10s} {'加速度计读数':>14s}   {'偏差':>8s}")
    for theta, measured in rows:
        if theta % 15 == 0:
            print(f"  {theta:>10d} {measured:>14.2f}   {measured - theta:>8.2f}")

    # ---- 2) 单调可分辨区间: 从 -90 度(读数无歧义的下界)向上找折返点
    scan2 = [(t, pitch_from_accel(*accel_for_sensor_pitch(t))) for t in
             [x * 5 for x in range(-18, 37)]]      # -90 .. 180, 步长 5
    fold = None
    for (t0, m0), (t1, m1) in zip(scan2, scan2[1:]):
        if m1 <= m0:
            fold = t1
            break
    print(f"\n[2] 单调可分辨区间: 从 -90 度开始, 读数首次不再随真实俯仰增大而增大的位置 "
          f"= {fold} 度")
    print("    即: 只有传感器俯仰落在 [-90, %s) 内, 加速度计读数才与真实姿态一一对应;" % fold)
    print("    超出后读数折返 (theta 与 180-theta 同读数), 信息丢失。")

    # ---- 3) 端点处的方向塌缩 (定量证据)
    print("\n[3] 端点处的可分辨性塌缩 (互补滤波要靠这个纠偏)")
    print(f"  {'真实俯仰':>10s} {'accel读数':>10s} {'accel读数灵敏度 dM/dT':>24s}")
    for theta in (0, 30, 60, 85, 90, 95, 120, 150, 180):
        eps = 0.05
        ax0, ay0, az0 = accel_for_sensor_pitch(theta - eps)
        ax1, ay1, az1 = accel_for_sensor_pitch(theta + eps)
        dm = (pitch_from_accel(ax1, ay1, az1) - pitch_from_accel(ax0, ay0, az0)) / (2 * eps)
        ax, ay, az = accel_for_sensor_pitch(theta)
        print(f"  {theta:>10d} {pitch_from_accel(ax, ay, az):>10.2f} {dm:>24.3f}")
    print("  灵敏度 <=0 表示加速度计在该姿态附近无法给出正确方向的纠偏,")
    print("  互补滤波只能靠陀螺积分维持, 且静止锚定会把基准朝读数方向拽。")

    # ---- 4) 与固件/文档宣称的 0..180 映射对照
    print("\n[4] 与文档宣称的映射对照")
    print("  固件输出角 = |wrap180(pitch - baseline)|, 是 1:1 的, 没有倍数缩放。")
    print("  固件/README 宣称: 合盖 angle≈0, 展开 angle≈180。")
    print("  推演: 该映射要求传感器俯仰变化 180 度, 而加速度计在 ±90 度以外折返,")
    print("        因此 180 度行程中有一半区间的加速度计纠偏方向是错的。")
    max_out = None
    # 数值验证: baseline 锁在 -90 (合盖), 装置转到 +90 (展开)
    base = pitch_from_accel(*accel_for_sensor_pitch(-90))
    best = pitch_from_accel(*accel_for_sensor_pitch(90))
    rel = abs(((best - base + 180.0) % 360.0) - 180.0)
    max_out = rel
    print(f"        数值验证: baseline(合盖, 真实-90) = {base:+.2f}, "
          f"俯仰真值 +90 时读数 = {best:+.2f}")
    print(f"        => 静态可达输出 = {rel:.2f} 度 "
          f"(需要陀螺积分把中间段撑过去, 否则到不了 180)")

    # ---- 5) 建议
    print("\n[5] 结论与建议")
    print("  a) 若机械行程 <= 90 度, 把基准放在行程中点, 输出 0..90 —— 与固件实现一致,")
    print("     全过程加速度计纠偏方向正确, 可稳定工作。")
    print("  b) 若确实需要 0..180 满量程, 必须改用能分辨 180 度的方案:")
    print("     - 把转轴装成让传感器绕其竖直轴转 (偏航), 或")
    print("     - 用两轴组合姿态 + 象限判定 (检查 az 符号) 来展开 atan2 的量程, 或")
    print("     - 接受「陀螺积分撑满程 + 只在端点校准」的方案, 并在文档中写明限制。")
    print("  c) 无论哪种, 都必须在实物上量出「屏幕角度 : 传感器俯仰」的真实比例再标定,")
    print("     这条已列入 tools/README_verify.md 的必须实物验证清单。")

    verdict_ok = fold is not None and fold <= 95
    print("\n" + "=" * 78)
    print(f"分析结论: 折返边界 = {fold} 度 (与 atan2 理论值 90 一致), "
          f"跨 180 度行程的静态可达输出 = {max_out:.2f} 度")
    print("与固件注释/README 的 0..180 宣称**部分不一致**: 数值范围合法,")
    print("但 180 度行程中有一半区间加速度计纠偏方向相反 —— 属设计约束,")
    print("需实物标定确认机械行程与安装比例后写进文档。")

    if args.json_out:
        payload = {
            "fold_boundary_deg": fold,
            "static_max_output_deg": max_out,
            "scan": [{"true_pitch": t, "accel_pitch": round(m, 4)} for t, m in rows],
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"已写出 JSON: {args.json_out}")

    return 0 if verdict_ok else 1


if __name__ == "__main__":
    sys.exit(main())
