#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dp_push.py —— 把工单状态通过涂鸦云开放 API 下发成 DP，让 Cobuilder 生成的固件去渲染。

和 bridge.py 的分工：
    bridge.py    从工单系统拿数据 + 判断状态跳变，对外暴露  GET /tickets
    dp_push.py   把 /tickets 的结果翻译成 DP，调用涂鸦云「下发指令」接口
    固件         Cobuilder 生成，只做一件事：DP 变了就改屏幕、说话

为什么走这条而不是固件里直连局域网：
    Cobuilder 生成的固件源码你最好别手改（下一轮重新生成就覆盖了）。
    用 DP 当接口，等于把「业务逻辑」和「AI 生成的固件」解耦——
    以后改播报措辞、加数据源，都不用碰固件、不用重新烧录。

零第三方依赖，只用标准库。

用法：
    # 0. 写配置（交互式，不用手编 JSON）
    python3 dp_push.py --init

    # 1. 自检签名实现是否正确（用官方文档里的样例向量，不联网）
    python3 dp_push.py --selftest

    # 2. 查一下设备是否可达
    python3 dp_push.py --probe

    # 3. 手动推一次，肉眼验证屏幕和播报
    python3 dp_push.py --set p0_count=3 status_level=urgent announce_text="有 3 个 P0 工单，请立刻处理"

    # 4. 常驻：每 30 秒拉一次 bridge.py，状态变了才推
    python3 dp_push.py --watch --interval 30

更省事的做法：直接跑 ./start.sh，它会把上面 1~4 步串起来。
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "tuya_cloud.json")

# 涂鸦云各区域的数据中心域名。默认用中国区，按你开发者账号所在区域改。
REGION_HOSTS = {
    "cn": "https://openapi.tuyacn.com",
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "ueaz": "https://openapi-ueaz.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
    "sg": "https://openapi-sg.trustpc.com",
}

EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


# --------------------------------------------------------------------------
# 签名：严格按官方《签名机制》实现
#   令牌 API   sign = HMAC-SHA256(client_id + t + nonce + stringToSign, secret)
#   业务 API   sign = HMAC-SHA256(client_id + access_token + t + nonce + stringToSign, secret)
#   两者结果都要转大写
# --------------------------------------------------------------------------
def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_string_to_sign(method: str, body: bytes, url_with_query: str, signed_headers=None) -> str:
    content_sha256 = _sha256_hex(body) if body else EMPTY_BODY_SHA256

    headers_part = ""
    if signed_headers:
        for k, v in signed_headers.items():
            headers_part += "{}:{}\n".format(k, v)

    # 注意：headers_part 为空时，这里会留下一行空行——这是官方格式要求的
    return "{}\n{}\n{}\n{}".format(method.upper(), content_sha256, headers_part, url_with_query)


def hmac_sign(message: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()


class TuyaCloud(object):
    def __init__(self, client_id, secret, region="cn", debug=False):
        self.client_id = client_id
        self.secret = secret
        self.host = REGION_HOSTS.get(region, REGION_HOSTS["cn"])
        self.debug = debug
        self._token = None
        self._token_expire_at = 0.0

    # ---------------- 底层请求 ----------------
    def _request(self, method, path, query=None, body_obj=None, with_token=True):
        query = query or {}
        body_bytes = b""
        if body_obj is not None:
            body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        # URL 参与签名的是 Path + 排序后的 Query，不含域名
        sorted_items = sorted(query.items())
        url_for_sign = path
        if sorted_items:
            url_for_sign += "?" + "&".join("{}={}".format(k, v) for k, v in sorted_items)

        t = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        string_to_sign = build_string_to_sign(method, body_bytes, url_for_sign)

        if with_token:
            token = self.get_token()
            sign_src = self.client_id + token + t + nonce + string_to_sign
        else:
            sign_src = self.client_id + t + nonce + string_to_sign

        sign = hmac_sign(sign_src, self.secret)

        headers = {
            "client_id": self.client_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "nonce": nonce,
            "Content-Type": "application/json",
        }
        if with_token:
            headers["access_token"] = self.get_token()

        full_url = self.host + path
        if sorted_items:
            full_url += "?" + "&".join("{}={}".format(k, v) for k, v in sorted_items)

        if self.debug:
            sys.stderr.write("[sign] {}\n".format(url_for_sign))
            sys.stderr.write("[sign] sign={}\n".format(sign))

        req = urllib.request.Request(
            full_url, data=body_bytes if body_bytes else None, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            raise RuntimeError("HTTP {} {} -> {}".format(e.code, full_url, detail))
        except urllib.error.URLError as e:
            raise RuntimeError("网络不可达 {} -> {}".format(full_url, e))

    # ---------------- 令牌 ----------------
    def get_token(self, force=False):
        if not force and self._token and time.time() < self._token_expire_at:
            return self._token

        resp = self._request(
            "GET", "/v1.0/token", query={"grant_type": "1"}, with_token=False
        )
        if not resp.get("success"):
            raise RuntimeError("获取 token 失败：{}".format(resp))

        result = resp["result"]
        self._token = result["access_token"]
        # 有效期 2 小时，提前 10 分钟换新的
        self._token_expire_at = time.time() + int(result.get("expire_time", 7200)) - 600
        return self._token

    # ---------------- 业务接口 ----------------
    def get_device(self, device_id):
        return self._request("GET", "/v1.0/devices/{}".format(device_id))

    def send_commands(self, device_id, commands):
        """commands: [{"code": "p0_count", "value": 3}, ...]"""
        return self._request(
            "POST",
            "/v1.0/devices/{}/commands".format(device_id),
            body_obj={"commands": commands},
        )


# --------------------------------------------------------------------------
# 状态 -> DP 的映射
#
# 措辞的**唯一权威来源**是 status_light.json 里的 announce 段（也就是你自己写的话）。
# 这里的两句只是兜底：bridge.py 没给 announce_text，或者你直接 --set 手动推的时候用。
# --------------------------------------------------------------------------

ANNOUNCE_URGENT = "注意，有 {p0} 个 P0 工单，请立刻处理"
ANNOUNCE_CELEBRATE = "干得漂亮，今天的工单清零了"

STATUS_CONFIG_PATH = os.path.join(HERE, "status_light.json")

# bridge.py 用的状态名和 DP 里用的不是一套，这里做一次归一化：
#     bridge.py : idle  / warning / urgent / offline
#     DP        : ok    / warn    / urgent / offline
BRIDGE_STATE_MAP = {
    "idle": "ok",
    "warning": "warn",
    "urgent": "urgent",
    "offline": "offline",
}

VALID_STATES = ("ok", "warn", "urgent", "offline")


def _safe_format(template, values):
    """模板里出现没定义的变量时保留占位符，而不是抛 KeyError 把轮询打断。"""
    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return str(template).format_map(_Safe(values))


def load_status_config(path=STATUS_CONFIG_PATH):
    """
    读 status_light.json，把里面的播报措辞拿来当兜底。

    这样你用 --set 手动推一次试效果时，念的也是你自己写的那句话。
    """
    global ANNOUNCE_URGENT, ANNOUNCE_CELEBRATE

    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (ValueError, OSError):
        return False

    ann = doc.get("announce") or {}
    if ann.get("urgent"):
        ANNOUNCE_URGENT = ann["urgent"]
    if ann.get("celebrate"):
        ANNOUNCE_CELEBRATE = ann["celebrate"]
    return True


def state_of(p0, p1, p2):
    if p0 > 0:
        return "urgent"
    if p1 > 0 or p2 > 0:
        return "warn"
    return "ok"


def announce_text_for(state, p0, p1, p2):
    """默认措辞。--watch 时优先用 bridge.py 传来的 announce_text。"""
    if state == "urgent":
        return _safe_format(ANNOUNCE_URGENT, {"p0": p0, "p1": p1, "p2": p2,
                                              "total": p0 + p1 + p2})
    return ""


def build_dps(p0, p1, p2, celebrate=False, sync_time=None, announce_text=None, state=None):
    """
    announce_text 传 None -> 用默认措辞（--set 路径）
    announce_text 传 ""   -> 明确不播报（状态没跳变就别重复喊）
    """
    state = state or state_of(p0, p1, p2)
    if state not in VALID_STATES:
        raise SystemExit("未知的 status_level：{}，可选 {}".format(state, VALID_STATES))

    if announce_text is None:
        announce_text = announce_text_for(state, p0, p1, p2)

    dps = [
        {"code": "p0_count", "value": int(p0)},
        {"code": "p1_count", "value": int(p1)},
        {"code": "p2_count", "value": int(p2)},
        {"code": "status_level", "value": state},
        {"code": "announce_text", "value": announce_text},
        {"code": "sync_time", "value": sync_time or time.strftime("%H:%M")},
    ]
    if celebrate:
        dps.append({"code": "celebrate", "value": True})
    return dps, state


# --------------------------------------------------------------------------
def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise SystemExit(
            "找不到配置文件 {}\n"
            "先跑一次初始化（会问你三个值，不用手编 JSON）：\n"
            "    python3 dp_push.py --init".format(path)
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("client_id", "secret", "device_id"):
        if not cfg.get(key) or str(cfg[key]).startswith("填"):
            raise SystemExit(
                "配置文件里的 {} 还没填（当前值：{!r}）\n"
                "改一下，或者重跑：python3 dp_push.py --init".format(key, cfg.get(key))
            )
    if cfg.get("region", "cn") not in REGION_HOSTS:
        raise SystemExit(
            "不认识的 region：{!r}，可选 {}".format(
                cfg.get("region"), "/".join(REGION_HOSTS))
        )
    return cfg


def _explain_cloud_error(e):
    """把底层异常翻译成能直接照做的排查清单。"""
    text = str(e)
    print("\n调用涂鸦云失败：")
    print("  {}".format(text))
    print("\n按这个顺序排查：")
    if "sign" in text.lower() or "1004" in text:
        print("  1. region 和设备所在的数据中心一致吗 —— 填错就先报签名错误")
        print("  2. client_id / secret 是「云应用」里的 Access ID / Access Secret，别拿错")
    elif "1106" in text or "permission" in text.lower():
        print("  1. 云应用有没有开通「设备控制」相关的 API 权限")
    elif "28841105" in text or "not exist" in text.lower():
        print("  1. device_id 是不是这台状态灯的（设备管理里点开看）")
    else:
        print("  1. client_id / secret / device_id 三个值都对吗")
        print("  2. region 和设备所在的数据中心一致吗")
        print("  3. 网络能访问 {} 吗".format(REGION_HOSTS.get("cn")))
    print("\n想看到原始响应，加 --debug。")
    return 1


def fetch_tickets(tickets_url):
    with urllib.request.urlopen(tickets_url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_set_args(pairs):
    """把 p0_count=3 / celebrate=true 这类参数转成带类型的 DP 列表"""
    int_fields = {"p0_count", "p1_count", "p2_count"}
    bool_fields = {"celebrate"}
    dps = []
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit("参数格式应为 key=value，收到：{}".format(pair))
        key, raw = pair.split("=", 1)
        if key in int_fields:
            value = int(raw)
        elif key in bool_fields:
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            value = raw
        dps.append({"code": key, "value": value})
    return dps


def cmd_selftest():
    """用官方文档里的两组样例向量验证签名实现，避免上线后对着 400 猜原因。"""
    cases = [
        (
            "令牌管理 API",
            {
                "client_id": "1KAD46OrT9HafiKdsXeg",
                "secret": "4OHBOnWOqaEC1mWXOpVL3yV50s0qGSRC",
                "t": "1588925778000",
                "nonce": "5138cc3a9033d69856923fd07b491173",
                "signed_headers": {
                    "area_id": "29a33e8796834b1efa6",
                    "call_id": "8afdb70ab2ed11eb85290242ac130003",
                },
                "method": "GET",
                "body": b"",
                "url": "/v1.0/token?grant_type=1",
                "expect": "9E48A3E93B302EEECC803C7241985D0A34EB944F40FB573C7B5C2A82158AF13E",
                "use_token": False,
            },
        ),
        (
            "业务管理 API",
            {
                "client_id": "1KAD46OrT9HafiKdsXeg",
                "secret": "4OHBOnWOqaEC1mWXOpVL3yV50s0qGSRC",
                "access_token": "3f4eda2bdec17232f67c0b188af3eec1",
                "t": "1588925778000",
                "nonce": "5138cc3a9033d69856923fd07b491173",
                "signed_headers": {
                    "area_id": "29a33e8796834b1efa6",
                    "call_id": "8afdb70ab2ed11eb85290242ac130003",
                },
                "method": "GET",
                "body": b"",
                "url": "/v2.0/apps/schema/users?page_no=1&page_size=50",
                "expect": "AE4481C692AA80B25F3A7E12C3A5FD9BBF6251539DD78E565A1A72A508A88784",
                "use_token": True,
            },
        ),
    ]

    ok = True
    for name, c in cases:
        sts = build_string_to_sign(
            c["method"], c["body"], c["url"], signed_headers=c["signed_headers"]
        )
        if c["use_token"]:
            src = c["client_id"] + c["access_token"] + c["t"] + c["nonce"] + sts
        else:
            src = c["client_id"] + c["t"] + c["nonce"] + sts
        got = hmac_sign(src, c["secret"])
        passed = got == c["expect"]
        ok = ok and passed
        print("[{}] {} {}".format("PASS" if passed else "FAIL", name, got))
        if not passed:
            print("      期望值 {}".format(c["expect"]))

    # 空 body 的 sha256 也要对
    empty_ok = EMPTY_BODY_SHA256 == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    print("[{}] 空 body 的 SHA-256".format("PASS" if empty_ok else "FAIL"))
    ok = ok and empty_ok

    print("\n签名实现{}。".format("正确" if ok else "有问题，先别往下走"))
    return 0 if ok else 1


def cmd_init(path=CONFIG_PATH):
    """交互式写配置，省得手编 JSON 编错。"""
    print("=" * 62)
    print(" 涂鸦云凭据配置")
    print("=" * 62)
    print(" 三个值都在涂鸦开发者平台：")
    print("   client_id / secret -> 云开发 → 你的云应用")
    print("   device_id          -> 设备管理 → 点开那台状态灯")
    print(" 区域必须和设备所在的数据中心一致，填错会报签名错误。")
    print()

    if os.path.exists(path):
        ans = input("{} 已存在，覆盖？[y/N] ".format(os.path.basename(path)))
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 0
        print()

    region = input("区域 cn/us/eu/ueaz/in/sg [cn]: ").strip() or "cn"
    if region not in REGION_HOSTS:
        print("不认识的区域 {}，可选：{}".format(region, "/".join(REGION_HOSTS)))
        return 1

    client_id = input("client_id  (Access ID)      : ").strip()
    secret = input("secret     (Access Secret)   : ").strip()
    device_id = input("device_id  (状态灯的设备 ID)  : ").strip()

    missing = [
        name for name, val in
        (("client_id", client_id), ("secret", secret), ("device_id", device_id))
        if not val
    ]
    if missing:
        print("\n这些不能为空：{}".format(" / ".join(missing)))
        return 1

    cfg = {
        "client_id": client_id,
        "secret": secret,
        "device_id": device_id,
        "region": region,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)  # 里面有 secret，别让同机器其他用户读
    except OSError:
        pass

    print("\n已写入 {}（权限 600）".format(path))
    print("下一步：python3 dp_push.py --probe")
    return 0


def cmd_probe(cloud, device_id):
    resp = cloud.get_device(device_id)
    if not resp.get("success"):
        print("设备查询失败：{}".format(json.dumps(resp, ensure_ascii=False)))
        return 1
    result = resp.get("result") or {}
    print("设备名称：{}".format(result.get("name")))
    print("设备 ID ：{}".format(result.get("id")))
    print("在线状态：{}".format("在线" if result.get("online") else "离线"))
    print("激活时间：{}".format(result.get("active_time")))
    print("\n提示：如果显示离线，DP 下发了设备也收不到。先把板子联网再试。")
    return 0


def cmd_watch(cloud, device_id, tickets_url, interval, celebrate_hold=20):
    """
    轮询 bridge.py，只在状态变化时下发。

    两个容易被忽略的细节：

    1. bridge.py 的 audio_url 只在「状态跳变」时才出现
       （urgent.mp3 = 进 P0，celebrate.mp3 = 当天清零）。
       它就是"这次要不要出声"的信号源，别自己去猜。

    2. 庆祝要下发两次：先 celebrate=true 触发动画，再 celebrate=false 复位。
       但复位那一拍必须等动画放完 —— 否则板子刚闪了 1 秒就被打断。
       所以有 celebrate_hold（默认 20 秒，固件里的动画是 15 秒）。
    """
    last_snapshot = None
    last_celebrate_at = 0.0

    # 先验一次凭据，别让它在循环里每 30 秒刷一遍同样的报错
    try:
        cloud.get_token()
    except RuntimeError as e:
        return _explain_cloud_error(e)

    print("开始监听 {}".format(tickets_url))
    print("每 {} 秒一次，只在状态变化时下发 DP。Ctrl-C 退出。".format(interval))
    print("-" * 62)

    while True:
        try:
            data = fetch_tickets(tickets_url)
            p0 = int(data.get("p0", 0))
            p1 = int(data.get("p1", 0))
            p2 = int(data.get("p2", 0))

            audio_url = data.get("audio_url") or ""

            # 事件名以 bridge.py 给的为准（p0_arrived / all_clear / warn）。
            # 没有 event 字段就退回从 audio_url 推断，兼容旧版 bridge.py。
            event = data.get("event") or ""
            if not event:
                if "celebrate" in audio_url:
                    event = "all_clear"
                elif "urgent" in audio_url:
                    event = "p0_arrived"

            celebrate = bool(data.get("celebrate", False)) or (event == "all_clear")

            # 措辞一律以 bridge.py 给的为准 —— 它渲染的是 status_light.json 里的模板，
            # 也就是你自己写的那几句话。bridge.py 没给（旧版本或裸调用）才用兜底。
            bridge_announce = data.get("announce_text") or ""
            if bridge_announce:
                announce = bridge_announce
            elif event == "all_clear":
                announce = ANNOUNCE_CELEBRATE
            elif event == "p0_arrived":
                announce = announce_text_for("urgent", p0, p1, p2)
            else:
                announce = ""  # 状态没跳变，这次不播

            state_hint = BRIDGE_STATE_MAP.get(data.get("state") or "")

            # state 也算进去：数据源掉线（offline）时 p0/p1/p2 都是 0，
            # 不把 state 纳入比较的话，"掉线"这一拍会被当成"没变化"而漏掉。
            snapshot = (p0, p1, p2, celebrate, state_hint)

            # 庆祝复位要等动画放完
            if (not celebrate and last_snapshot is not None and last_snapshot[3]
                    and (time.time() - last_celebrate_at) < celebrate_hold):
                time.sleep(interval)
                continue

            if snapshot != last_snapshot:
                dps, state = build_dps(
                    p0, p1, p2,
                    celebrate=celebrate,
                    announce_text=announce,
                    state=state_hint,
                )
                resp = cloud.send_commands(device_id, dps)
                ok = bool(resp.get("success"))
                print("[{}] {} {}  p0={} p1={} p2={}{}".format(
                    time.strftime("%H:%M:%S"),
                    "OK " if ok else "ERR",
                    state, p0, p1, p2,
                    "  celebrate" if celebrate else "",
                ))
                if ok:
                    if announce:
                        print("           播报：{}".format(announce))
                    last_snapshot = snapshot
                    if celebrate:
                        last_celebrate_at = time.time()
                else:
                    print("           云端返回：{}".format(
                        json.dumps(resp, ensure_ascii=False)))
        except KeyboardInterrupt:
            print("\n已停止。")
            return 0
        except Exception as e:  # 网络抖动不要退出，下一轮继续
            print("[{}] 异常：{}".format(time.strftime("%H:%M:%S"), e))

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="把工单状态通过涂鸦云 DP 下发给 T5 状态灯")
    parser.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--init", action="store_true", help="交互式生成 tuya_cloud.json")
    parser.add_argument("--selftest", action="store_true", help="用官方样例向量自检签名实现")
    parser.add_argument("--probe", action="store_true", help="查询设备信息，确认 device_id 和设备在线")
    parser.add_argument("--set", nargs="+", metavar="key=value", help="手动下发一组 DP")
    parser.add_argument("--watch", action="store_true", help="常驻，轮询 bridge.py 并同步 DP")
    parser.add_argument(
        "--tickets-url", default="http://127.0.0.1:8080/tickets", help="bridge.py 的地址"
    )
    parser.add_argument(
        "--status-config", default=STATUS_CONFIG_PATH,
        help="status_light.json 的路径（读里面的播报措辞当兜底）",
    )
    parser.add_argument("--interval", type=int, default=30, help="轮询间隔（秒）")
    parser.add_argument(
        "--celebrate-hold", type=int, default=20,
        help="庆祝动画放完前不复位 celebrate，单位秒（固件里动画 15 秒）",
    )
    parser.add_argument("--debug", action="store_true", help="打印参与签名的字符串")
    args = parser.parse_args()

    # 这两个不需要配置，先处理掉
    if args.selftest:
        return cmd_selftest()
    if args.init:
        return cmd_init(args.config)

    cfg = load_config(args.config)
    load_status_config(args.status_config)
    cloud = TuyaCloud(
        cfg["client_id"], cfg["secret"], region=cfg.get("region", "cn"), debug=args.debug
    )
    device_id = cfg["device_id"]

    if args.probe:
        try:
            return cmd_probe(cloud, device_id)
        except RuntimeError as e:
            return _explain_cloud_error(e)

    if args.set:
        dps = parse_set_args(args.set)
        print("准备下发 {} 条 DP：{}".format(
            len(dps), ", ".join("{}={}".format(d["code"], d["value"]) for d in dps)))
        try:
            resp = cloud.send_commands(device_id, dps)
        except RuntimeError as e:
            return _explain_cloud_error(e)
        if resp.get("success"):
            print("下发成功，去看板子。")
            return 0
        print("下发失败：")
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        print("\n排查顺序：")
        print("  1. DP 是不是被定义成「仅上报」而不是「可下发可上报」")
        print("  2. DP 标识符和物模型里的逐字一致吗")
        print("  3. 设备在线吗（先跑 --probe）")
        print("  4. 枚举值在物模型定义的选项里吗")
        return 1

    if args.watch:
        return cmd_watch(cloud, device_id, args.tickets_url, args.interval,
                         celebrate_hold=args.celebrate_hold)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
