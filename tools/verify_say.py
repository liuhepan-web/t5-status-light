#!/usr/bin/env python3
"""verify_say.py —— 「点击屏幕语音提示」端到端复验（板子在跑、bridge 在跑时用）

用法：
    /Users/fire/TuyaOpen/.venv/bin/python tools/verify_say.py [CLI口] [日志口]

    # 不传参数就自动探测 /dev/cu.usbmodem*
    # 本机只有 /Users/fire/TuyaOpen/.venv/bin/python 带 pyserial

原理：两个串口**同时**开着 ——
    CLI 口  = 下载口 = uart0 @115200   （发 `say`，等同点击屏幕）
    日志口  =        uart1 @460800     （只看，不写）

它替你做两件容易搞错的事：
  1. CLI 口打开后立刻 `dtr=False; rts=False` —— 不松的话那一下电平抖动会把板子
     复位甚至摁进下载模式，表现为「命令发出去完全没回显」。
  2. 按**四行日志**判定成功，而不是只看有没有报错：
        sl: manual speak requested (tap/say)
        sl: speak state=.. url=..
        url sink file size N
        ai player FG eof          ← 必须是几秒之后，0.1 秒就 eof = 假播完

注：这个脚本验的是**手动**那条路。固件里自动播报（工单状态跳变就出声）走的是
同一个 sl_speak 线程，日志是 `sl: ticket change -> auto speak state=..`，
要单独验它得让数据真的变一次 —— 见 tools/watch_auto_speak.py。
"""
import glob
import re
import sys
import threading
import time

try:
    import serial
except ImportError:
    sys.exit(
        "缺少 pyserial。用带它的解释器跑，例如：\n"
        "  /Users/fire/TuyaOpen/.venv/bin/python tools/verify_say.py"
    )


def pick_ports():
    """探测下载口(CLI) / 日志口。两个都可能被串口助手抢占，拿不到就报清楚。"""
    devs = sorted(glob.glob("/dev/cu.usbmodem*"))
    if len(devs) < 2:
        sys.exit(
            f"只找到 {len(devs)} 个 /dev/cu.usbmodem* —— 板子插好了吗？"
            "（需要两个：下载口 + 日志口）\n"
            + "\n".join("  " + d for d in devs)
        )
    return devs[0], devs[1]
    # 注：按编号排序只是启发式（本机下载口 ...161 < 日志口 ...163 刚好成立）。
    # 不成立时直接在命令行把两个口按 [CLI口] [日志口] 的顺序传进来。


log_lines = []
stop = threading.Event()


def log_reader(port):
    """日志口只读。注意**不要**去动 DTR/RTS，碰了会把板子复位。"""
    try:
        s = serial.Serial(port, 460800, timeout=0.2)
    except Exception as e:
        print(f"!! 打开日志口 {port} 失败: {e}")
        return
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
            line, buf = buf.split(b"\n", 1)
            txt = line.decode("utf-8", "replace").strip()
            if txt:
                log_lines.append(txt)
                print(f"[LOG] {txt}", flush=True)
    try:
        s.close()
    except Exception:
        pass


# key -> 正则；顺序即判定顺序
# top 用「手动触发」那一行做锚点 —— 它是 app_status_light_tap() 里打的，
# 比线程里的 `sl: speak state=` 更靠近触发点。兼容旧固件的 `sl: tap -> speak`。
EXPECTED = (
    ("tap",      r"sl: manual speak requested|sl: tap -> speak"),
    ("filesize", r"url sink file size"),
    ("error2",   r"datasink read error: -2"),
    ("stuck",    r"read failed at offset"),
    ("eof",      r"ai player FG eof"),
)


def main():
    args = sys.argv[1:3]
    auto_cli, auto_log = pick_ports()
    cli_port = args[0] if len(args) > 0 else auto_cli
    log_port = args[1] if len(args) > 1 else auto_log

    print(f"CLI 口 : {cli_port} @115200")
    print(f"日志口 : {log_port} @460800")
    print("（提醒：先确认固件里的 bridge IP 和 `ipconfig getifaddr en0` 一致）\n")

    threading.Thread(target=log_reader, args=(log_port,), daemon=True).start()
    time.sleep(0.5)

    c = serial.Serial(cli_port, 115200, timeout=0.2)
    c.dtr = False          # ★ 这两行是 CLI 能不能回话的分水岭
    c.rts = False
    time.sleep(0.3)
    c.reset_input_buffer()

    c.write(b"\r\n")
    time.sleep(0.4)
    c.write(b"help\r\n")
    time.sleep(1.5)

    print("== 发 say ==")
    mark = len(log_lines)
    c.write(b"say\r\n")

    t0 = time.time()
    seen = set()
    while time.time() - t0 < 14:
        time.sleep(0.25)
        joined = "\n".join(log_lines[mark:])
        for key, pat in EXPECTED:
            if key not in seen and re.search(pat, joined):
                seen.add(key)
                print(f"  + {key} @ {time.time()-t0:.1f}s")
        if "eof" in seen:
            break

    c.close()
    stop.set()
    time.sleep(0.4)

    print("\n========== 判定 ==========")
    if {"tap", "filesize", "eof"} <= seen and not (seen & {"error2", "stuck"}):
        print("✓ 通过：tap -> url sink file size -> eof，无 -2、无 4096 死锁")
        print("  （是否真的出声仍需人耳确认 —— 这里只能验到「音频流走完了」）")
        return 0
    if "error2" in seen:
        print("✗ 失败：datasink read error: -2")
        print("  → 走了 SRC_MEM（该路径在本固件下坏）。改用 ai_audio_play_tts_url()。")
        return 1
    if "stuck" in seen:
        print("✗ 失败：read failed at offset ...")
        print("  → 多半是 WAV 的 data 偏移 > 4096（FLLR 填充块）。量一下：")
        print("    python3 -c \"print(open('x.wav','rb').read().find(b'data'))\"")
        print("    应为 ~36；4088 就是没洗头。跑 assets/make_audio.sh 重生成。")
        return 1
    if "tap" not in seen:
        print("? 没抓到 tap —— 检查串口是否连错、或固件里没注册 say 命令（敲 help 看看）")
        return 2
    print(f"? 没走到 eof，实际见到：{sorted(seen)}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
