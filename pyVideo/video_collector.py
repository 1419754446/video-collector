#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频集中整理工具（Video Collector）—— 启动入口
================================================

真正的实现已拆分到 `app/` 包：
    app/theme.py        现代浅色主题与色板
    app/utils.py        文件/回收站/去重/缩略图工具
    app/worker.py       扫描、复制、移动的后台线程
    app/widgets.py      拖放输入框、可排序项、统计卡片、状态圆点
    app/main_window.py  主窗口
    app/main.py         QApplication 与主题装配

运行：
    python video_collector.py

依赖：
    PySide6（必需），opencv-python（可选，用于生成视频缩略图）
"""

import sys

from app.main import main


if __name__ == "__main__":
    sys.exit(main())
