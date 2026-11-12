#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨模块共用的常量。

``main_window`` 和 ``setup_dialog`` 都要用到默认扩展名、体积上限、解码风险清单，
放在这里避免两个界面模块互相 import 造成循环依赖。
"""

# 默认支持的视频扩展名（逗号分隔，与 C++ 版 DEFAULT_EXTS 保持一致）
DEFAULT_EXTS = (
    "mp4, avi, mkv, mov, wmv, flv, m4v, mpg, mpeg, ts, m2ts, mts, "
    "rmvb, rm, webm, 3gp, vob, f4v, asf, ogv, divx"
)

# 扩展名勾选面板里列出的常用项（含默认清单之外的补充项）
EXT_POOL = (
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "f4v", "webm",
    "m4v", "mpg", "mpeg", "ts", "m2ts", "mts", "rmvb", "rm",
    "vob", "divx", "asf", "ogv", "3gp", "mxf", "dat", "dat2",
)

# 体积过滤上限（MB）：再大就按 1TB 封顶，避免误输入天文数字把 int 撑爆
SIZE_LIMIT_MAX_MB = 1024 * 1024

# 内置播放器（QtMultimedia / FFmpeg）常解不开的容器：多半是 XVID / MPEG-2 / DivX
# 视频流或 AC3 / DTS 音轨。双击后不是不能播，但等解码失败再报错体验太差，
# 所以双击时先把这条提示摆出来，播放成功会自动切回画面。
RISKY_EXTS = {
    "avi", "wmv", "flv", "f4v", "rmvb", "rm", "vob", "divx", "asf",
    "mpg", "mpeg", "ts", "m2ts", "mts", "3gp", "ogv",
}
# mkv 只是容器，装 H.264 完全能播，所以单列成「弱提示」，不预先切占位图
SOFT_RISKY_EXTS = {"mkv", "mov", "webm"}
