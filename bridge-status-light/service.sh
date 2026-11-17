#!/usr/bin/env bash
#
# T5 状态灯 · 把 bridge 装成常驻服务（macOS launchd）
#
#   ./service.sh                 看当前状态 + 告诉你下一步做什么
#   ./service.sh print           只打印将要写入的 plist，不碰任何东西
#   ./service.sh install         装成常驻服务（开机自启、被杀自动拉起）
#   ./service.sh uninstall       卸掉（连 plist 一起删）
#   ./service.sh start|stop|restart
#   ./service.sh status
#   ./service.sh logs [行数]     看服务日志，默认 40 行
#
#   PORT=9090 ./service.sh install
#   PYTHON=/usr/bin/python3 ./service.sh install
#
# ---------------------------------------------------------------------
# 【为什么需要这个脚本】
#
# 这套架构里，"跑 bridge 的那台机器必须常开" 一直是靠人肉保证的：
# 前台跑 start.sh，关掉终端就没了；进程悄悄死掉时**不会有任何报错**，
# 板子那边的症状只是"偶尔灰一下" —— 极难联想到是服务没了。
#
# 实测过两次：同一个 bridge 一次活了 22 小时 42 分、一次只活了 7 分 51 秒，
# 两次都是 stdout 停在某一刻再无输出、没有 traceback —— 被外部杀掉的。
# 所以「进程守护」在这个架构里不是可选项。
#
# launchd 的 KeepAlive 就是干这个的：死了立刻拉起来、开机自动启动。
# ---------------------------------------------------------------------

set -uo pipefail

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"

LABEL="com.tuya.t5-status-bridge"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs"
OUT_LOG="${LOG_DIR}/t5-status-bridge.out.log"
ERR_LOG="${LOG_DIR}/t5-status-bridge.err.log"

PORT="${PORT:-8080}"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
[ -t 1 ] || { C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""; }

info() { echo "    $*"; }
dim()  { echo "    ${C_DIM}$*${C_OFF}"; }
warn() { echo "    ${C_WARN}注意：$*${C_OFF}"; }
die()  { echo; echo "${C_ERR}卡住了：$*${C_OFF}"; echo; exit 1; }

is_macos() { [ "$(uname -s)" = "Darwin" ]; }

# 找 python3 的绝对路径。
# 必须绝对 —— launchd 启动的进程 PATH 极简，写 `python3` 会找不到。
#
# ★ 故意优先系统 python3、并跳过 ~/.workbuddy|~/.venv 这类"某个工具自己装的运行时"：
#   把用户的常驻服务指到别人的隔离环境里，那天那个工具升级/清理，服务就全崩，
#   而症状只是"板子偶尔灰一下"，几乎不可能查到根因。
find_python() {
    if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON}" ]; then
        echo "${PYTHON}"; return 0
    fi

    local c
    for c in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
        [ -x "${c}" ] && { echo "${c}"; return 0; }
    done

    c="$(command -v python3 2>/dev/null || true)"
    [ -n "${c}" ] || return 1
    case "${c}" in
    */.workbuddy/* | */.venv/* | */venv/*) return 1 ;;
    esac
    echo "${c}"
}

port_busy() {
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

port_owner() {
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" (pid "$2")"}'
}

gen_plist() {
    local py="$1"
    /usr/bin/python3 - "$LABEL" "$HERE" "$py" "$PORT" "$OUT_LOG" "$ERR_LOG" <<'PY'
import plistlib, sys

label, workdir, py, port, out_log, err_log = sys.argv[1:7]
py_dir = py.rsplit("/", 1)[0]

plist = {
    "Label": label,
    # 用 start.sh 而不是直接跑 bridge.py —— 它的预检（Cookie 在不在、数据源通不通）
    # 和 cookie 自动同步都在里面，直接跑 bridge.py 会把这些都跳过。
    "ProgramArguments": [f"{workdir}/start.sh", "--lan"],
    "WorkingDirectory": workdir,
    "EnvironmentVariables": {
        # launchd 给的 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，
        # 不带上 python 所在目录的话，start.sh 里的 `python3` 会找不到。
        "PATH": f"{py_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHON": py,
        "PORT": port,
    },
    "RunAtLoad": True,
    # 死了就拉起来。配合下面的 ThrottleInterval，坏配置时不会疯狂重启刷日志。
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "StandardOutPath": out_log,
    "StandardErrorPath": err_log,
    # Cookie 故意**不写进这里** —— 那是能读你全部工单的凭据，
    # 放进 plist 等于摊在 ~/Library/LaunchAgents 里。start.sh 自己会去读 ~/.cs_cookie。
}
sys.stdout.write(plistlib.dumps(plist).decode("utf-8"))
PY
}

print_plist() {
    local py
    py="$(find_python)" || die "找不到合适的 python3。
   指定一个：PYTHON=/usr/bin/python3 $0 print
   （不会用只有某个工具才有的隔离运行时 —— 那种环境被清理掉时，服务会莫名其妙全崩）"
    echo "=== 将要写入 ${PLIST} ==="
    echo
    gen_plist "${py}"
    echo
    dim "python : ${py}"
    dim "工作目录: ${HERE}"
    dim "端口   : ${PORT}"
    dim "输出   : ${OUT_LOG}"
    dim "错误   : ${ERR_LOG}"
}

preflight() {
    local py="$1"

    [ -f "./bridge.py" ] || die "当前目录没有 bridge.py —— 本脚本要放在 bridge/ 里跑"
    [ -x "./start.sh" ] || die "start.sh 不存在或不可执行（chmod +x bridge/start.sh）"

    # 端口被占：装了也是白装（launchd 会不停重启它）
    if port_busy; then
        die "${PORT} 端口已经被占用了：$(port_owner)
   同一个端口装两个服务，只会互相抢、刷一堆 EADDRINUSE。
   要么先停掉占用的那个，要么换端口：PORT=9090 $0 install"
    fi

    # 真去取一次数 —— 取不到就别装，否则 launchd 会每 30 秒重启一次失败的进程
    info "预检：拉一次真实数据（最多等 30 秒）"
    if CS_COOKIE="${CS_COOKIE:-$(cat "$HOME/.cs_cookie" 2>/dev/null || true)}" \
       "${py}" ./bridge.py --check >/tmp/t5-service-preflight.log 2>&1; then
        dim "$(grep -E 'p0 = ' /tmp/t5-service-preflight.log | head -1 | sed 's/^ *//')"
    else
        echo
        echo "${C_ERR}预检没过，先别装服务。${C_OFF}原因是："
        echo
        sed 's/^/    /' /tmp/t5-service-preflight.log | tail -12
        echo
        echo "    取不到数就装服务，launchd 会每 30 秒重启一个注定失败的进程，"
        echo "    日志刷屏、还掩盖真正的问题。先让 ./start.sh --lan 能跑通，再回来装。"
        echo
        rm -f /tmp/t5-service-preflight.log
        exit 1
    fi
    rm -f /tmp/t5-service-preflight.log
}

load_service() {
    # 新版 macOS 用 bootstrap，老版用 load -w。先试新的，失败再退回去。
    launchctl bootstrap "gui/$(id -u)" "${PLIST}" 2>/dev/null && return 0
    launchctl load -w "${PLIST}" 2>/dev/null && return 0
    return 1
}

unload_service() {
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null && return 0
    launchctl unload -w "${PLIST}" 2>/dev/null && return 0
    return 1
}

installed() { [ -f "${PLIST}" ]; }

running() {
    launchctl list 2>/dev/null | awk -v l="${LABEL}" '$3 == l {print $1}'
}

# ---------------------------------------------------------------------
case "${1:-}" in
print | --print | --dry-run)
    print_plist
    exit 0
    ;;

install)
    is_macos || die "本脚本只处理 macOS 的 launchd。Linux 上请用：$0 print 看 systemd 的写法"
    py="$(find_python)" || die "找不到合适的 python3。
   指定一个：PYTHON=/usr/bin/python3 $0 install"

    if installed; then
        warn "${PLIST} 已经存在。先卸掉再装：$0 uninstall"
        exit 1
    fi

    echo "======================================================"
    echo " 把 bridge 装成常驻服务"
    echo "======================================================"
    echo

    preflight "${py}"

    mkdir -p "$HOME/Library/LaunchAgents" "${LOG_DIR}" || die "建目录失败"
    gen_plist "${py}" >"${PLIST}" || die "写 plist 失败"

    # 先自己 lint 一遍 —— 这个文件的语法错会让 launchctl 只回一句
    # 语焉不详的 "Load failed: 5"，很难查。
    if ! plutil -lint "${PLIST}" >/dev/null 2>&1; then
        plutil -lint "${PLIST}" | sed 's/^/    /'
        rm -f "${PLIST}"
        die "生成的 plist 语法有问题（已删掉，没有动系统）"
    fi
    dim "已写入 ${PLIST}（plutil 校验通过）"

    if ! load_service; then
        rm -f "${PLIST}"
        die "launchctl 加载失败（plist 已删掉，没留下半个服务）"
    fi
    dim "已加载到 launchd"

    # 等它真的起来 —— "加载成功" 不等于 "服务在跑"
    printf '    '
    ok=0
    for _ in $(seq 1 15); do
        printf '.'
        sleep 1
        if curl -fsS --noproxy '*' -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
            ok=1
            break
        fi
    done
    echo

    if [ "${ok}" = "1" ]; then
        echo
        echo "${C_OK}=> 服务已就绪${C_OFF}  http://127.0.0.1:${PORT}/tickets"
        echo
        info "看日志：$0 logs"
        info "看状态：$0 status"
        info "卸掉  ：$0 uninstall"
        echo
        if [ -n "$(running)" ]; then
            dim "launchd 里能看到它（pid $(running)）"
        fi
    else
        warn "plist 装好了，但 ${PORT} 端口 15 秒内没起来"
        echo "    看日志：$0 logs"
        echo "    launchd 因为 KeepAlive 会每 30 秒再试一次，不用你手动重试。"
        exit 1
    fi
    ;;

uninstall)
    is_macos || die "本脚本只处理 macOS"
    if ! installed; then
        info "${PLIST} 不存在，本来就没装。"
        exit 0
    fi
    unload_service && dim "已从 launchd 卸载" || warn "launchctl 卸载报错（可能本来就没加载）"
    rm -f "${PLIST}" && dim "已删除 ${PLIST}"
    echo "${C_OK}=> 已卸掉。${C_OFF}（日志文件留在 ${LOG_DIR}，要清自己删）"
    ;;

start)
    is_macos || die "本脚本只处理 macOS"
    installed || die "还没装。先跑：$0 install"
    load_service && info "已启动" || die "启动失败，看：$0 logs"
    ;;

stop)
    is_macos || die "本脚本只处理 macOS"
    installed || die "还没装。先跑：$0 install"
    unload_service && info "已停止" || die "停止失败"
    ;;

restart)
    is_macos || die "本脚本只处理 macOS"
    installed || die "还没装。先跑：$0 install"
    unload_service >/dev/null 2>&1 || true
    sleep 1
    load_service && info "已重启" || die "重启失败，看：$0 logs"
    ;;

status)
    # 用「标签：值」而不是对齐的列 —— printf 的 %-16s 是按**字节数**补空格的，
    # 中文标签会因此差出一倍宽度，列永远是歪的。少一层对齐，多一分可靠。
    echo
    echo "=== bridge 服务状态 ==="
    echo "  运行平台：$(uname -s)"
    echo "  脚本目录：${HERE}"
    echo "  端口：${PORT}"
    if ! is_macos; then
        echo "  服务守护：不适用（本脚本只处理 macOS 的 launchd）"
    elif installed; then
        echo "  plist：已安装"
        pid="$(running)"
        if [ -n "${pid}" ] && [ "${pid}" != "-" ]; then
            echo "  launchd：运行中（pid ${pid}）"
        else
            echo "  launchd：已加载但没有进程（可能在 30 秒重启间隔里）"
        fi
    else
        echo "  plist：未安装"
    fi

    if curl -fsS --noproxy '*' -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "  端口自检：有服务在响应 ✓"
    else
        echo "  端口自检：没人响应 ✗"
    fi
    if port_busy; then
        echo "  占用者：$(port_owner)"
    fi
    echo
    if is_macos && ! installed; then
        echo "  还没装成常驻服务。现在它是前台跑的 —— 关掉终端就没了，"
        echo "  而且死掉时不会有任何报错，板子那边只是'偶尔灰一下'。"
        echo
        echo "    装：$0 install      （先看会写什么：$0 print）"
        echo
    fi
    ;;

logs)
    n="${2:-40}"
    if [ ! -f "${OUT_LOG}" ] && [ ! -f "${ERR_LOG}" ]; then
        die "还没有日志文件。服务没装、或者刚装还没写过东西。"
    fi
    echo "=== ${OUT_LOG} 最后 ${n} 行 ==="
    tail -n "${n}" "${OUT_LOG}" 2>/dev/null || echo "（空）"
    if [ -s "${ERR_LOG}" ]; then
        echo
        echo "=== ${ERR_LOG} 最后 ${n} 行（有内容才说明出过错）==="
        tail -n "${n}" "${ERR_LOG}"
    fi
    ;;

-h | --help | "")
    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;

*)
    die "不认识的参数：$1（试试 $0 --help）"
    ;;
esac
