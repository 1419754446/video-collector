#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用入口
========

负责 QApplication 初始化、高 DPI 适配与全局主题装载。
主题由 MainWindow 依据上次保存的偏好决定，这里只做兜底应用。
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from .main_window import APP_NAME, MainWindow
from .theme import apply as apply_theme
from .utils import app_icon

APP_ID = "DoubaoVideoTools.VideoCollector.2"


def _bind_taskbar_identity():
    """
    让 Windows 任务栏按本程序自己的图标和身份分组。

    不设的话，源码运行（python.exe）时任务栏会显示 Python 的图标，
    而且 exe 与源码运行会被当成两个不同的程序。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001  只影响任务栏分组，失败可忽略
        pass


def main():
    """启动应用，返回 Qt 事件循环退出码。"""
    # Qt6 下高 DPI 缩放默认开启；设定小数缩放取整策略，避免 125%/150% 下控件错位发虚
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    _bind_taskbar_identity()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("DoubaoVideoTools")
    # 全局兜底：对话框、消息框等都有图标，不依赖每个窗口各自设置
    app.setWindowIcon(app_icon())
    apply_theme(app, "light")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
