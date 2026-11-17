#!/usr/bin/env bash
#
# T5 团队状态灯 · 板子侧一键安装
#
#   ./install.sh                 完整跑：装代码 → 编译 → 烧录
#   ./install.sh --dry-run       只检查环境并打印将要做的事，不动任何文件
#   ./install.sh --status        只看当前装到哪一步了
#   ./install.sh --check-lan     验证板子能不能访问到本机（烧录前后都能跑）
#   ./install.sh --build-only    不烧录，只编译
#
#   TUYAOPEN_ROOT=~/TuyaOpen ./install.sh
#   SL_BRIDGE_HOST=192.168.1.9 ./install.sh    手动指定本机 IP
#
# ---------------------------------------------------------------------
# 这条路**不需要任何涂鸦云凭据**。
#
# 板子主动来拉数据：GET http://<本机IP>:8080/tickets
# 固件只做三件事：拉数据、画屏幕、播语音。全部在同一条局域网里闭环。
#
# 会用到的 TuyaOpen 工程是 your_chat_bot —— 选它而不是更轻的 duo_eye_mood，
# 是因为它有 TUYA_T5AI_BOARD_LCD_3.5 这份现成的 3.5 寸板级配置。
# 注意它 src/display2/ 里的 tuya_lvgl_* 默认不参与编译（CONFIG_ENABLE_CHAT_DISPLAY2=n），
# 所以本工程自带 src/sl_lvgl.c，直接包 SDK 的 lv_vendor_*。
# 我们只把它的入口 tuya_main.c 换掉，不碰其余代码。
# ---------------------------------------------------------------------

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ_ROOT="$(cd "$HERE/.." && pwd)"

# 目标工程名（在 TuyaOpen 里的 apps/tuya.ai/<名字>）
APP_NAME="${APP_NAME:-status_light}"

# 默认 IP：先看命令行，再看环境变量，最后自动探测
BRIDGE_HOST="${SL_BRIDGE_HOST:-}"
BRIDGE_PORT="${SL_BRIDGE_PORT:-8080}"

TUYAOPEN_ROOT="${TUYAOPEN_ROOT:-}"

DRY_RUN=0
BUILD_ONLY=0

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
[ -t 1 ] || { C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""; }

step()  { echo; echo "${C_OK}==> $*${C_OFF}"; }
info()  { echo "    $*"; }
dim()   { echo "    ${C_DIM}$*${C_OFF}"; }
warn()  { echo "    ${C_WARN}注意：$*${C_OFF}"; }
die()   { echo; echo "${C_ERR}卡住了：$*${C_OFF}"; echo; exit 1; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "    ${C_DIM}[dry-run] $*${C_OFF}"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------
# 1. 探测本机局域网 IP
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
        ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
        [ -n "$ip" ] && { echo "$ip"; return 0; }
    fi

    # 最后再兜一次
    ip="$(python3 -c "
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

    return 1
}

# ---------------------------------------------------------------------
# 2. 找 TuyaOpen 源码
# ---------------------------------------------------------------------
find_tuyaopen() {
    local cand
    for cand in "$TUYAOPEN_ROOT" \
                "$HOME/TuyaOpen" "$HOME/tuyaopen" "$HOME/Downloads/TuyaOpen" \
                "$HOME/Documents/TuyaOpen" "$HOME/work/TuyaOpen" \
                "$PROJ_ROOT/../TuyaOpen" "$PROJ_ROOT/../../TuyaOpen"; do
        [ -n "$cand" ] && [ -f "$cand/tos.py" ] && { echo "$cand"; return 0; }
    done
    return 1
}

print_status() {
    echo
    echo "=== 当前状态 ==="
    printf '  %-22s %s\n' "本机局域网 IP" "${LAN_IP:-探测失败}"
    printf '  %-22s %s\n' "bridge 端口" "$BRIDGE_PORT"
    printf '  %-22s %s\n' "TuyaOpen 源码" "${TY_ROOT:-未找到}"
    printf '  %-22s %s\n' "目标工程" "${TY_ROOT:+$TY_ROOT/apps/tuya.ai/$APP_NAME}"

    if [ -n "${TY_ROOT:-}" ] && [ -d "$TY_ROOT/apps/tuya.ai/$APP_NAME" ]; then
        local n
        n="$(find "$TY_ROOT/apps/tuya.ai/$APP_NAME/src" -maxdepth 1 -name 'app_status_light.*' 2>/dev/null | wc -l | tr -d ' ')"
        if [ "$n" = "2" ]; then
            printf '  %-22s %s\n' "固件源文件" "已装入"
        else
            printf '  %-22s %s\n' "固件源文件" "未装入"
        fi

        if grep -q "app_status_light_init" "$TY_ROOT/apps/tuya.ai/$APP_NAME/src/tuya_main.c" 2>/dev/null; then
            printf '  %-22s %s\n' "入口已挂接" "是"
        else
            printf '  %-22s %s\n' "入口已挂接" "否"
        fi

        if [ -f "$TY_ROOT/apps/tuya.ai/$APP_NAME/build/switch_demo_1.0.0.bin" ] 2>/dev/null; then
            printf '  %-22s %s\n' "已编译产物" "有"
        fi
    fi

    if [ -f "$PROJ_ROOT/bridge/audio/urgent.wav" ]; then
        printf '  %-22s %s\n' "播报音频" "已生成"
    else
        printf '  %-22s %s\n' "播报音频" "未生成"
    fi
    echo
}

check_lan() {
    echo
    echo "=== 板子能不能访问到本机 ==="
    echo
    info "本机 IP    : ${LAN_IP:-探测失败}"
    info "板子要请求 : http://${LAN_IP:-?}:$BRIDGE_PORT/tickets"
    echo

    # bridge 在不在跑
    if curl -fsS --noproxy '*' -m 3 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null 2>&1; then
        info "bridge 正在本机运行 ✓"
    else
        warn "本机 $BRIDGE_PORT 端口没有服务在跑。先起它：cd $PROJ_ROOT/bridge && ./start.sh"
    fi

    # 从局域网 IP 能不能访问（这一步失败 = bridge 绑在了 127.0.0.1）
    if [ -n "${LAN_IP:-}" ]; then
        if curl -fsS --noproxy '*' -m 3 "http://$LAN_IP:$BRIDGE_PORT/health" >/dev/null 2>&1; then
            info "从局域网 IP 访问得到 ✓  （bridge 绑的是 0.0.0.0）"
        else
            warn "从 $LAN_IP 访问不到。bridge.py 的 --host 必须是 0.0.0.0（默认就是）"
        fi
    fi

    echo
    echo "  板子侧如果拉不到数据，按这个顺序查："
    echo "    1. 板子和本机是不是同一个 Wi-Fi —— 公司 Wi-Fi 常开 AP 隔离（客户端之间互相不可见）"
    echo "       → 换个不开隔离的热点试，或让 IT 放行"
    echo "    2. 本机防火墙有没有放行 $BRIDGE_PORT"
    echo "       → 系统设置 → 网络 → 防火墙；或临时关掉验证一次"
    echo "    3. 固件里的 IP 是不是 ${LAN_IP}（换网络后 IP 会变，要重新注入）"
    echo "    4. 串口日志里搜 sl: —— 能看到 \"poll thread started, target=http://...\""
    echo
}

# ---------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1 ;;
        --build-only) BUILD_ONLY=1 ;;
        --status)     LAN_IP="$(detect_lan_ip || true)"; TY_ROOT="$(find_tuyaopen || true)"; print_status; exit 0 ;;
        --check-lan)
            LAN_IP="$(detect_lan_ip || true)"
            [ -z "$LAN_IP" ] && die "探测不到本机局域网 IP，手动指定：SL_BRIDGE_HOST=192.168.x.x $0 --check-lan"
            check_lan
            exit 0 ;;
        -h|--help)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "不认识的参数：$1（试试 --help）" ;;
    esac
    shift
done

echo "======================================================"
echo " T5 状态灯 · 板子侧安装"
echo "======================================================"

# ---------------------------------------------------------------------
step "[1/6] 探测本机局域网 IP"
if [ -z "$BRIDGE_HOST" ]; then
    BRIDGE_HOST="$(detect_lan_ip || true)"
fi
[ -z "$BRIDGE_HOST" ] && die "探测不到局域网 IP。手动指定：SL_BRIDGE_HOST=192.168.x.x ./install.sh"

LAN_IP="$BRIDGE_HOST"
info "本机 IP : $BRIDGE_HOST"
dim "固件会用它拼出 http://$BRIDGE_HOST:$BRIDGE_PORT/tickets"
dim "换网络后 IP 会变，重跑一次本脚本即可。"

# ---------------------------------------------------------------------
step "[2/6] 准备播报音频"
AUDIO_DIR="$PROJ_ROOT/bridge/audio"
NEEDED="urgent warning celebrate"

# 包内自带的三段 wav 本来就是能直接用的（16kHz / 16bit / 单声道，data 偏移 36），
# 生成脚本只是「换成自己的话、换音色」。所以它失败**不能**拖垮整条安装 ——
# 这一点在非 macOS 上必然触发：make_audio.sh 依赖 macOS 自带的 say。
audio_ready() {
    local n f
    for n in $NEEDED; do
        f="$AUDIO_DIR/$n.wav"
        [ -s "$f" ] || return 1
        [ "$(head -c 4 "$f" 2>/dev/null)" = "RIFF" ] || return 1
    done
    return 0
}

if [ "$DRY_RUN" = "1" ]; then
    echo "    ${C_DIM}[dry-run] $PROJ_ROOT/assets/make_audio.sh${C_OFF}"
elif [ -x "$PROJ_ROOT/assets/make_audio.sh" ]; then
    if "$PROJ_ROOT/assets/make_audio.sh"; then
        :
    elif audio_ready; then
        warn "本机生成不了音频（'say' 只有 macOS 有），沿用包里自带的 wav"
        dim "要换成自己的话：在 macOS 上跑一次 assets/make_audio.sh，把 bridge/audio/ 拷过来"
    else
        die "音频既生成不了、bridge/audio/ 里也凑不齐（需要 $NEEDED 三个 .wav）。
   包内自带的是 16kHz/16bit/单声道，拷过来就能用；或者在 macOS 上跑一次
   assets/make_audio.sh 再拷过来。"
    fi
else
    warn "找不到 assets/make_audio.sh，跳过"
fi

# 到这里必须齐了。缺音频的后果不是报错而是「板子不出声」——
# 那种静默失败比安装失败难查得多，所以在烧之前就拦住。
if [ "$DRY_RUN" != "1" ] && ! audio_ready; then
    die "bridge/audio/ 里缺播报音频（需要 $NEEDED 这三个 .wav）。补齐再烧，否则板子只会静默无声。"
fi

# ---------------------------------------------------------------------
step "[3/6] 定位 TuyaOpen 源码"
TY_ROOT="$(find_tuyaopen || true)"
if [ -z "$TY_ROOT" ]; then
    echo
    echo "找不到 TuyaOpen 源码（判断依据：目录下有 tos.py）。"
    echo
    echo "先 clone 一份，首次约 2 分钟："
    echo
    echo "    git clone --depth 1 https://github.com/tuya/TuyaOpen.git ~/TuyaOpen"
    echo "    cd ~/TuyaOpen && ./tos.py config choice    # 选 T5AI，装工具链"
    echo
    echo "装好了再回来跑本脚本；或者显式指定位置："
    echo "    TUYAOPEN_ROOT=/path/to/TuyaOpen ./install.sh"
    echo
    exit 1
fi
info "TuyaOpen : $TY_ROOT"
[ -f "$TY_ROOT/tos.py" ] || die "$TY_ROOT 下没有 tos.py，这不像 TuyaOpen 的根目录"

# tos.py 的 Python 依赖装在 $TY_ROOT/.venv（一键安装时我们建好的）；
# 优先用它的解释器，避免系统 python3 缺 click/pyyaml 之类。
if [ -x "$TY_ROOT/.venv/bin/python" ]; then
    TOS="$TY_ROOT/.venv/bin/python $TY_ROOT/tos.py"
    dim "tos.py 用工程自带 venv：$TOS"
elif command -v tos.py >/dev/null 2>&1; then
    TOS="tos.py"
else
    TOS="python3 $TY_ROOT/tos.py"
    dim "tos.py 不在 PATH，改用 ${TOS}（依赖需自己装：click pyyaml gitpython pyserial kconfiglib requests）"
fi

SRC_APP="$TY_ROOT/apps/tuya.ai/your_chat_bot"
[ -d "$SRC_APP" ] || die "找不到参考工程：$SRC_APP
你的 TuyaOpen 版本可能不叫 your_chat_bot。列一下有哪些可选：
    ls $TY_ROOT/apps/tuya.ai/
然后 APP_NAME=... 重新跑（需要那个工程里有 src/display2/tuya_lvgl.h）"

DST_APP="$TY_ROOT/apps/tuya.ai/$APP_NAME"

# ---------------------------------------------------------------------
step "[4/6] 准备工程 $APP_NAME"
if [ -d "$DST_APP" ]; then
    info "工程已存在，跳过 fork（保住你之前的手改）"
else
    info "从 your_chat_bot fork -> apps/tuya.ai/$APP_NAME"
    run mkdir -p "$DST_APP"
    run cp -R "$SRC_APP/." "$DST_APP/"
fi

# 清掉上一次的构建缓存：改了板级配置或源文件后不复用旧 cache，
# 否则会出现"我明明改了代码，烧进去还是老行为"这种鬼故事。
if [ -d "$DST_APP/build" ]; then
    info "清掉旧的 build/ 缓存"
    run rm -rf "$DST_APP/build"
fi

# 拷固件源文件。放到 src/ 根目录就够了 ——
# 工程的 CMakeLists 里是 aux_source_directory(src)，会自动收走，不用改构建脚本。
for f in "$PROJ_ROOT"/src/app_status_light.c \
         "$PROJ_ROOT"/src/app_status_light.h \
         "$PROJ_ROOT"/src/sl_lvgl.c \
         "$PROJ_ROOT"/src/sl_lvgl.h \
         "$PROJ_ROOT"/src/ui_status.c \
         "$PROJ_ROOT"/src/ui_status.h ; do
    [ -f "$f" ] || die "缺源文件：$f"
    run cp "$f" "$DST_APP/src/"
done
info "装好 6 个源文件 -> src/"

# 覆盖入口。原版入口调 app_chat_bot_init()，会把整套 AI 对话拉起来；
# 我们只要「拉数据 + 画屏幕 + 播语音」。
[ -f "$HERE/tuya_main.c" ] || die "缺入口文件：$HERE/tuya_main.c"
if [ -f "$DST_APP/src/tuya_main.c" ] \
   && ! grep -q "T5 团队状态灯" "$DST_APP/src/tuya_main.c" 2>/dev/null; then
    run cp "$DST_APP/src/tuya_main.c" "$DST_APP/src/tuya_main.c.orig"
    dim "原入口备份成 src/tuya_main.c.orig"
fi
run cp "$HERE/tuya_main.c" "$DST_APP/src/tuya_main.c"
info "覆盖入口 -> src/tuya_main.c"

# ---------------------------------------------------------------------
step "[5/6] 把 IP 注入固件"
if [ "$DRY_RUN" = "1" ]; then
    echo "    ${C_DIM}[dry-run] 把 SL_BRIDGE_HOST 改成 $BRIDGE_HOST${C_OFF}"
else
    python3 - "$DST_APP/src/app_status_light.c" "$BRIDGE_HOST" "$BRIDGE_PORT" <<'PYEOF'
import re, sys

path, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path, encoding="utf-8").read()

# 先用 search 确认模板还在，再做替换。
# 关键：不能用「替换前后是否相同」判断成功 —— 重复跑本脚本时
# 探测到的 IP 和文件里已有的一致，内容本来就该不变，那不是错误。
m = re.search(r'#define\s+SL_BRIDGE_HOST\s+"([^"]*)"', src)
if not m:
    print("    ✗ 没找到 SL_BRIDGE_HOST 的定义，固件可能被改过结构了")
    sys.exit(1)
before = m.group(1)

# 只改 #define 后面的值；#ifndef 那层结构保持不动，
# 这样以后想用 -DSL_BRIDGE_HOST=... 覆盖也照样管用。
src = re.sub(r'(#define\s+SL_BRIDGE_HOST\s+)"[^"]*"',
             lambda mm: mm.group(1) + '"%s"' % host, src, count=1)
src = re.sub(r'(#define\s+SL_BRIDGE_PORT\s+)\d+',
             lambda mm: mm.group(1) + port, src, count=1)

open(path, "w", encoding="utf-8").write(src)

if before == host:
    print("    SL_BRIDGE_HOST = %s   （和上次一致，无需改动）" % host)
else:
    print("    SL_BRIDGE_HOST : %s -> %s" % (before, host))
print("    SL_BRIDGE_PORT = %s" % port)
PYEOF
    [ $? -ne 0 ] && die "注入 IP 失败"
fi

# ---------------------------------------------------------------------
step "[6/6] 编译"
cd "$TY_ROOT" || die "进不去 $TY_ROOT"

if [ "$DRY_RUN" = "1" ]; then
    echo "    ${C_DIM}[dry-run] cd $DST_APP && $TOS build${C_OFF}"
    echo
    echo "dry-run 结束。去掉 --dry-run 就会真的开始编译烧录。"
    exit 0
fi

# 选板级配置：非交互式 -c 指定板子，首次会自动下载 SDK 平台/工具链。
if [ ! -f "$DST_APP/.build/cache/using.config" ]; then
    info "选板子 TUYA_T5AI_BOARD_LCD_3.5（首次会下载工具链，较慢）"
    ( cd "$DST_APP" && $TOS config choice -c TUYA_T5AI_BOARD_LCD_3.5.config ) \
        || die "板级配置选择失败"
fi

echo
echo "    板子固定为 TUYA_T5AI_BOARD_LCD_3.5（3.5 寸 480x320，ILI9488 + GT1151 触摸）。"
echo "    换别的屏改 install.sh 里 config choice -c 那一行，并确认它能开 CONFIG_ENABLE_DISPLAY=y / CONFIG_ENABLE_LIBLVGL=y。"
echo

cd "$DST_APP" || die "进不去 $DST_APP"
$TOS build || die "编译失败。先看上面第一条 error —— 通常是要重跑 tos.py config choice 选板子/装工具链"

echo
echo "======================================================"
echo " ${C_OK}编译通过${C_OFF}"
echo "======================================================"

if [ "$BUILD_ONLY" = "1" ]; then
    echo
    echo "--build-only 已指定，不烧录。手动烧："
    echo "    cd $DST_APP && $TOS flash"
    exit 0
fi

# ---------------------------------------------------------------------
step "烧录"
echo "    把板子用 USB 连上。"
warn "T5AI-Board 的 TF 卡 DATA3/DATA2 引脚和烧录用的 UART 是复用的 —— 插着 TF 卡可能烧不进去，先拔掉。"
echo
read -r -p "    接好了按回车开始烧录（Ctrl-C 放弃）..." _

$TOS flash || die "烧录失败。检查：① USB 线是不是数据线不是充电线 ② TF 卡拔了没 ③ 板子有没有被别的程序占着串口"

echo
echo "======================================================"
echo " ${C_OK}烧录完成${C_OFF}"
echo "======================================================"
echo
echo "现在盯着串口日志（Ctrl-C 退出）："
echo
echo "    cd $DST_APP && tos.py monitor"
echo
echo "看到这两行就说明状态灯活了："
echo
echo "    sl: poll thread started, target=http://$BRIDGE_HOST:$BRIDGE_PORT/tickets"
echo "    sl: state 0 -> 2 (p0=3 p1=2 p2=0)"
echo
echo "别在等真实工单 —— 先把 bridge 灌上假数据，立刻就能看到灯变色、听到播报："
echo
echo "    # 另开一个终端"
echo "    cd $PROJ_ROOT/bridge && python3 bridge.py --mock --port 8080"
echo "    curl -X POST 'http://127.0.0.1:8080/sim?p0=2'            # 红闪 + 语音"
echo "    curl -X POST 'http://127.0.0.1:8080/sim?p0=0&p1=0&p2=0'  # 绿 + 庆祝动画"
echo
echo "接回真实的工单数据源："
echo
echo "    cd $PROJ_ROOT/bridge && ./start.sh"
echo
