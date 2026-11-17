#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_source.py —— 验证「接你自己的工单系统」这条链路。

起一个假的工单系统（HTTP 服务），让 bridge.py 真的去接它，然后断言：

  1. 工单数组能按优先级正确分组计数
  2. 已完成的工单被排除掉
  3. 中文优先级（紧急/高/中）也能映射
  4. 没映射的优先级值会被报告出来，而不是静默算错
  5. 状态跳变时给出正确的播报文案，没跳变时不给

不碰涂鸦云、不碰设备、不需要任何凭据。改完配置就跑这个。
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bridge           # noqa: E402
import dp_push          # noqa: E402
import sources          # noqa: E402

TICKET_PORT = 18771
BRIDGE_PORT = 18772
TODAY = time.strftime("%Y-%m-%d")
TMP = tempfile.mkdtemp(prefix="t5-selftest-")

# --------------------------------------------------------------------------
# 假工单系统返回的数据
# --------------------------------------------------------------------------

# 正常工作日：3 个 P0、2 个 P1、3 个 P2，另有 2 条已完成、2 条乱写的优先级
TICKETS_BUSY = [
    {"key": "T-101", "priority": "P0", "status": "open",        "resolved_at": None},
    {"key": "T-102", "priority": "P0", "status": "in_progress", "resolved_at": None},
    {"key": "T-103", "priority": "紧急", "status": "open",       "resolved_at": None},
    {"key": "T-104", "priority": "P1", "status": "open",        "resolved_at": None},
    {"key": "T-105", "priority": "高",  "status": "triage",     "resolved_at": None},
    {"key": "T-106", "priority": "P2", "status": "open",        "resolved_at": None},
    {"key": "T-107", "priority": "P2", "status": "open",        "resolved_at": None},
    {"key": "T-108", "priority": "中",  "status": "open",       "resolved_at": None},
    # 已完成 —— 不该算进状态灯
    {"key": "T-109", "priority": "P0", "status": "done",        "resolved_at": TODAY + "T09:12:00"},
    {"key": "T-110", "priority": "P1", "status": "closed",      "resolved_at": TODAY + "T10:40:00"},
    # 没映射的优先级 —— 不该算错，但要被报告出来
    {"key": "T-111", "priority": "P9", "status": "open",        "resolved_at": None},
    {"key": "T-112", "priority": "P9", "status": "open",        "resolved_at": None},
]

# 只剩 P1/P2：应该变成 warning，且**不发声**
TICKETS_CALM = [
    {"key": "T-201", "priority": "P1", "status": "open", "resolved_at": None},
    {"key": "T-202", "priority": "P2", "status": "open", "resolved_at": None},
]

# 全部清零
TICKETS_CLEAR = [
    {"key": "T-301", "priority": "P0", "status": "done", "resolved_at": TODAY + "T15:02:00"},
]

_STATE = {"tickets": TICKETS_BUSY}


class FakeTicketSystem(BaseHTTPRequestHandler):
    """假装是你们的工单系统。真实系统会带 Token 认证，这里省掉。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/tickets"):
            body = json.dumps(
                {"code": 0, "data": {"items": _STATE["tickets"]}},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def switch(tickets):
    _STATE["tickets"] = tickets


def check(name, got, want):
    ok = got == want
    print("  [{}] {}".format("PASS" if ok else "FAIL", name))
    if not ok:
        print("         期望: {!r}".format(want))
        print("         实际: {!r}".format(got))
    return ok


def write_config(path):
    cfg = {
        "source": {
            "transport": "http",
            "url": "http://127.0.0.1:{}/api/tickets?status=open&assignee=me".format(TICKET_PORT),
            "method": "GET",
            "headers": {"Authorization": "Bearer ${TICKET_TOKEN:-selftest-token}"},
            "timeout": 5,
            "shape": "list",
            "list_path": "data.items",
            "priority_field": "priority",
            "priority_map": {
                "p0": ["P0", "紧急"],
                "p1": ["P1", "高"],
                "p2": ["P2", "中"],
            },
            "status_field": "status",
            "exclude_status": ["done", "closed"],
            "resolved_field": "resolved_at",
        },
        "announce": {
            "urgent": "注意，有 {p0} 个 P0 工单，共 {total} 个待处理，请立刻处理",
            "celebrate": "干得漂亮，今天解决了 {resolved_today} 个工单",
            "warn": "",
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def main():
    # 配置写在临时目录里 —— 绝不碰工作目录里的 status_light.json。
    # 自测脚本擅自动用户的真实配置，是比测试失败更糟的事。
    cfg_path = os.path.join(TMP, "status_light.json")

    ticket_srv = ThreadingHTTPServer(("127.0.0.1", TICKET_PORT), FakeTicketSystem)
    ticket_srv.daemon_threads = True
    threading.Thread(target=ticket_srv.serve_forever, daemon=True).start()

    ok = True
    try:
        write_config(cfg_path)

        # ---------------- 1. 数据源解析 ----------------
        print("=" * 66)
        print(" 1. 工单数组 -> 三个数字")
        print("=" * 66)

        src = sources.TicketsSource.from_file(cfg_path)
        t = src.fetch()

        ok &= check("p0 计数（P0 + 紧急）", t["p0"], 3)
        ok &= check("p1 计数（P1 + 高）", t["p1"], 2)
        ok &= check("p2 计数（P2 + 中）", t["p2"], 3)
        ok &= check("今天解决数（已完成的那两条）", t["resolved_today"], 2)

        diag = t["_diag"]
        ok &= check("接口返回条数", diag["items_returned"], 12)
        ok &= check("被状态过滤掉的条数", diag["skipped_by_status"], 2)
        ok &= check(
            "没映射的优先级值被报告",
            dict(diag["unmapped_priority_values"]),
            {"P9": 2},
        )

        # ---------------- 2. 两种形态 ----------------
        print()
        print("=" * 66)
        print(" 2. counts 形态（接口直接给数量）")
        print("=" * 66)
        counts_cfg = {
            "transport": "http",
            "url": "http://127.0.0.1:{}/api/tickets".format(TICKET_PORT),
            "shape": "counts",
            "counts": {"p0": "data.priority.p0", "p1": "data.priority.p1"},
        }
        src2 = sources.TicketsSource(counts_cfg)
        try:
            src2.fetch()
            print("  [FAIL] 字段路径不存在时应当报错")
            ok = False
        except sources.SourceError as e:
            first = str(e).splitlines()[0]
            has_hint = "data" in str(e) or "字段" in str(e)
            print("  [{}] 字段路径写错时给出可读的提示".format("PASS" if has_hint else "FAIL"))
            print("         {}".format(first[:80]))
            ok &= has_hint

        # ---------------- 3. bridge.py 端到端 ----------------
        print()
        print("=" * 66)
        print(" 3. bridge.py 端到端（接假工单系统）")
        print("=" * 66)

        bridge.load_status_config(cfg_path)
        b_srv = bridge.Server(("127.0.0.1", BRIDGE_PORT), bridge.Handler, "127.0.0.1")
        b_srv.daemon_threads = True
        threading.Thread(target=b_srv.serve_forever, daemon=True).start()

        base = "http://127.0.0.1:{}".format(BRIDGE_PORT)

        r = get_json(base + "/tickets")
        print("  第一次拉取：p0={} p1={} p2={} state={}".format(
            r["p0"], r["p1"], r["p2"], r["state"]))
        ok &= check("状态判定为 urgent", r["state"], "urgent")
        ok &= check(
            "播报文案（用了配置里的模板和 {total} 变量）",
            r.get("announce_text"),
            "注意，有 3 个 P0 工单，共 8 个待处理，请立刻处理",
        )
        # 不写死后缀：音频容器格式由 bridge.AUDIO_EXT 决定（默认 .wav ——
        # macOS 自带的 say 能直接产出 16k/16bit 单声道 WAV，不需要 ffmpeg）。
        # 换格式时不该再来改测试。
        ok &= check("给出了音频地址", "audio/urgent." in (r.get("audio_url") or ""), True)

        r = get_json(base + "/tickets")
        print("  再拉一次（状态没变）")
        ok &= check("状态没变时不重复播报", r.get("announce_text"), None)
        ok &= check("状态没变时不给音频", r.get("audio_url"), None)

        switch(TICKETS_CALM)
        r = get_json(base + "/tickets")
        print("  切到只剩 P1/P2：p0={} p1={} p2={} state={}".format(
            r["p0"], r["p1"], r["p2"], r["state"]))
        ok &= check("状态判定为 warning", r["state"], "warning")
        ok &= check("warning 不发声", r.get("announce_text"), None)
        ok &= check("warning 不给音频", r.get("audio_url"), None)

        switch(TICKETS_CLEAR)
        r = get_json(base + "/tickets")
        print("  全部清零：p0={} p1={} p2={} state={}".format(
            r["p0"], r["p1"], r["p2"], r["state"]))
        ok &= check("状态回到 idle", r["state"], "idle")
        ok &= check(
            "庆祝文案（用了 {resolved_today} 变量）",
            r.get("announce_text"),
            "干得漂亮，今天解决了 1 个工单",
        )
        ok &= check("给出庆祝音频", "audio/celebrate." in (r.get("audio_url") or ""), True)

        b_srv.shutdown()
        b_srv.server_close()

        # ---------------- 4. 其他传输方式 ----------------
        print()
        print("=" * 66)
        print(" 4. 其他传输方式（不是每个内部系统都有现成 HTTP 接口）")
        print("=" * 66)

        prio = {"p0": ["P0", "紧急"], "p1": ["P1"], "p2": ["P2"]}
        base = {
            "shape": "list", "priority_field": "priority",
            "status_field": "status", "exclude_status": ["done"],
            "priority_map": prio,
        }

        def try_source(label, cfg, expect):
            try:
                got = sources.TicketsSource(cfg).fetch()
                return check(label, (got["p0"], got["p1"], got["p2"]), expect)
            except sources.SourceError as e:
                print("  [FAIL] {}：{}".format(label, str(e).splitlines()[0]))
                return False

        json_path = os.path.join(TMP, "tickets.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"data": {"items": [
                {"priority": "P0", "status": "open"},
                {"priority": "P1", "status": "open"},
                {"priority": "P2", "status": "open"},
                {"priority": "P0", "status": "done"},
            ]}}, f)
        ok &= try_source(
            "file / 读 JSON 文件",
            dict(base, transport="file", path=json_path, list_path="data.items"),
            (1, 1, 1),
        )

        csv_path = os.path.join(TMP, "tickets.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("priority,status\nP0,open\nP0,open\nP1,open\nP2,done\n")
        ok &= try_source(
            "file / 读 CSV 文件（list_path 留空）",
            dict(base, transport="file", path=csv_path, list_path=""),
            (2, 1, 0),
        )

        ok &= try_source(
            "command / 跑命令读它的 JSON",
            dict(base, transport="command", list_path="",
                 command="printf '[{\"priority\":\"P0\"},"
                         "{\"priority\":\"P2\"},{\"priority\":\"P2\"}]'"),
            (1, 0, 2),
        )

        db_path = os.path.join(TMP, "tickets.db")
        if os.path.isfile(db_path):
            os.remove(db_path)
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE tickets(key TEXT, priority TEXT, status TEXT, assignee TEXT)")
        con.executemany("INSERT INTO tickets VALUES(?,?,?,?)", [
            ("T1", "P0", "open", "me"),
            ("T2", "紧急", "open", "me"),
            ("T3", "P1", "open", "me"),
            ("T4", "P0", "open", "someone-else"),
        ])
        con.commit()
        con.close()
        ok &= try_source(
            "sql / 查 SQLite（只算 assignee=me 的）",
            dict(base, transport="sql", path=db_path,
                 query="SELECT priority, status FROM tickets WHERE assignee='me'"),
            (2, 1, 0),
        )

        ok &= try_source(
            "counts / 接口直接给数量",
            {
                "transport": "command", "shape": "counts",
                "command": "printf '{\"data\":{\"priority\":{\"p0\":4,\"p1\":1}}}'",
                "counts": {"p0": "data.priority.p0", "p1": "data.priority.p1"},
            },
            (4, 1, 0),
        )

        # ---------------- 5. 报错信息够不够直接 ----------------
        print()
        print("=" * 66)
        print(" 5. 配置写错时，报错信息能不能直接告诉你改哪一行")
        print("=" * 66)

        login_path = os.path.join(TMP, "login.html")
        with open(login_path, "w", encoding="utf-8") as f:
            f.write("<html><body>请先登录</body></html>")

        bad_cases = [
            ("字段路径写错 -> 列出这一层实际有哪些字段",
             dict(base, transport="file", path=json_path, list_path="data.wrong"),
             "items"),
            ("换数据源忘了改 list_path -> 告诉你怎么改",
             dict(base, transport="file", path=csv_path, list_path="data.items"),
             "list_path 改成空字符串"),
            ("返回的是登录页 -> 明说不是 JSON 并贴出原文",
             dict(base, transport="file", path=login_path, list_path=""),
             "不是 JSON"),
            ("命令执行失败 -> 给出返回码",
             dict(base, transport="command", list_path="", command="exit 3"),
             "返回码 3"),
            ("环境变量没设 -> 指出是哪个变量",
             dict(base, transport="command", list_path="", command="echo []",
                  url="${T5_SELFTEST_MISSING_VAR}"),
             "T5_SELFTEST_MISSING_VAR"),
        ]

        for label, cfg, must_contain in bad_cases:
            try:
                sources.TicketsSource(cfg).fetch()
                print("  [FAIL] {} —— 本该报错，却安静地跑过去了".format(label))
                ok = False
            except sources.SourceError as e:
                hit = must_contain in str(e)
                print("  [{}] {}".format("PASS" if hit else "FAIL", label))
                if not hit:
                    print("         报错里没提到 {!r}".format(must_contain))
                    print("         实际：{}".format(str(e).splitlines()[0]))
                ok &= hit

        # ---------------- 6. 端到端：措辞一路走到 DP ----------------
        print()
        print("=" * 66)
        print(" 6. 端到端：你自己写的播报措辞，能不能进到真正下发的 DP 里")
        print("=" * 66)

        class FakeCloud(object):
            """顶替 TuyaCloud，把每次下发的 DP 记下来。不连云。"""

            def __init__(self):
                self.calls = []

            def get_token(self, force=False):
                return "fake-token"

            def send_commands(self, device_id, commands):
                self.calls.append({c["code"]: c["value"] for c in commands})
                return {"success": True}

        bridge._prev.update({"p0": 0, "p1": 0, "p2": 0})
        switch(TICKETS_BUSY)

        b_srv2 = bridge.Server(("127.0.0.1", BRIDGE_PORT), bridge.Handler, "127.0.0.1")
        b_srv2.daemon_threads = True
        threading.Thread(target=b_srv2.serve_forever, daemon=True).start()

        fake = FakeCloud()
        threading.Thread(
            target=dp_push.cmd_watch,
            args=(fake, "fake-device",
                  "http://127.0.0.1:{}/tickets".format(BRIDGE_PORT), 1),
            daemon=True,
        ).start()

        time.sleep(2.0)
        switch(TICKETS_CLEAR)
        time.sleep(2.0)

        # 故意不 shutdown b_srv2：watch 线程是 daemon，随进程一起退出。
        # 提前关掉会让它下一轮打印一条 Connection refused，看着像失败其实不是。

        print("  共下发 {} 次 DP".format(len(fake.calls)))
        if len(fake.calls) < 2:
            print("  [FAIL] 至少应该有 2 次下发（进 P0、清零）")
            ok = False
        else:
            p0_dp, clear_dp = fake.calls[0], fake.calls[1]
            ok &= check("第 1 次：屏幕闪红", p0_dp.get("status_level"), "urgent")
            ok &= check(
                "第 1 次：播的是你自己写的措辞（含 {total} 变量）",
                p0_dp.get("announce_text"),
                "注意，有 3 个 P0 工单，共 8 个待处理，请立刻处理",
            )
            ok &= check("第 1 次：不触发庆祝", "celebrate" in p0_dp, False)
            ok &= check("第 2 次：触发庆祝动画", clear_dp.get("celebrate"), True)
            ok &= check(
                "第 2 次：播的是你自己写的庆祝词（含 {resolved_today}）",
                clear_dp.get("announce_text"),
                "干得漂亮，今天解决了 1 个工单",
            )

        # ---------------- 7. 数据源掉线 ----------------
        print()
        print("=" * 66)
        print(" 7. Cookie 过期 / 工单系统挂掉时，板子会不会安静地绿着")
        print("=" * 66)

        dead_cfg = os.path.join(TMP, "dead.json")
        with open(dead_cfg, "w", encoding="utf-8") as f:
            json.dump({
                "source": {
                    "transport": "http",
                    "url": "http://127.0.0.1:9/definitely-not-listening",
                    "timeout": 2,
                    "shape": "list",
                    "list_path": "",
                    "priority_field": "priority",
                    "priority_map": {"p0": ["P0"], "p1": ["P1"], "p2": ["P2"]},
                },
                "announce": {"source_lost": "工单系统连不上了，请检查"},
            }, f, ensure_ascii=False)

        bridge._prev.update({"p0": 0, "p1": 0, "p2": 0})
        bridge._fail["count"] = 0
        bridge.load_status_config(dead_cfg)

        b_srv3 = bridge.Server(("127.0.0.1", BRIDGE_PORT + 1), bridge.Handler, "127.0.0.1")
        b_srv3.daemon_threads = True
        threading.Thread(target=b_srv3.serve_forever, daemon=True).start()

        url3 = "http://127.0.0.1:{}/tickets".format(BRIDGE_PORT + 1)

        for i in range(1, bridge.FAIL_THRESHOLD):
            try:
                get_json(url3)
                print("  [FAIL] 第 {} 次取数本该失败".format(i))
                ok = False
            except urllib.error.HTTPError as e:
                ok &= check(
                    "第 {} 次取数失败返回 502（还给对方一次机会，不立刻报警）".format(i),
                    e.code, 502,
                )

        r = get_json(url3)
        print("  连续失败 {} 次后：state={} event={}".format(
            bridge.FAIL_THRESHOLD, r.get("state"), r.get("event")))
        ok &= check("切到 offline，不再假装一切正常", r.get("state"), "offline")
        ok &= check("事件标成 source_lost", r.get("event"), "source_lost")
        ok &= check("给出掉线播报", r.get("announce_text"), "工单系统连不上了，请检查")

        switch(TICKETS_CLEAR)
        bridge.load_status_config(cfg_path)
        bridge._fail["count"] = 0
        r = get_json(url3)
        print("  数据源恢复后：state={}".format(r.get("state")))
        ok &= check("恢复后自己走回正常状态", r.get("state"), "idle")

    finally:
        ticket_srv.shutdown()
        ticket_srv.server_close()
        shutil.rmtree(TMP, ignore_errors=True)

    print()
    print("=" * 66)
    print(" 结果：{}".format("全部通过" if ok else "有失败项，见上面的 FAIL"))
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
