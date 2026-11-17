/**
 * @file tuya_main.c
 * @brief T5 团队状态灯 —— 应用入口
 *
 * 这个文件是**替换**掉 your_chat_bot/src/tuya_main.c 用的。
 * 安装脚本会把它拷进工程覆盖同名文件，同时把工程里的 app_chat_bot.c /
 * reset_netcfg.c 一并带上（它们还会被编译，但不再被调用）。
 *
 * 和原版入口唯一的区别，就在 user_main() 里那三行：
 *
 *     board_register_hardware();     // 板级外设（LCD 面板、音频 codec、按键）
 *     sl_lvgl_init();                // LVGL 起来，并拉起刷屏线程（不用 display2 的 tuya_lvgl_init）
 *     app_status_light_init();       // 我们的：建界面 + 起轮询线程
 *
 * 原版这里调的是 app_chat_bot_init()，那会把整套 AI 对话拉起来。
 * 我们不需要它 —— 状态灯只做「拉数据、画屏幕、播语音」三件事。
 *
 * ====================================================================
 * 【这条路为什么不需要涂鸦云凭据】
 *
 * 板子和 bridge.py 在同一局域网，数据是板子**主动去拉**的，不经过涂鸦云。
 * 底下这段 tuya_iot_init / netmgr / tuya_iot_start 保留着，是因为：
 *
 *   1. Wi-Fi 链路由 netmgr 负责，跳不过去 —— 板子得先上网才能拉数据；
 *   2. 你之前用 Cobuilder 烧录时设备已经激活过一次，授权码存在 KV 里，
 *      这段代码会自动把它读出来，MQTT 该连就连，连不上也**不影响状态灯**；
 *   3. 留着它，以后想把 DP 那条路（dp_push.py）再接回来，不用改这里。
 *
 * 也就是说：MQTT 连没连上，状态灯都照常工作。
 * ====================================================================
 */

#include "tuya_cloud_types.h"

#include <assert.h>
#include <string.h>
#include "tal_api.h"
#include "tuya_config.h"
#include "tuya_iot.h"
#include "tuya_iot_dp.h"
#include "netmgr.h"
#include "tkl_output.h"
#include "tal_cli.h"
#include "tuya_authorize.h"

#if defined(ENABLE_WIFI) && (ENABLE_WIFI == 1)
#include "netconn_wifi.h"
#else
#include "tkl_wifi_stub.h"
#endif

#if defined(ENABLE_WIRED) && (ENABLE_WIRED == 1)
#include "netconn_wired.h"
#endif

#if defined(ENABLE_LIBLWIP) && (ENABLE_LIBLWIP == 1)
#include "lwip_init.h"
#endif

#include "board_com_api.h"

/* 显示：sl_lvgl.c 提供 sl_lvgl_init / sl_lvgl_lock / sl_lvgl_unlock
 *
 * 注意**不要**用 your_chat_bot 的 tuya_lvgl.h —— 它在 src/display2/ 里，
 * 而 display2 由 CONFIG_ENABLE_CHAT_DISPLAY2 控制，默认是 n。
 * 直接调会以 "undefined reference to tuya_lvgl_init" 的形式炸在链接期。 */
#include "sl_lvgl.h"

/* 网卡配置提示音（可选，ENABLE_COMP_AI_AUDIO 默认是开的） */
#if defined(ENABLE_COMP_AI_AUDIO) && (ENABLE_COMP_AI_AUDIO == 1)
#include "ai_audio_player.h"
#endif

/* 我们要做的那个应用 */
#include "app_status_light.h"

/* Tuya device handle */
tuya_iot_client_t ai_client;

/* Tuya license information (uuid authkey) */
tuya_iot_license_t license;

#ifndef PROJECT_VERSION
#define PROJECT_VERSION "1.0.0"
#endif

/**
 * @brief user defined log output api, in this demo, it will use uart0 as log-tx
 */
void user_log_output_cb(const char *str)
{
    tal_uart_write(TUYA_UART_NUM_0, (const uint8_t *)str, strlen(str));
}

/**
 * @brief user defined event handler
 *
 * 这里只保留必要的日志和网络事件提示。业务逻辑全在
 * app_status_light.c 的轮询线程里，不依赖云事件。
 */
void user_event_handler_on(tuya_iot_client_t *client, tuya_event_msg_t *event)
{
    (void)client;

    switch (event->id) {
    case TUYA_EVENT_BIND_START:
        PR_INFO("Device Bind Start!");
#if defined(ENABLE_COMP_AI_AUDIO) && (ENABLE_COMP_AI_AUDIO == 1)
        ai_audio_player_alert(AI_AUDIO_ALERT_NETWORK_CFG);
#endif
        break;

    /* MQTT with tuya cloud is connected, device online.
     * 注意：这里**不需要**做任何事 —— 状态灯的轮询线程是自愈的，
     * 网络一好它下一轮就能拉到数据。 */
    case TUYA_EVENT_MQTT_CONNECTED:
        PR_NOTICE("Device MQTT Connected! (状态灯不依赖它)");
        tal_event_publish("app.time.sync", NULL);
        break;

    case TUYA_EVENT_TIMESTAMP_SYNC:
        tal_event_publish("app.time.sync", NULL);
        break;

    case TUYA_EVENT_RESET_COMPLETE:
        PR_INFO("Device Reset Complete!");
        tal_system_reset();
        break;

    default:
        break;
    }
}

/**
 * @brief user defined network check callback, called every 1s
 */
bool user_network_check(void)
{
    netmgr_status_e status = NETMGR_LINK_DOWN;
    netmgr_conn_get(NETCONN_AUTO, NETCONN_CMD_STATUS, &status);
    return status == NETMGR_LINK_DOWN ? false : true;
}

void user_main(void)
{
    int ret = OPRT_OK;

#if defined(ENABLE_EXT_RAM) && (ENABLE_EXT_RAM == 1)
    cJSON_InitHooks(&(cJSON_Hooks){.malloc_fn = tal_psram_malloc, .free_fn = tal_psram_free});
#else
    cJSON_InitHooks(&(cJSON_Hooks){.malloc_fn = tal_malloc, .free_fn = tal_free});
#endif

    tal_log_init(TAL_LOG_LEVEL_DEBUG, 1024, (TAL_LOG_OUTPUT_CB)tkl_log_output);

    PR_NOTICE("==========================================");
    PR_NOTICE(" T5 团队状态灯");
    PR_NOTICE(" Project name:      %s", PROJECT_NAME);
    PR_NOTICE(" App version:       %s", PROJECT_VERSION);
    PR_NOTICE(" Compile time:      %s", __DATE__);
    PR_NOTICE(" TuyaOpen version:  %s", OPEN_VERSION);
    PR_NOTICE(" Platform board:    %s", PLATFORM_BOARD);
    PR_NOTICE("==========================================");

    tal_kv_init(&(tal_kv_cfg_t){
        .seed = "vmlkasdh93dlvlcy",
        .key  = "dflfuap134ddlduq",
    });
    tal_sw_timer_init();
    tal_workq_init();
    tal_time_service_init();
    tal_cli_init();
    tuya_authorize_init();

    /* 读授权码。之前用 Cobuilder 烧录过就会读到；读不到也不影响状态灯，
     * 只是 MQTT 连不上 —— 而状态灯走的是局域网 HTTP，不看 MQTT。 */
    if (OPRT_OK != tuya_authorize_read(&license)) {
        license.uuid    = TUYA_OPENSDK_UUID;
        license.authkey = TUYA_OPENSDK_AUTHKEY;
        PR_WARN("No license in KV, using build-time UUID/AUTHKEY.");
    }

    ret = tuya_iot_init(&ai_client, &(const tuya_iot_config_t){
                                        .software_ver = PROJECT_VERSION,
                                        .productkey   = TUYA_PRODUCT_ID,
                                        .uuid         = license.uuid,
                                        .authkey      = license.authkey,
                                        .event_handler = user_event_handler_on,
                                        .network_check = user_network_check,
                                    });
    assert(ret == OPRT_OK);

#if defined(ENABLE_LIBLWIP) && (ENABLE_LIBLWIP == 1)
    TUYA_LwIP_Init();
#endif

    /* ---- 网络 ---- */
    netmgr_type_e type = 0;
#if defined(ENABLE_WIFI) && (ENABLE_WIFI == 1)
    type |= NETCONN_WIFI;
#endif
#if defined(ENABLE_WIRED) && (ENABLE_WIRED == 1)
    type |= NETCONN_WIRED;
#endif
    netmgr_init(type);
#if defined(ENABLE_WIFI) && (ENABLE_WIFI == 1)
    netmgr_conn_set(NETCONN_WIFI, NETCONN_CMD_NETCFG,
                    &(netcfg_args_t){.type = NETCFG_TUYA_BLE | NETCFG_TUYA_WIFI_AP});
#endif

    /* ---- 板级外设：LCD 面板 + 触摸 + 音频 codec ---- */
    ret = board_register_hardware();
    if (ret != OPRT_OK) {
        PR_ERR("board_register_hardware failed: %d", ret);
    }

    /* ---- 屏幕：LVGL 起来并拉起刷屏线程 ----
     * 必须在 board_register_hardware() 之后 —— 面板要先注册好。
     * 必须在 app_status_light_init() 之前 —— 否则建控件时 LVGL 还没初始化。 */
    ret = sl_lvgl_init();
    if (ret != OPRT_OK) {
        PR_ERR("sl_lvgl_init failed: %d", ret);
    }

    /* ---- 状态灯：界面 + 音频 + 轮询线程 ---- */
    ret = app_status_light_init();
    if (ret != OPRT_OK) {
        PR_ERR("app_status_light_init failed: %d", ret);
    }

    /* ---- 启动 Tuya IoT 任务（Wi-Fi 链路 + MQTT 尝试）---- */
    tuya_iot_start(&ai_client);

    /* 直接把 Wi-Fi 省电关掉。
     * 原版这里是条件调用 —— 因为 app_chat_bot_init() 会按对话模式去仲裁省电策略。
     * 我们不跑对话逻辑，也没初始化低功耗框架，所以无条件关掉，
     * 保证 30 秒一次的轮询稳定拿到数据。 */
    tkl_wifi_set_lp_mode(0, 0);

    PR_NOTICE("=== 初始化完成。轮询日志过滤关键字：sl: ===");

    for (;;) {
        tuya_iot_yield(&ai_client);
    }
}

/* ------------------------------------------------------------------ */
/* 线程入口（非 Linux 平台）                                           */
/* ------------------------------------------------------------------ */

#if OPERATING_SYSTEM == SYSTEM_LINUX

void main(int argc, char *argv[])
{
    (void)argc;
    (void)argv;
    user_main();
}

#else

static THREAD_HANDLE ty_app_thread = NULL;

static void tuya_app_thread(void *arg)
{
    (void)arg;

    user_main();

    tal_thread_delete(ty_app_thread);
    ty_app_thread = NULL;
}

void tuya_app_main(void)
{
    THREAD_CFG_T thrd_param = {0};
    thrd_param.stackDepth   = 1024 * 8;   /* 我们额外起了 LVGL + 轮询线程，主线程也给宽一点 */
    thrd_param.priority     = THREAD_PRIO_1;
    thrd_param.thrdname     = "tuya_app_main";
    tal_thread_create_and_start(&ty_app_thread, NULL, NULL, tuya_app_thread, NULL, &thrd_param);
}

#endif
