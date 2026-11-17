#ifndef __SL_LVGL_H__
#define __SL_LVGL_H__

#include "tuya_cloud_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 起 LVGL：初始化显示屏 + 拉起刷屏线程
 *
 * 必须在 board_register_hardware() 之后调用（面板要先注册好），
 * 在你自己建控件之前调用。
 *
 * @return OPRT_OK on success
 */
OPERATE_RET sl_lvgl_init(void);

/**
 * @brief 取 LVGL 显示锁
 *
 * LVGL 不是线程安全的。从轮询线程改界面之前必须拿这把锁，
 * 否则会随机崩 —— 而且崩得毫无规律，最难查。
 */
OPERATE_RET sl_lvgl_lock(void);

/**
 * @brief 释放 LVGL 显示锁
 */
OPERATE_RET sl_lvgl_unlock(void);

#ifdef __cplusplus
}
#endif

#endif /* __SL_LVGL_H__ */
