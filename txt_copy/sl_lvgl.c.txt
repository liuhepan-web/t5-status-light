/**
 * @file sl_lvgl.c
 * @brief 起 LVGL + 提供跨线程显示锁
 *
 * ---------------------------------------------------------------------
 * 【为什么不直接用 your_chat_bot 的 tuya_lvgl.c】
 *
 * 那个文件在 `src/display2/` 里，而 display2 由 Kconfig 开关
 * `CONFIG_ENABLE_CHAT_DISPLAY2` 控制，**它的默认值是 n**。
 * 板级配置 `TUYA_T5AI_BOARD_LCD_3.5.config` 和 `app_default.config`
 * 都没有打开它 —— 所以显示默认是关的。
 *
 * 后果：直接调 `tuya_lvgl_init()` 会**链接失败**（undefined reference），
 * 而不是编译报错。而且这个开关的名字跟"显示"看起来没直接关系，
 * 很容易查半天查不到。
 *
 * ---------------------------------------------------------------------
 * 【这里走的路线】
 *
 * 直接用 SDK 自带的 `lv_vendor_*`（`src/liblvgl/v9/port/lv_vendor.h`）。
 * 这个头文件只要 `ENABLE_LIBLVGL=y` 就可用，不依赖任何 app 层代码。
 * `duo_eye_mood` 的 `src/ui/src/app_ui_eyes.c` 就是这么做的。
 *
 * 对比一下就很清楚 —— display2 里的 tuya_lvgl.c 其实只做了这么点事：
 *
 *     OPERATE_RET tuya_lvgl_init(void) {
 *         lv_vendor_init(DISPLAY_NAME);
 *         lv_vendor_start(5, 1024*8);
 *         return OPRT_OK;
 *     }
 *     OPERATE_RET tuya_lvgl_mutex_lock(void)   { lv_vendor_disp_lock();   ... }
 *     OPERATE_RET tuya_lvgl_mutex_unlock(void) { lv_vendor_disp_unlock(); ... }
 *
 * 一模一样。所以自己写这 20 行，换掉整个 display2 依赖（连带它下面的
 * WeChat / Chatbot 两套 UI）是划算的。
 * ---------------------------------------------------------------------
 */

#include "sl_lvgl.h"

#include "tal_log.h"
#include "lv_vendor.h"

/* DISPLAY_NAME 由板级配置在编译期注入（如选区号 "disp0"）。
 * 没定义 = 这块板的配置里根本没开显示 —— 在这里拦住，
 * 比让它以一堆 "undefined reference" 的形式炸在链接期清楚得多。 */
#ifndef DISPLAY_NAME
#error "DISPLAY_NAME 未定义：板级配置里没开显示。请在 tos.py config choice 里选带屏的板子（如 TUYA_T5AI_BOARD_LCD_3.5），并确认 CONFIG_ENABLE_DISPLAY=y / CONFIG_ENABLE_LIBLVGL=y"
#endif

/* 刷屏线程优先级与栈大小。照抄 display2 与 duo_eye_mood 的取值。 */
#ifndef SL_LVGL_TASK_PRIO
#define SL_LVGL_TASK_PRIO   5
#endif

#ifndef SL_LVGL_STACK_SIZE
#define SL_LVGL_STACK_SIZE  (1024 * 8)
#endif

OPERATE_RET sl_lvgl_init(void)
{
    lv_vendor_init(DISPLAY_NAME);

    /* 参数依次是 lvgl 任务优先级、栈大小 */
    lv_vendor_start(SL_LVGL_TASK_PRIO, SL_LVGL_STACK_SIZE);

    PR_NOTICE("sl: lvgl started (display=%s)", DISPLAY_NAME);
    return OPRT_OK;
}

OPERATE_RET sl_lvgl_lock(void)
{
    lv_vendor_disp_lock();
    return OPRT_OK;
}

OPERATE_RET sl_lvgl_unlock(void)
{
    lv_vendor_disp_unlock();
    return OPRT_OK;
}
