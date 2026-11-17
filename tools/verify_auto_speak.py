#!/usr/bin/env python3
"""verify_auto_speak.py —— 「工单来了就自动播报」的实机验证

用法：
    # 只旁观（bridge 照常跑真实数据，你等真实工单变化）
    /Users/fire/TuyaOpen/.venv/bin/python tools/verify_auto_speak.py --watch 300

    # 主动驱动（需要 bridge 正跑在 --mock 模式）
    /Users/fire/TuyaOpen/.venv/bin/python tools/verify_auto_speak.py --drive

它盯住日志口，把和自动播报有关的行挑出来，并判定这条链路是否成立：

    sl: state A -> B                                 状态真的跳了
    sl: ticket change -> auto speak state=B          → 自动播报被触发
    sl: speak state=B url=.../audio/xxx.wav          → 点播了对应音频
    url sink file size N  ->  ai player FG eof       → 音频流确实走完了

--drive 会依次把 mock 改成 三种状态，每种等一个轮询周期（30s）：

    idle      --> CELEBRATE   期望 celebrate.wav
    p0=3      --> URGENT      期望 urgent.wav
    p1=2      --> WARNING     期望 warning.wav   （同时验证"降级也播"）
    不动       --> 不播        期望【零】自动播报   （验证不是"每 30 秒念一遍"）

⚠️ --drive 会反复改 8080 上的 mock 数据。跑之前先记下基线：
       curl --noproxy '*' -s http://127.0.0.1:8080/tickets
   跑完把真实数据源恢复回来、再 diff 一次基线。
   （真实数据源模式下 /sim 会返回 409 明确拒绝，不会静默灌不进去。）
"""
import argparse
import json
import re
import sys
import threading
import time
import urllib.request

try:
    import serial
except ImportError:
    sys.exit("缺少 pyserial。用 /Users/fire/TuyaOpen/.venv/bin/python 跑。")

LOG_PORT = "/dev/cu.usbmodem56D70349163"
BASE = "http://127.0.0.1:8080"

# 关心这几类日志
PAT = {
    "state": r"sl: state (\d) -> (\d)",
    "auto":  r"sl: ticket change -> auto speak state=(\d)",
    "skip":  r"sl: auto speak skipped",
    "url":   r"sl: speak state=\d+ url=\S+/audio/(\S+\.wav)",
    "fsize": r"url sink file size (\d+)",
    "eof":   r"ai player FG eof",
}

lines = []
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
            if t:
                lines.append(re.sub(r"\x1b\[[0-9;]*m", "", t))
    s.close()


def sim(**kw):
    """POST /sim?p0=..  —— 注意必须是 POST，GET 会 404（别被这个坑骗一次）。"""
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    req = urllib.request.Request(f"{BASE}/sim?{q}", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def collect(mark):
    """把 mark 之后的行分门别类取出"""
    out = {k: [] for k in PAT}
    for ln in lines[mark:]:
        for k, pat in PAT.items():
            m = re.search(pat, ln)
            if m:
                out[k].append(m.groups())
    return out


def report(label, got, want_auto, want_url):
    auto, url, eof, fsz, st, skip = (
        got["auto"], got["url"], got["eof"], got["fsize"], got["state"], got["skip"])
    print(f"\n--- {label} ---")
    for g in st:
        print(f"    状态跳变 {g[0]} -> {g[1]}")
    for g in auto:
        print(f"    自动播报触发 state={g[0]}")
    if skip:
        print(f"    (被最小间隔拦下 {len(skip)} 次)")
    for g in url:
        print(f"    点播音频 {g[0]}")
    for g in fsz:
        print(f"    音频大小 {g[0]} 字节")
    if eof:
        print(f"    音频流收尾 eof x{len(eof)}")

    urls = [g[0] for g in url]
    ok = len(auto) == want_auto and (want_url is None or want_url in urls)
    print(f"    {'✓ 符合预期' if ok else '✗ 与预期不符'}"
          f"（自动播报 {len(auto)} 次，期望 {want_auto}；音频 {urls or '无'}"
          + (f"，期望 {want_url}" if want_url else "") + "）")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0,
                    help="只旁观 N 秒（真实数据源时用）")
    ap.add_argument("--drive", action="store_true",
                    help="主动驱动 mock 数据走完三种跳变（需要 bridge 在 --mock 模式）")
    args = ap.parse_args()

    if not args.drive and args.watch <= 0:
        sys.exit("要么 --drive，要么 --watch <秒数>。详见 --help")

    try:
        with urllib.request.urlopen(f"{BASE}/tickets", timeout=5) as r:
            cur = json.loads(r.read().decode())
        print(f"当前 bridge 状态: {cur.get('state')} (p0={cur.get('p0')} "
              f"p1={cur.get('p1')})")
    except Exception as e:
        sys.exit(f"连不上 {BASE} —— bridge 在跑吗？({e})")

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.6)

    # ---------------- 只旁观 ----------------
    if not args.drive:
        print(f"\n盯 {args.watch} 秒，等真实的工单变化…\n")
        mark = len(lines)
        for left in range(args.watch, 0, -5):
            time.sleep(5)
        stop.set()
        time.sleep(0.4)
        got = collect(mark)
        print(f"\n旁观 {args.watch}s 期间：")
        print(f"  状态跳变 {len(got['state'])} 次")
        print(f"  自动播报 {len(got['auto'])} 次")
        print(f"  点播音频 {[g[0] for g in got['url']] or '无'}")
        if not got["state"]:
            print("  （这段时间里工单没变化，所以没声音 —— 这正是期望行为）")
        else:
            for g in got["auto"]:
                print(f"  ✓ state={g[0]} 时自动播报了")
        return 0

    # ---------------- 主动驱动 ----------------
    steps = [
        ("a) 改成 idle -> 期望 celebrate.wav", 50, {"p0": 0, "p1": 0, "p2": 0},
         1, "celebrate.wav"),
        ("b) 灌 p0=3 -> 期望 urgent.wav", 50, {"p0": 3, "p1": 0, "p2": 0},
         1, "urgent.wav"),
        ("c) 改成 p1=2 -> 期望 warning.wav（降级也播）", 50,
         {"p0": 0, "p1": 2, "p2": 0}, 1, "warning.wav"),
        ("d) 什么都不动 -> 期望零播报（不是每 30s 念一遍）", 55, None, 0, None),
    ]

    print("=" * 66)
    print(" 自动播报验证：状态跳变就播，状态不变不播")
    print("=" * 66)

    all_ok = True
    for label, dur, post, want_auto, want_url in steps:
        mark = len(lines)
        if post is not None:
            try:
                res = sim(**post)
                print(f"\n[POST /sim?{post}] -> ok={res.get('ok')}")
            except Exception as e:
                print(f"\n[POST /sim?{post}] 失败: {e}")
                print("  → 409 = bridge 在真实数据源模式，/sim 会刻意拒绝；"
                      "先切 --mock。")
                stop.set()
                return 1
        t0 = time.time()
        while time.time() - t0 < dur:
            time.sleep(0.5)
        all_ok &= report(label, collect(mark), want_auto, want_url)

    stop.set()
    time.sleep(0.4)
    print("\n" + "=" * 66)
    print(" 总判定:", "✓ 全部通过" if all_ok else "✗ 有偏差，见上")
    print("=" * 66)
    print("提醒：把真实数据源恢复回来，并 diff 一次基线。")
    print("      cd bridge && python3 bridge.py --port 8080")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
