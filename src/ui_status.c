/**
 * @file ui_status.c
 * @brief T5 状态灯的屏幕渲染层（LVGL）
 *
 * 设计原则：这一层只做一件事 —— 把 (状态, 数字) 画到屏幕上。
 * 不含任何网络、业务逻辑，换掉这个文件不影响其它模块。
 *
 * 【LVGL 版本提示】
 * TuyaOpen 已升级到 LVGL v9。v9 与 v8 有几处 API 改名，本文件按 v9 写：
 *   lv_scr_act()          ->  lv_screen_active()
 *   lv_disp_get_default() ->  lv_display_get_default()
 * 如果编译报 "implicit declaration"，先确认 SDK 里的 LVGL 版本再改回来。
 *
 * 【线程安全提示】
 * 这个函数会从轮询线程里被调用，而 LVGL 只能在它自己的线程里操作。
 * TuyaOpen 的 tuya_lvgl.c 提供了互斥锁封装，务必套上（见 __lvgl_lock/__lvgl_unlock）。
 */

#include <stdio.h>
#include <string.h>

#include "ui_status.h"
#include "tal_log.h"

#include "lvgl.h"

#include "sl_lvgl.h"

#define SL_COLOR_IDLE       0x0B7A3B   /* 绿：全清 */
#define SL_COLOR_WARNING    0xB07500   /* 黄：有 P1（处理中） */
#define SL_COLOR_URGENT     0xC6221B   /* 红：有 P0（待处理） */
#define SL_COLOR_CELEBRATE  0x6E4BD6   /* 紫：刚清零 */
#define SL_COLOR_OFFLINE    0x4A4A4A   /* 灰：拉不到数据 */

#define SL_BG_DARK          0x121418

static lv_obj_t *sg_root     = NULL;
static lv_obj_t *sg_big_num  = NULL;
static lv_obj_t *sg_title    = NULL;
static lv_obj_t *sg_subtitle = NULL;
static lv_obj_t *sg_footer   = NULL;

static uint32_t  sg_state_color = SL_COLOR_IDLE;
static BOOL_T    sg_offline     = FALSE;

static int          sg_page      = SL_PAGE_P0;
static SL_STATE_E   sg_last_state = SL_STATE_IDLE;
static SL_TICKET_T  sg_last_t     = {0};

/* 点击屏幕的回调。本层只知道"被点了"，不关心做什么。 */
static void (*sg_tap_cb)(void) = NULL;

static void __lvgl_lock(void)
{
    sl_lvgl_lock();
}

static void __lvgl_unlock(void)
{
    sl_lvgl_unlock();
}

/* LVGL 事件回调 —— 跑在刷屏线程上，必须立刻返回。
 * 这里只把事件转给注册者，不做任何有可能阻塞的事。 */
static void __on_screen_tap(lv_event_t *e)
{
    (void)e;
    if (NULL != sg_tap_cb) {
        sg_tap_cb();
    }
}

static uint32_t __state_color(SL_STATE_E state)
{
    switch (state) {
    case SL_STATE_URGENT:    return SL_COLOR_URGENT;
    case SL_STATE_WARNING:   return SL_COLOR_WARNING;
    case SL_STATE_CELEBRATE: return SL_COLOR_CELEBRATE;
    case SL_STATE_IDLE:
    default:                 return SL_COLOR_IDLE;
    }
}

static const char *__state_title(SL_STATE_E state)
{
    switch (state) {
    case SL_STATE_URGENT:    return "P0 ALERT";
    case SL_STATE_WARNING:   return "P1 ACTIVE";
    case SL_STATE_CELEBRATE: return "ALL CLEAR!";
    case SL_STATE_IDLE:
    default:                 return "ALL CLEAR";
    }
}

OPERATE_RET ui_status_init(void)
{
    lv_obj_t *scr = lv_screen_active();

    __lvgl_lock();

    sg_root = lv_obj_create(scr);
    lv_obj_remove_style_all(sg_root);
    lv_obj_set_size(sg_root, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(sg_root, lv_color_hex(SL_BG_DARK), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(sg_root, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_clear_flag(sg_root, LV_OBJ_FLAG_SCROLLABLE);

    sg_title = lv_label_create(sg_root);
    lv_label_set_text(sg_title, "ALL CLEAR");
    lv_obj_set_style_text_color(sg_title, lv_color_hex(0xFFFFFF), LV_PART_MAIN);
    lv_obj_align(sg_title, LV_ALIGN_TOP_MID, 0, 48);

    sg_big_num = lv_label_create(sg_root);
    lv_label_set_text(sg_big_num, "0");
    lv_obj_set_style_text_color(sg_big_num, lv_color_hex(0xFFFFFF), LV_PART_MAIN);
    lv_obj_align(sg_big_num, LV_ALIGN_CENTER, 0, -20);

    sg_subtitle = lv_label_create(sg_root);
    lv_label_set_text(sg_subtitle, "P0 OPEN");
    lv_obj_set_style_text_color(sg_subtitle, lv_color_hex(0xCCCCCC), LV_PART_MAIN);
    lv_obj_align(sg_subtitle, LV_ALIGN_CENTER, 0, 64);

    sg_footer = lv_label_create(sg_root);
    lv_label_set_text(sg_footer, "--:--:--");
    lv_obj_set_style_text_color(sg_footer, lv_color_hex(0x888888), LV_PART_MAIN);
    lv_obj_align(sg_footer, LV_ALIGN_BOTTOM_MID, 0, -32);

    /* ---- 点击屏幕 -> 转发给注册者（见 ui_status_set_tap_cb） ----
     *
     * 触摸输入设备不用我们注册：lv_vendor_init() 内部会调
     * lv_port_indev_init(device)，板级配置开了 CONFIG_ENABLE_TP 就自动带上。
     *
     * 两个细节缺一不可：
     *  1. sg_root 虽然铺满全屏，但要显式带 CLICKABLE 才吃得到点击；
     *  2. 子控件（那几个 label）不会自动把事件冒泡给父控件 —— 不显式打开
     *     EVENT_BUBBLE 的话，点在文字上就没反应，只有点在空白处才行。
     *     这种"时灵时不灵"最容易当成接触不良去查。 */
    lv_obj_add_flag(sg_root, LV_OBJ_FLAG_CLICKABLE);

    uint32_t cn = lv_obj_get_child_count(sg_root);
    for (uint32_t i = 0; i < cn; i++) {
        lv_obj_add_flag(lv_obj_get_child(sg_root, i), LV_OBJ_FLAG_EVENT_BUBBLE);
    }

    lv_obj_add_event_cb(sg_root, __on_screen_tap, LV_EVENT_CLICKED, NULL);

    __lvgl_unlock();

    PR_NOTICE("ui: status screen ready");
    return OPRT_OK;
}

void ui_status_render(SL_STATE_E state, const SL_TICKET_T *t)
{
    if (NULL == t) {
        return;
    }

    __lvgl_lock();

    sg_last_state = state;
    sg_last_t     = *t;

    sg_state_color = sg_offline ? SL_COLOR_OFFLINE : __state_color(state);

    lv_obj_set_style_bg_color(sg_root, lv_color_hex(sg_state_color), LV_PART_MAIN);
    lv_label_set_text(sg_title, sg_offline ? "OFFLINE" : __state_title(state));

    char buf[48];

    if (SL_PAGE_P0 == sg_page) {
        snprintf(buf, sizeof(buf), "%d", t->p0);
        lv_label_set_text(sg_big_num, buf);
        lv_label_set_text(sg_subtitle, "P0 TODO");          /* 待处理 */
    } else {
        snprintf(buf, sizeof(buf), "%d", t->p1);
        lv_label_set_text(sg_big_num, buf);
        lv_label_set_text(sg_subtitle, "P1 DOING");         /* 处理中 */
    }

    snprintf(buf, sizeof(buf), "[Pg %d/%d] %s  fixed %d",
             sg_page + 1, SL_PAGE_COUNT,
             (t->updated[0] != '\0') ? t->updated : "--:--:--",
             t->resolved_today);
    lv_label_set_text(sg_footer, buf);

    __lvgl_unlock();
}

void ui_status_set_page(int page)
{
    if (page < 0 || page >= SL_PAGE_COUNT) {
        page = (page < 0) ? 0 : SL_PAGE_COUNT - 1;
    }
    if (page == sg_page) {
        return;
    }
    sg_page = page;
    ui_status_render(sg_last_state, &sg_last_t);
    PR_NOTICE("ui: page -> %d", sg_page);
}

int ui_status_get_page(void)
{
    return sg_page;
}

void ui_status_set_blink(BOOL_T on)
{
    __lvgl_lock();

    if (sg_offline) {
        lv_obj_set_style_bg_color(sg_root, lv_color_hex(SL_COLOR_OFFLINE), LV_PART_MAIN);
    } else {
        lv_obj_set_style_bg_color(sg_root,
                                  lv_color_hex(on ? sg_state_color : SL_BG_DARK),
                                  LV_PART_MAIN);
    }

    __lvgl_unlock();
}

void ui_status_set_offline(BOOL_T offline)
{
    sg_offline = offline;
}

void ui_status_set_tap_cb(void (*cb)(void))
{
    sg_tap_cb = cb;
}
