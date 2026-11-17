#!/usr/bin/env bash
#
# T5 状态灯 · 一条命令把本地侧全部跑起来
#
#   ./start.sh                自动判断：有 tuya_cloud.json 走云，没有走局域网
#   ./start.sh --lan          强制局域网直连 —— 不需要任何涂鸦云凭据
#   ./start.sh --cloud        强制涂鸦云 DP 下发（需要 tuya_cloud.json）
#   PORT=9090 ./start.sh      换端口
#   PYTHON=/usr/bin/python3 ./start.sh
#
# 两种模式唯一的区别，是「板子怎么拿到状态」：
#
#   局域网直连（--lan）—— 本项目的主路线，零云凭据
#       板子主动来拉 http://<本机IP>:8080/tickets，数据在 Wi-Fi 内闭环。
#       只需要起 bridge.py。前提：板子和本机在同一网段，且路由器没开 AP 隔离。
#
#   涂鸦云 DP（--cloud）—— 可选，需要三个云凭据
#       多起一个 dp_push.py --watch，把状态翻译成 DP 下发给设备。
#       板子不必和本机同网段，但要去涂鸦开发者平台取 client_id / secret / device_id。
#
# 任何一步没过就停下来并告诉你卡在哪，不会带着坏配置一路跑到黑。
# Ctrl-C 会一起停掉。

set -uo pipefail

cd "$(dirname "$0")" || exit 1

PY="${PYTHON:-python3}"
PORT="${PORT:-8080}"
INTERVAL="${INTERVAL:-30}"
BRIDGE_LOG="${BRIDGE_LOG:-/tmp/t5-bridge.log}"

usage() {
    cat <<'EOF'
用法：./start.sh [选项]

  （不带参数）   自动判断模式：有 tuya_cloud.json 走云，没有走局域网直连
  --lan          强制局域网直连（不需要任何涂鸦云凭据）
  --cloud        强制涂鸦云 DP 下发（需要 tuya_cloud.json）
  -h, --help     看这段说明

环境变量：
  PORT=8080              监听端口
  PYTHON=python3         用哪个解释器
  INTERVAL=30            云模式下同步 DP 的间隔（秒）
  BRIDGE_LOG=/tmp/...    bridge.py 的日志路径
EOF
}

MODE="auto"
case "${1:-}" in
--lan) MODE="lan" ;;
--cloud) MODE="cloud" ;;
-h | --help)
    usage
    exit 0
    ;;
"") ;;
*)
    echo "不认识的参数：$1"
    echo
    usage
    exit 1
    ;;
esac

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "找不到 $PY"
    echo "换一个：PYTHON=/usr/bin/python3 ./start.sh"
    exit 1
fi

echo "======================================================"
echo " T5 状态灯 · 本地侧启动"
echo "======================================================"

# ---------------------------------------------------------------------
# 工单系统的登录态。优先用环境变量，其次读 ~/.cs_cookie（set_cookie.sh 存的那份）。
# 不放项目目录是为了别让这个能读你全部工单的 Cookie 被顺手提交上去。
# ---------------------------------------------------------------------
if [ -z "${CS_COOKIE:-}" ] && [ -f "$HOME/.cs_cookie" ]; then
    CS_COOKIE="$(cat "$HOME/.cs_cookie")"
    export CS_COOKIE
fi

# ---------------------------------------------------------------------
# 探测本机局域网 IP —— 板子要照着这个地址来找我们
# ---------------------------------------------------------------------
detect_lan_ip() {
    local ip=""

    # macOS：en0 是 Wi-Fi，en1 常常是有线/雷雳网卡
    for dev in en0 en1 en2; do
        ip="$(ipconfig getifaddr "$dev" 2>/dev/null || true)"
        [ -n "$ip" ] && { echo "$ip"; return 0; }
    done

    # Linux / 兜底
    if command -v ip >/dev/null 2>&1; then
        ip="$(ip -4 route get 1.1.1.1 2>/dev/null |
            awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
        [ -n "$ip" ] && { echo "$ip"; return 0; }
    fi

    # 最后再兜一次：连一个外网地址，看内核挑了哪个源地址
    ip="$("$PY" -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('1.1.1.1', 80)); print(s.getsockname()[0])
except Exception:
    print('')
finally:
    s.close()
" 2>/dev/null || true)"
    [ -n "$ip" ] && { echo "$ip"; return 0; }

    echo "127.0.0.1"
}

# ---------------------------------------------------------------------
# 定模式
# ---------------------------------------------------------------------
if [ "$MODE" = "auto" ]; then
    if [ -f tuya_cloud.json ]; then
        MODE="cloud"
    else
        MODE="lan"
    fi
fi

if [ "$MODE" = "cloud" ] && [ ! -f tuya_cloud.json ]; then
    echo
    echo "--cloud 模式需要 tuya_cloud.json，但现在没有。"
    echo
    echo "  要么先初始化（会问你三个云凭据）：$PY dp_push.py --init"
    echo "  要么走局域网直连（不需要任何云凭据）：./start.sh --lan"
    exit 1
fi

# 两种模式的总步数不一样：局域网 3 步，云 4 步。
# 第 1 步（查数据源）两边共用，所以编号得跟着模式走，否则会出现 [1/3] 后面接 [2/4]。
if [ "$MODE" = "lan" ]; then
    TOTAL=3
else
    TOTAL=4
fi

# ---------------------------------------------------------------------
# 第 0 步：从浏览器自动同步登录 Cookie
#
# cs.tuya-inc.com 的 s-sid 大约 8 小时就轮换一次，手工从 F12 复制的那一份
# 撑不过一天。只要 Chrome 里还登录着工单系统，这里就顺手把最新的取过来。
#
# 同步失败不致命 —— 下面第 1 步的数据源检查会给出更准确的诊断，
# 而且有时只是想用 mock 数据跑一下，那根本不需要真的 cookie。
# ---------------------------------------------------------------------
COOKIE_LOG="${COOKIE_LOG:-/tmp/t5-cookie.log}"

if [ -f cookie_sync.py ] && command -v security >/dev/null 2>&1; then
    echo
    echo "[同步] 从 Chrome 读取登录 Cookie"
    SYNC_MSG="$($PY cookie_sync.py 2>&1)" && SYNC_RC=0 || SYNC_RC=$?
    echo "$SYNC_MSG" | sed 's/^/       /'
    if [ "$SYNC_RC" != "0" ]; then
        echo "       （没同步成功，不影响 —— 接着看下面的数据源检查）"
    fi
fi

# ---------------------------------------------------------------------
# 第 1 步：工单数据源。两种模式都要，配置错了后面全白搭。
# ---------------------------------------------------------------------
if [ -f status_light.json ]; then
    if grep -q '"transport"[[:space:]]*:[[:space:]]*"tuyacs"' status_light.json &&
        [ -z "${CS_COOKIE:-}" ]; then
        echo
        echo "status_light.json 用的是涂鸦 CS 工单系统，但没有登录 Cookie。"
        echo
        echo "    ./set_cookie.sh     粘贴一次 Cookie，之后就不用管了"
        echo
        exit 1
    fi

    echo
    echo "[1/${TOTAL}] 检查工单数据源"
    if ! $PY bridge.py --check; then
        echo
        echo "数据源没通，先把它修好再往下走。"
        echo "  Cookie 失效是常见原因：./set_cookie.sh --check 会告诉你是不是被弹回登录页了。"
        echo "  想看接口真实返回结构：$PY bridge.py --check --dump"
        exit 1
    fi
else
    echo
    echo "[1/${TOTAL}] 跳过工单数据源检查"
    echo "      没有 status_light.json，正在用内置的模拟数据。"
    echo "      接你自己的工单系统：cp status_light.example.json status_light.json"
fi

# =====================================================================
# 局域网直连 —— 起 bridge.py 就完事了，不需要任何云凭据
# =====================================================================
if [ "$MODE" = "lan" ]; then
    echo
    echo "[2/3] 启动 bridge.py（端口 ${PORT}，日志 ${BRIDGE_LOG}）"
    $PY bridge.py --port "$PORT" >"$BRIDGE_LOG" 2>&1 &
    BRIDGE_PID=$!

    # 后台盯着 cookie。s-sid 约 8 小时轮换一次，靠它自动跟上，不用你隔天再手工粘一回。
    # bridge.py 每次取数都会重读凭据文件，所以这边刷新了它立刻就用上，不用重启。
    COOKIE_PID=""
    if [ -f cookie_sync.py ] && command -v security >/dev/null 2>&1; then
        $PY cookie_sync.py --watch >"$COOKIE_LOG" 2>&1 &
        COOKIE_PID=$!
        echo "      cookie 自动同步已启动（日志 ${COOKIE_LOG}）"
    fi

    cleanup() {
        echo
        echo "收工，停掉 bridge.py (pid $BRIDGE_PID)"
        kill "$BRIDGE_PID" 2>/dev/null
        if [ -n "$COOKIE_PID" ]; then
            kill "$COOKIE_PID" 2>/dev/null
            echo "      cookie 同步也停了 (pid $COOKIE_PID)"
        fi
        wait "$BRIDGE_PID" 2>/dev/null
    }
    trap cleanup EXIT INT TERM

    sleep 1
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "bridge.py 没起来，看日志："
        tail -20 "$BRIDGE_LOG"
        exit 1
    fi

    if command -v curl >/dev/null 2>&1; then
        if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null; then
            echo "bridge.py 起来了但 /health 不通，看日志：$BRIDGE_LOG"
            exit 1
        fi
    fi

    LAN_IP="$(detect_lan_ip)"

    echo
    echo "[3/3] 板子要访问的地址"
    echo
    echo "    http://${LAN_IP}:${PORT}/tickets"

    # 固件里烧的地址和当前 IP 对不上是「板子没反应」的头号原因：换网络后 IP 会变。
    FW_HOST="$(sed -n 's/.*#define[[:space:]]*SL_BRIDGE_HOST[[:space:]]*"\([^"]*\)".*/\1/p' \
        ../src/app_status_light.c 2>/dev/null | head -1)"
    if [ -n "$FW_HOST" ] && [ "$FW_HOST" != "$LAN_IP" ]; then
        echo
        echo "    ⚠ 固件里烧的地址是 ${FW_HOST}，和当前 IP 不一致"
        echo "      → 重跑 ../firmware/install.sh 会重新注入并烧录"
    fi

    echo
    echo "  手动灌数据（不用等真实工单）："
    echo "    curl -X POST 'http://127.0.0.1:${PORT}/sim?p0=2'            # 红：有人在等你"
    echo "    curl -X POST 'http://127.0.0.1:${PORT}/sim?p0=0&p1=1'       # 黄：只在处理中"
    echo "    curl -X POST 'http://127.0.0.1:${PORT}/sim?p0=0&p1=0&p2=0'  # 绿 + 庆祝动画"
    echo
    echo "  板子够不够得着本机：../firmware/install.sh --check-lan"
    echo
    echo "Ctrl-C 退出"
    echo

    wait "$BRIDGE_PID"
    exit 0
fi

# =====================================================================
# 涂鸦云 DP 下发 —— 起 bridge.py + dp_push.py --watch
# =====================================================================
echo
echo "[2/4] 签名自检"
if ! $PY dp_push.py --selftest; then
    echo
    echo "签名自检没过，先别往下走。"
    exit 1
fi

echo
echo "[3/4] 查设备"
if ! $PY dp_push.py --probe; then
    echo
    echo "查设备失败。检查 tuya_cloud.json 里的 client_id / secret / device_id 和 region。"
    exit 1
fi

echo
echo "[4/4] 启动 bridge.py（端口 ${PORT}，日志 ${BRIDGE_LOG}）"
$PY bridge.py --port "$PORT" >"$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!

# 后台盯着 cookie（dp_push.py 也是从 bridge 的 /tickets 取数的，同样受它影响）
COOKIE_PID=""
if [ -f cookie_sync.py ] && command -v security >/dev/null 2>&1; then
    $PY cookie_sync.py --watch >"$COOKIE_LOG" 2>&1 &
    COOKIE_PID=$!
    echo "      cookie 自动同步已启动（日志 ${COOKIE_LOG}）"
fi

cleanup() {
    echo
    echo "收工，停掉 bridge.py (pid $BRIDGE_PID)"
    kill "$BRIDGE_PID" 2>/dev/null
    if [ -n "$COOKIE_PID" ]; then
        kill "$COOKIE_PID" 2>/dev/null
        echo "      cookie 同步也停了 (pid $COOKIE_PID)"
    fi
    wait "$BRIDGE_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

sleep 1
if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo "bridge.py 没起来，看日志："
    tail -20 "$BRIDGE_LOG"
    exit 1
fi

if command -v curl >/dev/null 2>&1; then
    if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null; then
        echo "bridge.py 起来了但 /health 不通，看日志：$BRIDGE_LOG"
        exit 1
    fi
fi

echo
echo "开始同步 DP（Ctrl-C 退出）"
echo
echo "  想看灯闪红、听播报，但不想等真实工单，另开一个终端跑 mock："
echo "    $PY bridge.py --mock --port 8081"
echo "    curl -X POST 'http://127.0.0.1:8081/sim?p0=2'              # 红：有人在等你"
echo "    curl -X POST 'http://127.0.0.1:8081/sim?p0=0&p1=1'        # 黄：只在处理中"
echo "    curl -X POST 'http://127.0.0.1:8081/sim?p0=0&p1=0&p2=0'   # 绿 + 庆祝动画"
echo

$PY dp_push.py --watch --interval "$INTERVAL" \
    --tickets-url "http://127.0.0.1:$PORT/tickets"
