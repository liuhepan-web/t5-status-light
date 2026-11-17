#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources.py —— 把「你自己的工单系统」接成三个数字。

这个模块只做一件事：**只读地**从你的工单系统里问出「现在有几个 P0 / P1 / P2」。
它不会创建、修改、关闭任何工单 —— 一个状态灯没有任何理由写你的工单库。

对上层只暴露一个入口：

    from sources import TicketsSource
    src = TicketsSource.from_file("status_light.json")
    src.fetch()
    # -> {"p0": 2, "p1": 5, "p2": 9, "resolved_today": 3, "_diag": {...}}

--------------------------------------------------------------------------
两种传输方式（transport）—— 数据怎么拿
--------------------------------------------------------------------------

  http      调一个 HTTP 接口拿 JSON。有 API 就用这个（Jira / 禅道 / 自研 都行）
  file      读本地的 JSON 或 CSV 文件。系统能导出、或已有同步任务往这落盘
  sql       SQL 查询。sqlite 用标准库，零依赖；mysql/pg 用 command 包一层（见文档）
  command   跑一条命令，读它 stdout 的 JSON。万能兜底，任何系统都能包一层

--------------------------------------------------------------------------
两种数据形态（shape）—— 拿到之后怎么数
--------------------------------------------------------------------------

  list      接口返回**工单数组**，本地按优先级字段分组计数   ← 绝大多数系统是这样
  counts    接口**直接返回**各优先级的数量（少数字段齐全的统计接口）

list 是最常见的情况：你的接口大概率返回"一堆工单"，而不是"P0 有几个"。
所以本模块默认 shape=list，并且帮你处理三件麻烦事：

  1. 你的优先级不叫 p0 —— 用 priority_map 翻译（P0/紧急/Highest/blocker/... 都行）
  2. 已完成的不该算 —— 用 exclude_status 把 done/closed/已解决 排除掉
  3. 只算你自己的工单 —— 在 URL 的查询参数里带上你的用户，见配置示例
"""

import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

_MISSING = object()

# 环境变量引用：${VAR} 或 ${VAR:-默认值}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# 可以放在文件里的凭据。${CS_COOKIE} 找不到环境变量时，会来这里读文件。
#
# 为什么要支持文件：工单系统的 s-sid 大约 8 小时就轮换一次，靠环境变量只能在
# 启动时固定住一个值，撑不过一天。放进文件后，cookie_sync.py 可以在后台一直
# 刷新它，而正在跑的 bridge.py 下次取数就自动用上新的 —— 不用重启。
#
# 优先级：显式环境变量 > 文件 > 配置里写的默认值。
# 想临时覆盖（比如跑自测）就 export CS_COOKIE=...，它会盖住文件。
_CRED_FILES = {
    "CS_COOKIE": "~/.cs_cookie",
}

# name -> (mtime_ns, value)。文件没动过就不重复读。
_CRED_CACHE = {}


def credential_from_file(name):
    """
    从约定好的文件里读凭据。每次都会看一眼 mtime —— 文件变了就重读。

    读不到（文件不存在、或者是空的）返回 None，由调用方决定报错还是用默认值。
    """
    template = _CRED_FILES.get(name)
    if not template:
        return None

    path = os.environ.get(name + "_FILE") or os.path.expanduser(template)

    try:
        st = os.stat(path)
    except OSError:
        return None

    cached = _CRED_CACHE.get(name)
    if cached and cached[0] == st.st_mtime_ns:
        return cached[1]

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            value = f.read().strip()
    except OSError:
        return None

    if not value:
        return None

    _CRED_CACHE[name] = (st.st_mtime_ns, value)
    return value


class SourceError(Exception):
    """配置或取数出错。信息尽量写成"下一步该改哪一行"。"""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def expand_env(value):
    """
    递归把配置里的 ${VAR} 换成环境变量。

    找不到变量时**报错而不是替换成空串** —— 否则会变成"token 是空的、
    接口返回 401、你以为是代码问题"，白白排查半小时。
    """
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    def repl(m):
        name, default = m.group(1), m.group(2)

        # 1. 显式环境变量最优先 —— 临时覆盖、跑自测都靠它
        if name in os.environ:
            return os.environ[name]

        # 2. 约定好的凭据文件（可以被 cookie_sync.py 持续刷新）
        from_file = credential_from_file(name)
        if from_file is not None:
            return from_file

        # 3. 配置里写了兜底值
        if default is not None:
            return default

        hint = ""
        if name in _CRED_FILES:
            hint = ("\n  也试过读 {path}，但没有这个文件、或者内容是空的。\n"
                    "  让它自动生成：python3 cookie_sync.py".format(
                        path=os.path.expanduser(_CRED_FILES[name])))
        raise SourceError(
            "配置里引用了环境变量 ${{{name}}}，但当前环境里没有它。\n"
            "  export {name}=... 之后再启动；\n"
            "  或者写成 ${{{name}:-兜底值}} 给它一个默认值。{hint}".format(
                name=name, hint=hint)
        )

    return _ENV_PATTERN.sub(repl, value)


def describe(obj, limit=10):
    """把任意对象描述成一句人话，用在报错信息里。"""
    if isinstance(obj, dict):
        keys = [str(k) for k in obj.keys()]
        if not keys:
            return "空对象 {}"
        shown = ", ".join(keys[:limit])
        more = "，等共 {} 个".format(len(keys)) if len(keys) > limit else ""
        return "对象，里面有这些字段：{}{}".format(shown, more)
    if isinstance(obj, (list, tuple)):
        return "数组，长度 {}".format(len(obj))
    return "{} 值 {!r}".format(type(obj).__name__, obj)


def dig(obj, path, default=_MISSING):
    """
    按 "a.b.0.c" 这样的路径取值。取不到时给出"当前这层有什么"的提示。

    这个提示是整个模块里最有价值的东西 —— 接一个新系统时 90% 的时间
    花在"字段到底叫什么名字"上，能直接看到实际字段名就不用猜了。
    """
    if path is None or path == "":
        return obj

    cur = obj
    walked = []

    for part in str(path).split("."):
        where = "/".join(walked) if walked else "(根)"

        if isinstance(cur, dict):
            if part not in cur:
                if default is not _MISSING:
                    return default
                raise SourceError(
                    "找不到路径 {p!r}：走到 {w} 时没有 {k!r} 这个字段。\n"
                    "  {w} 实际是 {d}".format(p=path, w=where, k=part, d=describe(cur))
                )
            cur = cur[part]

        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                if default is not _MISSING:
                    return default
                raise SourceError(
                    "路径 {p!r} 里的 {k!r} 不是有效的数组下标"
                    "（{w} 是数组，长度 {n}）。".format(
                        p=path, k=part, w=where, n=len(cur))
                )

        else:
            if default is not _MISSING:
                return default
            raise SourceError(
                "路径 {p!r} 走不下去了：{w} 是 {d}，不能再往里取 {k!r}。".format(
                    p=path, w=where, d=describe(cur), k=part)
            )

        walked.append(part)

    return cur


def parse_json(raw, where):
    """解析 JSON。不是 JSON 时把开头原文打出来 —— 通常是返回了登录页。"""
    try:
        return json.loads(raw)
    except ValueError:
        snippet = " ".join(raw.strip().split())[:200]
        raise SourceError(
            "返回的内容不是 JSON。\n"
            "  来源：{w}\n"
            "  开头 200 字：{s}\n"
            "  常见原因：Token 过期被重定向到登录页、接口地址写成了网页地址、"
            "或返回的是 XML/HTML。".format(w=where, s=snippet)
        )


# --------------------------------------------------------------------------
# 优先级分桶
# --------------------------------------------------------------------------

def bucket_of(value, priority_map):
    """
    把你的优先级写法翻译成 p0 / p1 / p2。

    比较时忽略大小写和首尾空格，所以 "P0" / "p0" / " P0 " 都能匹配上。
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    for bucket in ("p0", "p1", "p2"):
        for token in priority_map.get(bucket) or []:
            if str(token).strip().lower() == v:
                return bucket
    return None


# --------------------------------------------------------------------------
# shape=list：拿到工单数组，在本地数
# --------------------------------------------------------------------------

def as_items(payload, cfg):
    """从 payload 里取出工单数组。兼容"数组"和"包装成字典的列表"两种。"""
    list_path = cfg.get("list_path", "")

    # 顶层就是数组却还配了 list_path —— 换数据源时最容易忘的一处
    if list_path and isinstance(payload, list):
        raise SourceError(
            "返回的顶层就是一个数组（{} 条），但配置里写了 list_path={!r}。\n"
            "  把 list_path 改成空字符串 \"\" 就行。".format(len(payload), list_path)
        )

    node = dig(payload, list_path) if list_path not in (None, "") else payload

    if isinstance(node, dict):
        # 禅道这类会把列表包成 {"1": {...}, "2": {...}}，取 values 即可
        node = list(node.values())

    if not isinstance(node, list):
        raise SourceError(
            "期望这里是工单数组，实际是 {}。\n"
            "  在你返回的 JSON 里找到那个数组，把它的路径填到 list_path，\n"
            "  例如 \"list_path\": \"data.items\"。".format(describe(node))
        )
    return node


def count_list_mode(cfg, payload):
    priority_map = cfg.get("priority_map") or {}
    if not priority_map:
        raise SourceError(
            "shape=list 时必须配 priority_map —— 告诉我你们的优先级叫什么名字。\n"
            "  例如：\"priority_map\": {\"p0\": [\"P0\",\"紧急\"], \"p1\": [\"P1\",\"高\"], "
            "\"p2\": [\"P2\",\"中\"]}"
        )

    items = as_items(payload, cfg)

    prio_field = cfg.get("priority_field")
    if not prio_field:
        raise SourceError(
            "shape=list 时必须配 priority_field —— 一条工单里表示优先级的字段名。\n"
            "  例如 \"priority_field\": \"priority\"，嵌套的写 \"fields.priority.name\"。"
        )

    status_field = cfg.get("status_field")
    include = [str(s).strip().lower() for s in (cfg.get("include_status") or [])]
    exclude = [str(s).strip().lower() for s in (cfg.get("exclude_status") or [])]

    counts = {"p0": 0, "p1": 0, "p2": 0}
    unknown = {}
    skipped = 0
    malformed = 0
    total_seen = 0

    for it in items:
        total_seen += 1
        if not isinstance(it, dict):
            malformed += 1
            continue

        # 1) 已完成的不算
        if status_field:
            status = str(dig(it, status_field, "") or "").strip().lower()
            if include and status not in include:
                skipped += 1
                continue
            if exclude and status in exclude:
                skipped += 1
                continue

        # 2) 优先级分桶
        raw_prio = dig(it, prio_field, None)
        bucket = bucket_of(raw_prio, priority_map)
        if bucket:
            counts[bucket] += 1
        elif raw_prio is not None:
            key = str(raw_prio)
            unknown[key] = unknown.get(key, 0) + 1

    # 3) 今天解决的（可选，只影响展示）
    resolved_today = 0
    rfield = cfg.get("resolved_field")
    if rfield:
        today = time.strftime("%Y-%m-%d")
        for it in items:
            if not isinstance(it, dict):
                continue
            val = dig(it, rfield, None)
            if val and str(val)[:10] == today:
                resolved_today += 1

    counts["resolved_today"] = resolved_today
    counts["_diag"] = {
        "shape": "list",
        "items_returned": total_seen,
        "counted": counts["p0"] + counts["p1"] + counts["p2"],
        "skipped_by_status": skipped,
        "malformed_items": malformed,
        "unmapped_priority_values": unknown,
    }
    return counts


# --------------------------------------------------------------------------
# shape=counts：接口直接给了数量
# --------------------------------------------------------------------------

def count_counts_mode(cfg, payload):
    mapping = cfg.get("counts") or {}
    if not mapping:
        raise SourceError(
            "shape=counts 时必须配 counts 字段映射，例如：\n"
            "  \"counts\": {\"p0\": \"data.priority.p0\", \"p1\": \"data.priority.p1\", "
            "\"p2\": \"data.priority.p2\"}"
        )

    out = {}
    for bucket in ("p0", "p1", "p2", "resolved_today"):
        path = mapping.get(bucket)
        if not path:
            # 没配的桶按 0 算 —— 你不需要在界面上看这个优先级
            out[bucket] = 0
            continue
        try:
            out[bucket] = int(dig(payload, path) or 0)
        except SourceError as e:
            # 配了路径就必须取到值。取不到还当 0 处理是最坏的失败模式：
            # 屏幕一直绿着，而实际上 P0 已经堆成山了。
            raise SourceError(
                "{}\n"
                "  这是 counts 里 {k!r} 的取值路径。\n"
                "  如果你们的接口在「该优先级没有工单」时会把字段整个省掉，\n"
                "  就把 counts 里的 {k!r} 删掉 —— 没配的桶按 0 算；\n"
                "  否则说明路径写错了，用 --check --dump 看一眼真实结构。".format(
                    e, k=bucket)
            )

    out["_diag"] = {"shape": "counts"}
    return out


# --------------------------------------------------------------------------
# shape=rules：自己写判定规则，谁算 p0 你说了算
# --------------------------------------------------------------------------

def _norm(v):
    return str(v).strip().lower()


def eval_condition(item, cond, now_ms, errors):
    """
    判断一条工单是否满足一个条件。cond 里除 field 外的每个键都是一个算子，
    多个算子之间是 AND。

    支持：
        {"field": "state.value", "in": ["Pending"]}
        {"field": "state.value", "not_in": ["Closed"]}
        {"field": "state.value", "eq": "Processing"}
        {"field": "description.value", "contains": "崩溃"}
        {"field": "priority.value", "truthy": true}
        {"field": "last_requester_reply_at.value", "age_hours_lt": 48}
        {"field": "last_requester_reply_at.value", "age_hours_gte": 48}
    """
    field = cond.get("field")
    if not field:
        raise SourceError(
            "rules 里有一个条件没写 field。\n"
            "  每个条件都要形如 {\"field\": \"state.value\", \"in\": [\"Pending\"]}"
        )

    val = dig(item, field, None)
    ok = True

    for op, arg in cond.items():
        if op == "field":
            continue
        if op == "in":
            ok = ok and _norm(val) in [_norm(x) for x in (arg or [])]
        elif op == "not_in":
            ok = ok and _norm(val) not in [_norm(x) for x in (arg or [])]
        elif op == "eq":
            ok = ok and _norm(val) == _norm(arg)
        elif op == "ne":
            ok = ok and _norm(val) != _norm(arg)
        elif op == "contains":
            ok = ok and _norm(arg) in _norm(val)
        elif op == "truthy":
            want = bool(arg)
            got = val not in (None, "", 0, "0", "false", "False")
            ok = ok and (got == want)
        elif op in ("age_hours_lt", "age_hours_gte"):
            if val in (None, "", "0", 0):
                # 没有这个时间字段 —— 当作"很久以前"，于是 age_hours_lt 为假
                ok = ok and (op == "age_hours_gte")
                continue
            try:
                ms = float(val)
            except (TypeError, ValueError):
                # 解析不了就记一笔，别静默当成"不满足"
                errors.append("{}.{} 不是时间戳：{!r}".format(field, op, val))
                ok = False
                continue
            if ms < 1e11:      # 看起来是秒而不是毫秒
                ms *= 1000
            age_h = (now_ms - ms) / 3600000.0
            ok = ok and (age_h < float(arg) if op == "age_hours_lt"
                         else age_h >= float(arg))
        else:
            raise SourceError(
                "rules 里不认识的算子 {!r}（字段 {}）。\n"
                "  可用：in / not_in / eq / ne / contains / truthy / "
                "age_hours_lt / age_hours_gte".format(op, field)
            )

        if not ok:
            break

    return ok


def match_bucket(item, alts, now_ms, errors):
    """
    一个桶的规则是"若干组"的列表：组间是 OR，组内是 AND。

        "p0": [
            [ {"field": "state.value", "in": ["Pending"]} ],
            [ {"field": "state.value", "in": ["PendingUserFeedback"]},
              {"field": "last_requester_reply_at.value", "age_hours_lt": 48} ]
        ]
    """
    for group in alts or []:
        if isinstance(group, dict):
            group = [group]          # 允许只写一个条件、少一层方括号
        if all(eval_condition(item, c, now_ms, errors) for c in group):
            return True
    return False


def count_rules_mode(cfg, payload):
    """
    按 rules 给每条工单分桶计数。

    为什么要有这个模式：真实队列里"优先级"往往不是你要的信号。
    （比如涂鸦 CS 的队列里 275 条工单有 211 条优先级是空的。）
    真正决定"要不要叫我"的是状态 + 客户有没有回你 —— 那就得自己写规则。
    """
    rules = cfg.get("rules") or {}
    if not rules:
        raise SourceError(
            "shape=rules 时必须配 rules，例如：\n"
            "  \"rules\": {\n"
            "    \"p0\": [[{\"field\":\"state.value\",\"in\":[\"Pending\"]}]],\n"
            "    \"p1\": [[{\"field\":\"state.value\",\"in\":[\"Processing\"]}]]\n"
            "  },\n"
            "  \"else_bucket\": \"p2\""
        )

    items = as_items(payload, cfg)
    now_ms = time.time() * 1000
    errors = []

    counts = {"p0": 0, "p1": 0, "p2": 0}
    else_bucket = cfg.get("else_bucket", "p2")
    unmatched = 0
    malformed = 0
    samples = {}

    for it in items:
        if not isinstance(it, dict):
            malformed += 1
            continue

        hit = None
        for bucket in ("p0", "p1", "p2"):
            if bucket in rules and match_bucket(it, rules.get(bucket), now_ms, errors):
                hit = bucket
                break

        if hit is None:
            if else_bucket in counts:
                # 落到 else_bucket 是正常设计（比如"在等客户"那堆），不算异常
                counts[else_bucket] += 1
            else:
                unmatched += 1
                # 真的哪儿都没去，留两条样例帮你看规则漏了什么
                if len(samples) < 2:
                    samples.setdefault("未命中样例", [])
                    if len(samples["未命中样例"]) < 2:
                        samples["未命中样例"].append(
                            "{} | {}".format(
                                dig(it, "ticket_no.value", dig(it, "id", "?")),
                                dig(it, "state.label", dig(it, "state.value", "?")),
                            )
                        )
            continue

        counts[hit] += 1

    # 全量条数（接口通常直接给，没有就按拉到的条数算）
    total_path = cfg.get("count_field")
    if total_path:
        try:
            counts["_total_from_api"] = int(dig(payload, total_path) or 0)
        except SourceError:
            pass

    counts["resolved_today"] = _resolved_today(items, cfg)
    counts["_diag"] = {
        "shape": "rules",
        "items_returned": len(items),
        "counted": counts["p0"] + counts["p1"] + counts["p2"],
        "unmatched": unmatched,
        "malformed_items": malformed,
        "rule_errors": sorted(set(errors))[:5],
    }
    if samples:
        counts["_diag"].update(samples)
    return counts


def _resolved_today(items, cfg):
    """今天解决的数量。字段值按"日期字符串"或"毫秒时间戳"两种常见格式自动认。"""
    rfield = cfg.get("resolved_field")
    if not rfield:
        return 0

    today = time.strftime("%Y-%m-%d")
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        val = dig(it, rfield, None)
        if not val:
            continue
        s = str(val)
        if s.isdigit() and len(s) >= 12:        # 毫秒时间戳
            s = time.strftime("%Y-%m-%d", time.localtime(int(s) / 1000))
        if s[:10] == today:
            n += 1
    return n


# --------------------------------------------------------------------------
# transport：数据从哪来
# --------------------------------------------------------------------------

def fetch_http(cfg):
    url = cfg.get("url")
    if not url:
        raise SourceError("transport=http 时必须配 url。")

    method = str(cfg.get("method") or "GET").upper()
    headers = dict(cfg.get("headers") or {})
    timeout = float(cfg.get("timeout", 8))

    data = None
    body = cfg.get("body")
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = " ".join(e.read().decode("utf-8", "ignore").split())[:300]
        hint = ""
        if e.code in (401, 403):
            hint = ("\n  认证没过。检查 headers 里的 Token 是否过期；"
                    "配置里建议写 ${TICKET_TOKEN} 从环境变量读，别把 token 写死在文件里。")
        elif e.code == 404:
            hint = "\n  地址不存在。确认一下 URL 有没有多写/少写路径。"
        raise SourceError(
            "工单接口返回 HTTP {c}。\n  URL：{u}\n  响应：{d}{h}".format(
                c=e.code, u=url, d=detail, h=hint)
        )
    except urllib.error.URLError as e:
        raise SourceError(
            "连不上工单接口：{e}\n"
            "  URL：{u}\n"
            "  这是内网地址吗？那这个脚本得在公司网络里跑。".format(e=e, u=url)
        )
    except TimeoutError:
        raise SourceError(
            "工单接口 {t} 秒没响应：{u}\n"
            "  这个 timeout 不能随便调大 —— 板子拉 /tickets 的 socket 超时是 8 秒"
            "（固件 SL_HTTP_TIMEOUT_MS），\n"
            "  bridge 回得比它慢，板子会先掐断连接、把这一轮判成取数失败显示灰屏，\n"
            "  而 bridge 这边还会继续慢悠悠地问上游，最后报一堆 Broken pipe。\n"
            "  要放宽就去动板子的 SL_HTTP_TIMEOUT_MS（要重烧），两边一起改，留 2 秒余量。".format(
                t=timeout, u=url)
        )

    return parse_json(raw, url)


def fetch_file(cfg):
    path = cfg.get("path")
    if not path:
        raise SourceError("transport=file 时必须配 path。")
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    if not os.path.isfile(path):
        raise SourceError(
            "找不到文件 {p}。\n"
            "  如果这是别的程序定时导出的，先确认那个任务在跑、路径没写错。".format(p=path)
        )

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    if path.lower().endswith(".csv"):
        return list(csv.DictReader(io.StringIO(text)))

    return parse_json(text, path)


def fetch_sql(cfg):
    dsn = cfg.get("path") or cfg.get("dsn")
    query = cfg.get("query")
    if not dsn or not query:
        raise SourceError("transport=sql 时必须配 path（sqlite 文件）和 query。")
    if not os.path.isabs(dsn):
        dsn = os.path.join(os.getcwd(), dsn)

    # 只读打开，避免脚本意外改动工单库
    uri = "file://{}?mode=ro".format(urllib.parse.quote(dsn))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        raise SourceError("打不开 sqlite 库 {p}：{e}".format(p=dsn, e=e))

    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()
        # 每行当一条工单，复用 list 模式的分组逻辑
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        raise SourceError(
            "SQL 执行失败：{e}\n"
            "  SQL：{q}\n"
            "  SQL 的结果应该是「一行一个工单」，并且包含优先级列，"
            "然后把它填进 priority_field。".format(e=e, q=query)
        )
    finally:
        conn.close()


def fetch_command(cfg):
    cmd = cfg.get("command")
    if not cmd:
        raise SourceError("transport=command 时必须配 command。")
    timeout = float(cfg.get("timeout", 15))

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise SourceError("命令 {t} 秒没跑完：{c}".format(t=timeout, c=cmd))

    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()

    if proc.returncode != 0:
        raise SourceError(
            "命令返回码 {r}：{c}\n"
            "  标准错误：{e}".format(r=proc.returncode, c=cmd, e=err[:400] or "(空)")
        )
    if not out:
        raise SourceError(
            "命令没有输出。它应该往 stdout 打印 JSON。\n"
            "  命令：{c}\n"
            "  标准错误：{e}".format(c=cmd, e=err[:400] or "(空)")
        )

    return parse_json(out, "命令的输出")


# --------------------------------------------------------------------------
# transport=tuyacs：涂鸦 CS 工单系统（cs.tuya-inc.com）
# --------------------------------------------------------------------------

CS_DEFAULT_BASE = "https://cs.tuya-inc.com:7799"
CS_LIST_PATH = "/api/ticket/get_list"
CS_VIEW_PATH = "/order/tech"

# csrf 跟登录态一起变，缓存住，避免每次轮询都去抓一次页面
_CSRF_CACHE = {}


def _ssl_ctx(verify):
    import ssl
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(url, data=None, headers=None, timeout=30, verify=True):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(verify)) as r:
        return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()


class Deadline:
    """
    一次取数的**总时间预算**。

    为什么必须按总预算算，而不是按每次重试算 —— 这是实测踩出来的：

    调用方（板子）拉 /tickets 的 socket 超时是 8 秒（固件里的 SL_HTTP_TIMEOUT_MS）。
    bridge 回得比这慢，板子就先掐断连接、把这一轮判成取数失败显示灰屏；
    而 bridge 还在慢悠悠地问上游，最后往一个已经关掉的 socket 写响应头 ——
    日志里刷的那些 BrokenPipeError 就是这么来的（实测逮到过一次 **32 秒**才回的请求）。

    `timeout=30 × retries=4` 的最坏情况是 120 秒，比板子的容忍度大一个数量级。
    有了预算之后，重试要么在预算内完成，要么立刻认输返回 502 ——
    让板子干净地显示 OFFLINE，而不是两边在超时上互相干等。
    """

    def __init__(self, total):
        self.end = time.monotonic() + max(1.0, float(total))

    def left(self):
        return self.end - time.monotonic()

    def cap(self, want):
        """单次尝试最多给多久 —— 不超过剩余预算。"""
        return max(0.5, min(float(want), self.left()))


def _with_retry(fn, tries, what, deadline=None):
    """
    内网这台机器会偶发直接掐断 TLS 连接（SSLEOFError）。
    这不是配置错误，重试就好 —— 但重试次数用完必须把话说清楚。
    """
    last = None
    used = 0
    ran_out = False
    for i in range(max(1, tries)):
        used = i + 1
        try:
            return fn()
        except (urllib.error.URLError, OSError) as e:
            last = e
            if i < tries - 1:
                # 预算见底就别再重试了：重试出来的结果对方也等不到，
                # 只会让板子多显示几秒灰屏、再多刷几行 BrokenPipe。
                if deadline is not None and deadline.left() <= 1.0:
                    ran_out = True
                    break
                time.sleep(0.5 * (i + 1))

    # 报错必须说清「试了几次」。写死成 "连续 N 次失败" 会在预算提前放弃时
    # 变成假话 —— 明明只试了 1 次却报 4 次，照着这句去调 retries 会白花时间。
    extra = ""
    if ran_out:
        extra = ("\n  注：这是**时间预算**用完提前放弃的（试了 {u}/{n} 次），"
                 "不是次数用尽。\n"
                 "  板子等不了更久 —— 见 sources.Deadline 的说明。".format(u=used, n=tries))
    raise SourceError(
        "{w}失败 {u}/{n} 次：{e}{x}\n"
        "  如果报的是 SSL/EOF/reset 之类的网络错，多半是内网抖动或 VPN 断了，\n"
        "  过一会儿会自动恢复；确认网络正常的话把 retries 调大一点。".format(
            w=what, u=used, n=tries, e=last, x=extra)
    )


def _cs_headers(cookie, csrf, base):
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-Requested-With": "XMLHttpRequest",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "csrf-token": csrf,
        "Cookie": cookie.replace("\n", " ").strip(),
        "Referer": base + CS_VIEW_PATH,
        "User-Agent": "Mozilla/5.0",
    }


def fetch_csrf(base, cookie, timeout, verify, tries, deadline=None):
    """从页面 HTML 里抠出 csrf（它在 __NEXT_DATA__ 的 rctxSync 里）。"""
    def once():
        return _request(base + CS_VIEW_PATH,
                        headers={"Cookie": cookie.replace("\n", " ").strip(),
                                 "User-Agent": "Mozilla/5.0"},
                        timeout=deadline.cap(timeout) if deadline else timeout,
                        verify=verify)

    code, html, final_url = _with_retry(once, tries, "抓 csrf 页面", deadline)

    if "login" in (final_url or "").lower():
        raise SourceError(
            "被弹回登录页了（{u}）—— 说明 Cookie 已经失效。\n"
            "  重新去浏览器 F12 → Network → 随便点一下工单列表 → Copy as cURL，\n"
            "  把里面的 cookie 整段复制出来，更新环境变量 CS_COOKIE。".format(u=final_url)
        )

    m = re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
    if not m:
        raise SourceError(
            "页面里没找到 csrf 字段。\n"
            "  你可以在浏览器 console 里执行 window.csrf 把值抄出来，\n"
            "  然后填到配置的 \"csrf\" 里。"
        )
    return m.group(1)


def fetch_tuyacs(cfg):
    base = (cfg.get("url") or CS_DEFAULT_BASE).rstrip("/")
    cookie = cfg.get("cookie")
    if not cookie:
        raise SourceError(
            "transport=tuyacs 时必须配 cookie。\n"
            "  推荐写成 \"cookie\": \"${CS_COOKIE}\"，然后：\n"
            "    export CS_COOKIE='你从浏览器复制的那一整段'"
        )

    # 总预算 —— 必须明显小于板子的 socket 超时（固件里默认 8 秒），
    # 否则板子会先掐线，而 bridge 还在问上游。见 Deadline 的说明。
    budget = float(cfg.get("budget", 6))
    dl = Deadline(budget)
    timeout = float(cfg.get("timeout", 30))
    tries = int(cfg.get("retries", 4))
    verify = bool(cfg.get("tls_verify", True))

    view_code = cfg.get("view_code")
    if not view_code:
        raise SourceError(
            "transport=tuyacs 时必须配 view_code。\n"
            "  技术工单「待我处理」是 techTicketCSMyPending。"
        )

    # 1) csrf：配置里给了就用，否则自动抓一次并缓存
    csrf = cfg.get("csrf")
    cache_key = (base, cookie[-24:])
    if not csrf:
        csrf = _CSRF_CACHE.get(cache_key)
        if not csrf:
            csrf = fetch_csrf(base, cookie, timeout, verify, tries, dl)
            _CSRF_CACHE[cache_key] = csrf

    payload = {
        "page_no": 1,
        "page_size": int(cfg.get("page_size", 500)),
        "platform": int(cfg.get("platform", 8)),
        "view_code": view_code,
        "request_lang": cfg.get("request_lang", "zh"),
        "tenant_id": cfg.get("tenant_id", ""),
        "sort_field": cfg.get("sort_field", "gmtCreate"),
        "sort_direction": cfg.get("sort_direction", "desc"),
    }
    # 注意：search_field 要传**对象**。传字符串服务端会回 "系统错误，请联系管理员"。
    if cfg.get("search_field"):
        payload["search_field"] = cfg["search_field"]

    body = json.dumps(payload).encode("utf-8")
    headers = _cs_headers(cookie, csrf, base)

    def once():
        code, text, _ = _request(base + CS_LIST_PATH, data=body,
                                 headers=headers, timeout=dl.cap(timeout),
                                 verify=verify)
        try:
            return json.loads(text)
        except ValueError:
            raise SourceError(
                "工单接口没有返回 JSON（HTTP {}）。\n"
                "  前 200 字：{}".format(code, text[:200])
            )

    resp = _with_retry(once, tries, "拉工单列表", dl)

    if not resp.get("success"):
        code = resp.get("code")
        msg = resp.get("msg") or resp.get("errorMsg") or ""

        if code in (1010, 401) or "登录" in str(msg) or "login" in str(msg).lower():
            _CSRF_CACHE.pop(cache_key, None)
            raise SourceError(
                "登录态失效了（code={c} msg={m}）。\n"
                "  Cookie 是有有效期的，隔几天就要换一次。\n"
                "  重新去浏览器 F12 复制 cookie，更新环境变量 CS_COOKIE 再启动。".format(
                    c=code, m=msg)
            )
        raise SourceError(
            "工单接口返回失败：code={c} msg={m}\n"
            "  10200001「系统错误」通常是参数结构不对（比如 search_field 传成了字符串）。".format(
                c=code, m=msg)
        )

    return resp


TRANSPORTS = {
    "http": fetch_http,
    "file": fetch_file,
    "sql": fetch_sql,
    "command": fetch_command,
    "tuyacs": fetch_tuyacs,
}


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------

class TicketsSource(object):
    def __init__(self, cfg):
        # 原始配置留着不丢 —— fetch 时每次重新展开，这样凭据文件被后台刷新后，
        # 长驻进程也能吃到新值。
        self.raw_cfg = cfg
        self.cfg = expand_env(cfg)
        self.last_payload = None

    # ---------- 构造 ----------
    @classmethod
    def from_file(cls, path):
        if not os.path.isfile(path):
            raise SourceError(
                "找不到配置文件 {p}\n"
                "  复制一份改：cp status_light.example.json status_light.json".format(p=path)
            )
        with open(path, "r", encoding="utf-8") as f:
            try:
                doc = json.load(f)
            except ValueError as e:
                raise SourceError(
                    "配置文件 {p} 不是合法 JSON：{e}\n"
                    "  常见原因：多了尾逗号、注释没删（JSON 不支持 // 注释）。".format(p=path, e=e)
                )

        source = doc.get("source")
        if not source:
            raise SourceError(
                "配置文件 {p} 里缺少 \"source\" 段。\n"
                "  照着 status_light.example.json 补一份。".format(p=path)
            )

        obj = cls(source)
        obj.document = doc
        obj.validate()
        return obj

    # ---------- 校验 ----------
    def validate(self):
        transport = str(self.cfg.get("transport") or "http").lower()
        if transport not in TRANSPORTS:
            raise SourceError(
                "不认识的 transport：{t!r}，可选 {opt}。".format(
                    t=transport, opt=" / ".join(sorted(TRANSPORTS)))
            )

        shape = str(self.cfg.get("shape") or "list").lower()
        if shape not in ("list", "counts", "rules"):
            raise SourceError(
                "不认识的 shape：{s!r}，可选 list（返回工单数组，按优先级数）"
                " / counts（接口直接给数量）"
                " / rules（返回工单数组，按你自己写的规则数）。".format(s=shape)
            )

        if shape == "list" and not self.cfg.get("priority_map"):
            raise SourceError(
                "shape=list 需要 priority_map，告诉程序你们的优先级叫什么名字。\n"
                "  如果你更想按「状态 + 客户有没有回复」来判定，用 shape=rules。\n"
                "  参考 status_light.example.json 里的写法。"
            )
        if shape == "rules" and not self.cfg.get("rules"):
            raise SourceError(
                "shape=rules 需要 rules —— 告诉程序什么情况算 p0。\n"
                "  参考 status_light.example.json 里的 rules 段。"
            )
        return self

    @property
    def announce(self):
        """播报措辞。都写在这里，改词不用重新烧固件、也不用碰代码。"""
        return (self.document or {}).get("announce") or {}

    # ---------- 取数 ----------
    def fetch(self):
        # 每次取数都重新展开配置。cookie_sync.py 在后台刷新了 ~/.cs_cookie 之后，
        # 下一次取数就会自动用上新的登录态 —— 不需要重启 bridge.py。
        self.cfg = expand_env(self.raw_cfg)

        transport = str(self.cfg.get("transport") or "http").lower()
        shape = str(self.cfg.get("shape") or "list").lower()

        payload = TRANSPORTS[transport](self.cfg)
        self.last_payload = payload

        if shape == "counts":
            return count_counts_mode(self.cfg, payload)
        if shape == "rules":
            return count_rules_mode(self.cfg, payload)
        return count_list_mode(self.cfg, payload)


def render_announce(template, values):
    """
    渲染播报模板。模板里可以放 {p0} {p1} {p2} {total} {resolved_today}。

    缺变量时保留占位符原样，而不是抛 KeyError —— 播报少个数字，
    总比整个轮询循环因为一个笔误挂掉好。
    """
    if not template:
        return ""

    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return str(template).format_map(_Safe(values))


def trim_payload(obj, keep=1):
    """
    把返回里的长数组截成前 keep 条，其余折叠成一行说明。

    真实工单接口动辄返回几百条工单，原样打印会把终端刷满，
    反而看不到"字段到底叫什么"——那是你要的唯一信息。
    """
    if isinstance(obj, dict):
        return {k: trim_payload(v, keep) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) <= keep:
            return [trim_payload(v, keep) for v in obj]
        head = [trim_payload(v, keep) for v in obj[:keep]]
        head.append("…（此处还有 {} 条同类记录，已折叠）".format(len(obj) - keep))
        return head
    return obj
