#ifndef __UI_STATUS_H__
#define __UI_STATUS_H__

#include "tuya_cloud_types.h"
#include "app_status_light.h"

#ifdef __cplusplus
extern "C" {
#endif

OPERATE_RET ui_status_init(void);
void        ui_status_render(SL_STATE_E state, const SL_TICKET_T *t);
void        ui_status_set_blink(BOOL_T on);
void        ui_status_set_offline(BOOL_T offline);

/* 可切换页面：页面默认用定时器自动轮播，也可以在页面上点一下。
 *   SL_PAGE_P0 —— 首页：只显示 待处理(P0)
 *   SL_PAGE_P1 —— 第二页：显示 处理中(P1)（待补充 P2 已去掉，不再显示） */
typedef enum {
    SL_PAGE_P0 = 0,
    SL_PAGE_P1,
    SL_PAGE_COUNT
} SL_PAGE_E;

void ui_status_set_page(int page);
int  ui_status_get_page(void);

/**
 * @brief 注册「点击屏幕」回调（点击播报用）。
 *
 *        这一层只负责把点击事件转出去，**不知道回调会做什么** —— 播报、
 *        切页、上报都可以，从而保持"UI 层不碰业务"的边界。
 *
 *        回调在 **LVGL 刷屏线程** 里被调用，**必须是快速返回的**：
 *        任何阻塞（HTTP、sleep、等锁）都会让整块屏停止刷新。
 *        标准做法是在回调里只投递信号量/事件，真正干活交给工作线程。
 *
 * @param cb NULL 表示取消注册
 */
void ui_status_set_tap_cb(void (*cb)(void));

#ifdef __cplusplus
}
#endif

#endif /* __UI_STATUS_H__ */
