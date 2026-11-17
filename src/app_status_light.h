#ifndef __APP_STATUS_LIGHT_H__
#define __APP_STATUS_LIGHT_H__

#include "tuya_cloud_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 工单快照 */
typedef struct {
    int  p0;                /* P0 未解决数 */
    int  p1;                /* P1 未解决数 */
    int  resolved_today;    /* 今日已解决数 */
    char updated[32];       /* 服务端时间戳，仅用于屏幕显示 */
} SL_TICKET_T;

/* 展示状态 */
typedef enum {
    SL_STATE_IDLE = 0,      /* 全清：绿 */
    SL_STATE_WARNING,       /* 有 P1：黄   */
    SL_STATE_URGENT,        /* 有 P0：红闪     */
    SL_STATE_CELEBRATE,     /* 刚清零：彩     */
} SL_STATE_E;

/**
 * @brief 初始化状态灯：创建 UI、初始化音频、启动轮询线程
 * @return OPRT_OK on success
 */
OPERATE_RET app_status_light_init(void);

/**
 * @brief 供 UI / 调试命令直接注入一次工单快照（可绕过 HTTP）
 */
void app_status_light_inject(const SL_TICKET_T *t);

/**
 * @brief 当前状态（给按键、MCP 工具等外部调用读）
 */
SL_STATE_E app_status_light_get_state(void);

/**
 * @brief 立即触发一次拉取（按键刷新用）
 */
void app_status_light_refresh_now(void);

/**
 * @brief 按需播报：从转发服务拉一段音频播（点击屏幕语音提示时调用）。
 *        本版状态灯不自动播报，仅在需要"点击播报"时由 UI 层调用。
 * @return OPRT_OK on success
 */
OPERATE_RET app_status_light_speak(const char *url);

/**
 * @brief 点击屏幕 -> 按需播报当前状态。
 *
 *        **本函数立即返回**，只投递一个信号量；真正的 HTTP 下载 + 解码播放
 *        在一个独立的播报线程里做。
 *
 *        这一点很重要：本函数是给 LVGL 事件回调调的，而 LVGL 回调跑在刷屏
 *        线程上。如果在这里直接下载（最长 8 秒超时），整个 UI 会僵住不刷新。
 *
 *        播什么由当前状态决定（映射见 __audio_for_state）：
 *          URGENT    -> /audio/urgent.wav    有 P0
 *          WARNING   -> /audio/warning.wav   只有 P1
 *          IDLE/庆祝 -> /audio/celebrate.wav 全清
 *        音频内容在服务端，改措辞只要重新生成 wav，不用重烧固件。
 *
 * @return OPRT_OK on success
 */
OPERATE_RET app_status_light_tap(void);

#ifdef __cplusplus
}
#endif

#endif /* __APP_STATUS_LIGHT_H__ */
