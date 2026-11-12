#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
整理设置向导（四步）+ 扩展名勾选控件
====================================

与 C++ 版 ``ui/index.html`` 的 ``#setupMask`` 一一对应：

1. 搜索方式 —— 指定目录 / 全盘搜索；只有「指定目录」才需要源目录
2. 格式与大小 —— 扩展名勾选面板 + 体积范围
3. 操作方式 —— 复制 / 移动、重名处理
4. 开关 —— 含子文件夹 / 跳过隐藏 / 跳过相同 / 完成后打开

设计约束（与侧栏「当前设置」摘要配套）：

* 向导里的控件就是配置的**唯一真源**，侧栏只渲染只读摘要；
* 对话框只 hide 不销毁，值一直留在控件里，不存在"两处各存一份"；
* 主窗口把这里的控件引用别名为 ``self.src_edit`` 等原有名字，
  下游（worker 传参、QSettings 读写、运行态禁用）完全不用改。
"""

import os
import tempfile

from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .constants import DEFAULT_EXTS, EXT_POOL
from .theme import COLORS

_CHECK_ICON_PATH = ""


def _check_icon_path():
    """QSS 的 indicator:checked 只吃图片，这里画一张白色对勾存成 PNG 复用。"""
    global _CHECK_ICON_PATH
    if _CHECK_ICON_PATH and os.path.exists(_CHECK_ICON_PATH):
        return _CHECK_ICON_PATH
    # QPixmap 必须有 QGuiApplication 才存在；没有实例时直接放弃（反正也没人用）
    if QApplication.instance() is None:
        return ""
    path = os.path.join(tempfile.gettempdir(), "vc_ext_check.png")
    pix = QPixmap(16, 16)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor("#ffffff"), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawPolyline(QPolygonF([QPointF(3.5, 8.6), QPointF(6.6, 11.6), QPointF(12.4, 4.6)]))
    painter.end()
    pix.save(path, "PNG")
    _CHECK_ICON_PATH = path
    return path
from .widgets import (
    IconButton,
    InputGroup,
    SegmentedControl,
    SwitchRow,
    line_icon,
)

STEP_TITLES = ("① 搜索方式", "② 格式与大小", "③ 操作方式", "④ 开关")


def _repolish(widget):
    """动态属性改了之后要让 Qt 重新算一遍样式，否则选择器不生效。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


# --------------------------------------------------------------------------- #
# 步骤条 / 选择卡
# --------------------------------------------------------------------------- #
class StepChip(QLabel):
    """向导顶部的一枚步骤胶囊：active / done / idle 三种状态。"""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setObjectName("StepChip")
        self.setAlignment(Qt.AlignCenter)
        self.set_state("idle")

    def set_state(self, state):
        if self.property("state") == state:
            return
        self.setProperty("state", state)
        _repolish(self)


class ChoiceCard(QFrame):
    """第 1 步的「指定目录 / 全盘搜索」选择卡：整块可点，单选。"""

    clicked = Signal(str)

    def __init__(self, value, title, subtitle, parent=None):
        super().__init__(parent)
        self.setObjectName("ChoiceCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self._value = value
        self._on = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("ChoiceTitle")
        self.sub_label = QLabel(subtitle, self)
        self.sub_label.setObjectName("ChoiceSub")
        self.sub_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.sub_label)

    def value(self):
        return self._value

    def set_active(self, active):
        active = bool(active)
        if active == self._on:
            return
        self._on = active
        self.setProperty("on", "true" if active else "false")
        _repolish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._value)
        super().mousePressEvent(event)


# --------------------------------------------------------------------------- #
# 扩展名勾选面板
# --------------------------------------------------------------------------- #
class ExtPicker(QWidget):
    """一个按钮 + 弹出勾选面板，替代原来那行长文本框。

    对外仍然提供 ``text() / setText() / textChanged`` 这套 QLineEdit 式接口，
    主窗口 ``_parse_exts()`` 那套解析逻辑可以原样复用。
    """

    textChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._exts = []
        self._custom = []      # 不在 EXT_POOL 里的自定义项，单独记一份用于回填输入框
        self._pop = None
        self._boxes = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.btn = QPushButton("—", self)
        self.btn.setObjectName("ExtBtn")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setFixedHeight(36)
        self.btn.clicked.connect(self._toggle_popup)
        layout.addWidget(self.btn)

        self.set_exts(DEFAULT_EXTS.split(","))

    # -- QLineEdit 兼容接口 --------------------------------------------- #
    def text(self):
        return ",".join(self._exts)

    def setText(self, text):
        self.set_exts(self._split(text))

    def setCursorPosition(self, _pos):
        """文本框时代的老接口，保持存在即可（不需要光标概念）。"""

    def setToolTip(self, text):
        # 主窗口把完整清单的提示打在 ext_edit 上，真正需要显示的是那颗按钮
        super().setToolTip(text)
        if getattr(self, "btn", None) is not None:
            self.btn.setToolTip(text)

    @staticmethod
    def _split(text):
        items = []
        for token in (text or "").replace("，", ",").replace(";", ",").split(","):
            ext = token.strip().lstrip(".").lower()
            if ext and ext not in items:
                items.append(ext)
        return items

    # -- 状态 ------------------------------------------------------------ #
    def set_exts(self, exts):
        clean = []
        for ext in exts:
            ext = str(ext).strip().lstrip(".").lower()
            if ext and ext not in clean:
                clean.append(ext)
        self._exts = clean
        self._custom = [e for e in clean if e not in EXT_POOL]
        self._sync_boxes()
        self._refresh_label()
        self.textChanged.emit()

    def _refresh_label(self):
        if not self._exts:
            self.btn.setText("未选择 · 点开勾选格式  ▾")
            self.btn.setToolTip("至少勾选一种扩展名，否则无法开始搜索")
            return
        if len(self._exts) <= 5:
            summary = " · ".join(self._exts)
        else:
            summary = "{0} 等 {1} 种".format(" · ".join(self._exts[:4]), len(self._exts))
        # 末尾的小三角让人一眼看出这是"点开勾选"而不是只读文本
        self.btn.setText(summary + "  ▾")
        self.btn.setToolTip("当前 {0} 个扩展名：{1}".format(
            len(self._exts), ", ".join(self._exts)))

    def _sync_boxes(self):
        for ext, box in self._boxes.items():
            box.blockSignals(True)
            box.setChecked(ext in self._exts)
            box.blockSignals(False)

    def _toggle_ext(self, ext, checked):
        if checked:
            if ext not in self._exts:
                self._exts.append(ext)
        else:
            self._exts = [e for e in self._exts if e != ext]
        self._custom = [e for e in self._exts if e not in EXT_POOL]
        self._refresh_label()
        self.textChanged.emit()

    # -- 弹出面板 -------------------------------------------------------- #
    def _ensure_popup(self):
        if self._pop is not None:
            return
        pop = QFrame(self, Qt.Popup)
        pop.setObjectName("ExtPop")
        pop.setAttribute(Qt.WA_StyledBackground, True)
        pop.setFixedWidth(452)

        outer = QVBoxLayout(pop)
        outer.setContentsMargins(13, 11, 13, 12)
        outer.setSpacing(9)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(4)
        title = QLabel("常用格式", pop)
        title.setProperty("role", "field-label")
        head.addWidget(title)
        head.addStretch(1)
        for text, slot in (("全选", self._select_all),
                           ("清空", self._clear),
                           ("恢复默认", self._reset)):
            link = QPushButton(text, pop)
            link.setProperty("role", "link")
            link.setCursor(Qt.PointingHandCursor)
            link.clicked.connect(slot)
            head.addWidget(link)
        outer.addLayout(head)

        grid_host = QWidget(pop)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        for index, ext in enumerate(EXT_POOL):
            box = QCheckBox(ext, grid_host)
            box.setCursor(Qt.PointingHandCursor)
            box.toggled.connect(lambda checked, e=ext: self._toggle_ext(e, checked))
            self._boxes[ext] = box
            grid.addWidget(box, index // 4, index % 4)
        outer.addWidget(grid_host)

        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(0, 0, 0, 0)
        custom_row.setSpacing(6)
        self._custom_edit = QLineEdit(pop)
        self._custom_edit.setPlaceholderText("其它扩展名，逗号分隔，如 mxf,dat")
        self._custom_edit.setFixedHeight(32)
        self._custom_edit.setText(",".join(self._custom))
        self._custom_edit.returnPressed.connect(self._apply_custom)
        custom_row.addWidget(self._custom_edit, 1)
        add_btn = QPushButton("添加", pop)
        add_btn.setProperty("role", "ghost")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setFixedHeight(32)
        add_btn.clicked.connect(self._apply_custom)
        custom_row.addWidget(add_btn)
        outer.addLayout(custom_row)

        hint = QLabel("勾选即生效，关掉面板也会保留；自定义项用「清空」可一并移除。", pop)
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self._pop = pop
        self._sync_boxes()
        # 面板是懒创建的，创建完必须立刻套一次主题样式（否则露出系统默认控件）
        self.apply_theme()

    def _toggle_popup(self):
        if not self.isEnabled():
            return
        self._ensure_popup()
        if self._pop.isVisible():
            self._pop.hide()
            return
        self._custom_edit.setText(",".join(self._custom))
        self._sync_boxes()
        self._pop.adjustSize()
        pos = self.btn.mapToGlobal(QPoint(0, self.btn.height() + 6))
        # 面板比按钮宽，靠右对齐避免超出对话框边界
        pos.setX(pos.x() + self.btn.width() - self._pop.width())
        self._pop.move(pos)
        self._pop.show()
        self._custom_edit.setFocus(Qt.PopupFocusReason)

    def hide_popup(self):
        if self._pop is not None and self._pop.isVisible():
            self._pop.hide()

    def _apply_custom(self):
        if self._pop is None:
            return
        parsed = self._split(self._custom_edit.text())
        # 先移除旧的自定义项，再加入新解析出来的
        keep = [e for e in self._exts if e not in self._custom]
        added = []
        for ext in parsed:
            if ext not in keep and ext not in added:
                added.append(ext)
        self._exts = keep + added
        self._custom = [e for e in self._exts if e not in EXT_POOL]
        self._sync_boxes()
        self._refresh_label()
        self.textChanged.emit()

    def _select_all(self):
        self._exts = list(EXT_POOL) + list(self._custom)
        self._sync_boxes()
        self._refresh_label()
        self.textChanged.emit()

    def _clear(self):
        self._exts = []
        self._custom = []
        self._custom_edit.setText("")
        self._sync_boxes()
        self._refresh_label()
        self.textChanged.emit()

    def _reset(self):
        self._exts = self._split(DEFAULT_EXTS)
        self._custom = [e for e in self._exts if e not in EXT_POOL]
        self._custom_edit.setText(",".join(self._custom))
        self._sync_boxes()
        self._refresh_label()
        self.textChanged.emit()

    def apply_theme(self):
        if self._pop is not None:
            self._pop.setStyleSheet(self._popup_style())

    def _popup_style(self):
        c = COLORS
        check = _check_icon_path().replace("\\", "/")
        return (
            "#ExtPop {{ background: {panel}; border: 1px solid {border};"
            " border-radius: 12px; }}"
            "QCheckBox {{ color: {dim}; font-size: 12px; spacing: 6px; }}"
            "QCheckBox:hover {{ color: {text}; }}"
            "QCheckBox::indicator {{ width: 15px; height: 15px;"
            " border-radius: 4px; border: 1px solid {border};"
            " background: {input}; }}"
            "QCheckBox::indicator:hover {{ border-color: {accent}; }}"
            "QCheckBox::indicator:checked {{ background: {accent};"
            " border-color: {accent}; image: url({check}); }}"
            "QLineEdit {{ background: {input}; border: 1px solid {border};"
            " border-radius: 8px; padding: 0 8px; color: {text}; font-size: 12px; }}"
            "QLineEdit:focus {{ border-color: {accent}; }}"
            "QPushButton[role=\"link\"] {{ color: {dim}; font-size: 11.5px;"
            " border: none; padding: 2px 4px; background: transparent; }}"
            "QPushButton[role=\"link\"]:hover {{ color: {accent}; }}"
            "QPushButton[role=\"ghost\"] {{ color: {dim}; font-size: 12px;"
            " border: 1px solid {border}; border-radius: 8px; padding: 0 10px;"
            " background: transparent; }}"
            "QPushButton[role=\"ghost\"]:hover {{ border-color: {accent};"
            " color: {text}; }}"
            "QLabel[role=\"hint\"] {{ color: {mute}; font-size: 11px; }}"
            "QLabel[role=\"field-label\"] {{ color: {dim}; font-size: 12px;"
            " font-weight: 600; }}"
        ).format(panel=c["panel_solid"], border=c["border"], dim=c["text_dim"],
                 text=c["text"], input=c["input_bg"], accent=c["accent"],
                 mute=c["text_mute"], check=check)


# --------------------------------------------------------------------------- #
# 向导对话框
# --------------------------------------------------------------------------- #
class SetupDialog(QDialog):
    closed = Signal()
    search_mode_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._search_mode = "dir"
        self._step = 0
        self._alive_exts = []          # 保持引用，防止被 GC 掉（信号源都在里面）

        self._build()
        self.apply_theme()
        self._show_step(0)
        self.set_search_mode("dir")
        self.setMinimumWidth(560)

    # -- 搭建 ------------------------------------------------------------ #
    def _build(self):
        shell = QVBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("SetupCard")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setColor(QColor(0, 0, 0, 110))
        shadow.setOffset(0, 10)
        self.card.setGraphicsEffect(shadow)
        shell.addWidget(self.card)

        root = QVBoxLayout(self.card)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # 头部：标题 + 步骤条 + 关闭
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("整理设置", self.card)
        title.setObjectName("SetupTitle")
        head.addWidget(title)
        head.addSpacing(6)

        self._chips = []
        for text in STEP_TITLES:
            chip = StepChip(text, self.card)
            self._chips.append(chip)
            head.addWidget(chip)
        head.addStretch(1)

        self.btn_close = IconButton("close", "text_dim", 14, 26, self.card, "关闭（保留已选设置）")
        head.addWidget(self.btn_close)
        root.addLayout(head)

        self.stack = QStackedWidget(self.card)
        self.stack.addWidget(self._build_page_search())
        self.stack.addWidget(self._build_page_size())
        self.stack.addWidget(self._build_page_mode())
        self.stack.addWidget(self._build_page_switches())
        root.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(8)
        self.btn_prev = QPushButton("上一步", self.card)
        self.btn_prev.setProperty("role", "ghost")
        self.btn_prev.setFixedHeight(36)
        self.btn_prev.setCursor(Qt.PointingHandCursor)
        nav.addWidget(self.btn_prev)
        nav.addStretch(1)
        self.btn_next = QPushButton("下一步", self.card)
        self.btn_next.setProperty("role", "primary")
        self.btn_next.setFixedHeight(36)
        self.btn_next.setMinimumWidth(104)
        self.btn_next.setCursor(Qt.PointingHandCursor)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)

        self.btn_close.clicked.connect(self.close)
        self.btn_prev.clicked.connect(lambda: self._show_step(max(0, self._step - 1)))
        self.btn_next.clicked.connect(self._on_next)

    def _page(self):
        page = QWidget(self.card)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(9)
        return page, layout

    @staticmethod
    def _field_label(parent, text, hint=None):
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)
        label = QLabel(text, row)
        label.setProperty("role", "field-label")
        layout.addWidget(label)
        if hint:
            hint_label = QLabel(hint, row)
            hint_label.setProperty("role", "hint")
            layout.addWidget(hint_label)
        layout.addStretch(1)
        return row

    def _build_page_search(self):
        page, layout = self._page()

        layout.addWidget(self._field_label(page, "从哪里找视频"))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.choice_dir = ChoiceCard("dir", "指定目录", "扫描源目录（可递归）", page)
        self.choice_all = ChoiceCard("all", "全盘搜索", "只读扫描，可勾选磁盘", page)
        for card in (self.choice_dir, self.choice_all):
            card.clicked.connect(self._on_choice)
            row.addWidget(card, 1)
        layout.addLayout(row)


        # 源目录：全盘搜索时整块隐藏
        self._src_row = self._field_label(page, "源目录", "全盘搜索时不需要")
        self.btn_swap_dirs = IconButton("swap", "text_dim", 15, 26, page,
                                        "交换源目录与目标文件夹")
        self._src_row.layout().addWidget(self.btn_swap_dirs)
        layout.addWidget(self._src_row)

        self.src_group = InputGroup("选择或拖入文件夹…", "folder", page)
        self.src_edit = self.src_group.edit
        self.btn_src_history = IconButton("history", "text_dim", 14, 28, page,
                                          "最近使用过的源目录")
        self.src_group.add_button(self.btn_src_history)
        self.btn_src = IconButton("folder", "text_dim", 16, 28, page, "浏览源目录")
        self.src_group.add_button(self.btn_src)
        layout.addWidget(self.src_group)

        # 目标文件夹：两种搜索方式都要用
        layout.addWidget(self._field_label(page, "目标文件夹", "集中存放"))
        self.dst_group = InputGroup("选择一个已存在或新建的文件夹…", "folder", page)
        self.dst_edit = self.dst_group.edit
        self.btn_dst_history = IconButton("history", "text_dim", 14, 28, page,
                                          "最近使用过的目标文件夹")
        self.dst_group.add_button(self.btn_dst_history)
        self.btn_dst = IconButton("folder", "text_dim", 16, 28, page, "浏览目标文件夹")
        self.dst_group.add_button(self.btn_dst)
        layout.addWidget(self.dst_group)

        layout.addStretch(1)
        return page

    def _build_page_size(self):
        page, layout = self._page()

        layout.addWidget(self._field_label(page, "要哪些格式", "点开勾选，勾选即生效"))
        self.ext_picker = ExtPicker(page)
        self.ext_picker.textChanged.connect(self._on_change)
        layout.addWidget(self.ext_picker)

        layout.addSpacing(4)
        size_head = self._field_label(page, "体积范围", "单位 MB · 留空或 0 表示不限")
        layout.addWidget(size_head)

        size_row = QWidget(page)
        size_layout = QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(6)
        self.min_size_group = InputGroup("最小", None, size_row)
        self.min_size = self.min_size_group.edit
        self.min_size.setToolTip("小于这个体积的视频会被跳过；留空表示不限（单位 MB）")
        size_layout.addWidget(self.min_size_group, 1)
        dash = QLabel("—", size_row)
        dash.setProperty("role", "hint")
        dash.setAlignment(Qt.AlignCenter)
        dash.setFixedWidth(10)
        size_layout.addWidget(dash)
        self.max_size_group = InputGroup("最大", None, size_row)
        self.max_size = self.max_size_group.edit
        self.max_size.setToolTip("大于这个体积的视频会被跳过；留空表示不限（单位 MB）")
        size_layout.addWidget(self.max_size_group, 1)
        unit = QLabel("MB", size_row)
        unit.setProperty("role", "hint")
        unit.setFixedWidth(22)
        size_layout.addWidget(unit)
        layout.addWidget(size_row)

        note = QLabel("体积按文件的字节大小判断，读不到大小的文件不会被这条规则误伤。", page)
        note.setProperty("role", "hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch(1)
        return page

    def _build_page_mode(self):
        page, layout = self._page()

        layout.addWidget(self._field_label(page, "操作方式"))
        self.mode_seg = SegmentedControl([
            {"value": "copy", "title": "复制", "subtitle": "保留原文件"},
            {"value": "move", "title": "移动", "subtitle": "原文件移走"},
        ], page)
        self.mode_seg.changed.connect(self._on_change)
        layout.addWidget(self.mode_seg)

        layout.addSpacing(6)
        layout.addWidget(self._field_label(page, "文件重名时"))
        self.conflict_seg = SegmentedControl([
            {"value": "rename", "title": "自动重命名", "subtitle": "xxx (1).mp4"},
            {"value": "skip", "title": "同名跳过", "subtitle": "不覆盖已有"},
        ], page)
        self.conflict_seg.changed.connect(self._on_change)
        layout.addWidget(self.conflict_seg)

        note = QLabel("移动模式会把源文件从原位置移走（相当于剪切），开始整理前还会再确认一次。", page)
        note.setProperty("role", "hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch(1)
        return page

    def _build_page_switches(self):
        page, layout = self._page()

        layout.addWidget(self._field_label(page, "整理开关"))
        self.sw_recursive = SwitchRow("包含子文件夹", "递归扫描全部层级", True, page)
        self.sw_hidden = SwitchRow("跳过隐藏项", "忽略系统属性文件", True, page)
        self.sw_dedupe = SwitchRow("跳过相同视频", "按大小 + 首/中/尾内容指纹比对", True, page)
        self.sw_auto_open = SwitchRow("完成后打开目标", "任务结束自动打开目标文件夹", False, page)
        for switch in (self.sw_recursive, self.sw_hidden, self.sw_dedupe, self.sw_auto_open):
            switch.toggled.connect(self._on_change)
            layout.addWidget(switch)

        layout.addStretch(1)
        return page

    # -- 状态 ------------------------------------------------------------ #
    def search_mode(self):
        return self._search_mode

    def set_search_mode(self, mode):
        mode = "all" if str(mode or "").lower() == "all" else "dir"
        self._search_mode = mode
        self.choice_dir.set_active(mode == "dir")
        self.choice_all.set_active(mode == "all")
        # 全盘搜索不需要源目录：C++ 版同样是整块隐藏
        self._src_row.setVisible(mode == "dir")
        self.src_group.setVisible(mode == "dir")

    def _on_choice(self, mode):
        if self._worker_running():
            return
        self.set_search_mode(mode)
        self.search_mode_changed.emit(self._search_mode)

    def _worker_running(self):
        owner = self.parent()
        checker = getattr(owner, "_worker_busy", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001
                return False
        return False

    def _on_change(self, *_args):
        reporter = getattr(self.parent(), "_update_settings_summary", None)
        if callable(reporter):
            reporter()

    def _on_next(self):
        if self._step < len(self._chips) - 1:
            self._show_step(self._step + 1)
            return
        self.close()

    def _show_step(self, index):
        self._step = max(0, min(index, len(self._chips) - 1))
        self.stack.setCurrentIndex(self._step)
        for i, chip in enumerate(self._chips):
            chip.set_state("active" if i == self._step else ("done" if i < self._step else "idle"))
        self.btn_prev.setVisible(self._step > 0)
        self.btn_next.setText("完成" if self._step == len(self._chips) - 1 else "下一步")

    def open_at(self, index=0):
        self._show_step(index)
        self._raise_and_place()
        self.show()
        self.raise_()
        self.activateWindow()
        self.btn_next.setFocus(Qt.OtherFocusReason)

    def _raise_and_place(self):
        parent = self.parent()
        if parent is None:
            return
        self.adjustSize()
        geo = parent.geometry()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + max(20, (geo.height() - self.height()) // 3)
        self.move(x, y)

    # -- 外观 ------------------------------------------------------------ #
    def apply_theme(self, *_args):
        c = COLORS
        qss = (
            "#SetupCard {{ background: {panel}; border: 1px solid {border};"
            " border-radius: 16px; }}"
            "#SetupTitle {{ color: {text}; font-size: 15px; font-weight: 600; }}"
            "#StepChip {{ color: {mute}; font-size: 11.5px;"
            " border: 1px solid {border}; border-radius: 9px; padding: 3px 8px; }}"
            "#StepChip[state=\"active\"] {{ color: {accent}; border-color: {accent};"
            " background: {soft}; font-weight: 600; }}"
            "#StepChip[state=\"done\"] {{ color: {accent}; border-color: {soft}; }}"
            "#ChoiceCard {{ background: {panel}; border: 1px solid {border};"
            " border-radius: 12px; }}"
            "#ChoiceCard:hover {{ border-color: {accent}; }}"
            "#ChoiceCard[on=\"true\"] {{ border-color: {accent}; background: {soft}; }}"
            "#ChoiceTitle {{ color: {text}; font-size: 13px; font-weight: 600; }}"
            "#ChoiceSub {{ color: {mute}; font-size: 11px; }}"
            "#ExtBtn {{ background: {input}; border: 1px solid {border};"
            " border-radius: 10px; padding: 0 10px; text-align: left;"
            " color: {dim}; font-size: 12.5px; }}"
            "#ExtBtn:hover {{ border-color: {accent}; color: {text}; }}"
            "#ExtBtn:disabled {{ color: {mute}; }}"
            "QLabel[role=\"hint\"] {{ color: {mute}; font-size: 11.5px; }}"
        ).format(
            panel=c["panel_solid"], border=c["border"], text=c["text"],
            mute=c["text_mute"], dim=c["text_dim"], accent=c["accent"],
            soft=c["accent_soft"], input=c["input_bg"],
        )
        self.setStyleSheet(qss)
        self.ext_picker.apply_theme()
        for label in self.findChildren(QLabel):
            apply = getattr(label, "apply_theme", None)
            if callable(apply):
                apply()

    def closeEvent(self, event):
        # 只收起，不销毁：控件里的值就是配置本身，销毁等于把设置清空
        self.ext_picker.hide_popup()
        self.hide()
        event.ignore()
        self.closed.emit()

    def reject(self):
        """Esc / 关闭按钮都走这里：收起并保留已选设置，而不是销毁对话框。"""
        self.close()

    def accept(self):
        self.close()
