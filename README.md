# T5 团队状态灯

把一块 T5AI-Board 变成办公室里的工单状态灯：有工单在等你回话就整屏闪红 + 语音播报，
手头清完就放庆祝动画。

> **先读哪份？**
> - **只想尽快把板子跑起来** → 读 [`使用说明.md`](使用说明.md)。**一页纸，三步**：装板子 → 用假数据看效果 → 接真数据。给别人用就把这份和整个目录一起发过去。
> - **只想快速了解这是什么、能怎么用** → 读 [`内部分享-团队状态灯.md`](内部分享-团队状态灯.md)。四节：主要功能 / **怎么跑起来（三步，含卡住排查表）** / 未来怎么快速换到别的业务（几句话讲完）/ 实现过程。**这份自带动手步骤，是要拿去分享的那一份。**
> - **固件已经烧好了，现在要接你自己的工单系统** → 读 [`接工单系统-操作步骤.md`](接工单系统-操作步骤.md)。**这份已经接上真实系统并跑通了**（`cs.tuya-inc.com` 技术工单队列），接口、字段、枚举值全在里面，你只需要跑两条命令。
> - **想给别人用、或者一块板子要跑不同业务** → 读 [`部署与功能切换指南.md`](部署与功能切换指南.md)。一张表说清哪些切换不用重烧、哪些要，以及"给别人用"要准备什么。
> - 打算用 **Tuya Cobuilder** 平台生成固件 → 读 [`COBUILDER-操作手册.md`](COBUILDER-操作手册.md)，半天能跑通，固件那段活整体外包给平台。
> - 打算**手写固件**、或者想搞清底层每一处实现 → 继续读本文，约一天。
>
> 三条路线的**数据通道部分完全一样**（都是工单系统 → `bridge.py`），区别只在固件是生成还是手写。Cobuilder 路线的额外成本是云应用配置，省下的是固件工程 + LVGL + 音频播放器这几块。本文第六节之前的内容对三条路线都适用。

本文是完整的手写实施指南，配套代码都在本目录：

```
t5-status-light/
├── README.md                      ← 你正在看的这份（手写路线）
├── 使用说明.md                     ← ★ 一页纸三步上手（拿到代码先看这份）
├── 内部分享-团队状态灯.md           ← ★ 内部分享用：功能 / 怎么跑起来 / 换业务 / 实现
├── 部署与功能切换指南.md            ← 给别人用 / 一板多业务：切换成本总表
├── COBUILDER-操作手册.md           ← Cobuilder 平台路线
├── 接工单系统-操作步骤.md           ← ★ 接你自己的工单系统（真实系统上验证过）
├── dist/                          ← 编译好的固件（地址写死 192.168.124.22，换网络要重编）
├── src/
│   ├── app_status_light.h/.c      ← 轮询 + 状态机 + 语音触发（核心逻辑）
│   └── ui_status.h/.c             ← 屏幕渲染（LVGL）
├── bridge/
│   ├── start.sh                   ← 一条命令跑通本地侧（--lan 局域网 / --cloud 云）
│   ├── service.sh                 ← ★ 把 bridge 装成常驻服务（launchd：开机自启、被杀自拉起）
│   ├── cookie_sync.py             ← ★ 从 Chrome 自动同步登录态（s-sid 8 小时就轮换，手工粘撑不住）
│   ├── set_cookie.sh              ← 手工粘 Cookie 的兜底入口（自动那条走不通时用）
│   ├── sources.py                 ← 工单数据源适配层（HTTP/文件/SQL/命令/涂鸦CS + 规则引擎）
│   ├── bridge.py                  ← 数据中转服务（已接真实系统跑通）
│   ├── dp_push.py                 ← 把状态推成涂鸦云 DP（签名已用官方向量验证）
│   ├── probe.py                   ← 从浏览器 cURL 自动生成配置（给完全未知的接口用）
│   ├── selftest_source.py         ← 不碰云，验证接入链路（40 项断言）
│   ├── selftest_rules.py          ← 不碰云，验证规则引擎 + 涂鸦CS 数据源（35 项断言）
│   ├── selftest_watch.py          ← 不碰云，验证状态决策逻辑（7 项断言）
│   ├── selftest_cookie.py         ← 离线验证 cookie 同步的纯逻辑（32 项断言）
│   ├── selftest_shell.py          ← 静态检查所有 .sh（中文标点别把变量名吃掉）
│   ├── status_light.example.json  ← 配置模板（含 Jira / SQLite / 命令 / 涂鸦CS 等 6 种接法）
│   ├── status_light.json          ← ★ 你的实盘配置（已经配好涂鸦 CS 工单系统）
│   ├── tuya_cloud.example.json    ← 云应用凭据模板
│   └── audio/                     ← 播报音频放这里
├── tools/                         ← 板端复验脚本（都用 /Users/fire/TuyaOpen/.venv/bin/python 跑）
│   ├── check_boot.py              ← 冷启动复验：复位板子 + 抓 70s，自动判定「新固件在跑 / 开机不出声」
│   ├── verify_auto_speak.py       ← 验「工单来了就自动播报」：--watch 旁观 / --drive 驱动 mock
│   ├── verify_say.py              ← 验手动播报（串口 say，等同点屏幕），自动判定通过/失败
│   └── watch_tap.py               ← 验**手指点屏幕**（唯一需要人参与的一步）
└── assets/
    └── make_audio.sh              ← 用 say 生成播报音频（16kHz WAV + 洗头 + 自检，不需要 ffmpeg）
```

**本地侧两条路，先选一条**（都在 `cd t5-status-light/bridge` 之后跑）：

**路线 A —— 局域网直连（推荐，零云凭据）**

板子主动来拉数据，全在同一网段内闭环，不需要任何涂鸦云凭据。

```bash
./start.sh --lan       # 起 bridge，并打印板子该访问的地址
```

**登录态会自动跟上，你什么都不用管。** 工单系统的 `s-sid` 大约 **8 小时**就轮换一次
（这就是"手工从 F12 复制的那份隔天必失效"的原因，不是配错了）。`start.sh` 启动时会
先从 Chrome 要一份最新的，再在后台常驻一个同步进程持续跟进。

想单独看看登录态还剩多久、和浏览器里是否一致：

```bash
python3 cookie_sync.py --check
```

**路线 B —— 涂鸦云 DP 下发（可选，需要云凭据）**

板子不必和本机同网段，但要去涂鸦开发者平台取三个凭据。

```bash
python3 dp_push.py --init    # ① 交互式填云应用凭据
./start.sh --cloud           # ② 四道关依次过，然后常驻同步（登录态同样自动同步）
```

`./start.sh` 不带参数会自动判断：有 `tuya_cloud.json` 走 B，没有走 A。
失败会立刻停下并告诉你卡在哪一步。改动之后快速回归：

```bash
python3 selftest_source.py   # 接入侧 40 项
python3 selftest_rules.py    # 规则引擎 35 项
python3 selftest_watch.py    # 推送侧 7 项
python3 selftest_cookie.py   # 登录态同步的纯逻辑 32 项（离线，不碰 Chrome）
python3 selftest_shell.py    # 脚本静态检查：中文标点有没有吃掉变量名
```

**两种数据进板子的方式，二选一：**

| 方式 | 固件要求 | 适合 |
|---|---|---|
| 局域网直连 `GET /tickets` | 固件里要写 HTTP 轮询（即 `src/app_status_light.c`） | 手写固件、板子和你的电脑同网段 |
| 云端 DP 下发（`dp_push.py`） | 固件只认 DP，不用碰网络代码 | **Cobuilder 生成的固件**，或希望后续改措辞不重烧固件 |

> 走云端 DP 时，`bridge.py` 里的音频文件（`assets/`）用不上——播报内容由 `announce_text` 这个 DP 推给设备，由固件的 TTS 读出来。

---

## 一、整体链路

```
   内部工单系统  (Jira / 禅道 / 自研 / 企业微信)
          │   认证只在这一层做，固件里不碰 Token
          ▼
   数据中转服务 bridge.py          ← 跑在你工位电脑/一台小服务器上
          │   GET /tickets  →  {p0, p1, p2, audio_url}
          ▼
   ┌──────────────────────────────────────┐
   │  T5AI-Board · TuyaOpen 固件           │
   │                                      │
   │  app_status_light.c  拿数据、算状态    │
   │         ├─→ ui_status.c  改屏幕颜色/数字│
   │         └─→ ai_audio_*   播报语音      │
   └──────────────────────────────────────┘
```

**关键设计：状态判断在设备端，措辞在中转服务端。**

这样你想把播报从"有新的 P0 工单"改成"有 3 个 P0 等着你"，只需要改 `bridge.py` 里返回的 `audio_url`，不用重新烧固件。反过来，如果中转服务挂了，设备侧的状态机仍然能正常工作（只是不发声）。

**状态 → 表现 的映射：**

| 工单情况 | 屏幕 | 语音 |
|---|---|---|
| `p0 > 0` | 红底，整屏 500ms 明暗闪烁，中央显示 P0 数量 | 播 `urgent.wav` |
| 只有 `p1 > 0` | 黄底常亮 | 播 `warning.wav` |
| 从非零转成全 0 | 紫底 + "ALL CLEAR!" | 播 `celebrate.wav`，15 秒后回绿底 |
| 一直全为 0 | 绿底常亮 | 不发声 |
| 拉不到数据 | 灰底 "OFFLINE" | 不发声 |

**发声规则：状态真的跳变了才播，状态没变不播。** 轮询是 30 秒一次，但同状态
的重复快照不会出声 —— 否则就变成"每 30 秒念一遍"，那是这块板子最容易被拔电源的用法。
另外**开机不算跳变**：上电后第一份快照只更新屏幕，不播报（半夜断电重启不会突然说话）。

三个开关都在 `src/app_status_light.c` 顶部的配置区，改完重新烧录：

| 宏 | 默认 | 作用 |
|---|---|---|
| `SL_AUTO_SPEAK` | `1` | 置 `0` 关掉自动播报，只保留点屏幕 / 串口 `say` |
| `SL_SPEAK_ON_BOOT` | `0` | 置 `1` 让开机第一份快照也念一遍当前状态 |
| `SL_SPEAK_ON_CLEAR` | `1` | 置 `0` 则工单清零时不播"已清空" |
| `SL_SPEAK_MIN_GAP_MS` | `5000` | 两次**非升级**自动播报的最小间隔，防数据抖动刷屏 |

> 升级不受最小间隔限制：黄灯刚响过、随即变红，红灯照样立刻播 —— 宁可连播两句，
> 也不能把"变红了"吞掉。

手动那一路（点屏幕 / 串口 `say`）**不受任何间隔限制**，随时按当前状态重播一遍。
跑 `tools/verify_say.py` 验手动、`tools/verify_auto_speak.py --drive` 验自动。

---

## 二、动手前的检查清单

| 项 | 说明 |
|---|---|
| 硬件 | T5AI-Board + 3.5 寸 LCD 子板（320×480，ILI9488 + GT1151QM 触摸）。只有 Core 板没有原生显示支持，做不了。 |
| USB 线 | 要能传数据的，不是纯充电线 |
| TuyaOpen 授权码 | UUID + AuthKey，`tyutool_gui` 写入，没这个连不上涂鸦云 |
| 环境 | 已经跑通过一次 `tos.py build` + `tos.py flash` |
| 同一局域网 | 板子和中转服务必须互通，注意路由器别开 AP 隔离 |
| Bridge 机器 | 一台常年开着的电脑，用来跑 `bridge.py` |

> ⚠️ 如果这是你第一次碰 TuyaOpen，**先把环境搭建走完再说**。首次装工具链 + 下载 SDK + 第一次编译，通常要 2~3 小时，这部分没有捷径，但不属于这个项目本身的成本。

---

## 三、第一个决定：数据怎么进板子

三个选项，按省事程度排序：

**A. 板子直连工单系统 API** — 不推荐
固件里要处理 Token / 签名 / HTTPS 证书，而且内网系统基本不会给一个局域网设备开白名单。唯一适合的场景是你们工单系统本身就在同一网段且不需要认证。

**B. 加一层中转服务** — ✅ 推荐，本方案默认走这条
认证、字段映射、状态跳变判断、措辞渲染全在服务端，固件只留一个 `GET /tickets`。换工单系统只改 `status_light.json` 一个配置文件，代码不用动。

**C. 走涂鸦云 + MCP** — 想玩进阶再看
在智能体平台配 Agent，用 MCP 工具主动触发设备。最优雅，但要先把 A 或 B 跑通，别一上来就上这个。

---

## 四、先让中转服务跑起来（约 30 分钟）

### 4.1 生成播报音频

```bash
cd assets
./make_audio.sh                 # 零依赖：用 macOS 自带的 say，不需要 ffmpeg
```

脚本自己输出到 `../bridge/audio/`（`urgent.wav` / `warning.wav` / `celebrate.wav`），
跑完还会用 `afinfo` 自查一遍格式，不用手工搬。

**格式必须是 16 bit / 16 kHz / 单声道 WAV**——`make_audio.sh` 里已经写死了这三个参数，别改。
格式声明和实际文件不一致是这块板上最阴的坑：**不报错**，只表现为卡顿、变调或干脆没声。

### 4.2 启动服务并验证

```bash
cd bridge
python3 bridge.py --mock --port 8080     # mock：用内置假数据，先验链路
```

它会打印出板子该访问的地址，然后另开一个终端模拟几次状态跳变：

```bash
curl -X POST 'http://127.0.0.1:8080/sim?p0=3&p1=2'   # 进 P0，应返回 urgent.wav
curl        'http://127.0.0.1:8080/tickets'          # 再拉一次，应无 audio_url
curl -X POST 'http://127.0.0.1:8080/sim?p0=0&p1=0&p2=0'
curl        'http://127.0.0.1:8080/tickets'          # 清零，应返回 celebrate.wav
```

确认三件事：状态变化时才有 `audio_url`、重复拉取不重复播报、清零能触发庆祝。这三条都对了，服务端就算完工。

> `/sim` 只在 mock 模式下有效，接真实数据源后它会返回 409 并说明原因 —— 这是故意的，
> 免得你以为灌进去的假数据是真的。

### 4.3 接真实工单系统

**不用改代码，改配置。** 完整流程见 [`接工单系统-操作步骤.md`](接工单系统-操作步骤.md)，核心就两条命令：

```bash
# 从浏览器 F12 里把接口请求 Copy as cURL 存成 curl.txt，然后：
python3 probe.py --curl curl.txt    # 自动认字段、自动生成 status_light.json
python3 bridge.py --check           # 验数字和工单页面上的一致
```

没现成 HTTP 接口也能接——`sources.py` 还支持读本地 JSON/CSV 文件、查 SQLite、
以及跑任意命令读它的 stdout。配置写法见 `status_light.example.json` 里的五个示例。

> **改完配置和措辞，跑一次 `python3 selftest_source.py`**（40 项断言，不碰云不碰设备），
> 比对着真板子猜快得多。

---

## 五、固件改动 —— 一条命令

固件这块不用手搓了。`firmware/install.sh` 一次做完：探测本机 IP → 生成播报音频 →
fork 工程 → 装源文件 → 注入 IP → 编译 → 烧录。

```bash
cd firmware
./install.sh
```

**这条路不需要涂鸦云凭据。** 板子主动来拉本机的 `bridge.py`，
Wi-Fi 之内就闭环了。详细的步骤、选板子该选哪项、以及
「板子没反应按什么顺序查」都写在 [`firmware/README.md`](firmware/README.md)。

先看环境对不对（不动任何文件）：

```bash
./install.sh --status       # 装到哪一步了
./install.sh --dry-run      # 打印将要做的事
./install.sh --check-lan    # 板子能不能访问到本机
./install.sh --build-only   # 只编译不烧录
```

### 5.1 为什么 fork `your_chat_bot`，不是更轻的 `duo_eye_mood`

就一个原因：它有现成的 `config/TUYA_T5AI_BOARD_LCD_3.5.config`（3.5 寸那块屏）。
`duo_eye_mood` 只有 1.28 寸双圆屏的配置。音频两者都有 ——
`ENABLE_COMP_AI_AUDIO` 在 Kconfig 里是 `default y`。

**别用它的 `src/display2/tuya_lvgl.c`。** 那个模块由 `CONFIG_ENABLE_CHAT_DISPLAY2`
控制，而它**默认是 `n`**，板级配置和 `app_default.config` 都没打开。
直接调 `tuya_lvgl_init()` 会以**链接失败**（undefined reference）的形式炸掉 ——
不是编译报错，而且那个开关的名字跟"显示"看起来没关系，很容易查半天。

改用 SDK 自带的 `lv_vendor_*`（`src/liblvgl/v9/port/lv_vendor.h`，
只要 `ENABLE_LIBLVGL=y` 就有）。我们自己写了 `src/sl_lvgl.c`，20 行，
`duo_eye_mood` 的 `app_ui_eyes.c` 就是这么做的。

### 5.2 三个必须记住的接口细节

四个 `.c` 文件都过了 `clang -fsyntax-only -Wall -Wextra`，用的是 TuyaOpen master 的
**真实头文件**。下面三处是最容易写错、且**错了不一定编译报错**的：

**① 线程创建是 6 个参数，不是 8 个**

```c
/* 错：把名字、栈大小、优先级当参数平铺传 */
tal_thread_create_and_start(&h, "sl_poll", NULL, NULL, 4096, PRIO, fn, NULL);

/* 对：打包进 THREAD_CFG_T */
THREAD_CFG_T cfg = {0};
cfg.thrdname   = "sl_poll";
cfg.stackDepth = 1024 * 4;
cfg.priority   = THREAD_PRIO_1;        /* 不是 TRD_PRIO_4 */
tal_thread_create_and_start(&h, NULL, NULL, fn, NULL, &cfg);
```

**② `http_method` 是枚举，不是字符串**

```c
play.tts.http_method = AI_HTTP_METHOD_GET;   /* 不是 "GET" */
```

写成字符串在部分编译器上只是个 warning，但运行时行为未定义 ——
表现是"请求看着发出去了但没声音"，极难定位。

**③ HTTP 响应缓冲区由 SDK 内部申请**

```c
http_client_response_t resp = {0};   /* memset 成 0 就够了 */
/* 不要自己 resp.buffer = malloc(...) —— SDK 在 HTTPClient_Request 里
   会 memset 整个结构体再自己 HTTP_MALLOC，你给的那块会被丢掉。 */
```

### 5.3 状态名以 bridge.py 为准

```
urgent / warning / idle / offline
```

`dp_push.py` 里那套 `ok/warn/urgent/offline` 是走涂鸦云 DP 时才用的，
两条路的命名不一样，`dp_push.py` 内部有个 `BRIDGE_STATE_MAP` 在做翻译。
固件现在直接读服务端给的 `state` 字段（服务端才是权威 ——
它读的 `alert_buckets` 配置固件看不到），只在服务端没给时才本地兜底算。

**两边各算一套判据迟早会不一致**，这是原方案第七节里列的第 5 个坑的根因。

### 5.4 播报音频用 WAV，不用 MP3

`assets/make_audio.sh` 已经改成零依赖了 —— macOS 自带的 `say` 能直接输出
16kHz/16bit/单声道 WAV，不需要 `ffmpeg`：

```bash
say --file-format=WAVE --data-format=LEI16@16000 -o urgent.wav "文本"
```

固件侧**写死了 WAV**（`app_status_light_speak()` 里 `tts.tts.format = AI_AUDIO_CODEC_WAV`），
所以服务端必须给 `.wav`，换 MP3 要改固件重烧。之所以不按 URL 后缀自动选：
解码器列表是编译期定死的，按后缀猜只会把"格式不符"这类静默失败拖到运行期。

> 格式声明和实际文件不一致是这块板上最阴的坑之一：**不报错**，
> 只表现为卡顿、变调或干脆没声。`make_audio.sh` 跑完会用 `afinfo` 自查一遍。

## 六、时间预算：一天能到哪，两天能到哪

假设环境已经搭好、板子烧过一次：

| 时段 | 内容 |
|---|---|
| 0.5h | 生成音频、中转服务跑起来、curl 验证三种跳变 |
| 1.0h | fork `duo_eye_mood`、改配置、编译烧录一次成功 |
| 2.0h | 加 HTTP 轮询，串口日志里看到 JSON 正确解析 |
| 2.0h | 替换 UI 成状态页（颜色 + 大数字） |
| 1.0h | 接上音频播报 |
| 1.0h | 调颜色阈值、闪烁节奏、播报措辞 |
| 0.5h | 配网、上桌、拍照发群 |

**一天结束时你有的**：一块能真实反映工单状态、会闪会说话的板子。

**再加一天能加的**：
- 加个按键短按手动刷新（`app_status_light_refresh_now()` 已经留好接口）
- 接更多数据源——今天线上告警数、CI 失败次数、值班排班
- 把"清零庆祝"升级成真正的动画（GIF 素材塞进 `assets/`）
- 接 MCP，让你可以直接对板子说"今天还有几个 P0"，它念给你听

---

## 七、八个会卡住你的坑

**1. 中文显示成方块**
LVGL 默认字体（montserrat）不含中文字形。两个解法：用官方的 [LVGL 字体自定义教程](https://www.tuyaopen.ai/zh/learn/lvgl-font-change-tutorial) 生成只含所需字符的子集字体；或者像 `ui_status.c` 默认那样，状态全靠颜色 + 数字 + 英文表达。**建议先走第二条把链路跑通，再回头加字体。**

**2. 音频格式声明与实际不符**
声明成 `AI_AUDIO_CODEC_MP3` 就必须配真的 MP3 文件。**这个错不会报错**，只表现为卡顿、变调或干脆没声。
固件写死 `AI_AUDIO_CODEC_WAV`，`assets/make_audio.sh` 跑完会用 `afinfo` 自查格式。换格式时两边一起换。

**3. 在轮询线程里直接调 LVGL**
LVGL 不是线程安全的，从轮询线程直接改界面会随机崩溃——而且崩得毫无规律，最难查。`ui_status.c` 里的 `__lvgl_lock/__lvgl_unlock` 必须套上，它们转发到 `src/sl_lvgl.c` 的 `sl_lvgl_lock/unlock`（内部就是 SDK 的 `lv_vendor_disp_lock/unlock`）。

**4. 板子访问不到中转服务**
三件事依次排查：填的是不是局域网 IP、运行 `bridge.py` 的机器防火墙有没有放行 8080、路由器有没有开 AP 隔离（很多公司 Wi-Fi 默认开）。

**5. 每 30 秒播报一次**

两个完全不同的原因，按顺序查：

**(a) 两边各算了一套状态判据。** 这是老毛病：固件曾自己算 urgent/warning，
和服务端的 `alert_buckets` 不一致，于是一个判"变了"、一个判"没变"，来回刷。
现在固件直接读服务端给的 `state` 字段，只在服务端没给时才本地兜底（`__calc_state`），
不再各算各的。改过 `alert_buckets` 不用动固件 —— 服务端改了就算数。

**(b) 数据源自己在抖。** 加自动播报（`SL_AUTO_SPEAK`）之后，"每 30 秒响一次"
也可能是因为服务端返回的 `state` 每次都在变。判据很简单：看板子日志有没有连续的
`sl: state A -> B` / `sl: state B -> A` 来回跳。

```bash
# 连续抓 3 个轮询周期，看 state 是不是在来回翻
/Users/fire/TuyaOpen/.venv/bin/python tools/verify_auto_speak.py --watch 100
```

如果确实在翻，那是上游数据的问题，不是固件 —— 固件这层有 `SL_SPEAK_MIN_GAP_MS`
兜底（默认 5 秒），但治标不治本。查 `bridge.py` 那边的 `alert_buckets` /
工单系统的分页或缓存。

> 注意状态名两套体系：局域网这条路用 `bridge.py` 的 `urgent/warning/idle/offline`；
> 走涂鸦云 DP 那条路用 `dp_push.py` 的 `ok/warn/urgent/offline`。别混。

**6. T5AI-Board 的串口被 TF 卡占用**
这块板的 TF 卡 `DATA3`/`DATA2` 引脚和烧录用的 UART 是复用关系。如果你插了 TF 卡又烧不进固件，先把卡拔了。

**7. 改脚本时，中文标点会把变量名吃掉**
脚本里的提示语全是中文，很容易顺手写出 `echo "端口 $PORT，日志见下"`。
bash 会把 `PORT，` 整个当成变量名，在 `set -u` 下报 `PORT?: unbound variable` 然后**当场退出** ——
而报错行指向的是那行 `echo`，看着人畜无害，真正的原因要盯一会儿才反应过来。
本项目已经踩过**四次**（`$LAN_IP（`、`$PORT，`、`$COOKIE_FILE（`、`$COOKIE_LOG）`）。
**规则**：变量后面只要紧跟中文标点或中文文字，一律写 `${PORT}` 而不是 `$PORT`。
改完脚本跑一次 `python3 selftest_shell.py` 扫一遍，它专门抓这个。

**8. 登录态隔天就失效 —— 它本来就只有 8 小时**
工单系统的登录态不是一个 cookie，是好几个，有效期差得很远：

| cookie | 有效期 | 说明 |
|---|---|---|
| `SSO_USER_TOKEN` | 约 30 天 | 单点登录主凭据 |
| `fast-sid` | 约 3 天 | 快速校验 |
| **`s-sid`** | **约 8 小时** | 真正的会话票，**就是它让你觉得"系统在随机踢我"** |

浏览器每访问一次，服务端就把 `s-sid` 滚动续期一次。所以你从 F12 手工复制的那一份，
**最多撑 8 小时** —— 第二天必然失效，看起来毫无规律。这不是配置问题，是它的设计。

现在由 `cookie_sync.py` 自动跟进：直接从 Chrome 的 SQLite 里把 cookie 解密读出来
（用系统自带的 `security` + `openssl`，零依赖），`start.sh` 启动时同步一次，
之后后台常驻一个进程持续跟进；`bridge.py` 每次取数会重读凭据文件，所以不用重启。

出问题先跑这一条，它会告诉你 `s-sid` 还剩多久、本地文件和浏览器差在哪个字段：

```bash
python3 cookie_sync.py --check
```

---

## 八、为什么这个项目值得做

技术上它不难——真正花时间的是调颜色和措辞。但它的性价比在于：

- **每天被全办公室看见**。这是所有内部工具里曝光率最高的形态。
- **它自己会讲传播故事**。清零那一刻的庆祝动画，随手一拍就是能发群和发社区的素材。
- **它是后续大项目的验证台**。`app_status_light.c` 里的"设备侧主动拉数据 + 状态机 + 语音反馈"这套结构，和后面做 AI 诊断台、配网可观测要用的东西是同一个骨架。先用一天把它跑通，后面就是在这上面加东西。
