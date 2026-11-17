#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cookie_sync.py 的离线自检。

**不碰 Chrome、不碰网络、不碰你真实的 ~/.cs_cookie。** 只测纯逻辑：

    · 指纹算法（哪些 cookie 算"登录态变了"）
    · Chrome 时间戳换算
    · 明文开头的 32 字节域名前缀剥离
    · 原子写 + 权限
    · cookie 串的解析边界

这些是"看起来对、错起来很隐蔽"的地方 —— 比如指纹算错会导致
要么永远报"变了"（噪音刷屏），要么永远报"没变"（cookie 烂了也不更新）。
所以单独拿出来测。
"""

import hashlib
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cookie_sync as cs

OK = True
PASSED = FAILED = 0


def check(name, got, want):
    global OK, PASSED, FAILED
    if got == want:
        PASSED += 1
        print("  [PASS] %s" % name)
        return True
    FAILED += 1
    OK = False
    print("  [FAIL] %s" % name)
    print("         期望 %r" % (want,))
    print("         实际 %r" % (got,))
    return False


def expect_error(name, fn, must_contain):
    """fn() 应该抛 CookieError，且消息里含某个关键词。"""
    global OK, PASSED, FAILED
    try:
        fn()
    except cs.CookieError as e:
        msg = str(e)
        if all(k in msg for k in must_contain):
            PASSED += 1
            print("  [PASS] %s" % name)
            return True
        FAILED += 1
        OK = False
        print("  [FAIL] %s" % name)
        print("         报错信息里缺关键词 %r" % (must_contain,))
        print("         实际报错：%s" % msg.splitlines()[0])
        return False
    except Exception as e:
        FAILED += 1
        OK = False
        print("  [FAIL] %s" % name)
        print("         抛的不是 CookieError，而是 %s: %s" % (type(e).__name__, e))
        return False

    FAILED += 1
    OK = False
    print("  [FAIL] %s" % name)
    print("         本该报错，但没有")
    return False


# ---------------------------------------------------------------------------

def test_fingerprint():
    """指纹只认关键登录态 —— 这是"要不要更新"的判据，错不得。"""
    print()
    print(" -- 指纹：哪些变化才算登录态轮换 --")

    a = "s-sid=AAA; fast-sid=BBB; SSO_USER_TOKEN=CCC; _tpmSeqId=111"
    b = "s-sid=AAA; fast-sid=BBB; SSO_USER_TOKEN=CCC; _tpmSeqId=999"

    check("埋点 cookie 变了 -> 指纹不变",
          cs.fingerprint(a) == cs.fingerprint(b), True)

    check("指纹里只留关键项",
          sorted(cs.fingerprint(a).keys()),
          sorted(["s-sid", "fast-sid", "SSO_USER_TOKEN"]))

    c = a.replace("s-sid=AAA", "s-sid=ZZZ")
    check("s-sid 变了 -> 指纹变", cs.fingerprint(a) != cs.fingerprint(c), True)

    d = a.replace("SSO_USER_TOKEN=CCC", "SSO_USER_TOKEN=DDD")
    check("SSO_USER_TOKEN 变了 -> 指纹变", cs.fingerprint(a) != cs.fingerprint(d), True)

    check("空串 -> 空指纹", cs.fingerprint(""), {})

    # 值里含 = 的情况（base64 padding 很常见），不能把值截断
    e = "s-sid=a=b=c; fast-sid=x"
    check("值里含等号 -> 完整取出", cs.fingerprint(e).get("s-sid"), "a=b=c")

    # 前后有空格、末尾多个分号，都不该影响
    f = "  s-sid=AAA ;;  fast-sid=BBB ; "
    check("多余空格和分号 -> 解析正常",
          cs.fingerprint(f).get("s-sid"), "AAA")


def test_fmt_left():
    print()
    print(" -- 剩余时间的人话表达 --")

    check("None -> 短横", cs.fmt_left(None), "-")
    check("负数 -> 已过期", cs.fmt_left(-5), "已过期")
    check("零 -> 已过期", cs.fmt_left(0), "已过期")
    check("90 秒 -> 1 分钟", cs.fmt_left(90), "1 分钟后")
    check("2 小时", cs.fmt_left(7200), "2.0 小时后")
    check("3 天", cs.fmt_left(3 * 86400), "3.0 天后")


def test_chrome_expiry():
    print()
    print(" -- Chrome 时间戳换算 --")

    text, left = cs.chrome_expiry(0)
    check("0 表示 session cookie", (text, left), ("(跟随浏览器会话)", None))

    # 反过来构造一个时间戳，看能不能还原
    want = datetime.now(timezone.utc) + timedelta(hours=5)
    us = int((want - cs.CHROME_EPOCH).total_seconds() * 1_000_000)
    text, left = cs.chrome_expiry(us)
    check("5 小时后 -> 剩余秒数对得上",
          left is not None and abs(left - 5 * 3600) < 5, True)

    # 已经过期的时间戳
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    us = int((past - cs.CHROME_EPOCH).total_seconds() * 1_000_000)
    _, left = cs.chrome_expiry(us)
    check("过期时间戳 -> 负数", left is not None and left < 0, True)

    check("离谱的时间戳不炸",
          cs.chrome_expiry(10 ** 18)[0], "(时间戳异常)")


def test_strip_prefix():
    print()
    print(" -- 明文开头的 32 字节域名前缀 --")

    host = "cs.tuya-inc.com"
    value = b"the-real-value"
    prefixed = hashlib.sha256(host.encode()).digest() + value

    check("有前缀 -> 剥掉",
          cs.strip_domain_prefix(prefixed, host), value)
    check("没前缀 -> 原样",
          cs.strip_domain_prefix(value, host), value)
    check("太短 -> 原样",
          cs.strip_domain_prefix(b"short", host), b"short")
    check("恰好 32 字节 -> 原样（没有值可剥）",
          cs.strip_domain_prefix(hashlib.sha256(host.encode()).digest(), host),
          hashlib.sha256(host.encode()).digest())

    # 换个域名，前缀就对不上了，不该乱剥
    check("域名不匹配 -> 不剥",
          cs.strip_domain_prefix(prefixed, "other.com"), prefixed)


def test_decrypt_plaintext():
    print()
    print(" -- 未加密的值（老版本 / 非 Chromium）--")

    check("没有 v10 前缀 -> 直接当明文",
          cs.decrypt(b"raw-value", None), "raw-value")
    check("空值 -> 空串", cs.decrypt(b"", None), "")


def test_save_load(tmp):
    print()
    print(" -- 落盘：原子写 + 权限 --")

    old_path = cs.COOKIE_FILE
    cs.COOKIE_FILE = os.path.join(tmp, "cs_cookie_test")
    try:
        text = "s-sid=AAA; fast-sid=BBB"
        cs.save_cookie(text)

        check("写进去能读回来", cs.load_saved(), text)

        mode = os.stat(cs.COOKIE_FILE).st_mode & 0o777
        check("权限是 0600", oct(mode), oct(0o600))

        leftovers = [f for f in os.listdir(tmp) if f.startswith(".cs_cookie.tmp")]
        check("没留下临时文件", leftovers, [])

        # 再写一次，覆盖
        cs.save_cookie("s-sid=NEW")
        check("覆盖写入正常", cs.load_saved(), "s-sid=NEW")

        # 读回时会 strip 掉首尾空白
        with open(cs.COOKIE_FILE, "w") as f:
            f.write("  s-sid=X  \n")
        check("读回时去掉首尾空白", cs.load_saved(), "s-sid=X")
    finally:
        cs.COOKIE_FILE = old_path

    # 文件不存在时应返回空串而不是抛异常
    cs.COOKIE_FILE = os.path.join(tmp, "definitely_missing")
    try:
        check("文件不存在 -> 空串", cs.load_saved(), "")
    finally:
        cs.COOKIE_FILE = old_path


def test_error_paths(tmp):
    print()
    print(" -- 出错路径要说人话 --")

    old_roots = cs.CHROME_ROOTS
    cs.CHROME_ROOTS = [os.path.join(tmp, "no_such_browser")]
    try:
        expect_error("找不到浏览器 -> 提示装了哪些",
                     cs.read_cookies, ["Chrome", "Safari"])

        # 造一个空的 cookie 库：结构对，但没有目标域名
        import sqlite3
        prof = os.path.join(tmp, "FakeChrome", "Default")
        os.makedirs(prof)
        db = os.path.join(prof, "Cookies")
        con = sqlite3.connect(db)
        con.execute("create table cookies (host_key text, name text, "
                    "encrypted_value blob, expires_utc integer)")
        con.execute("insert into cookies values ('example.com','x',x'76',0)")
        con.commit()
        con.close()

        cs.CHROME_ROOTS = [os.path.join(tmp, "FakeChrome")]
        expect_error("库在但没登录态 -> 让先登录一次",
                     cs.read_cookies, ["cs.tuya-inc.com", "登录"])
    finally:
        cs.CHROME_ROOTS = old_roots


# ---------------------------------------------------------------------------

def main():
    global OK
    tmp = tempfile.mkdtemp(prefix="cookie_selftest_")
    try:
        print("=" * 66)
        print(" cookie_sync.py 自检（离线，不碰 Chrome / 不碰真实 cookie 文件）")
        print("=" * 66)

        test_fingerprint()
        test_fmt_left()
        test_chrome_expiry()
        test_strip_prefix()
        test_decrypt_plaintext()
        test_save_load(tmp)
        test_error_paths(tmp)

        print()
        print("=" * 66)
        if OK:
            print(" 结果：全部通过（%d 项）" % PASSED)
            print("=" * 66)
            return 0
        print(" 结果：%d 项通过，%d 项失败" % (PASSED, FAILED))
        print("=" * 66)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
