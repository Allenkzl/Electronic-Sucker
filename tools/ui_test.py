#!/usr/bin/env python3
"""
浏览器端到端测试 —— 用 Chrome DevTools Protocol 真实加载网页、真实点击按钮，
再回读设备状态，验证「网页 -> 桥接服务 -> 串口 -> 固件」整条链路。

前提：桥接服务已在运行（python3 bridge/bridge.py）。

用法:
    python3 tools/ui_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import websocket
except ImportError:
    print("需要 websocket-client: python3 -m pip install websocket-client", file=sys.stderr)
    raise SystemExit(2)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "http://127.0.0.1:8787/"
PROFILE = os.path.join(ROOT, ".chrome-uitest")
CRASH = os.path.join(ROOT, ".chrome-crash")
PORT = 9333

PASS, FAIL = "  ✅", "  ❌"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{PASS if ok else FAIL} {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=15)
        self.n = 0
        self.exceptions: list[str] = []
        self.call("Runtime.enable")
        self.call("Log.enable")

    def call(self, method: str, params: dict | None = None):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                break
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.exceptions.append(
                    d.get("exception", {}).get("description") or d.get("text", "unknown")
                )
            elif msg.get("method") == "Log.entryAdded":
                e = msg["params"]["entry"]
                if e.get("level") == "error":
                    self.exceptions.append(f"[{e.get('source')}] {e.get('text')}")
            if msg.get("id") == mid:
                return msg
        return {}

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        })
        res = r.get("result", {}).get("result", {})
        if r.get("result", {}).get("exceptionDetails"):
            return None
        return res.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def api_status() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8787/api/status", timeout=5) as r:
        return json.load(r)


def wait_for_page(port: int) -> str | None:
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                targets = json.load(r)
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return None


def main() -> int:
    try:
        api_status()
    except Exception as e:
        print(f"桥接服务未运行: {e}", file=sys.stderr)
        return 2

    subprocess.run(["pkill", "-f", PROFILE], capture_output=True)
    time.sleep(0.5)

    chrome = subprocess.Popen(
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--no-sandbox", "--disable-breakpad",
            "--disable-crash-reporter", f"--crash-dumps-dir={CRASH}",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}",
            "--window-size=1000,1500", PAGE,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        ws_url = wait_for_page(PORT)
        if not ws_url:
            print("无法连接到 Chrome 调试端口", file=sys.stderr)
            return 2

        cdp = CDP(ws_url)
        print("\n== 1. 页面加载与界面状态 ==")

        # 等前端把状态渲染出来（最多 12 秒）
        foot = ""
        for _ in range(24):
            foot = cdp.eval("document.getElementById('foot').textContent") or ""
            if "已连接" in foot or "等待" in foot:
                break
            time.sleep(0.5)

        check("页脚显示串口已连接", "已连接" in foot, foot)
        check("串口状态灯点亮", "on" in (cdp.eval("document.getElementById('pillSer').className") or ""))
        check("设备状态灯点亮", "on" in (cdp.eval("document.getElementById('pillDev').className") or ""))
        check("「吸取」按钮可用", cdp.eval("!document.getElementById('btnPick').disabled"))
        check("「放下」按钮可用", cdp.eval("!document.getElementById('btnDrop').disabled"))
        check("「急停」按钮可用", cdp.eval("!document.getElementById('btnStop').disabled"))

        print("\n== 2. 点击「吸取」 ==")
        cdp.eval("document.getElementById('btnPick').click()")
        time.sleep(1.0)
        s = api_status()
        check("设备吸盘已开启", s["pump"] == 1, f"pump={s['pump']}")
        check("界面吸盘灯变为「吸住中」",
              cdp.eval("document.getElementById('stPump').textContent") == "吸住中")
        check("吸取按钮进入激活态",
              "live" in (cdp.eval("document.getElementById('btnPick').className") or ""))

        print("\n== 3. 点击「放下」 ==")
        cdp.eval("document.getElementById('btnDrop').click()")
        time.sleep(0.35)
        s = api_status()
        check("吸盘已停止", s["pump"] == 0, f"pump={s['pump']}")
        check("电磁阀正在放气", s["valve"] == 1, f"valve={s['valve']}")
        check("界面电磁阀灯变为「放气中」",
              cdp.eval("document.getElementById('stValve').textContent") == "放气中")

        time.sleep(1.2)
        s = api_status()
        check("放气脉冲结束后电磁阀自动关闭", s["valve"] == 0, f"valve={s['valve']}")

        print("\n== 4. 快捷键与急停 ==")
        cdp.eval("document.getElementById('btnPick').click()")
        time.sleep(0.8)
        check("再次吸取成功", api_status()["pump"] == 1)
        cdp.eval("""
          document.dispatchEvent(new KeyboardEvent('keydown',
            {code:'Escape', bubbles:true, cancelable:true}));
        """)
        time.sleep(0.8)
        s = api_status()
        check("Esc 快捷键触发急停", s["pump"] == 0 and s["valve"] == 0,
              f"pump={s['pump']} valve={s['valve']}")

        print("\n== 5. 参数下发 ==")
        cdp.eval("document.getElementById('inDropMs').value = '600';"
                 "document.getElementById('inDropMs').dispatchEvent(new Event('change'))")
        time.sleep(1.4)
        check("放气时长已下发到设备", api_status()["drop_ms"] == 600,
              f"drop_ms={api_status()['drop_ms']}")
        cdp.eval("document.getElementById('inDropMs').value = '800';"
                 "document.getElementById('inDropMs').dispatchEvent(new Event('change'))")
        time.sleep(1.4)

        print("\n== 6. 运行日志 ==")
        log_txt = cdp.eval("document.getElementById('log').innerText") or ""
        check("页面已显示运行日志", "点击「吸取」" in log_txt or "点击「放下」" in log_txt,
              f"{len(log_txt.splitlines())} 行")

        print("\n== 7. 前端异常 ==")
        check("页面无 JS 异常 / 控制台错误", not cdp.exceptions,
              "；".join(cdp.exceptions[:3]) if cdp.exceptions else "无")

        cdp.eval("fetch('/api/stop',{method:'POST'})")
        cdp.close()
    finally:
        chrome.terminate()
        time.sleep(0.4)
        subprocess.run(["pkill", "-f", PROFILE], capture_output=True)

    print("\n" + "=" * 46)
    if failures:
        print(f"结果: {len(failures)} 项失败 -> {failures}")
        return 1
    print("结果: 全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
