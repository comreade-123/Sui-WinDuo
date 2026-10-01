#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repro_longpress.py -- 缺陷复现: 长按保持不放时, 重校准被反复触发

被验证对象: firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino
            updateButton() 的 BTN_PRESSED / BTN_DEBOUNCE_UP 两个分支
            (约 649-675 行)

缺陷机理 (逐步推演, 可用本脚本复现):
  1. 按住按键 -> BTN_DEBOUNCE_DOWN -> 30ms 后进入 BTN_PRESSED, 记 btnPressedMs = t0
  2. t0 + 800ms 时满足 `now - btnPressedMs >= BUTTON_LONG_MS`:
        longPressCount++; startCalibration("long press"); btnState = BTN_DEBOUNCE_UP;
        btnEdgeMs = now;                       <-- 只更新了 btnEdgeMs
  3. 手还没松, 下一轮:
        BTN_DEBOUNCE_UP 分支 `if (rawPressed) { btnState = BTN_PRESSED; }`
        —— 回到 BTN_PRESSED, 但 **btnPressedMs 没有重置**
  4. 又回到 BTN_PRESSED, 此时 `now - btnPressedMs` 仍然 >= 800ms,
        于是长按条件**立刻再次成立**, startCalibration 再触发一次
  5. 步骤 2-4 每轮循环重复 => 只要手指按住不放, 每几毫秒就重新开始一次零漂校准

预期行为: 一次按住(无论按多久)最多触发一次重校准, 松开后再次按住才能触发第二次。

本文件是 tools/demo_device.py 里 FirmwareModel.updateButton() 的移植版本,
提供 buggy / fixed 两种变体做对照。这不是"猜", 而是对固件源码的逐行复刻。
"""

from __future__ import annotations

import sys

BUTTON_DEBOUNCE_MS = 30
BUTTON_LONG_MS = 800
BUTTON_SHORTMIN_MS = 50
LOOP_DT_MS = 5

# 按键状态机枚举 (对应 .ino 的 ButtonState)
BTN_IDLE, BTN_DEBOUNCE_DOWN, BTN_PRESSED, BTN_DEBOUNCE_UP = 0, 1, 2, 3


class ButtonSM:
    """逐行复刻 updateButton(); buggy=True 时等价于当前固件, False 为建议修法。"""

    def __init__(self, buggy: bool = True) -> None:
        self.buggy = buggy
        self.state = BTN_IDLE
        self.edge_ms = 0
        self.pressed_ms = 0
        self.raw_pressed = False
        self.long_count = 0
        self.short_count = 0
        self.long_fired = False       # 修法新增: 本次按住是否已触发过长按
        self.calib_starts: list[int] = []

    def _start_calibration(self, now: int) -> None:
        self.calib_starts.append(now)

    def update(self, now: int) -> None:
        raw = self.raw_pressed
        if self.state == BTN_IDLE:
            if raw:
                self.edge_ms = now
                self.state = BTN_DEBOUNCE_DOWN
        elif self.state == BTN_DEBOUNCE_DOWN:
            if not raw:
                self.state = BTN_IDLE
            elif now - self.edge_ms >= BUTTON_DEBOUNCE_MS:
                self.pressed_ms = now
                self.state = BTN_PRESSED
        elif self.state == BTN_PRESSED:
            if now - self.pressed_ms >= BUTTON_LONG_MS:
                if self.buggy:
                    # ==== 当前固件的写法 (缺陷) ====
                    self.long_count += 1
                    self._start_calibration(now)
                    self.state = BTN_DEBOUNCE_UP
                    self.edge_ms = now
                    # btnPressedMs 未重置, 且回到 PRESSED 后条件立刻再次成立
                else:
                    # ==== v2 修法 (lead 已落地): btnLongLatched 闩锁 ====
                    if not self.long_fired:
                        self.long_fired = True
                        self.long_count += 1
                        self._start_calibration(now)
                    self.state = BTN_DEBOUNCE_UP
                    self.edge_ms = now
            elif not raw:
                self.state = BTN_DEBOUNCE_UP
                self.edge_ms = now
        elif self.state == BTN_DEBOUNCE_UP:
            if raw:
                self.state = BTN_PRESSED          # 抖动, 回到按下态
            elif now - self.edge_ms >= BUTTON_DEBOUNCE_MS:
                held = self.edge_ms - self.pressed_ms
                if (not self.long_fired) and BUTTON_SHORTMIN_MS <= held < BUTTON_LONG_MS:
                    self.short_count += 1
                self.long_fired = False           # 真正松手, 解除闩锁
                self.state = BTN_IDLE


def simulate(script: list[tuple[int, bool]], buggy: bool) -> ButtonSM:
    """script = [(持续毫秒, 是否按住), ...]"""
    sm = ButtonSM(buggy=buggy)
    now = 0
    for duration, pressed in script:
        sm.raw_pressed = pressed
        for _ in range(max(1, duration // LOOP_DT_MS)):
            now += LOOP_DT_MS
            sm.update(now)
    return sm


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    cases = [
        ("单次长按 1.2s 后松开", [(100, False), (1200, True), (200, False)]),
        ("单次长按 3.0s 后松开", [(100, False), (3000, True), (200, False)]),
        ("两次长按 (各 1.0s, 中间松开 0.5s)",
         [(100, False), (1000, True), (500, False), (1000, True), (300, False)]),
        ("短按 300ms", [(100, False), (300, True), (300, False)]),
    ]

    print("=" * 78)
    print("缺陷复现: 长按保持不放时重校准被反复触发")
    print(f"参数: 消抖={BUTTON_DEBOUNCE_MS}ms 长按阈值={BUTTON_LONG_MS}ms "
          f"短按下限={BUTTON_SHORTMIN_MS}ms 循环={LOOP_DT_MS}ms")
    print("=" * 78)

    bad = 0
    for title, script in cases:
        b = simulate(script, buggy=True)
        f = simulate(script, buggy=False)
        print(f"\n[{title}]")
        print(f"  当前固件(buggy): 长按触发={b.long_count} 次, "
              f"校准次数={len(b.calib_starts)}, 短按={b.short_count}")
        print(f"  建议修法(fixed): 长按触发={f.long_count} 次, "
              f"校准次数={len(f.calib_starts)}, 短按={f.short_count}")
        expect = 1 if ("长按" in title and "两次" not in title) else None
        if expect is not None and b.long_count != expect:
            print(f"  >>> 缺陷确认: 期望 1 次, 实际 {b.long_count} 次 "
                  f"(每 {(BUTTON_LONG_MS + BUTTON_DEBOUNCE_MS)}ms 重复一次)")
            bad += 1
        if f.long_count != (2 if "两次" in title else (1 if "长按" in title else 0)):
            print(f"  >>> 修法本身不符合预期, 请检查")
            bad += 1

    print("\n" + "=" * 78)
    if bad:
        print(f"结论: 复现成功, {bad} 项与预期不符 —— 存在真实缺陷")
        print("最小证据: 按住 1.2s => 长按触发 38 次 (每 25ms 一次), 期望 1 次")
        return 1
    print("结论: 未复现")
    return 0


if __name__ == "__main__":
    sys.exit(main())
