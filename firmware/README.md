# 最后一步 · 把状态灯烧进板子

这条路**不需要涂鸦云凭据**。板子和你的电脑在同一个局域网里闭环：

```
你的 Mac                                    T5 开发板
┌──────────────────────┐                   ┌──────────────────┐
│ 工单系统（内网）      │                   │                  │
│   ↓ HTTPS            │                   │  屏幕：整屏变色   │
│ bridge.py :8080      │ ←── HTTP 主动来拉 ─│  语音：播报       │
│   /tickets  /audio/* │                   │                  │
└──────────────────────┘                   └──────────────────┘
```

板子每 30 秒来拉一次 `/tickets`，服务端顺便告诉它"这次该播哪段音频"。
**播报说什么由服务端决定** —— 改措辞只改 `status_light.json`，不用重新烧固件。

---

## 一条命令

```bash
cd /Users/fire/WorkBuddy/2026-09-16-10-18-00/t5-status-light/firmware
./install.sh
```

它会依次做六件事，任何一步没过就停下并告诉你卡在哪：

| 步骤 | 做什么 |
|---|---|
| 1 | 探测本机局域网 IP（当前是 `192.168.124.22`） |
| 2 | 生成播报音频 —— 16kHz/16bit/单声道 WAV，**不需要 ffmpeg** |
| 3 | 定位 TuyaOpen 源码 |
| 4 | 从 `your_chat_bot` fork 出 `status_light` 工程，装上固件源文件 |
| 5 | 把探测到的 IP 注入固件 |
| 6 | 编译 → 烧录 |

**先看环境对不对**（不会动任何文件）：

```bash
./install.sh --status       # 装到哪一步了
./install.sh --dry-run      # 打印将要做的事
./install.sh --check-lan    # 板子能不能访问到本机
./install.sh --build-only   # 只编译不烧录
```

---

## 前置条件：TuyaOpen 源码

脚本找不到源码时会告诉你 clone 命令。手动准备：

```bash
git clone --depth 1 https://github.com/tuya/TuyaOpen.git ~/TuyaOpen
cd ~/TuyaOpen && ./tos.py config choice     # 选 T5AI，装工具链
```

源码在别处就显式指定：

```bash
TUYAOPEN_ROOT=/path/to/TuyaOpen ./install.sh
```

---

## 选板子那一步别选错

`tos.py` 会来问你选哪块板。选完 `status_light` 之后，板级配置选：

```
TUYA_T5AI_BOARD_LCD_3.5     3.5 寸 480x320，ILI9488 + GT1151 触摸
```

手上不是这块屏就相应改选，但务必确认它开启了：

```
CONFIG_ENABLE_DISPLAY=y
CONFIG_ENABLE_LIBLVGL=y
```

> **为什么从 `your_chat_bot` fork，而不是更轻的 `duo_eye_mood`**
> 因为它有现成的 `config/TUYA_T5AI_BOARD_LCD_3.5.config`（3.5 寸那块屏），
> `duo_eye_mood` 只有 1.28 寸双圆屏的配置。
> 我们只覆盖入口 `tuya_main.c`，其余代码一行不动。

> **为什么不直接用它的 `src/display2/tuya_lvgl.c`**
> 因为 `display2` 由开关 `CONFIG_ENABLE_CHAT_DISPLAY2` 控制，**默认是 `n`** ——
> 板级配置和 `app_default.config` 都没打开它。直接调 `tuya_lvgl_init()` 会以
> **链接失败**（undefined reference）的形式炸掉，而不是编译报错，很容易查半天。
>
> 所以这里改用 SDK 自带的 `lv_vendor_*`（`src/liblvgl/v9/port/lv_vendor.h`，
> 只要 `ENABLE_LIBLVGL=y` 就有），见 `src/sl_lvgl.c` —— 一共 20 行。
> `duo_eye_mood` 的 `app_ui_eyes.c` 也是这么做的。

---

## 烧完之后立刻验证，别等真实工单

灌假数据，三秒就能看到灯变色、听到播报：

```bash
# 终端 A：起一个假数据源
cd ../bridge && python3 bridge.py --mock --port 8080

# 终端 B：灌状态
curl -X POST 'http://127.0.0.1:8080/sim?p0=2'              # 红闪 + 语音播报
curl -X POST 'http://127.0.0.1:8080/sim?p0=0&p1=1'         # 黄灯，只变色不发声
curl -X POST 'http://127.0.0.1:8080/sim?p0=0&p1=0&p2=0'    # 绿 + 庆祝动画
```

串口日志里搜 `sl:`，看到这两行就是活了：

```
sl: poll thread started, target=http://192.168.124.22:8080/tickets
sl: state 0 -> 2 (p0=3 p1=2 p2=0)
```

确认无误后换回真实数据源：

```bash
cd ../bridge && ./start.sh --lan
```

（`--lan` 就是明确走局域网直连。不带参数也行 —— 没有 `tuya_cloud.json` 时它自动走这条路。）

---

## 板子没反应时，按这个顺序查

**1. 先看串口日志。** 搜 `sl:`。
- 一行都没有 → `app_status_light_init()` 没被调到，入口没装对
- 有 `poll thread started` 但没有 `state 0 -> 1` → 在拉数据但拉不到，看第 2 条

**2. 板子够不着本机。** 这是最常见的一个，三个原因依次排：

- **AP 隔离**：很多公司 Wi-Fi 默认开着"客户端之间互相不可见"。板子和电脑在同一个 SSID 也没用。
  → 换成手机热点试一次，或让 IT 放行。
- **本机防火墙**挡了 8080。→ 系统设置 → 网络 → 防火墙。
- **IP 变了**：换网络后 IP 会变，固件里还是旧的。→ 重跑 `./install.sh` 会自动重新注入。

用 `./install.sh --check-lan` 可以确认前两个。

**3. 屏幕全灰，写 OFFLINE。**
说明 HTTP 请求失败了。分两种：请求根本没通（连不上），或者 bridge 返回了非 200
（`502` = bridge 连不上工单系统）。串口日志里会打印具体是哪一种。

**4. 屏幕正常、但没声音。**
音频格式声明和实际文件不符是这块板上最阴的坑之一 —— 不报错，只表现为卡顿或没声。
固件是**按 URL 后缀自动选解码器**的（`__codec_of_url`），所以只要服务端发的后缀
和真实格式一致就不会踩。用 `afinfo ../bridge/audio/urgent.wav` 自查一下。

**5. P0 一直闪、不恢复。**
检查 `pkill` 掉的是不是 mock 服务 —— 有人忘了关 mock，板子在读假数据。

---

## 改了什么

相对 TuyaOpen 原版 `your_chat_bot/src/tuya_main.c`，入口只动了一处：

```c
board_register_hardware();     // 板级外设：LCD 面板、触摸、音频 codec
sl_lvgl_init();                // LVGL 起来并拉起刷屏线程（不用 display2 的 tuya_lvgl_init）
app_status_light_init();       // 我们的：建界面 + 起轮询线程
```

原版这里是 `app_chat_bot_init()`，会把整套 AI 对话拉起来。状态灯不需要它。

`tuya_iot_start()` 仍然保留 —— Wi-Fi 链路由 `netmgr` 负责，跳不过去。
但 **MQTT 连没连上不影响状态灯**：数据走的是局域网 HTTP，不看云。

## 固件源文件

| 文件 | 职责 |
|---|---|
| `firmware/tuya_main.c` | 应用入口，覆盖原版 |
| `../src/sl_lvgl.c` | 起 LVGL + 跨线程显示锁（20 行，包 SDK 的 `lv_vendor_*`） |
| `../src/app_status_light.c` | 轮询 + 状态机 + 决定播什么 |
| `../src/ui_status.c` | 只负责把状态画到屏幕上（LVGL） |

四个 `.c` 都经过 `clang -fsyntax-only -Wall -Wextra` 校验：对着 TuyaOpen master 的**真头文件**
（`tal_thread.h` / `tal_sw_timer.h` / `tal_mutex.h` / `http_client_interface.h` /
`ai_audio_player.h` / `lv_vendor.h`）编译，零错误零警告。

`app_status_light.c` 里有四处最容易写错的，文件头注释记了：线程创建是 6 个参数、
`http_method` 是枚举不是字符串、HTTP 响应缓冲区由 SDK 内部申请、状态名以 `bridge.py` 的
`urgent/warning/idle/offline` 为准。

---

## 播报（语音）行为

- **触发**：只要 `p0`（待处理）从无到有、或从少到多，就播报一次。
  之前只在"状态第一次跳到 urgent"时播，导致"已经红着、又来新工单"不响 —— 已修。
- **播放路径**：板子自己用 HTTP 把 `urgent.wav` 拉下来，走 `ai_audio_play_data`
  （start + feed + EOF）这条被验证过的路，不再依赖 `ai_audio_play_tts_url` 的
  playlist 自动播放（那条在 T5 上实测"拉到了但不出声、还反复重拉"）。
- **格式**：`bridge/audio/urgent.wav`、`celebrate.wav` 是 16kHz/16bit/单声道 **PCM WAV**，
  干净无额外 chunk；板子音频解码器默认编译进 MP3+WAV，**支持**。
- **还不出声？** 那就是板子硬件输出（喇叭/编解码器走线）或音量的问题，不是固件逻辑。
  自检：重启板子时留意有没有"配网提示音"（网络事件也会响），有则说明喇叭是好的、
  问题在文件解码；没有说明硬件没出声通道。串口日志看 `tos.py monitor`，关键字 `sl: audio`。

### 想立刻听一声（不等特殊工单）
停掉真实服务、跑 mock 模式，灌一个 P0：
```bash
# 终端 A：停掉现有 bridge，起 mock
cd /Users/fire/WorkBuddy/2026-09-16-10-18-00/t5-status-light/bridge
python3 bridge.py --mock --port 8080 &
# 终端 B：把 p0 顶到 3（板子看到 p0 增加 -> 闪红 + 播报）
curl -X POST 'http://127.0.0.1:8080/sim?p0=3'
# 听完后切回真实模式：
#   kill %1 ；cd firmware && ./start.sh --lan
```

---

## 屏幕页面（可切换）

板子没接输入设备驱动（无触摸/按键 indev），页面用**定时器自动轮播**，每 6 秒切一次：

| 页面 | 内容 |
|---|---|
| 第 1 页（首页） | 大数字 = `p0` 待处理；底部页码 `[Pg 1/2]` |
| 第 2 页 | 大数字 = `p1 + p2`（处理中 + 待客户补充合计）；副标题拆开 `P1 x  P2 y`；页码 `[Pg 2/2]` |

- 红灯闪烁只看 `p0`，和页面无关，任意页都闪。
- 想改轮播周期：固件 `app_status_light.c` 里 `SL_PAGE_CYCLE_MS`（默认 6000ms）。
- 中文标签（待处理/处理中）没上屏是因为 LVGL 默认字体不含中文字形；现在用英文
  `P0 / P1+P2` 表达。要显示中文需另生成子集字体（见 `ui_status.c` 头注释）。

