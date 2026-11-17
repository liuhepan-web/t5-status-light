#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rules 规则引擎 + tuyacs（涂鸦 CS 工单系统）数据源的回归测试。

    python3 selftest_rules.py

不联网、不碰真实工单系统、不需要真 Cookie —— 起一个假的服务端把各种
响应喂进来。这样以后改规则或改判据，跑一下就知道有没有踩坏东西。

覆盖的是那些"错了也很难发现"的地方：
    - 字段不在时会不会静默算成 0（最坏：屏幕永远绿，实际堆积如山）
    - 时间戳是秒还是毫秒
    - Cookie 过期时给的是不是人话，而不是一个 500
    - 连续抖动后能不能自己恢复
"""

import json
import os
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sources  # noqa: E402

PORT = 18781
BASE = "http://127.0.0.1:%d" % PORT

NOW_MS = time.time() * 1000

# 假服务端的当前行为，测试过程中切换
MODE = {"kind": "ok", "fail_times": 0, "csrf_hits": 0}
STATE = {"csrf_calls": 0, "list_calls": 0}


def ticket(no, state, reply_hours_ago=None, priority="0"):
    ms = NOW_MS - (reply_hours_ago or 0) * 3600000
    return {
        "ticket_no": {"label": no, "value": no},
        "state": {"label": state, "value": state},
        "priority": {"label": priority, "value": priority},
        "last_requester_reply_at": {
            "label": "x", "value": str(int(ms)) if reply_hours_ago is not None else "0"},
        "description": {"label": "样例", "value": "样例"},
    }


class FakeCS(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        # 抓 csrf 用的页面
        STATE["csrf_calls"] += 1
        if MODE["kind"] == "nocsrf":
            self._send(200, "<html><body>没有 csrf 字段</body></html>", "text/html")
            return
        self._send(200, '<html><script id="__NEXT_DATA__" type="application/json">'
                        '{"rctxSync":{"csrf":"FAKE-CSRF-TOKEN"}}</script></html>', "text/html")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8")

        if MODE["fail_times"] > 0:
            MODE["fail_times"] -= 1
            self.close_connection = True      # 模拟内网那种直接掐断
            try:
                self.wfile.write(b"")
            except Exception:
                pass
            return

        STATE["list_calls"] += 1
        MODE["last_body"] = json.loads(body)
        MODE["last_csrf"] = self.headers.get("csrf-token")
        MODE["last_cookie"] = self.headers.get("Cookie")

        if MODE["kind"] == "notjson":
            self._send(200, "<html>请先登录</html>", "text/html")
            return
        if MODE["kind"] == "expired":
            self._send(200, json.dumps({"success": False, "code": 1010,
                                        "msg": "登录信息已失效，请重新登录"}))
            return
        if MODE["kind"] == "systemerror":
            self._send(200, json.dumps({"success": False, "code": 10200001,
                                        "msg": "系统错误，请联系管理员"}))
            return

        items = [
            ticket("T-001", "Pending"),
            ticket("T-002", "Processing"),
            ticket("T-003", "PendingUserFeedback", reply_hours_ago=2),
            ticket("T-004", "PendingUserFeedback", reply_hours_ago=200),
            ticket("T-005", "PendingUserConfirm", reply_hours_ago=None),
        ]
        self._send(200, json.dumps({"success": True, "result": {
            "datas": items, "total_count": len(items)}}))


def check(label, got, want):
    ok = got == want
    print("  [{}] {}".format("PASS" if ok else "FAIL", label))
    if not ok:
        print("         期望 {!r}".format(want))
        print("         实际 {!r}".format(got))
    return ok


def expect_error(label, fn, must_contain):
    try:
        fn()
    except sources.SourceError as e:
        text = str(e)
        hit = all(k in text for k in must_contain)
        print("  [{}] {}".format("PASS" if hit else "FAIL", label))
        if not hit:
            print("         报错里应该提到 {}".format(must_contain))
            print("         实际：{}".format(text.replace("\n", " ")[:200]))
        return hit
    except Exception as e:                        # noqa: BLE001
        print("  [FAIL] {}（抛了非 SourceError：{}）".format(label, e))
        return False
    print("  [FAIL] {}（本该报错却没报）".format(label))
    return False


def src(cfg):
    return sources.TicketsSource(cfg)


def test_rules():
    print()
    print("=" * 66)
    print(" 1. rules 规则引擎")
    print("=" * 66)

    ok = True
    base = {
        "transport": "command",
        "shape": "rules",
        "list_path": "data.items",
        "command": "printf '%s' '{\"data\":{\"items\":[]}}'",
        "rules": {
            "p0": [
                [{"field": "state.value", "in": ["Pending"]}],
                [{"field": "state.value", "in": ["PendingUserFeedback"]},
                 {"field": "last_requester_reply_at.value", "age_hours_lt": 24}],
            ],
            "p1": [
                [{"field": "state.value", "in": ["Processing"]}],
            ],
        },
        "else_bucket": "p2",
    }

    payload = {"data": {"items": [
        ticket("A", "Pending"),
        ticket("B", "Processing"),
        ticket("C", "PendingUserFeedback", reply_hours_ago=2),     # 客户刚回 -> p0
        ticket("D", "PendingUserFeedback", reply_hours_ago=200),   # 客户没吭声 -> p2
        ticket("E", "PendingUserConfirm", reply_hours_ago=0.5),    # 不在 p0 的 in 列表 -> p2
    ]}}
    got = sources.count_rules_mode(base, payload)
    ok &= check("按状态+时效分桶", (got["p0"], got["p1"], got["p2"]), (2, 1, 2))
    ok &= check("全部条数都被归类（不漏不重）",
                got["_diag"]["counted"] + got["_diag"]["unmatched"], 5)

    print()
    print(" -- 时间戳格式 --")
    sec_payload = {"data": {"items": [
        dict(ticket("F", "PendingUserFeedback"),
             last_requester_reply_at={"label": "x", "value": str(int(NOW_MS / 1000) - 3600)}),
    ]}}
    got = sources.count_rules_mode(base, sec_payload)
    ok &= check("秒级时间戳也能识别（不误判成 1970 年）", got["p0"], 1)

    print()
    print(" -- 字段缺失 --")
    missing = {"data": {"items": [{"state": {"value": "PendingUserFeedback"}}]}}
    got = sources.count_rules_mode(base, missing)
    ok &= check("必填字段不在 -> 不算 p0（宁可少算也不错算）", got["p0"], 0)
    ok &= check("必填字段不在 -> 落到 else_bucket", got["p2"], 1)

    print()
    print(" -- 值不是时间戳 --")
    bad = {"data": {"items": [
        {"state": {"value": "PendingUserFeedback"},
         "last_requester_reply_at": {"value": "2026-09-14 14:25:44"}},
    ]}}
    got = sources.count_rules_mode(base, bad)
    ok &= check("时间字段格式不对 -> 记进 rule_errors 而不是静默当 0",
                len(got["_diag"]["rule_errors"]), 1)

    print()
    print(" -- 真没命中任何桶 --")
    norule = dict(base, else_bucket=None)
    got = sources.count_rules_mode(norule, {"data": {"items": [ticket("G", "Closed")]}})
    ok &= check("没配 else_bucket 时计入 unmatched", got["_diag"]["unmatched"], 1)
    ok &= check("并给出样例票号便于排查",
                got["_diag"].get("未命中样例") and "G" in got["_diag"]["未命中样例"][0], True)

    print()
    print(" -- 配置错误 --")
    # 规则只在有工单可判的时候才会被求值，所以这里得喂一条真数据
    one = os.path.join("/tmp", "t5_one_ticket.json")
    with open(one, "w", encoding="utf-8") as f:
        json.dump([{"state": {"value": "Pending"}}], f)
    filebase = dict(base, transport="file", path=one, list_path="")

    ok &= expect_error("写错算子 -> 明确指出可用算子",
                       lambda: src(dict(filebase, rules={
                           "p0": [[{"field": "state.value", "isin": ["Pending"]}]]})).fetch(),
                       ["isin", "age_hours_lt"])
    ok &= expect_error("shape=rules 但没配 rules",
                       lambda: src({"transport": "command", "shape": "rules",
                                    "command": "echo {}"}).validate(),
                       ["rules"])
    ok &= expect_error("条件里没写 field",
                       lambda: src(dict(filebase, rules={
                           "p0": [[{"in": ["Pending"]}]]})).fetch(),
                       ["field"])
    os.remove(one)
    return ok


def test_tuyacs():
    print()
    print("=" * 66)
    print(" 2. tuyacs 数据源（涂鸦 CS 工单系统）")
    print("=" * 66)

    ok = True
    cfg = {
        "transport": "tuyacs",
        "url": BASE,
        "cookie": "SSO_USER_TOKEN=fake; fast-sid=fake",
        "view_code": "techTicketCSMyPending",
        "platform": 8,
        "page_size": 500,
        "timeout": 5,
        "retries": 4,
        "tls_verify": False,
        "shape": "rules",
        "list_path": "result.datas",
        "count_field": "result.total_count",
        "rules": {
            "p0": [[{"field": "state.value", "in": ["Pending"]}],
                   [{"field": "state.value", "in": ["PendingUserFeedback"]},
                    {"field": "last_requester_reply_at.value", "age_hours_lt": 24}]],
            "p1": [[{"field": "state.value", "in": ["Processing"]}]],
        },
        "else_bucket": "p2",
    }

    MODE["kind"] = "ok"
    sources._CSRF_CACHE.clear()
    got = src(cfg).fetch()
    ok &= check("正常路径：分桶结果", (got["p0"], got["p1"], got["p2"]), (2, 1, 2))
    ok &= check("接口给的总数被记下来", got["_total_from_api"], 5)

    print()
    print(" -- 请求本身对不对 --")
    ok &= check("csrf 头带上了", MODE.get("last_csrf"), "FAKE-CSRF-TOKEN")
    ok &= check("cookie 按原样发出去（没被截断）",
                MODE.get("last_cookie"), "SSO_USER_TOKEN=fake; fast-sid=fake")
    body = MODE.get("last_body") or {}
    ok &= check("view_code 传对了", body.get("view_code"), "techTicketCSMyPending")
    ok &= check("page_size 生效", body.get("page_size"), 500)
    ok &= check("没把 search_field 传成字符串（会触发服务端系统错误）",
                "search_field" not in body, True)

    print()
    print(" -- csrf 缓存 --")
    before = STATE["csrf_calls"]
    sources.TicketsSource(cfg).fetch()
    ok &= check("第二次取数不再重复抓 csrf 页面",
                STATE["csrf_calls"], before)

    print()
    print(" -- 各种失败路径都要说人话 --")
    sources._CSRF_CACHE.clear()
    MODE["kind"] = "expired"
    ok &= expect_error("登录态失效 -> 告诉你去换 cookie",
                       lambda: sources.TicketsSource(cfg).fetch(),
                       ["登录态失效", "CS_COOKIE"])

    sources._CSRF_CACHE.clear()
    MODE["kind"] = "systemerror"
    ok &= expect_error("服务端系统错误 -> 提示常见原因",
                       lambda: sources.TicketsSource(cfg).fetch(),
                       ["系统错误"])

    sources._CSRF_CACHE.clear()
    MODE["kind"] = "nocsrf"
    ok &= expect_error("页面里没有 csrf -> 告诉你怎么手工拿",
                       lambda: sources.TicketsSource(cfg).fetch(),
                       ["csrf"])

    sources._CSRF_CACHE.clear()
    MODE["kind"] = "notjson"
    ok &= expect_error("返回 HTML 而不是 JSON -> 把前 200 字贴出来",
                       lambda: sources.TicketsSource(cfg).fetch(),
                       ["没有返回 JSON", "请先登录"])

    print()
    print(" -- 网络抖动 --")
    sources._CSRF_CACHE.clear()
    MODE["kind"] = "ok"
    MODE["fail_times"] = 2
    try:
        sources.TicketsSource(cfg).fetch()
        print("  [PASS] 抖 2 次后自动重试成功（不会把一次网络抖动当成配置错误）")
    except sources.SourceError as e:
        print("  [FAIL] 抖 2 次就放弃了：{}".format(str(e).replace("\n", " ")[:120]))
        ok = False

    sources._CSRF_CACHE.clear()
    MODE["kind"] = "ok"
    MODE["fail_times"] = 99
    ok &= expect_error("一直抖 -> 报错说清是网络问题而不是瞎猜",
                       lambda: sources.TicketsSource(cfg).fetch(),
                       # 不再要求出现「连续」二字：预算提前放弃时"连续 N 次"是假话，
                       # 现在写成「失败 u/n 次」+ 一句预算说明。这里只坚持它
                       # 必须把话说成「网络/内网抖动」这类可行动的方向，而不是瞎猜。
                       ["网络", "内网抖动"])

    print()
    print(" -- 配置校验 --")
    ok &= expect_error("没配 cookie -> 告诉你用 CS_COOKIE 环境变量",
                       lambda: src({"transport": "tuyacs", "url": BASE,
                                    "view_code": "x", "shape": "list",
                                    "priority_map": {"p0": ["1"]}}).fetch(),
                       ["cookie", "CS_COOKIE"])
    ok &= expect_error("没配 view_code",
                       lambda: src({"transport": "tuyacs", "url": BASE, "cookie": "x",
                                    "shape": "list",
                                    "priority_map": {"p0": ["1"]}}).fetch(),
                       ["view_code", "techTicketCSMyPending"])
    return ok


def test_alert_buckets():
    print()
    print("=" * 66)
    print(" 3. alert_buckets：让「在等客户」那堆不参与灯的判断")
    print("=" * 66)

    import bridge

    ok = True
    saved = bridge.ALERT_BUCKETS
    try:
        bridge.ALERT_BUCKETS = ("p0", "p1", "p2")
        ok &= check("默认：p2 有货就算在忙",
                    bridge.calc_state({"p0": 0, "p1": 0, "p2": 274}), "warning")

        bridge.ALERT_BUCKETS = ("p0", "p1")
        ok &= check("排除 p2 后：只剩等客户的，就是没事（绿）",
                    bridge.calc_state({"p0": 0, "p1": 0, "p2": 274}), "idle")
        ok &= check("排除 p2 后：处理中仍然是黄",
                    bridge.calc_state({"p0": 0, "p1": 1, "p2": 274}), "warning")
        ok &= check("p0 永远优先于一切",
                    bridge.calc_state({"p0": 1, "p1": 9, "p2": 274}), "urgent")
    finally:
        bridge.ALERT_BUCKETS = saved
    return ok


def test_p0_increase_announces():
    print()
    print("=" * 66)
    print(" 4. 已经红了的情况下，又来了新工单要再响一次")
    print("=" * 66)

    import bridge

    ok = True
    saved = dict(bridge._prev)
    try:
        bridge._prev.update({"p0": 1, "p1": 0, "p2": 5})
        ok &= check("1 -> 3：算事件（否则你最想被叫的那一下是静音的）",
                    bridge.pick_event("urgent", {"p0": 3, "p1": 0, "p2": 5}),
                    "p0_arrived")
        ok &= check("1 -> 1：不重复响",
                    bridge.pick_event("urgent", {"p0": 1, "p1": 0, "p2": 5}), None)
        ok &= check("1 -> 0 且还有处理中：转 warning",
                    bridge.pick_event("warning", {"p0": 0, "p1": 2, "p2": 5}), "warn")
        bridge._prev.update({"p0": 1, "p1": 0, "p2": 5})
        bridge.ALERT_BUCKETS = ("p0", "p1")
        ok &= check("从有活到没活：庆祝（p2 那堆不影响）",
                    bridge.pick_event("idle", {"p0": 0, "p1": 0, "p2": 274}), "all_clear")
    finally:
        bridge._prev.clear()
        bridge._prev.update(saved)
    return ok


def test_deadline_budget():
    """
    ★ 这一条守的是「两侧超时必须按总预算对齐」这个坑。

    板子拉 /tickets 只等 8 秒（固件 SL_HTTP_TIMEOUT_MS），而 timeout×retries
    的最坏情况可以是 120 秒 —— 板子会先掐线、把这一轮判成失败显示灰屏，
    bridge 之后再去写响应头就是一堆 Broken pipe。

    所以这里要断言两件事：
      1. 总耗时被 budget 截住，不随 retries 增长；
      2. 报错如实说「预算是用完了」，而不是谎报「连续 N 次失败」。
    """
    print()
    print("=" * 66)
    print(" 5. 时间预算：上游卡住时不能把 retries 叠成上百秒")
    print("=" * 66)

    import time as _t
    ok = True

    real_request = sources._request
    calls = {"n": 0}

    def stalled(url, data=None, headers=None, timeout=30, verify=True):
        calls["n"] += 1
        _t.sleep(timeout)              # 比你给我的超时还慢
        raise urllib.error.URLError("simulated stall")

    try:
        # Deadline 本身：单次上限不能超过剩余预算
        dl = sources.Deadline(2)
        ok &= check("cap() 不超过剩余预算", dl.cap(30) <= 2.05, True)
        ok &= check("cap() 有 0.5 秒下限（别让 socket 到手就超时）", dl.cap(0.1), 0.5)

        sources._request = stalled
        sources._CSRF_CACHE.clear()

        t0 = _t.monotonic()
        try:
            sources.fetch_csrf("https://x", "cookie=1", 30, True, 4, sources.Deadline(2.5))
            print("  [FAIL] 本该超时却成功了")
            ok = False
            el = 0.0
        except sources.SourceError as e:
            el = _t.monotonic() - t0
            text = str(e)
            ok &= check("2.5 秒预算 -> 3 秒内放弃（原来会等 30×4）", el < 3.2, True)
            ok &= check("预算见底就不再重试", calls["n"], 1)
            ok &= check("报错承认是预算用完", "预算" in text, True)
            ok &= check("报错如实写尝试次数，不谎报 retries 上限",
                        "失败 1/4 次" in text, True)
    finally:
        sources._request = real_request
        sources._CSRF_CACHE.clear()

    return ok


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), FakeCS)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        results = [test_rules(), test_tuyacs(),
                   test_alert_buckets(), test_p0_increase_announces(),
                   test_deadline_budget()]
    finally:
        srv.shutdown()
        srv.server_close()

    print()
    print("=" * 66)
    if all(results):
        print(" 结果：全部通过")
        print("=" * 66)
        return 0
    print(" 结果：有失败项，见上面的 FAIL")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(main())
