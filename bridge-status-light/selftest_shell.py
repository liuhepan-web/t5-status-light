#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shell 脚本静态自检 —— 专抓「中文标点把变量名吃掉」这类问题。

【这个自检为什么存在】

本项目所有脚本的提示语都是中文，于是很容易写出这样的行：

    echo "[2/3] 启动 bridge.py（端口 $PORT，日志 $BRIDGE_LOG）"

看着没问题。但在 bash 里，`$PORT，` 中的全角逗号会被当成变量名的一部分，
于是 bash 去找一个叫 `PORT，` 的变量 —— 当然找不到，在 `set -u` 下直接报：

    ./start.sh: line 176: PORT?: unbound variable

脚本当场退出，而且**报错位置指向的是 echo 那一行**，看起来人畜无害，
真正的原因（中文标点）要盯着看好一会儿才能反应过来。

这个坑在本项目里埋过两次（一次 `$LAN_IP（`，一次 `$PORT，`），
都是「脚本突然就退出了，但看不出为什么」。所以固化成自检。

【规则】

    变量后面如果紧跟非 ASCII 字符（中文标点、中文文字、★ 等），
    一律写成 ${VAR} 而不是 $VAR。

    echo "端口 $PORT" 后跟空格   → 安全
    echo "端口 ${PORT}，"        → 安全
    echo "端口 $PORT，"          → 危险

【适用范围】

扫描项目里所有 .sh，跳过 .git 和构建产物。
"""

import os
import re
import sys

# $VAR 后面紧跟一个非 ASCII 字节
PATTERN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)([^\x00-\x7F])")

# 这几个目录不看
SKIP_DIRS = {".git", "build", "__pycache__", "node_modules", "dist"}


def find_scripts(root):
    """找出项目下所有 .sh"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".sh"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def scan(path):
    """返回 [(行号, 变量名, 紧跟的字符, 整行内容)]"""
    hits = []
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                for m in PATTERN.finditer(line):
                    hits.append((i, m.group(1), m.group(2), line.rstrip()))
    except (OSError, UnicodeDecodeError) as e:
        hits.append((0, "", "", "读取失败：%s" % e))
    return hits


def main():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    print("=" * 66)
    print(" shell 脚本自检 —— 中文标点是否吃掉了变量名")
    print("=" * 66)
    print()

    scripts = find_scripts(root)
    if not scripts:
        print("  没找到任何 .sh，是不是跑错目录了？")
        print("  扫描根目录：%s" % root)
        return 1

    total_bad = 0

    for path in scripts:
        rel = os.path.relpath(path, root)
        hits = scan(path)

        if not hits:
            print("  [PASS] %s" % rel)
            continue

        total_bad += len(hits)
        print("  [FAIL] %s" % rel)
        for lineno, var, ch, line in hits:
            if lineno == 0:
                print("         %s" % line)
                continue
            print("         第 %d 行：$%s 后面紧跟 %r" % (lineno, var, ch))
            print("             %s" % line.strip())
            print("             → 改成 ${%s}" % var)

    print()
    print("=" * 66)

    if total_bad == 0:
        print(" 结果：全部干净")
        print()
        print(" 所有脚本里的变量插值都安全。")
        print(" （规则：变量后面紧跟中文标点时，用 ${VAR} 而不是 $VAR）")
        print("=" * 66)
        return 0

    print(" 结果：发现 %d 处会把变量名吃掉的写法" % total_bad)
    print()
    print(" 这类问题在 set -u 下会让脚本当场退出，报错行却指向 echo，")
    print(" 很容易误判成别的原因。请把上面几处改成 ${VAR} 写法。")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(main())
