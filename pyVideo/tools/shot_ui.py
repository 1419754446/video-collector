#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发辅助：离屏把主窗口渲染成 PNG，用来核对当前源码的界面与主题。

不弹窗、不读用户设置里的路径，只灌一批假数据，验证：

    python tools/shot_ui.py .screenshots\light.png 1400 880 light
    python tools/shot_ui.py .screenshots\dark.png  1400 880 dark
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 默认走真实 windows 平台：offscreen 插件在 Windows 上用的是精简字体库，
# 中文会渲染成方块，截出来的图看不出真实效果。需要完全无窗口时设 VC_QPA=offscreen。
os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("VC_QPA", "windows"))

from PySide6.QtCore import QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import theme  # noqa: E402
from app.main_window import MainWindow  # noqa: E402

NAMES = [
    ("旅行 vlog 剪辑 最终版.mp4", 1_284_556_800, "已复制"),
    ("产品宣传片_4K母版.mov", 3_921_000_000, "已复制"),
    ("会议录屏 2024-03-11.mkv", 862_000_000, "同名已存在，已跳过"),
    ("家庭录像_生日会.avi", 244_000_000, "已复制"),
    ("客户访谈 原片.mp4", 1_650_000_000, "失败：大小校验不通过（1.2 GB ≠ 1.5 GB）"),
    ("无人机航拍 01.mov", 972_000_000, "已移动"),
    ("素材_夕阳延时.mp4", 512_000_000, "已在目标目录"),
    ("教程录制 part1.mp4", 348_000_000, "已复制"),
    ("教程录制 part2.mp4", 366_000_000, "已复制"),
    ("旧项目归档.rmvb", 128_000_000, "排队中"),
    ("屏幕录制 2024-05-02.mp4", 74_000_000, "排队中"),
    ("婚礼跟拍 A机位.m2ts", 4_120_000_000, "已删除源文件"),
]

FOLDERS = [
    r"E:\素材\2024\旅行", r"E:\素材\宣传片", r"E:\素材\会议",
    r"E:\素材\家庭", r"E:\素材\客户", r"E:\素材\航拍",
    r"E:\素材\延时", r"E:\素材\教程", r"E:\素材\教程",
    r"E:\素材\归档", r"E:\素材\录屏", r"E:\素材\婚礼",
]


def inject(window: MainWindow) -> None:
    for index, (name, size, status) in enumerate(NAMES):
        window.on_item_added(index, name, size, FOLDERS[index])
    window._flush_items()
    for index, (name, size, status) in enumerate(NAMES):
        uid = window._index_uids.get(index)
        if uid is not None and status != "排队中":
            window._set_row_status(uid, status, r"E:\素材\视频汇总\%s" % name)
    window.on_progress(9, len(NAMES))
    window.append_log("扫描完成，共找到 12 个视频")
    window._flush_log()
    window._update_stats()
    window._update_status_bar()
    if window.table.model().rowCount():
        window.table.selectRow(0)


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, ".screenshots", "ui.png")
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1400
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 880
    mode = sys.argv[4] if len(sys.argv) > 4 else "light"

    # 截图过程不写用户偏好：关掉 QSettings 落盘的影响，只读不写
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    app = QApplication(sys.argv)
    theme.set_palette(mode)
    theme.apply(app, mode)

    window = MainWindow()
    window._apply_theme(mode, persist=False)
    window.resize(width, height)
    inject(window)
    window.show()
    window.resize(width, height)

    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)

    def shoot():
        window._flush_items()
        pixmap = window.grab()
        pixmap.save(target, "PNG")
        print("saved", target, pixmap.width(), "x", pixmap.height())
        app.quit()

    QTimer.singleShot(700, shoot)
    code = app.exec()
    # 收尾：不触发保存设置，直接退出
    window._closing = True
    return code


if __name__ == "__main__":
    raise SystemExit(main())
