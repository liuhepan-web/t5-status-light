#!/usr/bin/env bash
#
# 保存工单系统的登录 Cookie
#
#   ./set_cookie.sh            粘贴 Cookie，存到 ~/.cs_cookie
#   ./set_cookie.sh --check    看当前存的还能不能用
#   ./set_cookie.sh --clear    删掉
#
# 为什么存到 ~/.cs_cookie 而不是项目目录：
#   这个 Cookie 等于你工单系统的登录态，能读你名下所有工单。
#   放进项目目录就有被 git 提交、被同事拷走的风险。
#   放家目录、权限 600，只有你自己能读。
#
# 怎么拿到 Cookie：
#   浏览器打开 https://cs.tuya-inc.com:7799/order/tech
#   F12 → Network → 刷新页面 → 随便点一条 /api/... 的请求
#   → 右键 Copy → Copy as cURL → 把里面 cookie 后面那段整条复制出来
#
# ⚠ 但手工粘是有保质期的：cs.tuya-inc.com 的 s-sid 大约 8 小时轮换一次，
#   今天粘的明天准失效 —— 这不是你配错了，是它的设计。
#
#   平时请用自动那条路（只要 Chrome 里还登录着，它就一直跟得上）：
#
#       python3 cookie_sync.py           读一次
#       python3 cookie_sync.py --watch   常驻，定期同步
#
#   本脚本留着兜底：换了浏览器、自动读不到、或临时要切另一个账号时用。

set -uo pipefail

COOKIE_FILE="$HOME/.cs_cookie"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"

case "${1:-}" in
--clear)
    rm -f "$COOKIE_FILE"
    echo "已删除 $COOKIE_FILE"
    exit 0
    ;;
--check)
    if [ ! -f "$COOKIE_FILE" ]; then
        echo "还没有存过 Cookie。"
        echo "  自动（推荐）：python3 cookie_sync.py"
        echo "  手工：        ./set_cookie.sh"
        exit 1
    fi
    cd "$HERE" || exit 1
    CS_COOKIE="$(cat "$COOKIE_FILE")" $PY bridge.py --check
    exit $?
    ;;
"")
    ;;
*)
    echo "不认识的参数：$1"
    echo "用法：./set_cookie.sh [--check|--clear]"
    exit 1
    ;;
esac

echo "======================================================"
echo " 保存工单系统 Cookie"
echo "======================================================"
echo
echo "把 Copy as cURL 里 cookie 后面那一整段粘进来，然后回车："
echo "（粘贴后是看不见换行的，直接回车提交即可）"
echo

IFS= read -r COOKIE

if [ -z "$COOKIE" ]; then
    echo "没读到内容，取消。"
    exit 1
fi

case "$COOKIE" in
*SSO_USER_TOKEN*|*fast-sid*|*s-sid*)
    ;;
*)
    echo
    echo "这段里没有 SSO_USER_TOKEN / fast-sid 之类的登录字段，"
    echo "八成是复制错了（比如复制成了响应体而不是请求头）。"
    echo "要仍然保存请输入 y："
    IFS= read -r ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ] || exit 1
    ;;
esac

printf '%s' "$COOKIE" >"$COOKIE_FILE"
chmod 600 "$COOKIE_FILE"
echo
echo "已保存到 ${COOKIE_FILE}（权限 600）"

cd "$HERE" || exit 1
echo
echo "马上验一下能不能用："
echo
CS_COOKIE="$COOKIE" $PY bridge.py --check
