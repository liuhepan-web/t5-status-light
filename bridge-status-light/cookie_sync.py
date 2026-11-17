#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 Chrome 自动同步工单系统的登录 Cookie。

======================================================================
【为什么需要这个工具】
======================================================================

cs.tuya-inc.com 的登录态由这几个 cookie 组成，有效期差别很大：

    s-sid             约 8 小时   ← 短命，这就是"session 一直变"的根源
    fast-sid          约 3 天
    SSO_USER_TOKEN    约 30 天

浏览器每次访问，服务端都会**滚动续期** s-sid。所以你从 F12 里手工复制的那一份，
最多撑 8 小时就失效了 —— 这不是配置错了，这就是它的设计。

手工复制的路子注定跑不长。正确的做法是：**既然浏览器里那份永远是最新的，
那就定期去浏览器里把它读出来。**

======================================================================
【怎么读出来的】
======================================================================

Chrome 把 cookie 存在 SQLite 里，值是 AES 加密的：

    ~/Library/Application Support/Google/Chrome/<Profile>/Cookies

    密钥  = PBKDF2-HMAC-SHA1(
              从 Keychain 取的 "Chrome Safe Storage" 密码,
              salt=b"saltysalt", 迭代 1003 次, 输出 16 字节)
    解密  = AES-128-CBC(key, IV = 16 个空格)
    密文前 3 字节是 "v10" 版本标记，要去掉
    明文前 32 字节是 sha256(域名)，也要去掉

这几步是 Chrome 的固定实现，不是什么黑魔法。本脚本**零依赖** —— 用 macOS 自带的
`security` 取密钥、`openssl` 做解密，不需要 pip 装任何东西。

======================================================================
【用法】
======================================================================

    python3 cookie_sync.py              读一次，更新 ~/.cs_cookie，并验证
    python3 cookie_sync.py --check      只看现状（关键 cookie 还有多久过期），不写
    python3 cookie_sync.py --watch      常驻，定期同步（默认 300 秒一次）
    python3 cookie_sync.py --print      把 cookie 打到 stdout（不写文件，供管道用）
    python3 cookie_sync.py --profile    列出所有 Chrome profile 及匹配到的 cookie 数

======================================================================
【安全】
======================================================================

    · 只读 Chrome 的 cookie 数据库，不写浏览器任何数据
    · 读 SQLite 时先复制一份再读 —— 既不锁浏览器，也不会碰到原库
    · 输出文件权限 0600（只有你自己能读）
    · 日志里永远不打印 cookie 的值，只打印名字、长度、过期时间
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# Chrome 的 cookie 库根目录。想用 Edge / Brave 的话把这里换掉即可
# （它们都是 Chromium 系，加密方式完全一样）。
CHROME_ROOTS = [
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Application Support/Microsoft Edge",
    "~/Library/Application Support/BraveSoftware/Brave-Browser",
]

# 工单系统的登录态藏在哪些域名下。
# 注意这两个是不同的 host_key：cs.tuya-inc.com 是精确域，
# .tuya-inc.com 是带前导点的父域 —— SQLite 里它们是两行，都得取。
TARGET_HOSTS = ("cs.tuya-inc.com", ".tuya-inc.com")

# 这几个是真正的登录凭据。其余（_tpmGuid/_tpmPlt/_tpmSeqId 之类）是埋点，
# 每次请求都在变，但不影响登录 —— 靠它们来判断"登录态是否轮换"会一直误报。
KEY_COOKIES = ("s-sid", "fast-sid", "SSO_USER_TOKEN", "COOKICE_USER_TOKEN_PC")

# 写到哪。和 set_cookie.sh 用的是同一个文件，两者可以互相覆盖。
COOKIE_FILE = "~/.cs_cookie"

# watch 模式的默认间隔。s-sid 有 8 小时，5 分钟检查一次绰绰有余。
DEFAULT_INTERVAL = 300

# Chrome 加密的固定参数
CHROME_KEYCHAIN_SERVICE = "Chrome Safe Storage"
CHROME_KEYCHAIN_ACCOUNT = "Chrome"
CHROME_SALT = b"saltysalt"
CHROME_ITERATIONS = 1003
CHROME_KEY_LEN = 16
CHROME_IV = b" " * 16

# Chrome 的时间戳以 1601-01-01 为起点，单位微秒
CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class CookieError(Exception):
    """读 cookie 失败。信息尽量写成"下一步该干什么"。"""


# ---------------------------------------------------------------------------
# Chrome：找库、取密钥、解密
# ---------------------------------------------------------------------------

def find_profiles():
    """列出所有 Chrome 系 profile 下的 Cookies 文件，最近用过的排前面。"""
    found = []
    for root in CHROME_ROOTS:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            db = os.path.join(root, name, "Cookies")
            if os.path.isfile(db):
                found.append(db)
    # 按修改时间倒序 —— 最近动过的那个才是你在用的
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def load_db(db_path):
    """
    把 Cookies 复制到临时文件再打开。

    Chrome 运行时会对原库加锁，直接读可能拿到 SQLITE_BUSY；复制一份既绕开锁，
    也保证绝不改动浏览器数据。读完就删。
    """
    tmp = os.path.join(tempfile.gettempdir(), "_cookie_sync_%d.db" % os.getpid())
    shutil.copy2(db_path, tmp)
    try:
        con = sqlite3.connect(tmp)
        rows = con.execute(
            "select host_key, name, encrypted_value, expires_utc "
            "from cookies where host_key in ({}) "
            "order by name".format(",".join("?" * len(TARGET_HOSTS))),
            TARGET_HOSTS,
        ).fetchall()
        con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return rows


def keychain_password():
    """从 Keychain 取 Chrome 的加密密码。macOS 可能弹一次授权框。"""
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-w",
             "-s", CHROME_KEYCHAIN_SERVICE, "-a", CHROME_KEYCHAIN_ACCOUNT],
            capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        raise CookieError(
            "找不到 security 命令 —— 这个脚本目前只支持 macOS。\n"
            "  Linux 用 gnome-keyring / kwallet，Windows 用 DPAPI，需要另写。"
        )
    except subprocess.TimeoutExpired:
        raise CookieError(
            "取 Keychain 密钥超时。可能是弹出的授权框没人点。\n"
            "  去屏幕上看一眼，点「始终允许」之后就不会再问了。"
        )

    pw = p.stdout.strip()
    if not pw:
        raise CookieError(
            "拿不到 Keychain 里的 {svc}。\n"
            "  原因通常是：这台机器没装过 Chrome，或者授权框被拒绝了。\n"
            "  手动确认一下：security find-generic-password -w -s {svc} -a {acct}".format(
                svc=CHROME_KEYCHAIN_SERVICE, acct=CHROME_KEYCHAIN_ACCOUNT)
        )
    return pw


def derive_key(password):
    return hashlib.pbkdf2_hmac("sha1", password, CHROME_SALT,
                               CHROME_ITERATIONS, CHROME_KEY_LEN)


def decrypt(encrypted, key):
    """
    解一个 cookie 值。返回到手就能用的字符串。

    为什么用 openssl 子进程而不是 cryptography：
    这个项目坚持零依赖 —— 用户 clone 下来就能跑，不用先 pip install 一堆东西。
    """
    if not encrypted:
        return ""

    # 'v10' / 'v11' 是版本前缀；没有前缀说明是明文存的（很老的版本）
    if encrypted[:3] in (b"v10", b"v11"):
        ciphertext = encrypted[3:]
    else:
        return encrypted.decode("utf-8", "replace")

    p = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-d",
         "-K", key.hex(), "-iv", CHROME_IV.hex()],
        input=ciphertext, capture_output=True,
    )
    if p.returncode != 0:
        raise CookieError(
            "AES 解密失败：%s\n"
            "  多半是 Chrome 换了加密方式（或这台机器上装的是别的 Chromium 分支）。"
            % p.stderr.decode("utf-8", "replace").strip()
        )

    plain = p.stdout

    # Chrome 24+ 会在明文前面拼 32 字节的 sha256(域名) 当校验位。
    # 用真实域名算一遍比对 —— 对上才剥，对不上说明没有前缀，别乱切。
    return plain


def strip_domain_prefix(plain, host):
    """剥掉明文开头的 32 字节 sha256(域名)。没有就原样返回。"""
    if len(plain) > 32 and plain[:32] == hashlib.sha256(host.encode()).digest():
        return plain[32:]
    return plain


def chrome_expiry(expires_utc):
    """Chrome 的过期时间 -> (可读字符串, 距现在还有多少秒)。0 表示 session cookie。"""
    if not expires_utc:
        return "(跟随浏览器会话)", None
    try:
        when = CHROME_EPOCH + timedelta(microseconds=expires_utc)
    except (OverflowError, ValueError):
        return "(时间戳异常)", None
    left = (when - datetime.now(timezone.utc)).total_seconds()
    return when.astimezone().strftime("%Y-%m-%d %H:%M"), left


# ---------------------------------------------------------------------------
# 组装 cookie 串
# ---------------------------------------------------------------------------

def read_cookies(db_path=None, verbose=True):
    """
    返回 (cookie串, 明细列表)。

    明细里每项是 dict，含 name / host / expires / left，**不含值** ——
    这样它可以安全地打印出来。
    """
    candidates = [db_path] if db_path else find_profiles()
    if not candidates:
        raise CookieError(
            "找不到任何 Chrome 系的 Cookie 库。找过这些位置：\n  " +
            "\n  ".join(os.path.expanduser(r) for r in CHROME_ROOTS) +
            "\n  这个脚本目前只认 Chromium 系浏览器（Chrome / Edge / Brave）。\n"
            "  Safari 的 cookie 库受系统保护（需要「完全磁盘访问权限」），没走这条路。"
        )

    key = None
    last_err = None

    for db in candidates:
        try:
            rows = load_db(db)
        except Exception as e:
            last_err = e
            continue

        if not rows:
            continue  # 这个 profile 里没有工单系统的 cookie，换下一个

        if key is None:
            key = derive_key(keychain_password())

        pairs, detail = [], []
        for host, name, enc, exp in rows:
            try:
                value = decrypt(enc, key)
            except CookieError as e:
                last_err = e
                continue
            value = strip_domain_prefix(value, host)
            text = value.decode("utf-8", "replace")
            if not text:
                continue

            readable, left = chrome_expiry(exp)
            detail.append({"name": name, "host": host,
                           "expires": readable, "left": left,
                           "size": len(text)})
            pairs.append("%s=%s" % (name, text))

        if pairs:
            return "; ".join(pairs), detail

    if last_err:
        raise last_err
    raise CookieError(
        "Cookie 库里没有工单系统的登录态（找了 %d 个 profile）。\n"
        "  先用 Chrome 打开一次 https://cs.tuya-inc.com:7799/order/tech 并确认已登录，\n"
        "  然后再跑一次本脚本。" % len(candidates)
    )


def fingerprint(cookie_text):
    """
    只取关键登录态做指纹。

    为什么不用整串：_tpmGuid / _tpmSeqId / _tpmPlt 这类埋点 cookie 每次请求都在变，
    拿整串比对会一直报"变了"，把真正的登录轮换淹掉。
    """
    got = {}
    for item in cookie_text.split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        name, _, value = item.partition("=")
        if name.strip() in KEY_COOKIES:
            got[name.strip()] = value
    return got


def fmt_left(seconds):
    """把剩余秒数说成人话。"""
    if seconds is None:
        return "-"
    if seconds <= 0:
        return "已过期"
    if seconds < 3600:
        return "%d 分钟后" % (seconds // 60)
    if seconds < 86400:
        return "%.1f 小时后" % (seconds / 3600.0)
    return "%.1f 天后" % (seconds / 86400.0)


# ---------------------------------------------------------------------------
# 落盘 / 读回
# ---------------------------------------------------------------------------

def cookie_path():
    return os.path.expanduser(COOKIE_FILE)


def save_cookie(text):
    """
    原子写 + 权限 0600。

    先写临时文件再 rename：这样正在跑的 bridge.py 永远读不到"写了一半"的文件。
    """
    path = cookie_path()
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".cs_cookie.tmp.%d" % os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def load_saved():
    path = cookie_path()
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 验证：拿这份 cookie 真去问一次工单系统
# ---------------------------------------------------------------------------

def verify(cookie_text):
    """
    用这份 cookie 调一次工单接口。返回 (是否成功, 说明文字)。

    复用项目现成的取数逻辑，而不是另写一套 HTTP —— 否则"验证通过"和
    "实际能用"可能对不上。
    """
    cfg_path = os.path.join(HERE, "status_light.json")
    if not os.path.isfile(cfg_path):
        return None, "没有 status_light.json，跳过验证"

    sys.path.insert(0, HERE)
    try:
        import sources
    except ImportError as e:
        return None, "导入 sources 失败，跳过验证：%s" % e

    # expand_env 里环境变量优先级最高，所以这样塞进去最直接
    old = os.environ.get("CS_COOKIE")
    os.environ["CS_COOKIE"] = cookie_text
    try:
        src = sources.TicketsSource.from_file(cfg_path)
        data = src.fetch()
    except Exception as e:
        return False, str(e)
    finally:
        if old is None:
            os.environ.pop("CS_COOKIE", None)
        else:
            os.environ["CS_COOKIE"] = old

    # fetch() 返回计数字典，形如 {"p0":0,"p1":1,"p2":275,"resolved_today":0}
    if not isinstance(data, dict):
        return False, "取数返回了意料之外的类型：%s" % type(data).__name__
    return True, "p0=%s p1=%s p2=%s" % (
        data.get("p0", "?"), data.get("p1", "?"), data.get("p2", "?"))


# ---------------------------------------------------------------------------
# 各模式
# ---------------------------------------------------------------------------

def print_detail(detail):
    """把 cookie 明细打成表。只显示名字/位置/有效期，不显示值。"""
    if not detail:
        return
    print("  %-24s %-18s %-17s %s" % ("cookie 名", "域名", "过期时间", "还有"))
    print("  " + "-" * 74)
    for d in sorted(detail, key=lambda x: (x["host"], x["name"])):
        print("  %-24s %-18s %-17s %s"
              % (d["name"], d["host"], d["expires"], fmt_left(d["left"])))


def cmd_check(args):
    """只看现状，不动文件。"""
    print("=" * 66)
    print(" 工单 Cookie 现状")
    print("=" * 66)
    print()

    try:
        fresh, detail = read_cookies()
    except CookieError as e:
        print("读不到 Chrome 里的 cookie：")
        print()
        for line in str(e).splitlines():
            print("  " + line)
        return 1

    print("从 Chrome 读到 %d 个 cookie：" % len(detail))
    print()
    print_detail(detail)
    print()

    saved = load_saved()
    print("本地文件 %s：" % cookie_path())
    if not saved:
        print("  （还没有，跑一次 python3 cookie_sync.py 生成）")
    else:
        fresh_fp, saved_fp = fingerprint(fresh), fingerprint(saved)
        if fresh_fp == saved_fp:
            print("  ✓ 和浏览器里的一致（%d 字节）" % len(saved))
        else:
            changed = [k for k in set(fresh_fp) | set(saved_fp)
                       if fresh_fp.get(k) != saved_fp.get(k)]
            print("  ⚠ 和浏览器里的不一致，差异字段：%s" % ", ".join(sorted(changed)))
            print("    → 跑 python3 cookie_sync.py 更新")
    print()

    # 顺手报一下最关键的那个还剩多久
    sid = [d for d in detail if d["name"] == "s-sid"]
    if sid and sid[0]["left"] is not None:
        left = sid[0]["left"]
        print("最要紧的 s-sid 还有 %s。" % fmt_left(left))
        if left < 3600:
            print("（它大约 8 小时一轮换，靠它在 8 小时内就会重新同步一次没问题）")
    print()
    print("=" * 66)
    return 0


def cmd_sync(args, quiet=False):
    """读一次 → 比较 → 需要就写 → 验证。返回退出码。"""
    try:
        fresh, detail = read_cookies()
    except CookieError as e:
        if not quiet:
            print("读不到 Chrome 里的 cookie：")
            print()
            for line in str(e).splitlines():
                print("  " + line)
        return 1

    saved = load_saved()
    changed = fingerprint(fresh) != fingerprint(saved)

    if changed:
        path = save_cookie(fresh)
        if not quiet:
            print("✓ 已更新 %s（%d 个 cookie，%d 字节）"
                  % (path, len(detail), len(fresh)))
    elif not quiet:
        if saved:
            print("= 和浏览器里的一致，不用改（%d 字节）" % len(saved))
        else:
            print("= 没有本地文件，已写入")
            save_cookie(fresh)

    if args.verify or changed:
        ok, msg = verify(fresh)
        if ok is True:
            if not quiet:
                print("✓ 验证通过：%s" % msg)
        elif ok is False:
            print("✗ 验证失败（cookie 是新的，但工单系统不接受）：")
            for line in msg.splitlines():
                print("    " + line)
            return 1
        elif not quiet:
            print("- %s" % msg)

    if args.verbose and not quiet:
        print()
        print_detail(detail)

    return 0


def cmd_watch(args):
    """常驻：定期同步。"""
    interval = args.interval
    print("=" * 66)
    print(" 工单 Cookie 自动同步")
    print("=" * 66)
    print()
    print("  间隔      : %d 秒" % interval)
    print("  来源      : Chrome")
    print("  目标文件  : %s" % cookie_path())
    print("  退出      : Ctrl-C")
    print()

    rounds = fails = 0
    while True:
        rounds += 1
        stamp = datetime.now().strftime("%H:%M:%S")

        try:
            fresh, detail = read_cookies()
        except CookieError as e:
            fails += 1
            print("[%s] 读不到 cookie（第 %d 次连续失败）" % (stamp, fails))
            first = str(e).splitlines()[0]
            print("        %s" % first)
            if fails == 3:
                print("        提示：Chrome 开着、并且登录着工单系统吗？")
            # 失败时退避，但别超过间隔的两倍
            time.sleep(min(interval, 60 * fails))
            continue

        fails = 0
        saved = load_saved()
        if fingerprint(fresh) != fingerprint(saved):
            save_cookie(fresh)
            print("[%s] ✓ 登录态已轮换，文件已更新（%d 字节）" % (stamp, len(fresh)))
            if args.verify:
                ok, msg = verify(fresh)
                if ok is True:
                    print("        验证通过：%s" % msg)
                elif ok is False:
                    print("        验证失败：%s" % msg.splitlines()[0])
        else:
            sid = [d for d in detail if d["name"] == "s-sid"]
            left = fmt_left(sid[0]["left"]) if sid else "?"
            print("[%s] 无变化（s-sid 还有 %s）" % (stamp, left))

        time.sleep(interval)


def cmd_print(args):
    """把 cookie 打到 stdout，不落盘。方便 `export CS_COOKIE=$(...)`。"""
    try:
        fresh, _ = read_cookies()
    except CookieError as e:
        sys.stderr.write(str(e) + "\n")
        return 1
    sys.stdout.write(fresh)
    return 0


def cmd_profile(args):
    """列出所有 profile，以及各自能匹配到几个 cookie。"""
    profiles = find_profiles()
    print("找到 %d 个 Chromium 系 profile：" % len(profiles))
    print()
    print("  %-58s %-10s %s" % ("路径", "cookie 数", "最近修改"))
    print("  " + "-" * 92)
    for db in profiles:
        try:
            rows = load_db(db)
            cnt = str(len(rows))
        except Exception as e:
            cnt = "读失败"
        mtime = datetime.fromtimestamp(os.path.getmtime(db)).strftime("%Y-%m-%d %H:%M")
        short = db.replace(os.path.expanduser("~"), "~")
        print("  %-58s %-10s %s" % (short, cnt, mtime))
    print()
    print("  脚本会从上往下找，用第一个有工单 cookie 的 profile。")
    return 0


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="从 Chrome 自动同步工单系统的登录 Cookie",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带参数 = 读一次并更新 ~/.cs_cookie，然后验证。",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true",
                   help="只看现状（关键 cookie 还有多久过期），不写文件")
    g.add_argument("--watch", action="store_true",
                   help="常驻，定期同步；默认 300 秒一次")
    g.add_argument("--print", dest="do_print", action="store_true",
                   help="把 cookie 打到 stdout，不写文件")
    g.add_argument("--profile", action="store_true",
                   help="列出所有 Chrome profile 及匹配到的 cookie 数")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="--watch 的间隔秒数（默认 %d）" % DEFAULT_INTERVAL)
    ap.add_argument("--verify", action="store_true",
                    help="总是调一次工单接口验证（不带参数时只在更新后验证）")
    ap.add_argument("--verbose", action="store_true", help="多打一些细节")
    args = ap.parse_args()

    try:
        if args.check:
            return cmd_check(args)
        if args.watch:
            return cmd_watch(args)
        if args.do_print:
            return cmd_print(args)
        if args.profile:
            return cmd_profile(args)
        return cmd_sync(args)
    except KeyboardInterrupt:
        print()
        print("停。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
