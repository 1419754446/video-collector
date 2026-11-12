#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主题层（浅色 / 深色）
====================

设计语言（v2 重设计）：

- **三层表面**：窗口底（浅灰蓝）→ 面板（纯白 / 深灰）→ 强调块。
  侧栏用略微下沉的底色，卡片纯白上浮，靠"明度差 + 1px 细线"分层，
  不用投影，既干净又不拖慢上万行的表格。
- **一套字号阶梯**：标题 20 / 卡片标题 12.5 / 正文 13 / 说明 12 /
  标签 12.5。不再出现 10px 这种在 200% 缩放下也看不清的小字。
- **圆角分级**：大卡片 14、输入与按钮 10、小药丸全圆角。
- **强调色**：靛蓝 #4b5cf0 为主，青 #12b6c9 为辅，只用在主操作、
  进度、选中态；不把整块界面涂成蓝色。
- 深浅色共用同一套结构，只换色值。

对外接口保持向后兼容：
    COLORS       当前生效的色板（就地更新）
    APP_QSS      默认主题的样式表
"""

import os
import tempfile

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication

_CHECK_ICON_PATH = ""

# --------------------------------------------------------------------------- #
# 色板
# --------------------------------------------------------------------------- #
PALETTES = {
    # ---------------------------------------------------------------- 浅色 --
    "light": {
        "name": "浅色",
        # 表面
        "bg": "#eff2f8",
        "bg_2": "#e9edf6",
        "panel": "#f7f9fd",          # 侧栏：比窗口底更亮一档
        "panel_soft": "#f5f8fd",
        "panel_solid": "#ffffff",    # 卡片
        "panel_deep": "#edf1f9",
        "seg_track": "#eff2fa",
        "seg_pill": "#ffffff",
        # 线条
        "border": "#e3e8f1",
        "border_strong": "#ccd4e3",
        "divider": "#e8ecf4",
        # 文字
        "text": "#141a26",
        "text_dim": "#4d5872",
        "text_mute": "#7e8a9e",
        "text_on_accent": "#ffffff",
        # 强调
        "accent": "#4b5cf0",
        "accent_hover": "#3f4edb",
        "accent_soft": "#eceffe",
        "accent_2": "#12b6c9",
        "grad_1": "#5b6cf5",
        "grad_2": "#4b5cf0",
        "grad_3": "#12b6c9",
        "grad_1_h": "#6b7bff",
        "grad_2_h": "#5465f6",
        "grad_3_h": "#2cc6d6",
        # 主按钮专用渐变：深色主题单独压深，保证白字对比度（进度条仍用 grad_*）
        "btn_grad_1": "#5b6cf5",
        "btn_grad_2": "#4b5cf0",
        "btn_grad_3": "#0fa3b4",
        "btn_grad_1_h": "#6b7bff",
        "btn_grad_2_h": "#5465f6",
        "btn_grad_3_h": "#16b6c8",
        "glow": "#4b5cf0",
        "acc_grad_top": "#f4f7ff",
        # 语义
        "success": "#10875f",
        "success_soft": "#e4f5ed",
        "warn": "#9a6408",
        "warn_soft": "#fbf0da",
        "danger": "#c62f47",
        "danger_soft": "#fdecef",
        "muted_soft": "#f1f4fa",
        "track": "#dfe5f1",
        # 交互
        "input_bg": "#ffffff",
        "row_hover": "#f7f9fd",
        "selection": "#e9eeff",
        # 预览舞台（深色，浅色主题下也保持暗，视频画面才不刺眼）
        "stage_bg": "#0d1117",
        "stage_ink": "#8a94a6",
        "stage_text": "#dfe6f2",
        "stage_mute": "#8b97ab",
        # 缩略图占位
        "tile": "#e9eef8",
        "tile_ink": "#9aa6bc",
        "tile_edge": "#dde4f1",
        # 阴影投色（自绘用）
        "shadow": "#0b1220",
        "is_dark": False,
    },
    # ---------------------------------------------------------------- 深色 --
    "dark": {
        "name": "深色",
        # 表面压深一档、卡片提亮一档：原来窗口底 #0b0d12 与卡片 #161a21
        # 只差两级明度，侧栏和主区在深色下糊成一片，看不出"三层表面"。
        "bg": "#06080c",
        "bg_2": "#04060a",
        "panel": "#12161d",
        "panel_soft": "#181d25",
        "panel_solid": "#1c222c",
        "panel_deep": "#0b0e13",
        "seg_track": "#0c0f14",
        "seg_pill": "#2b323f",
        "border": "#2b323f",
        "border_strong": "#3b4451",
        "divider": "#242b36",
        "text": "#e9edf5",
        "text_dim": "#a8b3c6",
        "text_mute": "#76829a",
        "text_on_accent": "#ffffff",
        "accent": "#8b9bff",
        "accent_hover": "#a1afff",
        "accent_soft": "#1e2540",
        "accent_2": "#35d6e6",
        "grad_1": "#9aa6ff",
        "grad_2": "#8b9bff",
        "grad_3": "#35d6e6",
        "grad_1_h": "#aab4ff",
        "grad_2_h": "#9fabff",
        "grad_3_h": "#54e0ee",
        # 主按钮专用渐变：深色下用饱和的品牌蓝，白字才压得住
        "btn_grad_1": "#5566f0",
        "btn_grad_2": "#4655e6",
        "btn_grad_3": "#0e9db0",
        "btn_grad_1_h": "#6273fa",
        "btn_grad_2_h": "#5262f0",
        "btn_grad_3_h": "#14adc2",
        "glow": "#8b9bff",
        "acc_grad_top": "#161b24",
        "success": "#46d29c",
        "success_soft": "#15271f",
        "warn": "#e8bd63",
        "warn_soft": "#2a2417",
        "danger": "#ff7f94",
        "danger_soft": "#2b171c",
        "muted_soft": "#252c37",
        "track": "#2a3240",
        "input_bg": "#141920",
        "row_hover": "#252c37",
        "selection": "#2b3654",
        "stage_bg": "#05070a",
        "stage_ink": "#6c7688",
        "stage_text": "#c9d3e2",
        "stage_mute": "#7a8598",
        "tile": "#232a35",
        "tile_ink": "#75818f",
        "tile_edge": "#2e3644",
        "shadow": "#000000",
        "is_dark": True,
    },
}

CURRENT = "light"
COLORS = dict(PALETTES[CURRENT])


def set_palette(name):
    """切换当前色板（就地更新 COLORS，保证 import 过的模块看到新值）。"""
    global CURRENT
    if name not in PALETTES:
        name = "light"
    CURRENT = name
    COLORS.clear()
    COLORS.update(PALETTES[name])
    return COLORS


def hex_of(key):
    """取色值字符串（QSS 用）。"""
    return COLORS.get(key, "#000000")


def qcolor(key, fallback=None):
    """取 QColor。"""
    value = COLORS.get(key)
    if isinstance(value, tuple):
        return QColor(*value)
    if isinstance(value, str) and value:
        color = QColor(value)
        if color.isValid():
            return color
    return QColor(fallback or "#000000")


# --------------------------------------------------------------------------- #
# QSS 模板
# --------------------------------------------------------------------------- #
_QSS_TEMPLATE = """
/* ============================ 基础 ============================ */
* { outline: none; }
QWidget {
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    font-weight: 500;
    color: %(text)s;
}
QMainWindow, #AppRoot, #MainArea, #TopBar, #StatsRow, #ScrollBody,
#SidebarScroll, #ProgressHost {
    background: transparent;
}
QToolTip {
    background-color: %(panel_solid)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 12px;
}

/* ============================ 侧栏 ============================ */
#Sidebar {
    background-color: %(panel_solid)s;
    border-right: 1px solid %(border)s;
}
#BrandTitle {
    font-size: 17px;
    font-weight: 700;
    color: %(text)s;
}
#BrandSub {
    font-size: 11.5px;
    font-weight: 500;
    color: %(text_mute)s;
}

/* ============================ 卡片 ============================ */
#Card, #TableCard, #LogCard {
    background-color: %(panel_solid)s;
    border: 1px solid %(border)s;
    border-radius: 14px;
}
/* 侧栏里的分组不做成卡片：白面板内再套白卡片会糊成一片，
   改用"分组标题 + 细线分隔"，层次靠留白和字重建立。 */
#CardGhost {
    background: transparent;
    border: none;
    border-radius: 0;
}
#CardTitle, QLabel[role="card-title"] {
    color: %(text_mute)s;
    font-size: 12px;
    font-weight: 600;
}
QLabel[role="field-label"] { color: %(text_dim)s; font-size: 12.5px; font-weight: 600; }
QLabel[role="hint"] { color: %(text_mute)s; font-size: 12px; font-weight: 500; }
QLabel[role="section"] { color: %(text_mute)s; font-size: 12px; font-weight: 600; }
QLabel[role="title"] { color: %(text)s; font-size: 20px; font-weight: 700; }
QLabel[role="subtitle"] { color: %(text_dim)s; font-size: 12.5px; font-weight: 600; }
QLabel[role="muted"] { color: %(text_mute)s; font-size: 12px; font-weight: 500; }
QLabel[role="value"] { color: %(text)s; font-size: 13px; font-weight: 600; }
QLabel[role="caption"] { color: %(text_mute)s; font-size: 12px; font-weight: 500; }
QLabel[role="danger"] { color: %(danger)s; font-size: 12.5px; font-weight: 500; }
QLabel[role="warn"] { color: %(warn)s; font-size: 12.5px; font-weight: 500; }

/* ============================ 输入 ============================ */
QLineEdit {
    background-color: %(input_bg)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
    padding: 7px 11px;
    color: %(text)s;
    font-size: 13px;
    selection-background-color: %(accent)s;
    selection-color: %(text_on_accent)s;
}
QLineEdit:hover { border-color: %(border_strong)s; }
QLineEdit:focus { border-color: %(accent)s; background-color: %(input_bg)s; }
QLineEdit:disabled { color: %(text_mute)s; background-color: %(panel_deep)s; }

/* 输入框容器：图标 + 无边框输入 + 内置图标按钮 */
#InputGroup {
    background-color: %(input_bg)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
}
#InputGroup:hover { border-color: %(border_strong)s; }
#InputGroup[focused="true"] {
    border-color: %(accent)s;
    background-color: %(panel_solid)s;
}
#InputGroup QLineEdit {
    background: transparent;
    border: none;
    padding: 0;
    border-radius: 0;
}

/* 搜索框 */
#SearchBox {
    background-color: %(input_bg)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
}
#SearchBox[focused="true"] { border-color: %(accent)s; }
#SearchBox QLineEdit {
    background: transparent;
    border: none;
    padding: 0;
    border-radius: 0;
}
#Kbd {
    color: %(text_mute)s;
    background-color: %(muted_soft)s;
    border: 1px solid %(divider)s;
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 10.5px;
    font-family: Consolas, "Cascadia Mono", monospace;
}

/* 下拉选择 */
QComboBox {
    background-color: %(input_bg)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
    padding: 4px 24px 4px 10px;
    color: %(text)s;
    font-size: 13px;
}
QComboBox:hover { border-color: %(border_strong)s; }
QComboBox:focus { border-color: %(accent)s; }
QComboBox:disabled { color: %(text_mute)s; background-color: transparent; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow { image: none; width: 0; height: 0; }
QComboBox QAbstractItemView {
    background-color: %(panel_solid)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 10px;
    padding: 5px;
    outline: none;
    selection-background-color: %(accent_soft)s;
    selection-color: %(text)s;
}
QComboBox QAbstractItemView::item {
    min-height: 26px;
    padding: 2px 8px;
    border-radius: 6px;
}

/* ============================ 按钮 ============================ */
QPushButton {
    background-color: %(panel_solid)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
    padding: 6px 14px;
    font-size: 13px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: %(row_hover)s;
    border-color: %(border_strong)s;
}
QPushButton:pressed { background-color: %(panel_deep)s; }
QPushButton:disabled {
    color: %(text_mute)s;
    background-color: transparent;
    border-color: %(border)s;
}
QPushButton:focus { border-color: %(accent)s; }

/* 主操作：品牌渐变，不用 3D 底边 */
QPushButton[role="primary"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 %(btn_grad_1)s, stop:0.62 %(btn_grad_2)s, stop:1 %(btn_grad_3)s);
    border: none;
    color: %(text_on_accent)s;
    font-size: 13px;
    font-weight: 700;
    padding: 0 18px;
}
QPushButton[role="primary"]:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 %(btn_grad_1_h)s, stop:0.62 %(btn_grad_2_h)s, stop:1 %(btn_grad_3_h)s);
}
QPushButton[role="primary"]:pressed {
    background: %(accent_hover)s;
}
QPushButton[role="primary"]:disabled {
    background: %(muted_soft)s;
    color: %(text_mute)s;
}

QPushButton[role="ghost"] {
    background: transparent;
    border: 1px solid transparent;
    color: %(text_dim)s;
}
QPushButton[role="ghost"]:hover {
    color: %(text)s;
    background-color: %(panel_deep)s;
    border-color: transparent;
}
QPushButton[role="ghost"]:disabled { color: %(text_mute)s; background: transparent; }

QPushButton[role="danger"] {
    color: %(danger)s;
    background-color: transparent;
    border: 1px solid %(border)s;
}
QPushButton[role="danger"]:hover {
    border-color: %(danger)s;
    background-color: %(danger_soft)s;
}
QPushButton[role="danger"]:disabled {
    color: %(text_mute)s;
    background: transparent;
    border-color: %(border)s;
}

QPushButton[role="icon"] {
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0;
}
QPushButton[role="icon"]:hover { background-color: %(panel_deep)s; }
QPushButton[role="icon"]:pressed { background-color: %(accent_soft)s; }

QPushButton[role="link"] {
    background: transparent;
    border: none;
    color: %(text_mute)s;
    font-size: 12px;
    padding: 3px 6px;
}
QPushButton[role="link"]:hover { color: %(accent)s; }

/* ============================ 表格 ============================ */
/* QTableView 是 Model/View 版的实际控件；QTableWidget 一并保留，
   便于旧控件/设计稿仍能吃到同一套样式 */
QTableView, QTableWidget {
    background: transparent;
    border: none;
    gridline-color: transparent;
    selection-background-color: transparent;
    font-size: 12.5px;
    font-weight: 500;
}
QTableView::item, QTableWidget::item {
    border: none;
    padding: 0 10px;
    color: %(text_dim)s;
}
/* 视图底座也要透明，否则深色主题下未选中行会露出系统白色底 */
QTableView QAbstractScrollArea, QTableView > QWidget {
    background: transparent;
}
QHeaderView { background: transparent; border: none; }
QHeaderView::section {
    background-color: transparent;
    color: %(text_mute)s;
    border: none;
    border-bottom: 1px solid %(divider)s;
    padding: 9px 10px;
    font-size: 12px;
    font-weight: 600;
}
QTableCornerButton::section { background: transparent; border: none; }

/* ============================ 日志 ============================ */
QPlainTextEdit {
    background: transparent;
    border: none;
    color: %(text_dim)s;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
    font-weight: 500;
    selection-background-color: %(accent)s;
    selection-color: %(text_on_accent)s;
}

/* ============================ 分隔 ============================ */
/* QSplitter 的手柄默认用系统浅色（#f0f0f0）绘制，深色主题下会变成一条刺眼的白线。
   这里显式给它主题色，并把宽度钉成 1px —— 只写 background-color 时 Qt 会回退到
   样式的 PM_SplitterWidth（5~6px），深色下就是一道 5px 的灰带。 */
QSplitter { background: transparent; }
QSplitter::handle { background-color: %(border)s; width: 1px; height: 1px; }
QSplitter::handle:hover { background-color: %(accent)s; }

/* ============================ 预览 ============================ */
/* 分隔由 QSplitter 手柄负责，面板自己不再重复画左边线，避免两条线并排 */
#PreviewPanel {
    background-color: %(panel)s;
    border-left: none;
}
#VideoStage {
    background-color: %(stage_bg)s;
    border: 1px solid %(border)s;
    border-radius: 12px;
}
#VideoStage #VideoPlaceholder { background-color: transparent; }
QLabel#PvName { color: %(text)s; font-size: 13.5px; font-weight: 600; }
#PvMeta { color: %(text_dim)s; font-size: 12px; font-weight: 500; }
#PvPath { color: %(text_mute)s; font-size: 12px; font-weight: 500; }
#PvPath:hover { color: %(accent)s; }
#PvState { color: %(warn)s; font-size: 12px; }
#PvTip { color: %(text_mute)s; font-size: 11.5px; font-weight: 500; }

/* ============================ 滑块 ============================ */
QSlider::groove:horizontal {
    height: 4px;
    /* 两端各留 7px，手柄滑到最外侧时不会被裁掉半边 */
    margin: 0 7px;
    background: %(panel_deep)s;
    border: none;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background-color: %(accent)s;
    border-radius: 2px;
}
QSlider::add-page:horizontal { background: transparent; }
QSlider::handle:horizontal {
    background: %(panel_solid)s;
    border: 2px solid %(accent)s;
    width: 12px;
    height: 12px;
    margin: -5px -7px;
    border-radius: 7px;
}
QSlider::handle:horizontal:hover { background-color: %(accent_soft)s; }
QSlider::handle:horizontal:pressed {
    background-color: %(accent)s;
    width: 14px;
    height: 14px;
    margin: -6px -7px;
    border-radius: 8px;
}

/* ============================ 滚动条 ============================ */
QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 1px; }
QScrollBar::handle:vertical {
    background: %(border)s;
    border-radius: 3px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: %(border_strong)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

/* 侧栏在 1280×720 这类最小窗口尺寸下装不下全部选项，需要滚动；通用滚动条 6px +
   border 色在浅色侧栏上几乎看不见，这里单独加粗并加深，让人知道下面还有内容 */
#SidebarScroll QScrollBar:vertical { width: 8px; margin: 2px 0 2px 1px; }
#SidebarScroll QScrollBar::handle:vertical {
    background: %(border_strong)s;
    border-radius: 4px;
    min-height: 40px;
}
#SidebarScroll QScrollBar::handle:vertical:hover { background: %(text_mute)s; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 1px 2px; }
QScrollBar::handle:horizontal {
    background: %(border_strong)s;
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: %(text_mute)s; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

/* ============================ 菜单 ============================ */
QMenu {
    background-color: %(panel_solid)s;
    border: 1px solid %(border_strong)s;
    border-radius: 10px;
    padding: 5px;
}
QMenu::item {
    padding: 7px 24px 7px 13px;
    border-radius: 6px;
    color: %(text_dim)s;
    font-size: 13px;
}
QMenu::item:selected { background-color: %(accent_soft)s; color: %(text)s; }
QMenu::item:disabled { color: %(text_mute)s; }
QMenu::separator { height: 1px; background: %(divider)s; margin: 4px 6px; }

/* ============================ 复选框 ============================ */
QCheckBox { color: %(text_dim)s; font-size: 13px; spacing: 8px; }
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 5px;
    border: 1px solid %(border_strong)s;
    background: %(input_bg)s;
}
QCheckBox::indicator:checked {
    background-color: %(accent)s;
    border-color: %(accent)s;
}
QCheckBox::indicator:hover { border-color: %(accent)s; }

/* ============================ 对话框 ============================ */
QMessageBox { background-color: %(panel_solid)s; }
QMessageBox QLabel { color: %(text)s; font-size: 13px; }
"""


def _check_icon_path():
    """画一张白色对勾存成 PNG 给 QSS 用。

    QSS 的 `::indicator:checked` 只能贴图片、画不了对勾，只写 background
    的话勾选后就是一块纯色方块（用户反馈的"像素块"）。表格里的勾选框走
    widgets.paint_check_box 自绘，这里只服务剩下那几处原生 QCheckBox。
    """
    global _CHECK_ICON_PATH
    if _CHECK_ICON_PATH and os.path.exists(_CHECK_ICON_PATH):
        return _CHECK_ICON_PATH
    app = QApplication.instance()
    if app is None:
        return ""
    path = os.path.join(tempfile.gettempdir(), "vc_qss_check.png")
    pix = QPixmap(16, 16)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor("#FFFFFF"), 2.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    path_pts = QPainterPath(QPointF(3.0, 8.4))
    path_pts.lineTo(QPointF(6.6, 12.0))
    path_pts.lineTo(QPointF(13.0, 4.4))
    painter.drawPath(path_pts)
    painter.end()
    if not pix.save(path, "PNG"):
        return ""
    _CHECK_ICON_PATH = path
    return path


def build_qss():
    """按当前色板生成完整样式表。"""
    qss = _QSS_TEMPLATE % COLORS
    icon = _check_icon_path().replace("\\", "/")
    if icon:
        # 追加在最末，覆盖前面只有底色的那条规则；没有图片时不加，
        # 免得写出一条 url() 为空的非法样式
        qss += "\nQCheckBox::indicator:checked { image: url(%s); }\n" % icon
    return qss


APP_QSS = build_qss()


def apply(app, name="light"):
    """切换主题并应用样式表，返回实际生效的主题名。"""
    set_palette(name)
    app.setStyleSheet(build_qss())
    return CURRENT
