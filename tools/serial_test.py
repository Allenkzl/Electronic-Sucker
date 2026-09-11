#!/usr/bin/env python3
"""
串口自测工具 —— 不经过网页，直接和固件对话，用来验证接线和固件是否正常。

用法:
    python3 tools/serial_test.py                 # 只监听，看 READY / 状态上报
    python3 tools/serial_test.py STATUS PING     # 发指定命令
    python3 tools/serial_test.py --seq           # 跑一遍完整动作序列（吸取->放下->急停）
    python3 tools/serial_test.py --port /dev/cu.wchusbserial1110 STATUS
"""

from __future__ import annotations

import argparse
import glob
import sys
import time

try:
    import serial
except ImportError:
    print("缺少 pyserial，请先执行: python3 -m pip install pyserial", file=sys.stderr)
    raise SystemExit(2)

BAUD = 115200
BLOCKLIST = ("Bluetooth", "debug-console", "wlan-debug")


def find_port() -> str | None:
    found = []
    for pat in (
        "/dev/cu.wchusbserial*",
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
    ):
        for p in sorted(glob.glob(pat)):
            if not any(b in p for b in BLOCKLIST) and p not in found:
                found.append(p)
    for p in found:
        if "wchusbserial" in p or "usbmodem" in p:
            return p
    return found[0] if found else None


def drain(ser: serial.Serial, seconds: float, echo: bool = True) -> list[str]:
    """在 seconds 秒内收集所有设备输出行。"""
    lines: list[str] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        lines.append(text)
        if echo:
            print(f"  <- {text}")
    return lines


WATCHDOG_MARK = '"ev":"watchdog"'


def run_stress(ser: serial.Serial, rounds: int) -> int:
    """连续做 PICK/DROP，统计固件看门狗被误触发的次数。

    这是针对一个真实 bug 的回归测试：
    如果 loop() 在处理串口之前就取 millis()，它会早于 handleLine() 刚写入的
    lastCmdAt，无符号相减下溢成一个极大的数，于是看门狗在刚吸取的瞬间就把
    气泵关掉——表现为「点了吸取但马上自己松开」，且随机复现。
    注意必须精确匹配 "ev":"watchdog"：状态报文里还有一个 "watchdog":6000
    字段，用子串匹配会把它全部误判成触发。
    """
    for cmd in ("SET watchdog 6000", "SET drop_ms 400"):
        ser.write((cmd + "\n").encode("ascii"))
        ser.flush()
        time.sleep(0.25)

    hits = 0
    for i in range(rounds):
        ser.write(b"PICK 0\n")
        ser.flush()
        hits += sum(WATCHDOG_MARK in l for l in drain(ser, 0.12, echo=False))
        ser.write(b"DROP 400\n")
        ser.flush()
        hits += sum(WATCHDOG_MARK in l for l in drain(ser, 0.70, echo=False))
        if (i + 1) % 10 == 0:
            print(f"  ...{i + 1}/{rounds} 轮，累计误触发 {hits} 次")

    ser.write(b"STOP\n")
    ser.flush()
    drain(ser, 0.3, echo=False)

    if hits:
        print(f"❌ 压力测试 {rounds} 轮：看门狗误触发 {hits} 次")
        return 1
    print(f"✅ 压力测试 {rounds} 轮：看门狗误触发 0 次")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="电子吸盘串口自测")
    ap.add_argument("commands", nargs="*", help="要发送的命令，例如 STATUS / PICK / DROP / STOP")
    ap.add_argument("--port", help="串口设备路径，默认自动探测")
    ap.add_argument("--seq", action="store_true", help="跑一遍完整动作序列")
    ap.add_argument("--stress", type=int, default=0, metavar="N",
                    help="压力测试：连续 N 轮 PICK/DROP，统计看门狗误触发（回归测试）")
    ap.add_argument("--listen", type=float, default=1.5, help="每条命令后的监听秒数，默认 1.5")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("没有找到串口设备", file=sys.stderr)
        return 2

    print(f"打开 {port} @ {BAUD} ...")
    try:
        ser = serial.Serial(port, BAUD, timeout=0.3, write_timeout=2)
    except (serial.SerialException, OSError) as e:
        print(f"打开失败: {e}", file=sys.stderr)
        print("提示：Arduino IDE 的串口监视器如果开着会独占端口。", file=sys.stderr)
        return 2

    with ser:
        print("等待开发板复位并上报 READY ...")
        boot = drain(ser, 4.0)
        if not any("READY" in l for l in boot):
            print("!! 没有收到 READY —— 固件可能没跑起来，或串口被占用", file=sys.stderr)

        if args.stress:
            return run_stress(ser, args.stress)

        if args.seq:
            plan = [
                ("PING", 1.0),
                ("SET pick_ms 0", 0.5),
                ("SET drop_ms 800", 0.5),
                ("STATUS", 0.8),
                ("PICK", 1.5),
                ("STATUS", 0.8),
                ("DROP", 1.8),
                ("STATUS", 0.8),
                ("STOP", 0.8),
            ]
        elif args.commands:
            plan = [(c, args.listen) for c in args.commands]
        else:
            plan = [("STATUS", args.listen)]

        for cmd, wait in plan:
            print(f"  -> {cmd}")
            ser.write((cmd + "\n").encode("ascii"))
            ser.flush()
            drain(ser, wait)

    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
