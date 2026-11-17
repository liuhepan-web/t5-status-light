#!/usr/bin/env python3
"""
冷启动复验：复位板子，抓整段日志，回答三个问题
  1) 起来的是新固件吗（含 auto speak 逻辑）
  2) 开机有没有出声（SL_SPEAK_ON_BOOT=0，应该没有）
  3) 轮询有没有正常拿到状态（真实数据源）
"""
import re
import subprocess
import threading
import time

import serial

LOG_PORT = "/dev/cu.usbmodem56D70349163"
DL_PORT = "/dev/cu.usbmodem56D70349161"
BAUD = 460800

lines = []
stop = threading.Event()


def reader():
    s = serial.Serial(LOG_PORT, BAUD, timeout=0.2)
    buf = b""
    while not stop.is_set():
        try:
            chunk = s.read(4096)
        except Exception:
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            t = ln.decode("utf-8", "replace").strip()
            if t:
                lines.append(t)
    s.close()


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(1.5)

print(">>> 复位板子 ...")
subprocess.run(
    ["/Users/fire/TuyaOpen/tools/tyutool/tyutool_cli", "--plain",
     "reset", "-d", "t5ai", "-p", DL_PORT],
    capture_output=True, text=True, timeout=30,
)

time.sleep(70)
stop.set()
time.sleep(0.4)

joined = "\n".join(lines)

print("\n===== 板上日志（只挑 sl: / ui: / 关键行）=====")
for l in lines:
    if re.search(r"\[sl|sl:|ui:|app_status_light", l):
        print("  " + l)

print("\n===== 判定 =====")

booted = bool(re.search(r"sl: status light init done", joined))
print("① 新固件起来了：", "✓ 看到 'sl: status light init done'" if booted
      else "? 没抓到 init 行（可能复位没生效或窗口太短）")

spoke_on_boot = bool(re.search(
    r"sl: ticket change -> auto speak|sl: speak state=.*url=", joined))
print("② 开机出声：", "✗ 出声了（不该有）" if spoke_on_boot else "✓ 没出声")

manual = bool(re.search(r"sl: manual speak requested", joined))
print("   （顺带确认没有误触发手动播报：", "✗ 有" if manual else "✓ 无", "）")

states = re.findall(r"sl: state \d+ -> \d+ \((p0=\d+ p1=\d+)\)", joined)
print("③ 状态跳变：", states if states else "(窗口内无跳变 —— 真实状态未变，属正常)")

http_fail = len(re.findall(r"sl: http request failed", joined))
print("④ HTTP 失败次数：", http_fail,
      "（开机瞬间 Wi-Fi 未起会有 1~2 次，属正常）" if http_fail <= 2
      else "← 偏多，要看网络")
