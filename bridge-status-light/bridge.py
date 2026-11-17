#!/usr/bin/env python3
"""
bridge.py —— T5 状态灯的数据中转服务

为什么需要它，而不是让开发板直接去调工单系统？

  1. 内部工单系统基本不会给一个局域网里的开发板开白名单；
  2. 认证逻辑（Token / 签名 / 单点登录）放在服务端处理，固件里只留一个 GET；
  3. "说什么话"由服务端决定 —— 想改成"有 3 个 P0"时只改这里，不用重新烧固件；
  4. 换工单系统（Jira / 禅道 / 内部自研 / 企业微信）只需要换 fetch_tickets() 一个函数。

对外只有一个接口给板子用：

  GET /tickets
  -> {"p0":2,"p1":5,"p2":9,"resolved_today":3,
      "updated":"14:22:05","audio_url":"http://192.168.1.50:8080/audio/urgent.mp3"}

外加两个辅助接口：
  GET  /audio/<name>.mp3   静态音频，板子从这里拉播报
  POST /sim?p0=3&p1=0&p2=0 手动灌数据，演示和调试用（不用碰真实系统）

零第三方依赖，只用标准库。直接 python3 bridge.py 就能跑。
"""

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse, parse_qs

import sources

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(HERE, "audio")
STATUS_CONFIG = os.path.join(HERE, "status_light.json")

# 播报音频文件名。放 bridge/audio/ 目录，由 assets/make_audio.sh 生成。
#
# 默认 WAV：macOS 自带的 say 能直接输出 16kHz/16bit/单声道 WAV，
# 不需要 ffmpeg。固件是按 URL 后缀自动选解码器的，所以换成 mp3
# （AUDIO_EXT=.mp3）也照样能跑，不用改固件。
AUDIO_EXT = os.environ.get("AUDIO_EXT", ".wav")

AUDIO_URGENT = "urgent"        # 进 P0
AUDIO_CELEBRATE = "celebrate"  # 当天清零

# 后缀 -> Content-Type。板子不看这个头，但用浏览器/curl 验证时
# 它决定了会不会被当文件下载。
AUDIO_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}

# 播报措辞的兜底值。真正生效的是 status_light.json 里的 announce 段。
DEFAULT_ANNOUNCE = {
    "urgent": "注意，有 {p0} 个 P0 工单，请立刻处理",
    "celebrate": "干得漂亮，今天的工单清零了",
    "warn": "",
    "source_lost": "工单系统连不上了，请检查",
}
ANNOUNCE = dict(DEFAULT_ANNOUNCE)

# 取数连续失败的次数。
#
# 为什么需要这个：Cookie 过期、接口改了、对方系统挂了 —— 这些都会让取数失败。
# 如果失败时静静沿用上一次的数字，板子会绿着，而工单其实堆着没人管。
# 「看着一切正常，其实数据是死的」是这块板子最危险的失败模式，
# 所以连不上时要让它**看起来就不对劲**。
_fail = {"count": 0, "last_error": ""}
FAIL_THRESHOLD = 3

# 工单数据源。为 None 时退回内置的模拟数据（演示用）。
SOURCE = None

# 哪些桶参与"灯该是什么颜色"的判断。
#
# 为什么需要它：有些桶天生不会归零。比如涂鸦 CS 的「待我处理」队列里
# 有 270 多条在等客户补充材料的工单 —— 如果它们参与判断，灯会永远是黄的，
# 「全部清零 → 庆祝」这个动作一辈子都触发不了。
# 把这类"背景信息"排除掉，灯才有意义。
# 参与灯色判断的桶：只 p0 / p1（待客户补充 p2 不参与，也不上屏）。
ALERT_BUCKETS = ("p0", "p1")

# 上一次的快照，用于判断"状态是否发生变化"
_prev = {"p0": 0, "p1": 0, "p2": 0}


def local_ip() -> str:
    """拿到本机在局域网里的 IP —— 板子必须用这个地址才能访问到。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------
# 数据源：接你自己的工单系统
# --------------------------------------------------------------------------

def fetch_tickets() -> dict:
    """
    返回 {"p0": int, "p1": int, "p2": int, "resolved_today": int}

    数据来自 status_light.json 里配置的工单系统，具体怎么取、怎么数
    都在 sources.py 里。没有配置时退回模拟数据（方便先跑通链路）。

    换工单系统 = 改 status_light.json，不用动这个文件。
    """
    if SOURCE is None:
        return MOCK_STATE.copy()
    result = SOURCE.fetch()

    # 工单系统接口报错时会带上 error 字段，直接抛出去让上层返回 502
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(str(result["error"]))

    for key in ("p0", "p1", "p2", "resolved_today"):
        try:
            result[key] = int(result.get(key, 0) or 0)
        except (TypeError, ValueError):
            raise RuntimeError(
                "工单系统返回的 {} 不是数字：{!r}".format(key, result.get(key))
            )
    return result


# 演示用：POST /sim?p0=3 就能改这里
MOCK_STATE = {"p0": 0, "p1": 0, "p2": 0, "resolved_today": 0}


def load_status_config(path=STATUS_CONFIG, required=False):
    """
    读 status_light.json。返回 TicketsSource，或 None（没配且不强制）。

    报错信息直接打印并退出——配置错的时候继续跑只会让你在后面更难查。
    """
    global SOURCE, ANNOUNCE, ALERT_BUCKETS

    if not os.path.isfile(path):
        if required:
            print("找不到配置文件：{}".format(path))
            print("复制一份再改：cp status_light.example.json status_light.json")
            sys.exit(1)
        return None

    try:
        src = sources.TicketsSource.from_file(path)
    except sources.SourceError as e:
        print("配置文件有问题：")
        print("  {}".format(e))
        sys.exit(1)

    SOURCE = src

    cfg_announce = src.announce or {}
    for key in ANNOUNCE:
        if key in cfg_announce:
            ANNOUNCE[key] = cfg_announce[key]

    buckets = (src.document or {}).get("alert_buckets")
    if buckets:
        ALERT_BUCKETS = tuple(str(b) for b in buckets)
    return src


# 事件 -> 播报模板的键名
EVENT_ANNOUNCE_KEY = {
    "p0_arrived": "urgent",
    "all_clear": "celebrate",
    "warn": "warn",
    "source_lost": "source_lost",
}


def build_announce(event: Optional[str], t: dict) -> str:
    """
    状态跳变时给一句要播的话，否则返回空串（状态没变就别重复喊）。

    措辞来自 status_light.json 的 announce 段，可用的变量：
        {p0} {p1} {p2} {total} {resolved_today}
    """
    key = EVENT_ANNOUNCE_KEY.get(event or "")
    if not key:
        return ""

    template = ANNOUNCE.get(key, "")
    if not template:
        return ""

    values = {
        "p0": t.get("p0", 0),
        "p1": t.get("p1", 0),
        "p2": t.get("p2", 0),
        "total": sum(int(t.get(k, 0) or 0) for k in ("p0", "p1", "p2")),
        "resolved_today": t.get("resolved_today", 0),
    }
    return sources.render_announce(template, values)


# --------------------------------------------------------------------------
# 状态判定与播报选择 —— 和固件里的状态机保持一致
# --------------------------------------------------------------------------

def calc_state(t: dict) -> str:
    """
    决定灯的颜色。**参与判断的桶由配置里的 `alert_buckets` 决定**，
    默认 ("p0", "p1") —— p2 不参与：

      p0 待处理        -> urgent（红，闪 + 播报）
      其它 alert 桶有货 -> warning（黄，常亮）
      全 0             -> idle（绿）

    为什么必须有这个开关（不是可有可无的选项）：
    涂鸦 CS 队列里有 270 多条在等客户补材料的工单，**它们永远不会归零**。
    要是把 p2 也算进"在忙"，这盏灯就永远是黄的、永远不会亮 —— 那就成了个摆设。
    反过来，换成"待办清单"这类业务时，你会希望三个桶都参与。

    ⚠️ 这个变量以前**被读了却没用**（calc_state 写死了只看 p0/p1），
    于是 example 配置里那句 `alert_buckets` 是个静默失效的旋钮 ——
    设置成什么都不会生效，而且看不出任何异常。自测里那条红的 FAIL 就是它。
    """
    def n(bucket):
        return int(t.get(bucket, 0) or 0) if bucket in ALERT_BUCKETS else 0

    if n("p0") > 0:
        return "urgent"
    if any(n(b) > 0 for b in ALERT_BUCKETS if b != "p0"):
        return "warning"
    return "idle"


def pick_event(new_state: str, t: dict) -> Optional[str]:
    """
    只在"值得打扰你"的时候返回事件名，否则 None。

    事件是显式语义，比让下游去猜一个 URL 里有没有 "urgent" 靠谱得多。

    这里有个容易做错的地方：如果只在状态**跳变**时触发，那么
    「已经有 1 个工单在等你」变成「又多来 2 个」是不出声的 —— 可那正是
    你最想被叫一声的时刻。所以 P0 **变多**也算事件。
    """
    old_state = calc_state(_prev)
    old_p0 = int(_prev.get("p0", 0) or 0)
    new_p0 = int(t.get("p0", 0) or 0)

    # P0 变多：无论之前在不在 urgent，都值得响一次
    if new_p0 > old_p0:
        return "p0_arrived"

    if new_state == old_state:
        return None

    if new_state == "urgent":
        return "p0_arrived"     # 进 P0：整屏闪红 + 播报
    if new_state == "idle":
        return "all_clear"      # 全部清零：庆祝动画 + 播报
    return "warn"               # 进 warning：只变色，不发声


# 哪种事件配哪个音频文件。None 表示不发声。
EVENT_AUDIO = {
    "p0_arrived": AUDIO_URGENT,
    "all_clear": AUDIO_CELEBRATE,
    "warn": None,
}


def pick_audio(event: Optional[str]) -> Optional[str]:
    """事件 -> 音频文件名（含后缀）。返回 None 表示这次不发声。

    找不到文件也返回 None。这样服务端没放音频时，固件会退回板载提示音 ——
    比让板子去请求一个 404 好（404 在串口日志里长得很像网络故障，会带偏排查方向）。
    """
    if not event:
        return None
    base = EVENT_AUDIO.get(event)
    if not base:
        return None

    # 先按配置的后缀找，再试另一种常见后缀
    for ext in (AUDIO_EXT, ".mp3", ".wav"):
        name = base + ext
        if os.path.isfile(os.path.join(AUDIO_DIR, name)):
            return name
    return None


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "T5StatusBridge/1.0"

    # 单块音频一次最多发多少字节
    AUDIO_CHUNK = 16 * 1024

    # 给每个连接一个兜底超时。板子偶尔会在半路掐断连接，
    # 没有这个超时，卡住的写会一直占着一个线程（曾经把整个 bridge 卡死）。
    timeout = 30

    def log_message(self, fmt, *args):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
            # 板子等不及先走了：它自己的 socket 超时（固件里 8 秒）一到就掐断连接，
            # 我们再写响应头必然 Broken pipe。这不是故障 —— 板子会把这一轮判成
            # 取数失败、30 秒后重来一遍。所以别让 socketserver 刷一整屏栈，
            # 那只会盖住真正的错。（和 _serve_audio 的处理保持一致）
            return

    def _serve_audio(self, full):
        """把音频发出去 —— 必须支持 Range + HTTP/1.1 keep-alive。

        板子上的播放器是**边下边播**的（tuya ai player 的 datasink_url）：
        它给 socket 设的读超时只有 AI_PLAYER_HTTP_YIELD_MS（默认 10ms），
        某次读超时时 libhttp 会把 bytes_read==0 判成「对端提前关闭」并返回 -1，
        datasink 于是断开、改用 `Range: bytes=N-` 续传。

        所以服务端要是不认 Range，续传就会永远停在同一个偏移上原地打转：
        板子日志里表现为 `url sink read failed at offset 4096/115022` 无限刷。
        这里同时把响应升到 HTTP/1.1，让连接可以被复用，少几次重握手。
        """
        size = os.path.getsize(full)
        ext = os.path.splitext(full)[1].lower()
        mime = AUDIO_MIME.get(ext, "application/octet-stream")

        start, end = 0, size - 1
        partial = False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:                      # bytes=-N：取末尾 N 字节
                    start = max(0, size - int(b))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True
            except ValueError:
                start, end, partial = 0, size - 1, False

        length = end - start + 1

        # 只在音频这条路上用 1.1：/tickets 仍按原样走，不动它
        self.protocol_version = "HTTP/1.1"
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        remain = length
        try:
            with open(full, "rb") as f:
                f.seek(start)
                while remain > 0:
                    chunk = f.read(min(self.AUDIO_CHUNK, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
            # 板子拿够就断开重连是正常节奏，不必刷栈
            return

    def do_GET(self):
        path = urlparse(self.path).path
        print("[%s] <- %s %s" % (datetime.now().strftime("%H:%M:%S"),
                                  self.client_address[0], self.path))

        if path == "/tickets":
            try:
                raw = fetch_tickets()
            except Exception as e:                      # noqa: BLE001
                _fail["count"] += 1
                _fail["last_error"] = str(e)

                if _fail["count"] >= FAIL_THRESHOLD:
                    # 连续失败：把板子切到 offline。
                    # 宁可让它显示「我不知道」，也不要让它显示「一切正常」。
                    print("[%s] 取数已连续失败 %d 次，切换为 offline" % (
                        datetime.now().strftime("%H:%M:%S"), _fail["count"]))
                    self._json({
                        "p0": 0, "p1": 0, "p2": 0, "resolved_today": 0,
                        "state": "offline",
                        "event": "source_lost",
                        "announce_text": build_announce("source_lost", {}),
                        "error": _fail["last_error"],
                        "updated": datetime.now().strftime("%H:%M:%S"),
                    })
                    return

                self._json({
                    "error": str(e),
                    "failed_times": _fail["count"],
                    "note": "连续 {} 次取数失败后，板子会切到 offline".format(
                        FAIL_THRESHOLD),
                }, 502)
                return

            if _fail["count"]:
                print("[%s] 取数恢复正常（此前失败 %d 次）" % (
                    datetime.now().strftime("%H:%M:%S"), _fail["count"]))
            _fail["count"] = 0

            new_state = calc_state(raw)
            event = pick_event(new_state, raw)
            audio = pick_audio(event)

            # 播报只在状态跳变时给，措辞来自 status_light.json
            announce = build_announce(event, raw)

            _prev.update({k: raw.get(k, 0) for k in ("p0", "p1", "p2", "resolved_today")})

            resp = {
                "p0": raw.get("p0", 0),
                "p1": raw.get("p1", 0),
                "p2": raw.get("p2", 0),
                "resolved_today": raw.get("resolved_today", 0),
                "state": new_state,
                "updated": datetime.now().strftime("%H:%M:%S"),
            }
            if event:
                resp["event"] = event
            if audio:
                resp["audio_url"] = "http://%s:%d/audio/%s" % (
                    self.server.lan_ip, self.server.server_port, audio)
            if announce:
                resp["announce_text"] = announce

            self._json(resp)
            return

        if path.startswith("/audio/"):
            name = os.path.basename(path)
            full = os.path.join(AUDIO_DIR, name)
            if not os.path.isfile(full):
                self._json({"error": "audio not found", "name": name}, 404)
                return
            self._serve_audio(full)
            return

        if path == "/health":
            self._json({"ok": True, "time": time.time()})
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/sim":
            if SOURCE is not None:
                # 接了真实数据源时，mock 数字根本不参与输出。
                # 如果这里还回 {"ok": true}，你会以为灌进去了，然后在板子上白等。
                self._json({
                    "ok": False,
                    "reason": "当前用的是真实数据源（status_light.json），"
                              "/sim 改的是内置模拟数据，不会有任何效果。",
                    "怎么办": "想灌假数据演示，用 mock 模式启动："
                              "python3 bridge.py --mock --port 8080",
                }, 409)
                return

            q = parse_qs(parsed.query)
            for k in ("p0", "p1", "p2", "resolved_today"):
                if k in q:
                    try:
                        MOCK_STATE[k] = int(q[k][0])
                    except ValueError:
                        pass
            self._json({"ok": True, "mock_state": MOCK_STATE})
            return

        self._json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, lan_ip):
        super().__init__(addr, handler)
        self.lan_ip = lan_ip


def cmd_check(config_path, dump=False):
    """
    只拉一次工单数据并打印结果。不连云、不碰设备。

    接一个新系统的第一步就跑这个 —— 它会把「字段名对不对」
    「优先级有没有对上」这些问题一次暴露出来。
    """
    print("=" * 62)
    print(" 工单数据源自检")
    print("=" * 62)

    try:
        src = sources.TicketsSource.from_file(config_path)
    except sources.SourceError as e:
        print("配置有问题：")
        print("  {}".format(e))
        return 1

    cfg = src.cfg
    print(" 传输方式 : {}".format(cfg.get("transport", "http")))
    print(" 数据形态 : {}".format(cfg.get("shape", "list")))
    if cfg.get("url"):
        print(" 接口地址 : {}".format(cfg["url"]))
    elif cfg.get("path"):
        print(" 数据文件 : {}".format(cfg["path"]))
    elif cfg.get("command"):
        print(" 执行命令 : {}".format(cfg["command"]))
    print("-" * 62)

    try:
        t = src.fetch()
    except sources.SourceError as e:
        print("取数失败：")
        print("  {}".format(e))
        return 1

    print("  p0 = {}    p1 = {}    p2 = {}    今天解决 = {}".format(
        t["p0"], t["p1"], t["p2"], t.get("resolved_today", 0)))
    print("  判定状态 : {}".format(calc_state(t)))

    diag = t.get("_diag") or {}
    if diag.get("shape") == "list":
        print("-" * 62)
        print("  接口返回 {} 条，{} 条被状态规则过滤掉".format(
            diag.get("items_returned", 0), diag.get("skipped_by_status", 0)))
        if diag.get("malformed_items"):
            print("  {} 条格式不对（不是对象），已跳过".format(diag["malformed_items"]))

        unknown = diag.get("unmapped_priority_values") or {}
        if unknown:
            print()
            print("  ⚠ 这些优先级值没被映射，被忽略了：")
            for k, v in sorted(unknown.items(), key=lambda kv: -kv[1]):
                print("      {!r} 出现 {} 次".format(k, v))
            print("    把它们加进 priority_map；如果它们本来就不该算数，忽略这条提示。")

    if dump:
        print("-" * 62)
        print("  返回的结构（长数组已折叠成一条样例）：")
        trimmed = sources.trim_payload(src.last_payload)
        text = json.dumps(trimmed, ensure_ascii=False, indent=2)
        if len(text) > 1800:
            text = text[:1800] + "\n  ...（内容较长，已截断）"
        print(text)
        print()
        print("  把上面看到的字段名填回 status_light.json：")
        print("    工单数组的路径        -> list_path")
        print("    表示优先级的字段      -> priority_field")
        print("    表示状态的字段        -> status_field")

    print("=" * 62)
    return 0


def main():
    ap = argparse.ArgumentParser(description="T5 状态灯数据中转服务")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--p0", type=int, default=0, help="启动时的 mock P0 数量")
    ap.add_argument(
        "--status-config", default=STATUS_CONFIG,
        help="工单数据源配置（默认同目录的 status_light.json）",
    )
    ap.add_argument(
        "--check", action="store_true",
        help="只拉一次工单数据并打印，不启动服务、不连云",
    )
    ap.add_argument("--dump", action="store_true", help="配合 --check，打印原始返回")
    ap.add_argument(
        "--mock", action="store_true",
        help="强制使用内置模拟数据，忽略 status_light.json（演示 / 自测用）",
    )
    args = ap.parse_args()

    if args.check:
        return cmd_check(args.status_config, dump=args.dump)

    MOCK_STATE["p0"] = args.p0
    _prev.update(MOCK_STATE)

    src = None if args.mock else load_status_config(args.status_config)

    ip = local_ip()
    srv = Server((args.host, args.port), Handler, ip)

    print("=" * 62)
    print(" T5 状态灯 · 数据中转服务")
    print("=" * 62)
    if src is None:
        if args.mock:
            print(" 数据源     : 内置模拟数据（--mock 强制）")
        else:
            print(" 数据源     : 内置模拟数据（没找到 status_light.json）")
            print("              接真实工单系统：cp status_light.example.json status_light.json")
    else:
        cfg = src.cfg
        print(" 数据源     : {} / {} 模式".format(
            cfg.get("transport", "http"), cfg.get("shape", "list")))
        if cfg.get("url"):
            print(" 接口地址   : {}".format(cfg["url"]))
    print(" 板子请访问 : http://%s:%d/tickets" % (ip, args.port))
    print(" 手动模拟   : curl -X POST 'http://%s:%d/sim?p0=3'" % (ip, args.port))
    print(" 清零庆祝   : curl -X POST 'http://%s:%d/sim?p0=0&p1=0&p2=0'" % (ip, args.port))
    print(" 音频目录   : %s" % AUDIO_DIR)
    print("=" * 62)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
