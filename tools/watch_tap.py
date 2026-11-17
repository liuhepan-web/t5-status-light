#!/usr/bin/env python3
"""watch_tap.py —— 「用手指点屏幕」的实机验证（唯一需要人参与的一步）

用法：
    /Users/fire/TuyaOpen/.venv/bin/python tools/watch_tap.py [秒数]

它做的事：盯住日志口，同时实时刷屏告诉你还剩几秒、有没有收到点击。
你只要站在板子前面，等提示出现后**用手指点一下屏幕**就行。

为什么非要手指点：
  `say` 命令只验证了 app_status_light_tap() 之后的半条链路（播报）。
  它**验证不到**触摸这一段：
      GT1151QM 芯片 -> I2C -> LVGL indev -> LV_EVENT_CLICKED -> __on_screen_tap()
  这一段任何一环断了，`say` 照样能响，但手点没反应。

已知的两个"看着像坏了其实没事"的信号，先说明白免得白查：
  1. 启动日志 `tdd_tp_gt1151.c:60  Tp Product id: 0xffffff3f`
     —— 正常也不是 0x38353131。SDK 里 `GT1151_PRODUCT_ID_CODE` 定义了**但从没被用**，
        驱动程序压根不校验，只是 PR_DEBUG 打一下。它**不能**用来判断触摸好坏。
  2. 启动日志 `check i2c pin whether used by other function.`
     —— 只是 I2C pinmux 的信息性提示，不是错误。
"""
import re
import sys
import threading
import time

try:
    import serial
except ImportError:
    sys.exit("缺少 pyserial。用 /Users/fire/TuyaOpen/.venv/bin/python 跑。")

LOG_PORT = "/dev/cu.usbmodem56D70349163"

hits = []
stop = threading.Event()


def reader():
    s = serial.Serial(LOG_PORT, 460800, timeout=0.2)
    buf = b""
    while not stop.is_set():
        try:
            c = s.read(4096)
        except Exception:
            break
        if not c:
            continue
        buf += c
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            t = ln.decode("utf-8", "replace").strip()
            if not t:
                continue
            # 只关心和"点击"有关的行。
            # 锚点是 app_status_light_tap() 里那句 `sl: manual speak requested`
            # —— 它就在手动触发点上，自动播报（状态跳变）也会打 `sl: speak state=`，
            # 但那不含 "manual"，所以不会把自动播报误判成手指点击。
            if re.search(r"sl: manual speak requested|sl: speak state=|"
                         r"url sink file size|player tts event|FG eof", t):
                hits.append(t)
                print("\n  ✓ " + t, flush=True)
    s.close()


def main():
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.6)

    print("=" * 62)
    print(" 实机验证：用手指点屏幕")
    print("=" * 62)
    print(" 现在请用手指**点一下屏幕任意位置**。")
    print(" 提示：点文字上也应该有效（代码显式打开了 EVENT_BUBBLE）。")
    print("=" * 62, flush=True)

    t0 = time.time()
    last = -1
    while time.time() - t0 < dur:
        left = int(dur - (time.time() - t0))
        if left != last:
            last = left
            print(f"  还剩 {left:2d} 秒 ... 已收到 {len(hits)} 条点击事件",
                  end="\r", flush=True)
        time.sleep(0.2)
    print()
    stop.set()
    time.sleep(0.4)

    print("\n========== 结果 ==========")
    tapped = [h for h in hits if "sl: manual speak requested" in h]
    if tapped:
        print("✓ 手指点击生效 —— 触摸链路(GT1151 -> indev -> CLICKED -> tap)是通的")
        for h in tapped:
            print("   " + h)
        if any("FG eof" in h for h in hits):
            print("✓ 并且音频流走完了（是否真出声仍需耳朵确认）")
        return 0
    if any("sl: speak state=" in h for h in hits):
        print("? 听到了播报，但**没有** `sl: manual speak requested` ——")
        print("  说明那是工单状态跳变触发的**自动**播报，不是你的手指。")
        print("  请再点一次；若始终没有 manual 行，问题就在触摸这一段。")
        return 2
    print("✗ 没收到任何点击事件。按下面顺序查（从最常见开始）：")
    print("  1. 确定点的是**屏幕**不是边框/外壳；T5AI-Board 是电阻/电容屏，要稍用力。")
    print("  2. 确认串口连的是**日志口**(...163)，别连成下载口。")
    print("  3. 确认板子当时没有卡在别的界面；看屏幕上有没有正常显示状态页。")
    print("  4. 都排除后，问题在触摸这一段(不是播报这一段) ——")
    print("     先看启动日志有没有 `tdd_tp_gt1151.c:60`（说明 I2C open 过了），")
    print("     再看 `CONFIG_ENABLE_TP` / `ENABLE_LVGL_TP` 是否为 1。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
