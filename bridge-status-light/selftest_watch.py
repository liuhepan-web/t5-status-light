#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_watch.py —— 不碰涂鸦云，验证 --watch 的整条决策逻辑。

做法：起一个真的 bridge.py，把「云端」换成一个假的记录器，
依次灌 P0 → warning → 清零，检查下发的 DP 序列是否符合预期。

    python3 selftest_watch.py

改过措辞、状态映射或 --watch 逻辑之后跑一遍，比对着真板子猜快得多。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dp_push  # noqa: E402

PORT = 18099
BASE = "http://127.0.0.1:%d" % PORT


class FakeCloud(object):
    """顶替 TuyaCloud，把每次下发的 DP 记下来。"""

    def __init__(self):
        self.calls = []

    def get_token(self):
        return "fake-token"

    def send_commands(self, device_id, commands):
        self.calls.append({c["code"]: c["value"] for c in commands})
        return {"success": True, "result": True}


def post(path):
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def check(label, got, want):
    ok = got == want
    print("  [{}] {}\n        实际 {!r}\n        期望 {!r}".format(
        "PASS" if ok else "FAIL", label, got, want) if not ok
        else "  [PASS] {} -> {!r}".format(label, got))
    return ok


def main():
    srv = subprocess.Popen(
        # --mock：强制用内置模拟数据。否则万一目录里放着真的 status_light.json，
        # 这个自测就会去连真实工单系统，结果变得不可预期。
        [sys.executable, os.path.join(HERE, "bridge.py"),
         "--port", str(PORT), "--mock"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)

    fake = FakeCloud()
    try:
        t = threading.Thread(
            target=dp_push.cmd_watch,
            args=(fake, "fake-device", BASE + "/tickets", 1),
            kwargs={"celebrate_hold": 3},
            daemon=True,
        )
        t.start()

        time.sleep(1.8)
        post("/sim?p0=2")                      # 进 P0
        time.sleep(1.8)
        post("/sim?p0=0&p1=3")                 # 转 warning
        time.sleep(1.8)
        post("/sim?p0=0&p1=0&p2=0")            # 清零 → 触发庆祝

        time.sleep(1.6)
        calls_during_hold = len(fake.calls)

        time.sleep(5.5)
        calls_after_hold = len(fake.calls)
    finally:
        srv.terminate()

    print()
    print("=" * 62)
    print(" 下发序列检查")
    print("=" * 62)

    ok = True
    c = fake.calls

    def show():
        for i, item in enumerate(c):
            print("        #{} {}".format(i, item))

    if len(c) != 5:
        print("  [FAIL] 期望最终 5 次下发，实际 {}".format(len(c)))
        show()
        return 1

    ok &= check("1. 初始态：绿底，不发声",
                (c[0]["status_level"], c[0]["announce_text"], "celebrate" in c[0]),
                ("ok", "", False))

    ok &= check("2. 进 P0：红底 + 播报",
                (c[1]["status_level"], c[1]["announce_text"], "celebrate" in c[1]),
                ("urgent", "注意，有 2 个 P0 工单，请立刻处理", False))

    ok &= check("3. 转 warning：黄底，不发声",
                (c[2]["status_level"], c[2]["announce_text"], "celebrate" in c[2]),
                ("warn", "", False))

    ok &= check("4. 清零：绿底 + 庆祝 + 播报",
                (c[3]["status_level"], c[3]["announce_text"], c[3].get("celebrate")),
                ("ok", "干得漂亮，今天的工单清零了", True))

    ok &= check("5. 动画期间不复位 celebrate（否则动效被 1 秒后那一拍打断）",
                calls_during_hold, 4)

    ok &= check("6. 动画放完后复位 celebrate=false",
                (c[4].get("celebrate", False), c[4]["announce_text"]),
                (False, ""))

    ok &= check("7. 复位拍不重复播报",
                c[4]["announce_text"], "")

    print()
    print("逻辑{}。".format("正确" if ok else "有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
