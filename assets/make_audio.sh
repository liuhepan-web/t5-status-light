#!/usr/bin/env bash
#
# 生成状态灯要用的播报音频
#
#   ./make_audio.sh              生成到 ../bridge/audio/
#   OUT=/tmp/x ./make_audio.sh   换个输出目录
#   VOICE=Sinji ./make_audio.sh  换个音色（say -v '?' 可列全部）
#
# ---------------------------------------------------------------------
# 为什么用 WAV 而不是 MP3
#
# 原来这脚本依赖 ffmpeg 把 AIFF 转 MP3。但 ffmpeg 要 brew 单独装，
# 而 macOS 自带的 say 其实**能直接输出 16kHz/16bit/单声道 WAV**：
#
#     say --file-format=WAVE --data-format=LEI16@16000 -o x.wav "文本"
#
# 这正好就是 T5 要的格式（LEI16 = 小端 16 位整数），一个字节都不用转。
# 零依赖、不会因为没装 ffmpeg 卡住，音质对语音播报完全够。
#
# 顺带说一句：格式声明和实际文件不一致是这块板子上最阴的坑之一 ——
# 把 WAV 声明成 MP3 不会报错，只会表现为"卡顿、变调或者干脆没声"。
#
# 固件侧是把格式**写死成 AI_AUDIO_CODEC_WAV** 的（见 app_status_light_speak），
# 故意不按 URL 后缀去猜：解码器列表是编译期定死的，按后缀猜只会把"格式不符"
# 这类静默失败从编译期拖到运行期，而运行期它是无声的、最难查。
# 所以：想换 MP3 就得改固件重烧；不想重烧就用 WAV。
#
# ---------------------------------------------------------------------
# 为什么还要「洗一遍头」（strip_filler）
#
# macOS 的 say/afconvert 写 WAVE 时，默认会在 fmt 和 data 之间塞一个
# ~4044 字节的 FLLR 填充块，把 data 推到偏移 4088。
#
# 这对板子是致命的：播放器一次只读 AI_PLAYER_FRAMEBUF_SIZE = 4096 字节，
# 于是第一包里只剩 8 字节真 PCM —— 解码器凑不齐一帧，就一个字节都不消费；
# player->offset 停在 4096（正好等于缓冲上限），下一次读变成 max_len=0，
# libhttp 直接判失败，datasink 只能 Range 重连 —— 但缓冲还是满的。
# 结果就是永远卡在第一包，板子日志刷：
#     url sink read failed at offset 4096/115022, will retry
#
# 洗成 44 字节规范头（RIFF/WAVE/fmt16/data）后，第一包 = 44 字节头 +
# 4052 字节 PCM，解码器立刻能吐一帧并消化数据，整条流就通了。
#
# 注意这不是偶发：跟填充块具体大小无关，只跟「data 起点 > 4096」有关，
# 而 FLLR 正好是 4KB 量级 —— 在 macOS 上必然复现。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$(cd "$HERE/.." && pwd)/bridge/audio}"

VOICE="${VOICE:-Tingting}"    # 中文女声。可用 say -v '?' 查所有音色
RATE="${RATE:-190}"           # 语速；180~200 之间听着最自然

mkdir -p "$OUT"

if ! command -v say >/dev/null 2>&1; then
    echo "找不到 say —— 这个脚本依赖 macOS 自带的语音合成。"
    exit 1
fi

# 把 WAV 洗成 44 字节规范头（RIFF/WAVE/fmt16/data），丢掉 FLLR 之类的填充块。
# 只用标准库 wave —— 和这个脚本「零依赖」的基调保持一致。
strip_filler() {
    local f="$1"
    python3 - "$f" <<'PY'
import sys, wave

path = sys.argv[1]
old = open(path, "rb").read().find(b"data")

with wave.open(path, "rb") as w:
    ch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
    pcm = w.readframes(w.getnframes())

if old <= 44:
    print("  洗头：已经是规范头，跳过")
    sys.exit(0)

with wave.open(path, "wb") as w:
    w.setnchannels(ch)
    w.setsampwidth(sw)
    w.setframerate(fr)
    w.writeframes(pcm)

new = open(path, "rb").read().find(b"data")
print("  洗头：data 偏移 %d -> %d（丢掉 %d 字节填充）" % (old, new, old - new))
PY
}

synth() {
    local name="$1"
    local text="$2"
    local out="$OUT/${name}.wav"

    printf '· %-12s ← "%s"\n' "${name}.wav" "$text"

    # LEI16@16000 = 小端 16 位整数 / 16kHz 单声道
    say -v "$VOICE" -r "$RATE" \
        --file-format=WAVE --data-format=LEI16@16000 \
        -o "$out" "$text"

    if [ ! -s "$out" ]; then
        echo "  ✗ 生成失败：$out 是空的"
        return 1
    fi

    strip_filler "$out"
}

echo "输出目录：$OUT"
echo "音色：$VOICE   语速：$RATE"
echo

# 进 P0 —— 短促、有压迫感，但不能吓人。控制在 3 秒内。
synth "urgent" "注意，有新的紧急工单待处理，请尽快响应。"

# 只有 P1（处理中）—— 只在"点击屏幕"时播，不自动响。
# 别把这段接进自动播报：一天响几十次，同事第三天就把板子关了。
synth "warning" "还有工单在处理中，暂时不用你动手。"

# 当天清零 —— 这个项目最有传播力的一刻，值得喜庆一点
synth "celebrate" "今天的工单已经全部清零，干得漂亮！"

echo

# 自检：格式不对的话后面会以"没声音"的形式返工，在这里先拦住
if command -v afinfo >/dev/null 2>&1; then
    all_ok=1
    for f in "$OUT/urgent.wav" "$OUT/warning.wav" "$OUT/celebrate.wav"; do
        info="$(afinfo "$f" 2>/dev/null | grep 'Data format' || true)"
        # data 偏移必须很小：>4096 就会被板子的 4KB 首包卡死（见文件头注释）
        off="$(python3 -c 'import sys;print(open(sys.argv[1],"rb").read().find(b"data"))' "$f")"
        printf '  %-14s data@%-6s %s\n' "$(basename "$f")" "$off" "${info:-取不到格式信息}"
        case "$info" in
            *"16000 Hz"*"Int16"*) ;;
            *) all_ok=0 ;;
        esac
        if [ "$off" -gt 64 ]; then
            echo "  ✗ $(basename "$f") 的 data 块在偏移 ${off}，超过板子首包的 4096 字节，会卡死"
            all_ok=0
        fi
    done

    if [ "$all_ok" = "1" ]; then
        echo
        echo "格式校验通过：16kHz / Int16，正是 T5 要的。"
    else
        echo
        echo "注意：上面有不符 16000 Hz / Int16 的文件，T5 上可能放不出来。"
        exit 1
    fi
fi

echo
echo "下一步：python3 ../bridge/bridge.py --mock --port 8080 然后"
echo "        curl 'http://127.0.0.1:8080/audio/urgent.wav' -o /tmp/a.wav && afplay /tmp/a.wav"
