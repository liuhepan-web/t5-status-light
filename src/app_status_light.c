/**
 * @file app_status_light.c
 * @brief T5 团队状态灯 —— 核心逻辑层（轮询 + 状态机）
 *
 * 数据来源：跑着 bridge.py 的那台机器（通常就是你的 Mac）
 *   GET http://<bridge_host>:8080/tickets
 *   -> {"p0":0,"p1":1,"resolved_today":0,"state":"warning",
 *       "updated":"17:45:03"}
 *
 * 这条路径**不需要任何涂鸦云凭据**：板子和这台机器在同一个局域网就行。
 *
 * 职责边界：
 *   app_status_light.c  —— 拿数据、算状态
 *   ui_status.c         —— 只负责把状态画到屏幕上
 *
 * 本版为"纯净状态灯"：只展示 待处理(P0) / 处理中(P1) 两页，P0 红灯闪烁；
 * 无摄像头识人、无差评爆闪。
 *
 * 语音播报有两个入口，都走同一条链路（信号量 -> sl_speak 线程 -> 音频 URL 边下边播）：
 *   1. 自动：工单状态发生跳变时自己出声，不需要任何人操作（见 __auto_speak）；
 *   2. 手动：点屏幕 / 串口敲 `say`，随时按当前状态重播一遍。
 * 两者互不干扰 —— 手动点永远立刻生效，不受自动播报的最小间隔限制。
 */

#include <stdio.h>       /* snprintf —— 拼音频 URL */
#include <string.h>
#include <stdlib.h>      /* atoi —— 解析音频 URL 里的端口号 */
#include "app_status_light.h"
#include "ui_status.h"

#include "tal_log.h"
#include "tal_thread.h"
#include "tal_mutex.h"
#include "tal_semaphore.h"
#include "tal_system.h"
#include "tal_sw_timer.h"
#include "http_client_interface.h"
#include "cJSON.h"

#include "ai_audio_player.h"
#include "tdl_audio_manage.h"   /* tdl_audio_find / tdl_audio_open：打开喇叭设备 */
#include "tal_cli.h"            /* 串口 say 命令：不开屏幕也能触发播报 */

/* ------------------------------------------------------------------ */
/* 配置区：改这里就够了                                                */
/* ------------------------------------------------------------------ */
/* 用 #ifndef 包住，这样安装脚本既能改文件、也能用 -DSL_BRIDGE_HOST=... 覆盖 */

#ifndef SL_BRIDGE_HOST
#define SL_BRIDGE_HOST      "192.168.124.22"  /* bridge.py 所在机器的局域网 IP */
#endif

#ifndef SL_BRIDGE_PORT
#define SL_BRIDGE_PORT      8080
#endif

#ifndef SL_POLL_INTERVAL_MS
#define SL_POLL_INTERVAL_MS (30 * 1000)       /* 轮询周期 30s */
#endif

#define SL_BRIDGE_PATH      "/tickets"
#define SL_HTTP_TIMEOUT_MS  (8 * 1000)

#define SL_CELEBRATE_SEC    15                /* 庆祝动画持续时长，要 >= 固件动画时长 */
#define SL_BLINK_PERIOD_MS  500               /* P0 红灯闪烁周期 */
#define SL_PAGE_CYCLE_MS    (6 * 1000)        /* 页面自动轮播周期 */

/* ---- 自动播报（工单来了就出声，不用手动点）------------------------ */

#ifndef SL_AUTO_SPEAK
#define SL_AUTO_SPEAK       1                 /* 0 = 关掉自动播报，只保留点屏幕/`say` */
#endif

#ifndef SL_SPEAK_ON_BOOT
#define SL_SPEAK_ON_BOOT    0                 /* 1 = 开机第一份快照也念一遍当前状态 */
#endif

#ifndef SL_SPEAK_ON_CLEAR
#define SL_SPEAK_ON_CLEAR   1                 /* 1 = 工单全清时也播一句"已清空" */
#endif

#ifndef SL_SPEAK_MIN_GAP_MS
/* 两次"非升级"自动播报之间的最小间隔，防抖动刷屏。
 * 轮询是 30s 一次、且只在状态真跳变时才播，所以这个值平时根本碰不到；
 * 它的作用是兜住"数据源抽风、状态来回横跳"这种异常。
 * 注意：升级（黄->红）不受这个间隔限制，永远立刻播。 */
#define SL_SPEAK_MIN_GAP_MS (5 * 1000)
#endif

/* ------------------------------------------------------------------ */
/* 内部状态                                                            */
/* ------------------------------------------------------------------ */

static SL_TICKET_T      sg_ticket     = {0};
static SL_STATE_E       sg_state      = SL_STATE_IDLE;
static SL_STATE_E       sg_prev_state = SL_STATE_IDLE;

static THREAD_HANDLE    sg_poll_thrd  = NULL;
static TIMER_ID         sg_celebrate_tm = NULL;
static TIMER_ID         sg_blink_tm     = NULL;
static TIMER_ID         sg_page_tm      = NULL;   /* 页面轮播 */
static BOOL_T           sg_blink_on     = TRUE;
static int              sg_page         = SL_PAGE_P0;

static MUTEX_HANDLE     sg_lock = NULL;

/* 点击/自动播报：LVGL 的回调或状态机只 post 这个信号量，真正的下载+播放
 * 交给独立线程。信号量初值 0、上限 1 —— 连点多次会合并成一次，
 * 不会排队播好几遍（后一条把前一条顶掉，用户听到的永远是最新那条）。 */
static SEM_HANDLE       sg_speak_sem  = NULL;
static THREAD_HANDLE    sg_speak_thrd = NULL;

/* 自动播报用：第一份快照不算"工单来了"（那只是板子刚启动、状态从初始 IDLE
 * 跳到真实值），否则每次上电/重启都会突然念一句。处理完第一份就置 FALSE。 */
static BOOL_T           sg_first_snapshot = TRUE;

/* 这两个只在自动播报里用；SL_AUTO_SPEAK=0 时若还声明着，
 * 就会变成"定义了但没用到"的静态变量，在 -Werror 下直接编不过。
 * 所以跟着开关一起编进去。 */
#if SL_AUTO_SPEAK
static uint32_t         sg_last_speak_ms  = 0;   /* 上次自动播报的时刻 */
static int              sg_last_speak_sev = 0;   /* 上次自动播报的严重度，见 __sev_of */
#endif

/* 定义在文件末尾（那里才有 app_status_light_speak 可用），init 里要用，
 * 所以先声明一下，免得 C99 判成隐式声明。 */
static void __speak_thread(void *arg);

/* UI 层的点击回调签名是 void(*)(void)，而我们这边返回 OPERATE_RET，
 * 类型不匹配（-Werror 直接拦）。垫一层薄的适配即可。 */
static void __on_ui_tap(void)
{
    (void)app_status_light_tap();
}

/* ------------------------------------------------------------------ */
/* 串口调试命令                                                        */
/* ------------------------------------------------------------------ */

/* `say` —— 等同于点一下屏幕：按当前状态播报。
 *
 * 为什么值得留一条：这块板子的播报只有"点屏幕"一个入口，测试和排障时
 * 总得伸手指。有了它，串口敲一行就能复现，也能被脚本/MCP 调用。
 * 命令本身只投递信号量，跟点击走的是同一条路。 */
static void __cli_say(int argc, char *argv[])
{
    (void)argc;
    (void)argv;
    tal_cli_echo("sl: say -> triggering announcement\n");
    (void)app_status_light_tap();
}

static const cli_cmd_t sg_cli_cmds[] = {
    { "say", "按当前状态播报一次（等同点击屏幕）", __cli_say },
};

/* ------------------------------------------------------------------ */
/* 拉取与解析                                                          */
/* ------------------------------------------------------------------ */

static OPERATE_RET __fetch_ticket(SL_TICKET_T *out, char *state_out, size_t state_len)
{
    http_client_response_t resp = {0};

    http_client_header_t headers[] = {
        { .key = "Accept", .value = "application/json" },
    };

    http_client_status_t st = http_client_request(
        &(const http_client_request_t){
            .host          = SL_BRIDGE_HOST,
            .port          = SL_BRIDGE_PORT,
            .method        = "GET",
            .path          = SL_BRIDGE_PATH,
            .headers       = headers,
            .headers_count = sizeof(headers) / sizeof(headers[0]),
            .body          = (const uint8_t *)"",
            .body_length   = 0,
            .timeout_ms    = SL_HTTP_TIMEOUT_MS,
        },
        &resp);

    if (HTTP_CLIENT_SUCCESS != st) {
        PR_ERR("sl: http request failed, status=%d", st);
        return OPRT_COM_ERROR;
    }

    /* 非 200 也要当失败 —— 502 是 bridge 在说"我连不上工单系统"。 */
    if (resp.status_code < 200 || resp.status_code >= 300) {
        PR_ERR("sl: http status %u", resp.status_code);
        http_client_free(&resp);
        return OPRT_COM_ERROR;
    }

    if (NULL == resp.body || 0 == resp.body_length) {
        PR_ERR("sl: empty response body");
        http_client_free(&resp);
        return OPRT_COM_ERROR;
    }

    /* 响应体不一定以 \0 结尾，必须带长度解析 */
    cJSON *root = cJSON_ParseWithLength((const char *)resp.body, resp.body_length);
    if (NULL == root) {
        PR_ERR("sl: json parse failed");
        http_client_free(&resp);
        return OPRT_INVALID_PARM;
    }

    cJSON *j;
    j = cJSON_GetObjectItem(root, "p0");             if (cJSON_IsNumber(j)) out->p0             = j->valueint;
    j = cJSON_GetObjectItem(root, "p1");             if (cJSON_IsNumber(j)) out->p1             = j->valueint;
    j = cJSON_GetObjectItem(root, "resolved_today"); if (cJSON_IsNumber(j)) out->resolved_today = j->valueint;

    j = cJSON_GetObjectItem(root, "updated");
    if (cJSON_IsString(j) && j->valuestring) {
        strncpy(out->updated, j->valuestring, sizeof(out->updated) - 1);
        out->updated[sizeof(out->updated) - 1] = '\0';
    }

    /* 服务端算好的状态。它是权威 —— 它读的 alert_buckets 配置固件看不到 */
    if (NULL != state_out && state_len > 0) {
        state_out[0] = '\0';
        j = cJSON_GetObjectItem(root, "state");
        if (cJSON_IsString(j) && j->valuestring) {
            strncpy(state_out, j->valuestring, state_len - 1);
            state_out[state_len - 1] = '\0';
        }
    }

    cJSON_Delete(root);
    http_client_free(&resp);
    return OPRT_OK;
}

/* ------------------------------------------------------------------ */
/* 状态机                                                              */
/* ------------------------------------------------------------------ */

/* 兜底：服务端没给 state 字段时本地算一遍。
 * 规则必须和 bridge.py 的 calc_state 一致。 */
static SL_STATE_E __calc_state(const SL_TICKET_T *t)
{
    if (t->p0 > 0) {
        return SL_STATE_URGENT;
    }
    if (t->p1 > 0) {
        return SL_STATE_WARNING;
    }
    return SL_STATE_IDLE;
}

/* 服务端状态名 -> 本地枚举。认不出来返回 -1，交给调用方走兜底。 */
static int __map_server_state(const char *s)
{
    if (NULL == s || '\0' == s[0]) {
        return -1;
    }
    if (0 == strcmp(s, "urgent"))  return SL_STATE_URGENT;
    if (0 == strcmp(s, "warning")) return SL_STATE_WARNING;
    if (0 == strcmp(s, "idle"))    return SL_STATE_IDLE;
    if (0 == strcmp(s, "offline")) return -2;      /* -2 = 服务端自己说数据源断了 */
    return -1;
}

static BOOL_T __is_all_clear(const SL_TICKET_T *t)
{
    /* 只看 待处理(P0) + 处理中(P1)，无 P2 */
    return (0 == t->p0 && 0 == t->p1) ? TRUE : FALSE;
}

/* 庆祝结束，回到 IDLE */
static void __celebrate_timeout(TIMER_ID id, void *arg)
{
    (void)id;
    (void)arg;

    sg_state = SL_STATE_IDLE;
    ui_status_render(SL_STATE_IDLE, &sg_ticket);
    PR_NOTICE("sl: celebrate done -> idle");
}

/* P0 红灯闪烁 */
static void __blink_timeout(TIMER_ID id, void *arg)
{
    (void)id;
    (void)arg;

    sg_blink_on = sg_blink_on ? FALSE : TRUE;
    ui_status_set_blink(sg_blink_on);
}

/* ---------------- 状态变化 -> 自动播报 ---------------- */

#if SL_AUTO_SPEAK

/* 状态越"严重"数值越大。用来判断这次变化是不是"升级"：
 *   升级（黄->红）必须马上播 —— 工单优先级变高了，不能因为刚播过就吞掉；
 *   降级或来回抖动才由最小间隔兜住。 */
static int __sev_of(SL_STATE_E st)
{
    switch (st) {
    case SL_STATE_URGENT:  return 2;
    case SL_STATE_WARNING: return 1;
    default:               return 0;    /* IDLE / CELEBRATE */
    }
}

/* 状态跳变时自动播一句。调用点只有 __enter_state()，而 __enter_state 在
 * `ns == sg_state` 时会提前 return —— 所以"轮询 30 秒一次、状态没变的那些轮次"
 * 根本不会走到这里，天然不会变成"每 30 秒念一遍"。
 *
 * 这个函数在**持有 sg_lock** 的情况下被调用：这里只 post 信号量（不阻塞），
 * 下载/播放交给 sl_speak 线程去做，所以不会把锁带进 HTTP。
 */
static void __auto_speak(SL_STATE_E ns)
{
    if (NULL == sg_speak_sem) {
        return;
    }

    /* IDLE 永远不出声（它没有对应的提示音，celebrate.wav 是给"刚清空"用的） */
    if (SL_STATE_IDLE == ns) {
        return;
    }

#if !SL_SPEAK_ON_BOOT
    if (sg_first_snapshot) {
        return;
    }
#endif

#if !SL_SPEAK_ON_CLEAR
    if (SL_STATE_CELEBRATE == ns) {
        return;
    }
#endif

    int      sev = __sev_of(ns);
    uint32_t now = (uint32_t)tal_system_get_millisecond();

    /* 不是升级、且离上次自动播报太近 -> 丢掉这次。
     * 升级场景直接跳过这个判断：宁可连播两句，也不能把"变红了"吞掉。 */
    if (sev <= sg_last_speak_sev
        && (now - sg_last_speak_ms) < SL_SPEAK_MIN_GAP_MS) {
        PR_NOTICE("sl: auto speak skipped, st=%d only %ums after last",
                  ns, (unsigned)(now - sg_last_speak_ms));
        return;
    }

    sg_last_speak_ms  = now;
    sg_last_speak_sev = sev;

    PR_NOTICE("sl: ticket change -> auto speak state=%d (p0=%d p1=%d)",
              ns, sg_ticket.p0, sg_ticket.p1);

    (void)tal_semaphore_post(sg_speak_sem);
}

#endif /* SL_AUTO_SPEAK */

static void __enter_state(SL_STATE_E ns)
{
    if (ns == sg_state) {
        return;
    }

    PR_NOTICE("sl: state %d -> %d (p0=%d p1=%d)",
              sg_state, ns, sg_ticket.p0, sg_ticket.p1);

    sg_prev_state = sg_state;
    sg_state      = ns;

    switch (ns) {
    case SL_STATE_URGENT:
        /* 进 P0：闪红 */
        tal_sw_timer_start(sg_blink_tm, SL_BLINK_PERIOD_MS, TAL_TIMER_CYCLE);
        break;

    case SL_STATE_WARNING:
    case SL_STATE_CELEBRATE:
        tal_sw_timer_stop(sg_blink_tm);
        sg_blink_on = TRUE;
        ui_status_set_blink(TRUE);
        if (SL_STATE_CELEBRATE == ns) {
            tal_sw_timer_start(sg_celebrate_tm, SL_CELEBRATE_SEC * 1000, TAL_TIMER_ONCE);
        }
        break;

    case SL_STATE_IDLE:
    default:
        tal_sw_timer_stop(sg_blink_tm);
        sg_blink_on = TRUE;
        ui_status_set_blink(TRUE);
        break;
    }

    ui_status_render(ns, &sg_ticket);

    /* 真的跳变了 -> 按新状态自动播报一句。
     * 放在 render 之后：先让屏幕变过来再出声，视觉和听觉同步。 */
#if SL_AUTO_SPEAK
    __auto_speak(ns);
#endif
}

/* 页面自动轮播：每 SL_PAGE_CYCLE_MS 切到下一页 */
static void __page_timeout(TIMER_ID id, void *arg)
{
    (void)id;
    (void)arg;

    sg_page = (sg_page + 1) % SL_PAGE_COUNT;
    ui_status_set_page(sg_page);
    PR_NOTICE("sl: page -> %d", sg_page);
}

/* 由一次快照推进状态机（不做任何语音播报） */
static void __apply_ticket(const SL_TICKET_T *t, int server_state)
{
    SL_STATE_E ns;

    tal_mutex_lock(sg_lock);

    /* 能拿到数据就说明链路上游是活的，先把 offline 摘掉。 */
    ui_status_set_offline(FALSE);

    BOOL_T  had_open = __is_all_clear(&sg_ticket) ? FALSE : TRUE;
    sg_ticket = *t;

    ns = __calc_state(t);
    if (server_state == SL_STATE_URGENT
        || server_state == SL_STATE_WARNING
        || server_state == SL_STATE_IDLE) {
        ns = (SL_STATE_E)server_state;
    }

    if (SL_STATE_IDLE == ns) {
        /* 刚清零 -> 先进庆祝态，若干秒后自动回 IDLE */
        if (SL_STATE_CELEBRATE != sg_state && had_open) {
            PR_NOTICE("sl: all clear, celebrate");
            __enter_state(SL_STATE_CELEBRATE);
        } else if (SL_STATE_CELEBRATE != sg_state) {
            __enter_state(SL_STATE_IDLE);
        }
    } else {
        /* 庆祝过程中来了新工单 -> 立刻切走，别误报喜 */
        if (SL_STATE_CELEBRATE == sg_state) {
            tal_sw_timer_stop(sg_celebrate_tm);
        }
        __enter_state(ns);
    }

    /* 无论状态是否变化，数字都要刷新 */
    ui_status_render(sg_state, &sg_ticket);

    /* 第一份快照处理完 —— 这之后再变化才算"工单来了"。
     * 必须放在上面那些 __enter_state 调用**之后**：否则开机那一跳
     * （IDLE -> 真实状态）会被当成真的工单到达而念出声。 */
    sg_first_snapshot = FALSE;

    tal_mutex_unlock(sg_lock);
}

/* 服务端说"我连不上工单系统" —— 这时候 p0/p1 全是 0，
 * 直接画成绿色就是在撒谎。宁可显示"我不知道"。 */
static void __mark_offline(void)
{
    tal_mutex_lock(sg_lock);
    ui_status_set_offline(TRUE);
    ui_status_render(sg_state, &sg_ticket);
    tal_mutex_unlock(sg_lock);
}

/* ------------------------------------------------------------------ */
/* 轮询线程                                                            */
/* ------------------------------------------------------------------ */

static void __poll_thread(void *arg)
{
    (void)arg;

    SL_TICKET_T t        = {0};
    char        state[32]= {0};

    PR_NOTICE("sl: poll thread started, target=http://%s:%d%s",
              SL_BRIDGE_HOST, SL_BRIDGE_PORT, SL_BRIDGE_PATH);

    for (;;) {
        memset(&t, 0, sizeof(t));

        if (OPRT_OK == __fetch_ticket(&t, state, sizeof(state))) {
            int ss = __map_server_state(state);

            if (-2 == ss) {
                __mark_offline();
            } else {
                __apply_ticket(&t, ss);
            }
        } else {
            /* 连不上 bridge：屏幕转灰，但轮询继续 —— 网络恢复后会自动好 */
            __mark_offline();
        }

        tal_system_sleep(SL_POLL_INTERVAL_MS);
    }
}

/* ------------------------------------------------------------------ */
/* 对外接口                                                            */
/* ------------------------------------------------------------------ */

OPERATE_RET app_status_light_init(void)
{
    OPERATE_RET rt = OPRT_OK;

    TUYA_CALL_ERR_RETURN(tal_mutex_create_init(&sg_lock));

    /* 1. 屏幕（LVGL 必须已经被 sl_lvgl_init() 起起来，见 firmware/tuya_main.c） */
    TUYA_CALL_ERR_RETURN(ui_status_init());

    /* 2. 音频 —— 顺序很重要，别调换：
     *    ai_audio_player_init()  起播放器服务；
     *    tdl_audio_open()        才真正把 codec/DMA 打开、PA 使能脚置高；
     *    set_vol()               必须放在 open 之后。
     * 之前是先设音量再打开设备，结果每次启动都刷两条
     * "tdd_audio.c ret:-23"（设备还没开），音量也就没设上。
     * 打开后链路就绪：之后工单状态一变就自动播报（见 __auto_speak），
     * 也可以随时点屏幕 / 串口 `say` 手动重播一遍。 */
    TUYA_CALL_ERR_RETURN(ai_audio_player_init());
    {
        TDL_AUDIO_HANDLE_T spk_hdl = NULL;
        if (OPRT_OK == tdl_audio_find(AUDIO_CODEC_NAME, &spk_hdl)) {
            TUYA_CALL_ERR_LOG(tdl_audio_open(spk_hdl, NULL));
        } else {
            PR_ERR("sl: audio codec '%s' not found, playback will be silent", AUDIO_CODEC_NAME);
        }
    }
    /* 音量调不上去也不算致命，记一笔就行，别把整个 init 拖挂 */
    TUYA_CALL_ERR_LOG(ai_audio_player_set_vol(70));


    /* 3. 定时器 */
    TUYA_CALL_ERR_RETURN(tal_sw_timer_create(__celebrate_timeout, NULL, &sg_celebrate_tm));
    TUYA_CALL_ERR_RETURN(tal_sw_timer_create(__blink_timeout,     NULL, &sg_blink_tm));
    TUYA_CALL_ERR_RETURN(tal_sw_timer_create(__page_timeout,      NULL, &sg_page_tm));
    TUYA_CALL_ERR_RETURN(tal_sw_timer_start(sg_page_tm, SL_PAGE_CYCLE_MS, TAL_TIMER_CYCLE));

    /* 4. 轮询线程 —— 注意是 6 个参数，配置打包进 THREAD_CFG_T */
    THREAD_CFG_T thrd = {0};
    thrd.thrdname   = "sl_poll";
    thrd.stackDepth = 4 * 1024;      /* HTTP + cJSON 递归解析，别给太小 */
    thrd.priority   = THREAD_PRIO_1;

    TUYA_CALL_ERR_RETURN(tal_thread_create_and_start(
        &sg_poll_thrd, NULL, NULL, __poll_thread, NULL, &thrd));

    /* 5. 播报线程 —— 自动播报（状态跳变）和手动播报（点屏幕 / `say`）
     * 都只是 post 这个信号量，真正的下载/播放在这里做。
     * 独立线程是必须的：一次下载最长 8 秒，放 LVGL 回调里会让屏幕僵住。
     * 优先级比轮询线程低：播报再重要，也不能把取数挤掉。 */
    TUYA_CALL_ERR_RETURN(tal_semaphore_create_init(&sg_speak_sem, 0, 1));

    THREAD_CFG_T speak_thrd = {0};
    speak_thrd.thrdname   = "sl_speak";
    speak_thrd.stackDepth = 4 * 1024;   /* HTTP + 字符串处理；音频缓冲在播放器内部，不占栈 */
    speak_thrd.priority   = THREAD_PRIO_2;

    TUYA_CALL_ERR_RETURN(tal_thread_create_and_start(
        &sg_speak_thrd, NULL, NULL, __speak_thread, NULL, &speak_thrd));

    ui_status_set_tap_cb(__on_ui_tap);

    /* 6. 串口 say 命令（tal_cli_init 已在 tuya_main 里调过）。
     * 注册失败不影响主流程，记一条日志即可。 */
    if (0 != tal_cli_cmd_register(sg_cli_cmds,
                                  (uint8_t)(sizeof(sg_cli_cmds) / sizeof(sg_cli_cmds[0])))) {
        PR_ERR("sl: cli cmd register failed (say 不可用)");
    }

    PR_NOTICE("sl: status light init done");
    return rt;
}

void app_status_light_inject(const SL_TICKET_T *t)
{
    if (NULL == t) {
        return;
    }
    __apply_ticket(t, -1);
}

SL_STATE_E app_status_light_get_state(void)
{
    return sg_state;
}

void app_status_light_refresh_now(void)
{
    /* 简单做法：把轮询周期的 sleep 打断。
     * 更讲究的做法是加一个信号量，让线程立即醒来。 */
    PR_NOTICE("sl: manual refresh requested");
}

/* 按需播报（点击屏幕语音提示时调用）：把音频 URL 交给原厂 AI 播放器，
 * 由它自己边下边播。
 *
 * 为什么用 URL，而不是「先整包下到 PSRAM，再喂给播放器(SRC_MEM)」：
 *   1. 不必为一段 3 秒的提示音一次占 100+ KB PSRAM，也不用等下载完才出声；
 *   2. SRC_URL 是原厂对话机器人播云端 TTS 走的同一条路(datasink_url)，
 *      播放列表 auto_play 会直接把播放拉起来，是这条链路上最成熟的分支；
 *   3. 该分支自带断点续传（Range 重连），局域网抖一下不至于整段丢。
 *
 *   【实测结论】SRC_MEM 那条在本固件下不可用：播放线程第一次读数据就返回
 *   -2(OPRT_INVALID_PARM) 然后立刻 stop，一秒都撑不到；内置提示音
 *   ai_audio_player_alert() 走的也是 SRC_MEM，所以同样不出声。
 *   换到 SRC_URL 后绕开了整条 mem/ringbuf 路径。
 *
 * 播什么由服务端决定 —— 改措辞只要重新生成 wav（见 assets/make_audio.sh），
 * 不用重烧固件。 */
OPERATE_RET app_status_light_speak(const char *url)
{
    if (NULL == url || 0 == strlen(url)) {
        return OPRT_INVALID_PARM;
    }

    AI_AUDIO_PLAY_TTS_T tts = {0};
    tts.tts.url    = (char *)url;   /* 播放列表内部会 strdup，不必常驻 */
    tts.tts.format = AI_AUDIO_CODEC_WAV;

    /* 内部先清队再入队（tone playlist capacity=2、auto_play=true），
     * 所以连点多次只会听到最后一次，不会排队播好几遍。 */
    OPERATE_RET rt = ai_audio_play_tts_url(&tts, false);
    if (OPRT_OK != rt) {
        PR_ERR("sl: queue tts url failed rt=%d (%s)", rt, url);
    } else {
        PR_NOTICE("sl: audio queued %s", url);
    }
    return rt;
}


/* ------------------------------------------------------------------ */
/* 播报：自动（工单状态跳变）/ 手动（点屏幕、串口 say）                 */
/* ------------------------------------------------------------------ */

/* 当前状态该播哪段音频。文件都在 bridge 的 /audio/ 下 ——
 * 也就是"播什么"由服务端着，改措辞只要重新生成 wav（见 assets/make_audio.sh），
 * 不用重烧固件。 */
static const char *__audio_for_state(SL_STATE_E st)
{
    switch (st) {
    case SL_STATE_URGENT:  return "urgent.wav";     /* 有 P0 */
    case SL_STATE_WARNING: return "warning.wav";    /* 只有 P1 */
    case SL_STATE_CELEBRATE:
    case SL_STATE_IDLE:
    default:               return "celebrate.wav";  /* 全清 */
    }
}

/* 播报线程：被自动/手动任一路唤醒后，取当前状态去点播对应音频。
 *
 * 这里**总是读最新的 sg_state**，而不是把触发时的状态带过来 —— 这正是我们要的：
 * 连点几次、或状态连续跳变时，用户听到的永远是最新那个状态，
 * 不会排着队把过期内容一条条念完。
 *
 * 下载最长要 8 秒，所以绝不能放在 LVGL 回调或状态机里做。 */
static void __speak_thread(void *arg)
{
    (void)arg;

    for (;;) {
        if (OPRT_OK != tal_semaphore_wait_forever(sg_speak_sem)) {
            continue;
        }

        /* 取快照时持锁，但别把锁带进 HTTP */
        SL_STATE_E st;
        tal_mutex_lock(sg_lock);
        st = sg_state;
        tal_mutex_unlock(sg_lock);

        char url[128] = {0};
        snprintf(url, sizeof(url), "http://%s:%d/audio/%s",
                 SL_BRIDGE_HOST, SL_BRIDGE_PORT, __audio_for_state(st));

        PR_NOTICE("sl: speak state=%d url=%s", st, url);
        app_status_light_speak(url);
    }
}

/* 给 LVGL 事件回调调：**只投递信号量，立刻返回**。
 * 在这里直接下载会让整块屏僵住（见头文件里的说明）。
 *
 * 手动触发不设任何间隔限制 —— 用户点了就必须响，哪怕自动播报刚播过。 */
OPERATE_RET app_status_light_tap(void)
{
    if (NULL == sg_speak_sem) {
        return OPRT_COM_ERROR;
    }
    PR_NOTICE("sl: manual speak requested (tap/say)");
    return tal_semaphore_post(sg_speak_sem);
}
