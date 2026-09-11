#!/usr/bin/env python3
"""
电子吸盘控制器 —— 串口桥接服务

   浏览器网页  <--HTTP/SSE-->  本服务  <--串口 115200-->  Arduino Uno

只依赖 Python 标准库 + pyserial，不需要 npm、不需要编译。

用法:
    python3 bridge/bridge.py                  # 自动找串口，监听 127.0.0.1:8787
    python3 bridge/bridge.py --list           # 只列出候选串口
    python3 bridge/bridge.py --port /dev/cu.wchusbserial1110
    python3 bridge/bridge.py --http-port 8787 --open

HTTP 接口（只绑定 127.0.0.1，局域网访问不到）:
    GET  /                      控制网页
    GET  /api/status            串口 + 设备实时状态
    GET  /api/events            SSE 实时事件流
    POST /api/pick              {"pick_ms": 0}      吸盘开，0 = 吸到点「放下」为止
    POST /api/drop              {"drop_ms": 800}    吸盘关 + 电磁阀脉冲放料
    POST /api/stop              两路全关（急停）
    POST /api/settings          {"pick_ms":0,"drop_ms":800,"watchdog":15000}
    POST /api/mode              {"mode": "servo" | "digital"}
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import signal
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    print("缺少 pyserial，请先执行: python3 -m pip install pyserial", file=sys.stderr)
    raise SystemExit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
SETTINGS_FILE = os.path.join(ROOT, "bridge", "settings.json")

BAUD = 115200
HEARTBEAT_SEC = 1.5        # 心跳间隔；必须远小于看门狗超时，否则会误触发
MIN_WATCHDOG_MS = 6000     # 看门狗安全下限：至少是心跳间隔的 4 倍，留足余量
READY_TIMEOUT_SEC = 4.0
RECONNECT_DELAY_SEC = 2.0
MAX_LOG = 60

DEFAULT_SETTINGS = {
    "pick_ms": 0,       # 0 = 一直吸到点「放下」
    "drop_ms": 800,     # 与厂家参考代码一致
    "watchdog": 15000,  # 毫秒；0 = 关闭
    "mode": "servo",    # servo | digital
}


def clamp_watchdog(value) -> int:
    """把看门狗限制在安全区间：0 表示关闭，否则不得低于 MIN_WATCHDOG_MS。

    看门狗必须显著大于心跳间隔，否则桥接服务正常工作时也会被误判为失联，
    导致气泵被莫名其妙地关掉。
    """
    v = max(0, int(value))
    if v == 0:
        return 0
    return max(MIN_WATCHDOG_MS, v)

PORT_PATTERNS = [
    "/dev/cu.wchusbserial*",   # CH340（本机这块 Uno 兼容板）
    "/dev/cu.usbmodem*",       # 原厂 Uno / 其他 USB CDC
    "/dev/cu.usbserial*",      # FTDI
    "/dev/ttyACM*",
    "/dev/ttyUSB*",
]
PORT_BLOCKLIST = ("Bluetooth", "debug-console", "wlan-debug")


# ============================== 串口桥 ==============================

class SerialBridge:
    """串口连接 + 状态管理 + 事件广播。线程安全。"""

    def __init__(self, port: str | None):
        self.forced_port = port
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        self.subscribers: set[queue.Queue] = set()
        self.subscribers_lock = threading.Lock()
        self.log: deque = deque(maxlen=MAX_LOG)
        self.settings = dict(DEFAULT_SETTINGS)
        self._load_settings()

        self.state = {
            "port": port,
            "connected": False,
            "ready": False,
            "pump": 0,
            "valve": 0,
            "last_error": None,
            "last_seen": 0.0,
            "server_time": time.time(),
        }
        self.state.update(self.settings)
        self._stop = threading.Event()

    # ---------------- 事件广播 ----------------

    def emit(self, kind: str, text: str, level: str = "info"):
        ev = {"t": time.time(), "kind": kind, "level": level, "text": text}
        self.log.append(ev)
        payload = json.dumps({"type": "log", "event": ev}, ensure_ascii=False)
        with self.subscribers_lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def broadcast_state(self):
        payload = json.dumps({"type": "state", "state": self.snapshot()}, ensure_ascii=False)
        with self.subscribers_lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self.subscribers_lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self.subscribers_lock:
            self.subscribers.discard(q)

    # ---------------- 设置持久化 ----------------

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in DEFAULT_SETTINGS:
                if k in saved:
                    self.settings[k] = saved[k]
        except (OSError, ValueError):
            pass
        self.settings["watchdog"] = clamp_watchdog(self.settings["watchdog"])

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.emit("settings", f"设置保存失败: {e}", "warn")

    def snapshot(self) -> dict:
        s = dict(self.state)
        s.update(self.settings)
        s["server_time"] = time.time()
        s["stale"] = bool(
            s["connected"] and s["last_seen"] and (time.time() - s["last_seen"] > 5)
        )
        return s

    # ---------------- 串口收发 ----------------

    def _send(self, line: str) -> bool:
        with self.lock:
            if not self.ser or not self.ser.is_open:
                return False
            try:
                self.ser.write((line + "\n").encode("ascii", "ignore"))
                self.ser.flush()
                return True
            except (serial.SerialException, OSError) as e:
                self.state["last_error"] = f"写入失败: {e}"
                self.emit("serial", f"写入失败: {e}", "error")
                return False

    def command(self, line: str) -> bool:
        ok = self._send(line)
        if not ok:
            self.emit("cmd", f"设备未连接，命令未发出: {line}", "warn")
        return ok

    def _push_settings(self):
        for k in ("pick_ms", "drop_ms", "watchdog"):
            self._send(f"SET {k} {int(self.settings[k])}")
            time.sleep(0.05)
        self._send(f"MODE {self.settings['mode']}")

    def _close(self, reason: str):
        with self.lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
        if self.state["connected"]:
            self.emit("serial", f"串口已断开: {reason}", "warn")
        self.state.update(connected=False, ready=False, pump=0, valve=0)
        self.broadcast_state()

    def run(self):
        """串口主循环：连接 -> 读行 -> 心跳 -> 断线重连。"""
        while not self._stop.is_set():
            port = self.forced_port or find_port()

            if not port:
                msg = "没有找到可用的串口（请确认开发板已插好）"
                if self.state["last_error"] != msg:
                    self.emit("serial", msg, "error")
                self.state.update(connected=False, ready=False, last_error=msg, port=None)
                self.broadcast_state()
                self._stop.wait(RECONNECT_DELAY_SEC)
                continue

            try:
                ser = serial.Serial(port, BAUD, timeout=0.2, write_timeout=2)
            except (serial.SerialException, OSError) as e:
                hint = ""
                if "busy" in str(e).lower() or "permission" in str(e).lower():
                    hint = "（检查 Arduino IDE 的串口监视器是否还开着，它会独占端口）"
                msg = f"打开 {port} 失败: {e}{hint}"
                if self.state["last_error"] != msg:
                    self.emit("serial", msg, "error")
                self.state.update(connected=False, ready=False, last_error=msg, port=port)
                self.broadcast_state()
                self._stop.wait(RECONNECT_DELAY_SEC)
                continue

            with self.lock:
                self.ser = ser
            self.state.update(port=port, connected=True, ready=False, last_error=None)
            self.emit("serial", f"已打开串口 {port} @ {BAUD}")
            self.broadcast_state()

            # 握手：原厂 Uno 打开串口会因 DTR 复位并打印 READY；
            # 但本机这块 CH340 板不复位，所以补发一个 PING，认 PONG 也算就绪。
            deadline = time.time() + READY_TIMEOUT_SEC
            try:
                ser.write(b"PING\n")
                ser.flush()
            except (serial.SerialException, OSError):
                pass
            while time.time() < deadline and not self._stop.is_set():
                try:
                    raw = ser.readline()
                except (serial.SerialException, OSError):
                    raw = b""
                if raw:
                    text = raw.decode("utf-8", "replace").strip()
                    self.state["last_seen"] = time.time()
                    if text in ("READY", "PONG"):
                        self.state["ready"] = True
                        self.emit("serial", f"开发板就绪（握手 {text}）")
                        self.broadcast_state()
                        break
                    if text:
                        self._handle_line(text)
                time.sleep(0.05)

            if not self.state["ready"]:
                self.emit("serial", "握手超时：未收到 READY/PONG，仍继续尝试通信", "warn")

            # 把界面上保存的参数同步给固件
            self._push_settings()
            self._send("STATUS")

            last_hb = time.time()
            while not self._stop.is_set():
                try:
                    raw = ser.readline()
                except (serial.SerialException, OSError) as e:
                    self._close(f"读取异常: {e}")
                    break

                if raw:
                    self.state["last_seen"] = time.time()
                    self._handle_line(raw.decode("utf-8", "replace").strip())

                if time.time() - last_hb >= HEARTBEAT_SEC:
                    if not self._send("PING"):
                        self._close("心跳失败")
                        break
                    last_hb = time.time()

                if not ser.is_open:
                    self._close("端口被关闭")
                    break

            self._stop.wait(RECONNECT_DELAY_SEC)

        # 退出前断电
        self._send("STOP")
        self._close("服务退出")

    def _handle_line(self, text: str):
        if not text:
            return
        if text == "PONG":
            return
        if text == "READY":
            self.state["ready"] = True
            self.broadcast_state()
            return
        if not text.startswith("{"):
            self.emit("device", text)
            return

        try:
            msg = json.loads(text)
        except ValueError:
            self.emit("device", text)
            return

        if msg.get("ev") == "state":
            self.state.update(
                pump=int(msg.get("pump", 0)),
                valve=int(msg.get("valve", 0)),
                pick_ms=msg.get("pick_ms", self.settings["pick_ms"]),
                drop_ms=msg.get("drop_ms", self.settings["drop_ms"]),
                watchdog=msg.get("watchdog", self.settings["watchdog"]),
                mode=msg.get("mode", self.settings["mode"]),
            )
            self.settings.update(
                pick_ms=self.state["pick_ms"],
                drop_ms=self.state["drop_ms"],
                watchdog=self.state["watchdog"],
                mode=self.state["mode"],
            )
            self.broadcast_state()
        elif msg.get("ev") == "watchdog":
            self.emit("device", "看门狗触发：长时间无命令，已自动关闭两路输出", "warn")
        elif msg.get("ev") == "boot":
            self.emit("device", f"固件启动 {msg.get('fw')}（吸盘 {msg.get('pump_pin')} / 电磁阀 {msg.get('valve_pin')}）")
        elif "ok" in msg:
            self.emit("device", f"设备确认 {msg['ok']}")
        elif "err" in msg:
            self.emit("device", f"设备报错: {msg['err']}", "error")


# ============================== 串口发现 ==============================

def candidate_ports() -> list[str]:
    found: list[str] = []
    for pat in PORT_PATTERNS:
        for p in sorted(glob.glob(pat)):
            if any(b in p for b in PORT_BLOCKLIST):
                continue
            if p not in found:
                found.append(p)
    # 再拿 pyserial 的枚举结果补一遍（带 VID:PID 描述）
    for info in list_ports.comports():
        p = info.device
        if any(b in p for b in PORT_BLOCKLIST):
            continue
        if p not in found:
            found.append(p)
    return found


def find_port() -> str | None:
    ports = candidate_ports()
    for p in ports:
        if "wchusbserial" in p or "usbmodem" in p:
            return p
    return ports[0] if ports else None


# ============================== HTTP 服务 ==============================

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def make_handler(bridge: SerialBridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SuckerBridge/1.0"
        protocol_version = "HTTP/1.1"

        # ---------- 工具 ----------
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def log_message(self, fmt, *args):
            pass  # 静音，避免刷屏

        # ---------- GET ----------
        def do_GET(self):
            path = self.path.split("?", 1)[0]

            if path == "/api/status":
                return self._json(bridge.snapshot())

            if path == "/api/log":
                return self._json({"events": list(bridge.log)})

            if path == "/api/events":
                return self._sse()

            return self._static(path)

        def _sse(self):
            q = bridge.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                hello = json.dumps(
                    {"type": "state", "state": bridge.snapshot()}, ensure_ascii=False
                )
                self.wfile.write(f"data: {hello}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        payload = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                bridge.unsubscribe(q)

        def _static(self, path):
            if path in ("/", ""):
                path = "/index.html"
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith("..") or os.path.isabs(rel):
                return self._json({"error": "bad path"}, 400)
            full = os.path.join(WEB_DIR, rel)
            if not os.path.isfile(full):
                return self._json({"error": "not found", "path": rel}, 404)
            ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ---------- POST ----------
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            body = self._read_json()

            if path == "/api/pick":
                ms = int(body.get("pick_ms", bridge.settings["pick_ms"]))
                ok = bridge.command(f"PICK {ms}")
                bridge.settings["pick_ms"] = ms
                bridge._save_settings()
                bridge.emit("cmd", "点击「吸取」" + ("（保持到放下）" if ms == 0 else f"（{ms} ms）"))
                return self._json({"ok": ok, "pick_ms": ms})

            if path == "/api/drop":
                ms = int(body.get("drop_ms", bridge.settings["drop_ms"]))
                ok = bridge.command(f"DROP {ms}")
                bridge.settings["drop_ms"] = ms
                bridge._save_settings()
                bridge.emit("cmd", f"点击「放下」（放气脉冲 {ms} ms）")
                return self._json({"ok": ok, "drop_ms": ms})

            if path == "/api/stop":
                ok = bridge.command("STOP")
                bridge.emit("cmd", "急停：两路全关", "warn")
                return self._json({"ok": ok})

            if path == "/api/settings":
                changed = []
                for key in ("pick_ms", "drop_ms", "watchdog"):
                    if key in body:
                        v = max(0, int(body[key]))
                        if key == "watchdog":
                            safe = clamp_watchdog(v)
                            if safe != v:
                                bridge.emit(
                                    "cmd",
                                    f"看门狗 {v} ms 太短（心跳间隔 {int(HEARTBEAT_SEC*1000)} ms），"
                                    f"已自动抬到安全下限 {safe} ms",
                                    "warn",
                                )
                                v = safe
                        bridge.settings[key] = v
                        bridge.command(f"SET {key} {v}")
                        changed.append(f"{key}={v}")
                if "mode" in body and body["mode"] in ("servo", "digital"):
                    bridge.settings["mode"] = body["mode"]
                    bridge.command(f"MODE {body['mode']}")
                    changed.append(f"mode={body['mode']}")
                bridge._save_settings()
                if changed:
                    bridge.emit("cmd", "更新参数：" + ", ".join(changed))
                return self._json({"ok": True, "settings": bridge.settings})

            if path == "/api/mode":
                mode = body.get("mode")
                if mode not in ("servo", "digital"):
                    return self._json({"error": "mode must be servo|digital"}, 400)
                ok = bridge.command(f"MODE {mode}")
                bridge.settings["mode"] = mode
                bridge._save_settings()
                bridge.emit("cmd", f"输出模式切换为 {mode}")
                return self._json({"ok": ok, "mode": mode})

            return self._json({"error": "not found"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="电子吸盘串口桥接服务")
    ap.add_argument("--port", help="指定串口设备，例如 /dev/cu.wchusbserial1110")
    ap.add_argument("--http-port", type=int, default=8787, help="网页端口，默认 8787")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址，默认仅本机")
    ap.add_argument("--list", action="store_true", help="列出候选串口后退出")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    if args.list:
        ports = candidate_ports()
        if not ports:
            print("没有发现串口设备")
        for p in ports:
            mark = "  <-- 将使用" if p == (args.port or find_port()) else ""
            print(f"  {p}{mark}")
        return 0

    if not os.path.isdir(WEB_DIR):
        print(f"找不到网页目录: {WEB_DIR}", file=sys.stderr)
        return 2

    bridge = SerialBridge(args.port)
    threading.Thread(target=bridge.run, name="serial", daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.http_port), make_handler(bridge))
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.http_port}/"

    print("=" * 62)
    print("  电子吸盘控制器")
    print(f"  网页地址 : {url}")
    print(f"  串口     : {args.port or '自动探测'}")
    print("  停止服务 : Ctrl+C")
    print("=" * 62)

    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def shutdown(signum, frame):
        print("\n正在停止，并发送 STOP 关闭两路输出 ...")
        bridge._stop.set()
        bridge.command("STOP")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown(None, None)
    finally:
        time.sleep(0.2)
        bridge._send("STOP")
        print("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
