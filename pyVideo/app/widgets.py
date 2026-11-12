#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自定义控件库
============

对齐 C++（WebView2）版界面的视觉语言，全部用 QPainter 自绘，
不依赖任何图片资源，高 DPI 下也清晰。

包含：
- BackgroundWidget   带径向光晕的窗口底
- LogoBadge          渐变圆角 Logo
- Card               圆角卡片容器
- InputGroup         图标 + 输入框 + 内置按钮
- SegmentedControl   分段控件（取代单选框）
- Switch / SwitchRow 开关（取代复选框）
- Chip               统计胶囊
- IconButton         图标按钮
- ProgressTrack      渐变进度条（支持不确定态）
- Toast / ToastManager  右下角轻提示
- StatusPillDelegate 表格状态药丸
"""

import os

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .utils import THUMB_HEIGHT, THUMB_WIDTH, app_icon


class FlowRow(QWidget):
    """按宽度自动换行的一行控件。

    窗口压窄时（例如 1280×720 的笔记本），统计胶囊原本会挤出可视区被裁掉；
    这里改成排不下就换到下一行，行高随内容增长。
    """

    def __init__(self, parent=None, h_spacing=8, v_spacing=8):
        super().__init__(parent)
        self._items = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

    def add(self, widget):
        widget.setParent(self)
        self._items.append(widget)
        widget.show()
        self._relayout()

    def items(self):
        return list(self._items)

    def count(self):
        return len(self._items)

    def _item_size(self, widget):
        """胶囊这类控件是 setFixedHeight(30) 的，sizeHint 却只报 17，取两者较大值。"""
        hint = widget.sizeHint()
        return (max(hint.width(), widget.minimumWidth()),
                max(hint.height(), widget.minimumHeight()))

    def _columns(self, width):
        """按当前宽度算出每行放几个，返回每行的 (widget, 宽度, 高度) 分组。"""
        rows, current, used = [], [], 0
        for widget in self._items:
            if not widget.isVisible():
                continue
            w, h = self._item_size(widget)
            if current and used + self._h_spacing + w > width:
                rows.append(current)
                current, used = [], 0
            if current:
                used += self._h_spacing
            current.append((widget, w, h))
            used += w
        if current:
            rows.append(current)
        return rows

    def _row_height(self):
        visible = [w for w in self._items if w.isVisible()]
        return max((self._item_size(w)[1] for w in visible), default=0)

    def _relayout(self):
        width = max(self.width(), 1)
        rows = self._columns(width)
        y = 0
        for row in rows:
            height = 0
            x = 0
            for widget, w, h in row:
                widget.setGeometry(x, y, w, h)
                x += w + self._h_spacing
                height = max(height, h)
            y += height + self._v_spacing
        total = max(y - self._v_spacing, 0)
        # 用固定高把行数带来的高度变化直接告诉父布局，避免它自己算错
        if total and self.minimumHeight() != total:
            self.setFixedHeight(total)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def refresh(self):
        """可见性/文案变化后重新排布。"""
        self._relayout()

    def sizeHint(self):
        visible = [w for w in self._items if w.isVisible()]
        if not visible:
            return QSize(0, 0)
        sizes = [self._item_size(w) for w in visible]
        return QSize(sum(s[0] for s in sizes) + self._h_spacing * (len(sizes) - 1),
                     max(s[1] for s in sizes))

    def minimumSizeHint(self):
        return QSize(60, self._row_height())


def _dpr():
    """当前屏幕的缩放倍率（高 DPI 下 QPixmap 必须按设备像素创建）。"""
    app = QApplication.instance()
    if app is not None:
        screen = app.primaryScreen()
        if screen is not None:
            return float(screen.devicePixelRatio() or 1.0)
    return 1.0


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _rounded(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)
    return path


def _font(size, weight=QFont.Normal):
    font = QFont()
    # setPixelSize 只接受整数：12.5 这类字号统一四舍五入，避免 TypeError
    font.setPixelSize(int(round(size)))
    font.setWeight(weight)
    return font


def _mix(color_a, color_b, ratio):
    """线性混色，ratio=0 返回 a，1 返回 b。"""
    a, b = QColor(color_a), QColor(color_b)
    return QColor(
        int(a.red() + (b.red() - a.red()) * ratio),
        int(a.green() + (b.green() - a.green()) * ratio),
        int(a.blue() + (b.blue() - a.blue()) * ratio),
        int(a.alpha() + (b.alpha() - a.alpha()) * ratio),
    )


# --------------------------------------------------------------------------- #
# 支持拖放的可选目录输入框
# --------------------------------------------------------------------------- #
class DirDropLineEdit(QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText("选择或拖入文件夹…")
        self.setClearButtonEnabled(False)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile() and os.path.isdir(url.toLocalFile()):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isdir(path):
                self.setText(os.path.normpath(path))
                event.acceptProposedAction()
                return
        event.ignore()


# --------------------------------------------------------------------------- #
# 支持按「真实值」排序的表格项
# --------------------------------------------------------------------------- #
class SortableItem(QTableWidgetItem):
    """
    把可比较的值放在 Qt.UserRole 里，大小列就能按字节数排序，
    而不是按 "1.2 MB" 字符串排序。
    """

    def __lt__(self, other):
        mine = self.data(Qt.UserRole)
        theirs = other.data(Qt.UserRole)
        if mine is not None and theirs is not None:
            try:
                return mine < theirs
            except TypeError:
                pass
        return super().__lt__(other)


# --------------------------------------------------------------------------- #
# 图标：24×24 视口内绘制再缩放
# --------------------------------------------------------------------------- #
_ICON_CACHE = {}


def line_icon(kind, color=None, size=16):
    """
    单色线性图标。kind 支持：
    play / pause / close / trash / folder / folder_open / save / computer /
    search / image / sun / moon / panel / popout / chevron_down / chevron_right /
    copy / refresh / expand / check / film / clock / layers /
    swap / history / help / filter
    """
    color = color or theme.COLORS["text"]
    dpr = _dpr()
    key = (kind, color, size, dpr)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached

    pix = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(size / 24.0, size / 24.0)

    ink = QColor(color)
    pen = QPen(ink)
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    def path_from(points, close=False):
        path = QPainterPath()
        path.moveTo(*points[0])
        for point in points[1:]:
            path.lineTo(*point)
        if close:
            path.closeSubpath()
        return path

    if kind == "play":
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawPolygon(QPolygonF([
            QPointF(8.0, 5.5), QPointF(19.0, 12.0), QPointF(8.0, 18.5),
        ]))
    elif kind == "pause":
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawRoundedRect(QRectF(7.0, 5.5, 3.6, 13.0), 1.4, 1.4)
        painter.drawRoundedRect(QRectF(13.4, 5.5, 3.6, 13.0), 1.4, 1.4)
    elif kind == "close":
        painter.drawLine(QPointF(5.5, 5.5), QPointF(18.5, 18.5))
        painter.drawLine(QPointF(18.5, 5.5), QPointF(5.5, 18.5))
    elif kind == "trash":
        painter.drawLine(QPointF(4.5, 7.0), QPointF(19.5, 7.0))
        painter.drawLine(QPointF(9.0, 7.0), QPointF(9.0, 5.0))
        painter.drawLine(QPointF(9.0, 5.0), QPointF(15.0, 5.0))
        painter.drawLine(QPointF(15.0, 5.0), QPointF(15.0, 7.0))
        painter.drawPath(path_from([
            (6.5, 7.0), (7.3, 19.5), (16.7, 19.5), (17.5, 7.0),
        ]))
        painter.drawLine(QPointF(10.2, 10.5), QPointF(10.6, 16.0))
        painter.drawLine(QPointF(13.8, 10.5), QPointF(13.4, 16.0))
    elif kind in ("folder", "folder_open"):
        path = QPainterPath()
        path.moveTo(3.5, 9.5)
        path.lineTo(3.5, 7.5)
        path.quadTo(3.5, 5.5, 5.5, 5.5)
        path.lineTo(9.5, 5.5)
        path.lineTo(11.5, 7.5)
        path.lineTo(18.5, 7.5)
        path.quadTo(20.5, 7.5, 20.5, 9.5)
        path.lineTo(20.5, 17.5)
        path.quadTo(20.5, 19.5, 18.5, 19.5)
        path.lineTo(5.5, 19.5)
        path.quadTo(3.5, 19.5, 3.5, 17.5)
        path.closeSubpath()
        painter.drawPath(path)
        if kind == "folder_open":
            painter.drawLine(QPointF(3.5, 12.5), QPointF(20.5, 12.5))
    elif kind == "save":
        painter.drawLine(QPointF(12.0, 4.0), QPointF(12.0, 14.0))
        painter.drawPath(path_from([(8.0, 10.2), (12.0, 14.4), (16.0, 10.2)]))
        painter.drawPath(path_from([(4.5, 17.0), (4.5, 18.5)]))
        painter.drawPath(path_from([
            (4.5, 17.0), (4.5, 19.0), (19.5, 19.0), (19.5, 17.0),
        ]))
    elif kind == "computer":
        painter.drawRoundedRect(QRectF(2.2, 4.0, 19.6, 12.2), 2.0, 2.0)
        painter.drawLine(QPointF(12.0, 16.2), QPointF(12.0, 19.2))
        painter.drawLine(QPointF(8.2, 19.4), QPointF(15.8, 19.4))
    elif kind == "search":
        painter.drawEllipse(QRectF(4.5, 4.5, 13.0, 13.0))
        painter.drawLine(QPointF(17.0, 17.0), QPointF(20.5, 20.5))
    elif kind == "image":
        painter.drawRoundedRect(QRectF(2.0, 4.0, 20.0, 16.0), 2.6, 2.6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QRectF(6.2, 8.0, 2.6, 2.6))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path_from([
            (3.0, 18.4), (9.0, 12.0), (12.6, 15.6), (15.0, 13.0), (20.6, 18.4),
        ]))
    elif kind == "sun":
        painter.drawEllipse(QRectF(7.8, 7.8, 8.4, 8.4))
        for start, end in (
            ((12, 2.6), (12, 5.0)), ((12, 19.0), (12, 21.4)),
            ((2.6, 12), (5.0, 12)), ((19.0, 12), (21.4, 12)),
            ((5.2, 5.2), (6.9, 6.9)), ((17.1, 17.1), (18.8, 18.8)),
            ((18.8, 5.2), (17.1, 6.9)), ((6.9, 17.1), (5.2, 18.8)),
        ):
            painter.drawLine(QPointF(*start), QPointF(*end))
    elif kind == "moon":
        path = QPainterPath()
        path.moveTo(20.0, 14.5)
        path.cubicTo(15.5, 16.5, 10.5, 13.0, 10.0, 8.5)
        path.cubicTo(9.7, 6.4, 10.6, 4.6, 12.0, 3.4)
        path.cubicTo(7.0, 3.2, 3.0, 7.2, 3.2, 12.2)
        path.cubicTo(3.4, 17.3, 7.8, 21.2, 13.0, 20.6)
        path.cubicTo(16.0, 20.3, 18.6, 18.6, 20.0, 14.5)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "panel":
        painter.drawRoundedRect(QRectF(3.0, 4.5, 18.0, 15.0), 3.0, 3.0)
        painter.drawLine(QPointF(15.0, 4.5), QPointF(15.0, 19.5))
    elif kind == "popout":
        painter.drawRoundedRect(QRectF(3.0, 6.5, 13.0, 11.0), 2.5, 2.5)
        painter.drawPath(path_from([(18.0, 8.5), (21.0, 8.5), (21.0, 17.5), (18.0, 17.5)]))
    elif kind == "chevron_down":
        painter.drawPath(path_from([(7.0, 10.0), (12.0, 15.0), (17.0, 10.0)]))
    elif kind == "chevron_right":
        painter.drawPath(path_from([(10.0, 7.0), (15.0, 12.0), (10.0, 17.0)]))
    elif kind == "copy":
        painter.drawRoundedRect(QRectF(8.5, 3.0, 12.0, 13.0), 2.2, 2.2)
        painter.drawPath(path_from([
            (15.0, 18.5), (15.0, 19.0), (15.0, 21.0), (3.5, 21.0), (3.5, 9.0),
            (5.5, 9.0),
        ]))
    elif kind == "refresh":
        painter.drawArc(QRectF(4.0, 4.0, 16.0, 16.0), 55 * 16, 280 * 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawPolygon(QPolygonF([
            QPointF(18.4, 3.2), QPointF(21.0, 7.6), QPointF(16.2, 8.2),
        ]))
    elif kind == "expand":
        painter.drawPath(path_from([(4.0, 9.0), (4.0, 4.0), (9.0, 4.0)]))
        painter.drawPath(path_from([(15.0, 4.0), (20.0, 4.0), (20.0, 9.0)]))
        painter.drawPath(path_from([(20.0, 15.0), (20.0, 20.0), (15.0, 20.0)]))
        painter.drawPath(path_from([(9.0, 20.0), (4.0, 20.0), (4.0, 15.0)]))
    elif kind == "check":
        painter.drawPath(path_from([(5.0, 12.6), (10.0, 17.6), (19.0, 7.2)]))
    elif kind == "film":
        painter.drawRoundedRect(QRectF(2.5, 4.5, 19.0, 15.0), 3.0, 3.0)
        painter.drawLine(QPointF(7.5, 4.5), QPointF(7.5, 19.5))
        painter.drawLine(QPointF(16.5, 4.5), QPointF(16.5, 19.5))
        painter.drawLine(QPointF(7.5, 12.0), QPointF(16.5, 12.0))
    elif kind == "clock":
        painter.drawEllipse(QRectF(3.0, 3.0, 18.0, 18.0))
        painter.drawPath(path_from([(12.0, 7.0), (12.0, 12.4), (15.8, 14.6)]))
    elif kind == "layers":
        painter.drawPath(path_from([
            (12.0, 3.0), (20.5, 7.5), (12.0, 12.0), (3.5, 7.5),
        ], close=True))
        painter.drawPath(path_from([(3.5, 12.0), (12.0, 16.5), (20.5, 12.0)]))
        painter.drawPath(path_from([(3.5, 16.5), (12.0, 21.0), (20.5, 16.5)]))
    elif kind == "swap":
        # 上下两条方向相反的箭头
        painter.drawPath(path_from([(3.5, 8.5), (19.0, 8.5)]))
        painter.drawPath(path_from([(15.5, 5.0), (19.0, 8.5), (15.5, 12.0)]))
        painter.drawPath(path_from([(20.5, 15.5), (5.0, 15.5)]))
        painter.drawPath(path_from([(8.5, 12.0), (5.0, 15.5), (8.5, 19.0)]))
    elif kind == "history":
        painter.drawArc(QRectF(4.0, 4.0, 16.0, 16.0), 130 * 16, 280 * 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawPolygon(QPolygonF([
            QPointF(3.0, 9.0), QPointF(7.4, 9.0), QPointF(4.2, 12.6),
        ]))
        painter.setPen(pen)
        painter.drawPath(path_from([(12.0, 7.5), (12.0, 12.4), (15.6, 14.4)]))
    elif kind == "help":
        painter.drawEllipse(QRectF(3.0, 3.0, 18.0, 18.0))
        painter.drawArc(QRectF(8.2, 7.4, 7.6, 6.2), 190 * 16, 160 * 16)
        painter.drawLine(QPointF(12.0, 14.2), QPointF(12.0, 16.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QRectF(11.1, 17.4, 1.8, 1.8))
    elif kind == "filter":
        painter.drawPath(path_from([
            (3.5, 5.0), (20.5, 5.0), (14.0, 12.4), (14.0, 19.0),
            (10.0, 17.0), (10.0, 12.4),
        ], close=True))
    elif kind == "more":
        # 横向三个点（「更多」菜单）
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        for cx in (5.8, 12.0, 18.2):
            painter.drawEllipse(QRectF(cx - 1.5, 10.5, 3.0, 3.0))
    elif kind == "chart":
        # 报告类操作：三根柱状条
        painter.drawLine(QPointF(5.5, 19.0), QPointF(5.5, 13.0))
        painter.drawLine(QPointF(12.0, 19.0), QPointF(12.0, 6.5))
        painter.drawLine(QPointF(18.5, 19.0), QPointF(18.5, 10.0))
    elif kind == "undo":
        # 左上角箭头 + 向右下的半圆弧（与 C++ 版 SVG 同构）
        painter.drawPath(path_from([(9.0, 14.0), (4.0, 9.0), (9.0, 4.0)]))
        painter.drawLine(QPointF(4.0, 9.0), QPointF(14.0, 9.0))
        painter.drawArc(QRectF(8.0, 9.0, 12.0, 12.0), 90 * 16, -180 * 16)
    else:  # 兜底
        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QRectF(10.0, 10.0, 4.0, 4.0))

    painter.end()
    icon = QIcon(pix)
    _ICON_CACHE[key] = icon
    return icon


def clear_icon_caches():
    """主题切换后清空缓存，让图标按新色板重绘。"""
    _ICON_CACHE.clear()
    _DOT_CACHE.clear()
    global _THUMB_PLACEHOLDER
    _THUMB_PLACEHOLDER = None


# --------------------------------------------------------------------------- #
# 状态色 / 圆点 / 缩略占位
# --------------------------------------------------------------------------- #
_DOT_CACHE = {}


def status_kind(status):
    """把状态文字归到 success / skip / fail / inplace / deleted / pending 六类。"""
    text = status or ""
    if text.startswith("撤销失败"):
        return "fail"
    if text.startswith("失败") or text.startswith("删除失败"):
        return "fail"
    if text.startswith("已撤销"):
        return "deleted"
    if text.startswith("同名"):
        return "skip"
    if text.startswith("跳过"):
        # 体积过滤 / 其它主动跳过：归到「跳过」，别当成复制成功
        return "skip"
    if text.startswith("已删除"):
        return "deleted"
    if text.startswith("已在"):
        return "inplace"
    if not text or text.startswith("排队") or text.startswith("待处理"):
        return "pending"
    return "success"


def status_short(status):
    """
    把状态文案压成短标签。

    状态列只有 148px，`同名已存在，已跳过` / `失败：大小校验不通过（1.2 GB ≠ 1.5 GB）`
    这类完整文案在药丸里一定会被省略号糊掉，反而看不清状态本身。
    所以表格里只显示短标签，完整原因留在 tooltip、预览面板、日志与导出报告里。
    """
    text = status or ""
    kind = status_kind(text)
    if kind == "fail":
        return "撤销失败" if text.startswith("撤销失败") else "失败"
    if kind == "skip":
        return "已跳过"
    if kind == "deleted":
        return "已撤销" if text.startswith("已撤销") else "已删除"
    if kind == "inplace":
        return "已在目标"
    if kind == "pending":
        return "待处理"
    head = text.split("：")[0].strip()
    return head or "已复制"


def status_color(status):
    kind = status_kind(status)
    return {
        "success": theme.COLORS["success"],
        "skip": theme.COLORS["warn"],
        "fail": theme.COLORS["danger"],
        "inplace": theme.COLORS["text_mute"],
        "deleted": theme.COLORS["text_mute"],
        "pending": theme.COLORS["text_mute"],
    }[kind]


def status_dot_icon(status):
    """状态圆点图标，供需要图标的场景使用。"""
    color_hex = status_color(status)
    cached = _DOT_CACHE.get(color_hex)
    if cached is not None:
        return cached
    dpr = _dpr()
    pix = QPixmap(int(16 * dpr), int(16 * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color_hex))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(2, 2, 12, 12)
    painter.end()
    icon = QIcon(pix)
    _DOT_CACHE[color_hex] = icon
    return icon


_THUMB_PLACEHOLDER = None


def thumb_placeholder():
    """视频缩略图占位：中性色块 + 播放三角（按屏幕倍率绘制，避免发虚）。"""
    global _THUMB_PLACEHOLDER
    if _THUMB_PLACEHOLDER is not None:
        return _THUMB_PLACEHOLDER

    dpr = _dpr()
    pix = QPixmap(int(THUMB_WIDTH * dpr), int(THUMB_HEIGHT * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    # 占位块跟随主题：浅色下用浅灰蓝，深色下用比面板略亮一档的深蓝，
    # 否则整列都是"黑洞"，既不美观也看不出这是一格缩略图。
    if theme.COLORS.get("is_dark"):
        fill, ink, edge = QColor("#161c27"), QColor("#5d6a7e"), QColor("#232c3b")
    else:
        fill = QColor(theme.COLORS["tile"])
        ink = QColor(theme.COLORS["tile_ink"])
        edge = QColor(theme.COLORS["tile_edge"])

    # 与真实缩略图保持同样的 6px 内缩 + 7px 圆角，切换前后不会"跳"
    box = QRectF(6.0, 6.0, THUMB_WIDTH - 12.0, THUMB_HEIGHT - 12.0)
    painter.setPen(Qt.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(box, 7, 7)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(edge, 1.0))
    painter.drawRoundedRect(box, 7, 7)

    cx, cy = THUMB_WIDTH / 2.0, THUMB_HEIGHT / 2.0
    radius = 9
    painter.setBrush(ink)
    painter.setPen(Qt.NoPen)
    painter.drawPolygon(QPolygonF([
        QPointF(cx - radius * 0.5, cy - radius),
        QPointF(cx + radius, cy),
        QPointF(cx - radius * 0.5, cy + radius),
    ]))
    painter.end()
    _THUMB_PLACEHOLDER = pix
    return _THUMB_PLACEHOLDER


# --------------------------------------------------------------------------- #
# 背景：底色 + 两个径向光晕
# --------------------------------------------------------------------------- #
class BackgroundWidget(QWidget):
    """窗口底：自上而下极轻微的明度渐变，让整块界面有纵深而不发灰。"""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        gradient = QLinearGradient(0.0, 0.0, 0.0, float(self.height()))
        gradient.setColorAt(0.0, QColor(theme.COLORS["bg"]))
        gradient.setColorAt(1.0, QColor(theme.COLORS["bg_2"]))
        painter.fillRect(self.rect(), QBrush(gradient))
        painter.end()


# --------------------------------------------------------------------------- #
# Logo
# --------------------------------------------------------------------------- #
class LogoBadge(QWidget):
    """
    侧栏品牌标。

    直接绘制应用图标本身（assets/app.ico 的多尺寸图层），而不是另画一套图形：
    这样侧栏标志、窗口标题栏、任务栏、exe 文件图标、以及 C++ 版的侧栏标志
    永远是同一张图，改图标只需重跑 tools/make_app_icon.py。
    """

    def __init__(self, size=42, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # 注意：QIcon.pixmap() 传入逻辑尺寸即可，Qt 会按 devicePixelRatio 返回高分辨率位图。
        # 若自己再乘一次 DPR，200% 缩放下会拿到双倍大小的图，被控件裁掉。
        pixmap = app_icon().pixmap(QSize(self._size, self._size))
        if not pixmap.isNull():
            # 用目标矩形绘制，避免不同平台下 pixmap 的 DPR 语义差异导致尺寸跑偏
            painter.drawPixmap(QRectF(0, 0, self._size, self._size), pixmap,
                               QRectF(pixmap.rect()))
        painter.end()


# --------------------------------------------------------------------------- #
# 卡片
# --------------------------------------------------------------------------- #
class Card(QFrame):
    def __init__(self, title=None, parent=None, spacing=10, margins=(16, 14, 16, 14),
                 variant="card"):
        super().__init__(parent)
        # variant="ghost" 用于侧栏分组：白面板内不再套白卡片，只保留留白与分隔线
        self.setObjectName("Card" if variant == "card" else "CardGhost")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(*margins)
        self._layout.setSpacing(spacing)
        self.title_label = None
        if title:
            self.title_label = QLabel(title, self)
            self.title_label.setObjectName("CardTitle")
            self._layout.addWidget(self.title_label)

    def body(self):
        return self._layout

    def add(self, widget, stretch=0):
        self._layout.addWidget(widget, stretch)


# --------------------------------------------------------------------------- #
# 图标按钮
# --------------------------------------------------------------------------- #
class IconButton(QPushButton):
    def __init__(self, icon_kind=None, token="text_dim", size=17, box=30, parent=None, tooltip=None):
        super().__init__(parent)
        self.setProperty("role", "icon")
        self.setFixedSize(box, box)
        self.setCursor(Qt.PointingHandCursor)
        self._icon_kind = icon_kind
        self._token = token
        self._icon_size = size
        self._apply_icon()
        if tooltip:
            self.setToolTip(tooltip)

    def _apply_icon(self):
        if self._icon_kind:
            self.setIcon(line_icon(self._icon_kind, theme.COLORS.get(self._token, self._token), self._icon_size))
            self.setIconSize(QSize(self._icon_size, self._icon_size))

    def apply_theme(self):
        self._apply_icon()
        self.update()


# --------------------------------------------------------------------------- #
# 下拉框
# --------------------------------------------------------------------------- #
class ComboBox(QComboBox):
    """QComboBox + 自绘 chevron。

    QSS 里的 `QComboBox::down-arrow` 只能换成图片，没法画一个跟随主题变色的
    三角；而单纯 `image: none` 会让下拉框看起来像普通输入框。这里把
    QComboBox 的箭头关掉（见 theme.py），用 line_icon 画一个同色系的。
    """

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        size = 12
        x = self.width() - size - 9
        y = (self.height() - size) // 2
        line_icon("chevron_down", theme.COLORS["text_mute"], size).paint(
            painter, QRect(x, y, size, size))
        painter.end()


# --------------------------------------------------------------------------- #
# 输入组：左图标 + 输入框 + 右侧按钮
# --------------------------------------------------------------------------- #
class InputGroup(QFrame):
    def __init__(self, placeholder="", icon_kind=None, parent=None):
        super().__init__(parent)
        self.setObjectName("InputGroup")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(36)
        self._icon_kind = icon_kind
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 1, 3, 1)
        layout.setSpacing(6)

        self.icon_label = None
        if icon_kind:
            self.icon_label = QLabel(self)
            self.icon_label.setFixedSize(15, 15)
            self.icon_label.setPixmap(
                line_icon(icon_kind, theme.COLORS["text_mute"], 15).pixmap(15, 15))
            layout.addWidget(self.icon_label)

        self.edit = DirDropLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit, 1)
        self._extra_buttons = []
        self._focused = False

    def add_button(self, widget):
        self._extra_buttons.append(widget)
        self.layout().addWidget(widget)
        return widget

    def _set_focused(self, focused):
        self._focused = focused
        self.setProperty("focused", "true" if focused else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def eventFilter(self, obj, event):
        if obj is self.edit:
            if event.type() == QEvent.FocusIn:
                self._set_focused(True)
            elif event.type() == QEvent.FocusOut:
                self._set_focused(False)
        return super().eventFilter(obj, event)

    def focusInEvent(self, event):
        self._set_focused(True)
        self.edit.setFocus(Qt.OtherFocusReason)
        super().focusInEvent(event)

    def apply_theme(self):
        if self.icon_label and self._icon_kind:
            self.icon_label.setPixmap(
                line_icon(self._icon_kind, theme.COLORS["text_mute"], 15).pixmap(15, 15))
        for button in self._extra_buttons:
            fn = getattr(button, "apply_theme", None)
            if callable(fn):
                fn()


# --------------------------------------------------------------------------- #
# 分段控件
# --------------------------------------------------------------------------- #
class SegButton(QAbstractButton):
    def __init__(self, value, title, subtitle=None, icon_kind=None, mode="stack", parent=None):
        super().__init__(parent)
        self._value = value
        self._title = title
        self._subtitle = subtitle
        self._icon_kind = icon_kind
        self._mode = mode
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_Hover, True)
        if mode == "stack":
            self.setMinimumHeight(40 if subtitle else 32)
        else:
            self.setMinimumHeight(30)

    def value(self):
        return self._value

    def sizeHint(self):
        metrics = self.fontMetrics()
        if self._mode == "inline":
            width = metrics.horizontalAdvance(self._title) + (24 if self._icon_kind else 12) + 18
            return QSize(width, 30)
        width = max(
            metrics.horizontalAdvance(self._title) + 18,
            (self.fontMetrics().horizontalAdvance(self._subtitle or "") * 0.92) + 12,
        )
        return QSize(int(width), 40 if self._subtitle else 32)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(0, 0, -1, -1)
        rect = QRectF(r)
        radius = 9.0

        if self.isChecked():
            # 选中项是一枚白色药丸：底部两圈极淡投影，让它从凹槽里"浮"起来
            shadow = QColor(theme.COLORS["shadow"])
            painter.setPen(Qt.NoPen)
            for offset, alpha in ((2.0, 9), (1.0, 14)):
                shadow.setAlpha(alpha)
                painter.setBrush(shadow)
                painter.drawRoundedRect(rect.adjusted(0, offset, 0, offset), radius, radius)
            painter.setPen(QPen(QColor(theme.COLORS["border"]), 1.0))
            painter.setBrush(QColor(theme.COLORS["seg_pill"]))
            painter.drawRoundedRect(rect, radius, radius)
        elif self.underMouse() and self.isEnabled():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.COLORS["panel_deep"]))
            painter.drawRoundedRect(rect, radius, radius)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(Qt.NoBrush)

        title_color = QColor(theme.COLORS["text"] if self.isChecked()
                             else theme.COLORS["text_dim"])
        sub_color = QColor(theme.COLORS["text_mute"])
        if not self.isEnabled():
            title_color.setAlpha(110)
            sub_color.setAlpha(90)

        # 文字一律画在整数矩形里：QRectF 的小数坐标会让抗锯齿把字边擦虚
        if self._mode == "inline":
            content = r.adjusted(9, 0, -9, 0)
            if self._icon_kind:
                icon = line_icon(self._icon_kind, title_color.name(), 14)
                icon.paint(painter, QRect(content.left(), r.center().y() - 7, 14, 14))
                content.setLeft(content.left() + 20)
            painter.setPen(title_color)
            painter.setFont(_font(12, QFont.DemiBold))
            painter.drawText(content, Qt.AlignVCenter | Qt.AlignHCenter, self._title)
        else:
            content = r.adjusted(6, 4, -6, -3)
            if self._subtitle:
                painter.setPen(title_color)
                painter.setFont(_font(13, QFont.DemiBold))
                painter.drawText(
                    QRect(content.left(), content.top(), content.width(), 18),
                    Qt.AlignHCenter | Qt.AlignVCenter, self._title)
                painter.setPen(sub_color)
                painter.setFont(_font(11.5))
                painter.drawText(
                    QRect(content.left(), content.top() + 18, content.width(), 14),
                    Qt.AlignHCenter | Qt.AlignVCenter, self._subtitle)
            else:
                painter.setPen(title_color)
                painter.setFont(_font(13, QFont.DemiBold))
                painter.drawText(content, Qt.AlignCenter, self._title)
        painter.end()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def apply_theme(self):
        self.update()


class SegmentedControl(QFrame):
    """分段控件：外观是浅槽 + 选中项高亮，替代 QRadioButton。"""

    changed = Signal(str)

    def __init__(self, options, parent=None, compact=False):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._compact = compact
        self._buttons = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(4)
        mode = "inline" if compact else "stack"
        for option in options:
            button = SegButton(
                option["value"], option.get("title", ""),
                option.get("subtitle"), option.get("icon"), mode, self)
            self._group.addButton(button)
            layout.addWidget(button, 1)
            self._buttons.append(button)
            button.clicked.connect(lambda _=False, b=button: self._on_clicked(b))
        if self._buttons:
            self._buttons[0].setChecked(True)
        if compact:
            self.setFixedHeight(34)
        else:
            self.setMinimumHeight(46)

    def _on_clicked(self, button):
        self.changed.emit(button.value())

    def value(self):
        for button in self._buttons:
            if button.isChecked():
                return button.value()
        return self._buttons[0].value() if self._buttons else ""

    def set_value(self, value, emit=False):
        for button in self._buttons:
            if button.value() == value:
                button.setChecked(True)
                if emit:
                    self.changed.emit(value)
                return

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        for button in self._buttons:
            button.setEnabled(enabled)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(theme.COLORS["border"]), 1.0))
        painter.setBrush(QColor(theme.COLORS["seg_track"]))
        painter.drawRoundedRect(rect, 12, 12)
        painter.end()


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #
class Switch(QAbstractButton):
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 22)
        self.setAttribute(Qt.WA_Hover, True)
        self._offset = 1.0 if checked else 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def _on_anim(self, value):
        self._offset = float(value)
        self.update()

    def _animate(self, checked):
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        radius = rect.height() / 2.0

        if self.isChecked():
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(theme.COLORS["grad_1"]))
            gradient.setColorAt(1.0, QColor(theme.COLORS["accent_2"]))
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)
        else:
            fill = QColor(theme.COLORS["panel_deep"])
            if self.underMouse() and self.isEnabled():
                fill = QColor(theme.COLORS["border_strong"])
                fill.setAlpha(120)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(theme.COLORS["border"]), 1.0))
        painter.drawRoundedRect(rect, radius, radius)

        knob = 16.0
        left = rect.left() + 3.0 + (rect.width() - knob - 6.0) * self._offset
        top = rect.top() + (rect.height() - knob) / 2.0
        # 手柄投影：两圈极淡的黑，避免白色圆点糊在浅色槽上
        shadow = QColor(theme.COLORS["shadow"])
        painter.setPen(Qt.NoPen)
        shadow.setAlpha(14)
        painter.setBrush(shadow)
        painter.drawEllipse(QRectF(left, top + 1.5, knob, knob))
        shadow.setAlpha(20)
        painter.setBrush(shadow)
        painter.drawEllipse(QRectF(left, top + 0.8, knob, knob))

        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(left, top, knob, knob))

        if not self.isEnabled():
            painter.setBrush(QColor(0, 0, 0, 30))
            painter.drawRoundedRect(rect, radius, radius)
        painter.end()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def apply_theme(self):
        self.update()


class SwitchRow(QWidget):
    """一行开关：标题 + 说明 + 右侧开关。整行可点。"""

    toggled = Signal(bool)

    def __init__(self, title, subtitle=None, checked=True, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(12)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)
        self.title_label = QLabel(title, self)
        self.title_label.setFont(_font(12.5, QFont.DemiBold))
        self.title_label.setStyleSheet("color: %s;" % theme.COLORS["text"])
        text_box.addWidget(self.title_label)
        self.sub_label = None
        if subtitle:
            self.sub_label = QLabel(subtitle, self)
            self.sub_label.setStyleSheet(
                "color: %s; font-size: 12px;" % theme.COLORS["text_mute"])
            text_box.addWidget(self.sub_label)
        layout.addLayout(text_box, 1)

        self.switch = Switch(checked, self)
        self.switch.toggled.connect(self.toggled.emit)
        layout.addWidget(self.switch, 0, Qt.AlignVCenter)

    def isChecked(self):
        return self.switch.isChecked()

    def setChecked(self, value):
        self.switch.setChecked(value)

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self.switch.setEnabled(enabled)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.switch.toggle()
        super().mousePressEvent(event)

    def apply_theme(self):
        self.title_label.setStyleSheet("color: %s;" % theme.COLORS["text"])
        if self.sub_label:
            self.sub_label.setStyleSheet(
                "color: %s; font-size: 12px;" % theme.COLORS["text_mute"])
        self.switch.apply_theme()


# --------------------------------------------------------------------------- #
# 统计胶囊
# --------------------------------------------------------------------------- #
# 勾选框
# --------------------------------------------------------------------------- #
def paint_check_box(painter, rect, state, radius=5.0):
    """画一个勾选框。

    Qt 的 QSS 只能给 `::indicator` 换底色，画不出对勾——这正是"勾选后只剩
    一块纯蓝方块"的根因。行内勾选、表头全选、向导里的复选框统一走这里自绘：
    选中画对勾，部分选中画横杠，未选画描边框。

    state: "checked" | "partial" | "unchecked"
    """
    box = QRectF(rect)
    side = min(box.width(), box.height())
    inner = QRectF(0, 0, side, side)
    inner.moveCenter(box.center())

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    if state in ("checked", "partial"):
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.COLORS["accent"]))
        painter.drawRoundedRect(inner, radius, radius)
        pen = QPen(QColor("#FFFFFF"), max(1.6, side / 9.0))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        if state == "checked":
            path = QPainterPath(
                QPointF(inner.left() + side * 0.23, inner.top() + side * 0.52))
            path.lineTo(QPointF(inner.left() + side * 0.42, inner.top() + side * 0.71))
            path.lineTo(QPointF(inner.left() + side * 0.78, inner.top() + side * 0.29))
            painter.drawPath(path)
        else:
            painter.drawLine(
                QPointF(inner.left() + side * 0.27, inner.center().y()),
                QPointF(inner.left() + side * 0.73, inner.center().y()))
    else:
        painter.setPen(QPen(QColor(theme.COLORS["border_strong"]), 1.2))
        painter.setBrush(QColor(theme.COLORS["input_bg"]))
        painter.drawRoundedRect(inner.adjusted(0.6, 0.6, -0.6, -0.6), radius, radius)
    painter.restore()


class CheckBox(QAbstractButton):
    """自绘复选框（替代 QCheckBox）。

    向导里的格式勾选、删除副本确认都改用它：QSS 的 indicator 在深色主题下
    勾选后只是一块纯色方块，看不到对勾。
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.toggled.connect(self.update)

    def sizeHint(self):
        fm = self.fontMetrics()
        width = 15 + 10 + fm.horizontalAdvance(self.text()) + 2
        return QSize(max(width, 22), max(22, fm.height() + 6))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        side = 15.0
        top = (self.height() - side) / 2.0
        paint_check_box(painter, QRectF(1.0, top, side, side),
                        "checked" if self.isChecked() else "unchecked")
        text = self.text()
        if text:
            painter.setFont(_font(12))
            painter.setPen(QColor(theme.COLORS["text_dim"]))
            avail = int(self.width() - side - 12)
            painter.drawText(
                QRectF(side + 10, 0, avail, self.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(text, Qt.ElideRight, max(avail, 10)))
        painter.end()

    def apply_theme(self):
        self.update()


class CheckHeaderView(QHeaderView):
    """表头第 0 列画全选框（与 C++ 版 `#selectAll` 同一语义）。

    勾选态 = 行选中态，所以"全选/半选"直接由 selectionModel 的选中行数推算，
    不需要第二份数据；点这一列接管为全选/清空，也不会触发排序。
    """

    def __init__(self, orientation, parent=None, check_column=0):
        super().__init__(orientation, parent)
        self._check_column = check_column
        self.setSectionsClickable(True)
        self.setHighlightSections(False)

    def _state(self):
        model = self.model()
        selection = self.selectionModel()
        if model is None or selection is None:
            return "unchecked"
        rows = model.rowCount()
        if rows <= 0:
            return "unchecked"
        picked = len(selection.selectedRows(self._check_column))
        if picked <= 0:
            return "unchecked"
        return "checked" if picked >= rows else "partial"

    def mousePressEvent(self, event):
        column = self.logicalIndexAt(event.position().toPoint())
        if column == self._check_column:
            selection = self.selectionModel()
            view = self.parentWidget()
            if selection is not None and view is not None:
                if self._state() == "checked":
                    selection.clearSelection()
                else:
                    view.selectAll()
            self.viewport().update()
            return
        super().mousePressEvent(event)

    def paintSection(self, painter, rect, logicalIndex):
        if logicalIndex != self._check_column:
            super().paintSection(painter, rect, logicalIndex)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        super().paintSection(painter, rect, logicalIndex)
        side = 15.0
        box = QRectF(rect.left() + (rect.width() - side) / 2.0,
                     rect.top() + (rect.height() - side) / 2.0, side, side)
        paint_check_box(painter, box, self._state())
        painter.restore()

    def apply_theme(self):
        self.viewport().update()


# --------------------------------------------------------------------------- #
class _Dot(QWidget):
    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(7, 7)

    def set_color(self, color):
        self._color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._color))
        painter.drawEllipse(QRectF(0, 0, 7, 7))
        painter.end()


class Chip(QFrame):
    """
    统计胶囊：圆点 + 名称 + 数值。

    checkable=True 时可当状态筛选按钮：未选中是"淡色底 + 语义色文字"，
    选中变成实心强调色，一眼能看出当前在看哪一类。
    """

    clicked = Signal()

    # kind -> (底色 token, 文字 token)
    _KIND_TOKENS = {
        "ok": ("success_soft", "success"),
        "fail": ("danger_soft", "danger"),
        "warn": ("warn_soft", "warn"),
        "gray": ("muted_soft", "text_dim"),
        "pending": ("muted_soft", "text_mute"),
        "": ("muted_soft", "text_dim"),
    }

    def __init__(self, caption, kind="", value="0", parent=None, checkable=False):
        super().__init__(parent)
        self.setFixedHeight(30)
        self._kind = kind
        self._checkable = checkable
        self._active = False
        self._hover = False
        self.setAttribute(Qt.WA_StyledBackground, False)
        if checkable:
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip("点击只看这一类；再点一次或按 Esc 取消筛选")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 0, 9, 0)
        layout.setSpacing(6)
        self.dot = _Dot(self._dot_color(), self)
        self.dot.setFixedSize(6, 6)
        self.caption_label = QLabel(caption, self)
        self.caption_label.setFont(_font(12))
        self.value_label = QLabel(value, self)
        self.value_label.setFont(_font(13, QFont.DemiBold))
        layout.addWidget(self.dot)
        layout.addWidget(self.caption_label)
        layout.addWidget(self.value_label)
        self.apply_theme()

    def _tokens(self):
        return self._KIND_TOKENS.get(self._kind, self._KIND_TOKENS[""])

    def _dot_color(self):
        return theme.COLORS[self._tokens()[1]]

    def set_value(self, value):
        self.value_label.setText(str(value))

    def set_kind(self, kind):
        self._kind = kind
        self.apply_theme()

    def set_active(self, active):
        active = bool(active) and self._checkable
        if active != self._active:
            self._active = active
            self.apply_theme()

    def is_active(self):
        return self._active

    def enterEvent(self, event):
        if self._checkable:
            self._hover = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._checkable:
            self._hover = False
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if self._checkable and event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        if self._active:
            painter.setPen(QPen(QColor(theme.COLORS["accent"]), 1.0))
            painter.setBrush(QColor(theme.COLORS["accent"]))
        else:
            fill = QColor(theme.COLORS[self._tokens()[0]])
            if self._hover:
                fill = fill.darker(104)
            painter.setPen(QPen(QColor(theme.COLORS["divider"]), 1.0))
            painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()

    def apply_theme(self):
        if self._active:
            self.dot.set_color("#ffffff")
            self.caption_label.setStyleSheet("color: #ffffff; font-size: 12px;")
            self.value_label.setStyleSheet(
                "color: #ffffff; font-size: 13px; font-weight: 600;")
        else:
            self.dot.set_color(theme.COLORS[self._tokens()[1]])
            self.caption_label.setStyleSheet(
                "color: %s; font-size: 12px;" % theme.COLORS["text_dim"])
            self.value_label.setStyleSheet(
                "color: %s; font-size: 13px; font-weight: 600;"
                % theme.COLORS[self._tokens()[1]])
        self.update()


# --------------------------------------------------------------------------- #
# 渐变进度条
# --------------------------------------------------------------------------- #
class ProgressTrack(QWidget):
    """6px 圆头进度条，品牌渐变填充；不确定态时显示来回滑动的光带。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self._value = 0
        self._total = 0
        self._busy = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def set_progress(self, done, total):
        self._busy = False
        self._timer.stop()
        self._total = max(int(total), 0)
        self._value = max(int(done), 0)
        self.update()

    def set_busy(self, busy=True):
        self._busy = busy
        if busy:
            self._phase = 0.0
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.012) % 1.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        width = float(self.width())
        height = float(self.height())
        radius = height / 2.0

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.COLORS["track"]))
        painter.drawRoundedRect(QRectF(0, 0, width, height), radius, radius)

        gradient = QLinearGradient(0.0, 0.0, width, 0.0)
        gradient.setColorAt(0.0, QColor(theme.COLORS["grad_1"]))
        gradient.setColorAt(0.62, QColor(theme.COLORS["grad_2"]))
        gradient.setColorAt(1.0, QColor(theme.COLORS["grad_3"]))
        brush = QBrush(gradient)

        if self._busy:
            band = max(width * 0.32, 72.0)
            span = width + band
            left = self._phase * span - band
            painter.save()
            painter.setClipRect(QRectF(left, 0, band, height))
            painter.setBrush(brush)
            painter.drawRoundedRect(QRectF(0, 0, width, height), radius, radius)
            painter.restore()
        else:
            ratio = 0.0 if not self._total else min(self._value / float(self._total), 1.0)
            filled = width * ratio
            if filled > 0.5:
                painter.setBrush(brush)
                painter.drawRoundedRect(
                    QRectF(0, 0, max(filled, height), height), radius, radius)
        painter.end()


# --------------------------------------------------------------------------- #
# Toast
# --------------------------------------------------------------------------- #
class Toast(QFrame):
    closed = Signal(object)

    _ACCENTS = {
        "info": "accent",
        "success": "success",
        "warn": "warn",
        "error": "danger",
    }

    def __init__(self, text, kind="info", parent=None):
        super().__init__(parent)
        self._kind = kind
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setMaximumWidth(360)
        self.setCursor(Qt.ArrowCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 11, 15, 11)
        layout.setSpacing(8)
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setFont(_font(12))
        self.label.setStyleSheet("color: %s; font-size: 12px;" % theme.COLORS["text"])
        layout.addWidget(self.label)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        self._fade = QVariantAnimation(self)
        self._fade.setDuration(180)
        self._fade.valueChanged.connect(lambda v: self._effect.setOpacity(float(v)))
        self.adjustSize()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(theme.COLORS["border_strong"]), 1.0))
        painter.setBrush(QColor(theme.COLORS["panel_solid"]))
        painter.drawRoundedRect(rect, 11, 11)

        accent = theme.COLORS[self._ACCENTS.get(self._kind, "accent")]
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(accent))
        bar = QRectF(1.0, 10.0, 3.0, rect.height() - 20.0)
        painter.drawRoundedRect(bar, 1.5, 1.5)
        painter.end()

    def fade_in(self):
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

    def fade_out(self):
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._on_faded)
        self._fade.start()

    def _on_faded(self):
        try:
            self._fade.finished.disconnect(self._on_faded)
        except TypeError:
            pass
        self.closed.emit(self)
        self.hide()
        self.deleteLater()

    def apply_theme(self):
        self.label.setStyleSheet("color: %s; font-size: 12px;" % theme.COLORS["text"])
        self.update()


class ToastManager(QObject):
    """右下角堆叠提示，替代打断式的模态弹窗。"""

    MAX_TOASTS = 4
    GAP = 8
    MARGIN = 18

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.toasts = []
        host.installEventFilter(self)

    def show(self, text, kind="info", ms=2800):
        toast = Toast(text, kind, self.host)
        toast.closed.connect(self._remove)
        self.toasts.append(toast)
        while len(self.toasts) > self.MAX_TOASTS:
            oldest = self.toasts.pop(0)
            oldest.hide()
            oldest.deleteLater()
        toast.show()
        toast.raise_()
        self._relayout(animate=True)
        toast.fade_in()
        QTimer.singleShot(ms, toast.fade_out)
        return toast

    def _remove(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
        self._relayout(animate=True)

    def _relayout(self, animate=False):
        if not self.host:
            return
        bottom = self.host.height() - self.MARGIN
        for toast in reversed(self.toasts):
            toast.adjustSize()
            width = min(toast.sizeHint().width() + 4, toast.maximumWidth())
            height = toast.sizeHint().height()
            x = self.host.width() - width - self.MARGIN
            y = bottom - height
            toast.setGeometry(QRect(x, max(y, 8), width, height))
            bottom = y - self.GAP

    def eventFilter(self, obj, event):
        if obj is self.host and event.type() in (QEvent.Resize, QEvent.Show):
            self._relayout()
        return False


# --------------------------------------------------------------------------- #
# 表格状态药丸
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 表格行代理
# --------------------------------------------------------------------------- #
# 视图与模型约定的自定义角色（模型层 table_model 从这里引用，避免两处各定义一份）
UID_ROLE = Qt.UserRole + 1
THUMB_ROLE = Qt.UserRole + 3


class StatusPillDelegate(QStyledItemDelegate):
    """
    通用行代理：
    - 统一处理 hover / 选中底色与行底分隔线
    - 缩略图列（thumb_column）裁切铺满并做圆角，没有缩略图时画占位图
    - 状态列（status_column）画成圆角药丸，其余列交给默认代理
    """

    def __init__(self, status_column=5, thumb_column=2, check_column=0, parent=None):
        super().__init__(parent)
        self._status_column = status_column
        self._thumb_column = thumb_column
        self._check_column = check_column

    def _paint_thumb(self, painter, rect, pixmap):
        """把缓存封面按单元格大小居中裁切，圆角裁切边与占位图一致。"""
        dpr = self.parent().devicePixelRatioF() if self.parent() else _dpr()
        if pixmap is None or pixmap.isNull():
            pixmap = thumb_placeholder()
        inner = QRectF(rect).adjusted(6.0, 6.0, -6.0, -6.0)
        if inner.width() <= 2 or inner.height() <= 2:
            return
        tw, th = int(inner.width() * dpr), int(inner.height() * dpr)
        scaled = pixmap.scaled(
            tw, th, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        src_w, src_h = tw / dpr, th / dpr
        src_x = max((scaled.width() / dpr - src_w) / 2.0, 0.0)
        src_y = max((scaled.height() / dpr - src_h) / 2.0, 0.0)

        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        clip = QPainterPath()
        clip.addRoundedRect(inner, 7, 7)
        painter.setClipPath(clip)
        painter.drawPixmap(inner, scaled, QRectF(src_x, src_y, src_w, src_h))
        painter.restore()

    def paint(self, painter, option, index):
        from PySide6.QtWidgets import QStyleOptionViewItem

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(theme.COLORS["selection"]))
            if index.column() == 0:
                # 选中行的左侧强调条：比整行铺色更克制，也更容易定位
                painter.fillRect(
                    QRectF(option.rect.left(), option.rect.top(), 3.0, option.rect.height()),
                    QColor(theme.COLORS["accent"]))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, QColor(theme.COLORS["row_hover"]))

        # 缩略图列直接用模型给的 QPixmap 绘制；老版本通过 BackgroundRole 传位图，
        # 这里保留兼容判断，但 Model/View 之后走的是 THUMB_ROLE 分支
        if index.column() == self._check_column:
            # 勾选态就是行选中态：委托直接按 State_Selected 画框，
            # 原有「选中行 / Ctrl+A / 整理选中」语义完全不变
            state = "checked" if option.state & QStyle.State_Selected else "unchecked"
            side = 15.0
            box = QRectF(
                option.rect.left() + (option.rect.width() - side) / 2.0,
                option.rect.top() + (option.rect.height() - side) / 2.0,
                side, side)
            paint_check_box(painter, box, state)
        elif index.column() == self._thumb_column:
            self._paint_thumb(painter, option.rect, index.data(THUMB_ROLE))
        else:
            brush = index.data(Qt.BackgroundRole)
            if brush is not None:
                try:
                    texture = brush.texture()
                except AttributeError:
                    texture = QPixmap()
                if not texture.isNull():
                    painter.drawPixmap(option.rect.topLeft(), texture)
                else:
                    try:
                        painter.fillRect(option.rect, brush)
                    except TypeError:
                        pass

        if index.column() == self._check_column:
            pass
        elif index.column() == self._thumb_column:
            pass
        elif index.column() == self._status_column:
            text = str(index.data(Qt.DisplayRole) or "")
            kind = status_kind(text)
            pairs = {
                "success": ("success", "success_soft"),
                "skip": ("warn", "warn_soft"),
                "fail": ("danger", "danger_soft"),
                "inplace": ("text_dim", "muted_soft"),
                "deleted": ("text_dim", "muted_soft"),
                "pending": ("text_mute", "muted_soft"),
            }
            fg_key, bg_key = pairs[kind]
            fg = QColor(theme.COLORS[fg_key])
            bg = QColor(theme.COLORS[bg_key])

            painter.setFont(_font(12))
            metrics = painter.fontMetrics()
            available = option.rect.width() - 16
            # 药丸里只画短标签，完整原因靠 tooltip / 预览面板 / 导出报告承载
            label = status_short(text)
            shown = metrics.elidedText(label, Qt.ElideRight, max(available - 30, 24))
            text_width = metrics.horizontalAdvance(shown)
            pill_height = 24
            # 左 10 内边距 + 6 圆点 + 4 间隙 = 文字从 +20 起；右侧也留 10，
            # 否则文字紧贴药丸右边缘，看着整块往右偏
            pill_width = min(text_width + 30, available)
            pill = QRectF(
                option.rect.left() + 10,
                option.rect.center().y() - pill_height / 2.0,
                pill_width,
                pill_height,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(pill, pill_height / 2.0, pill_height / 2.0)
            # 小圆点：语义色的第二重编码，扫一眼就能分辨状态
            painter.setBrush(fg)
            painter.drawEllipse(QRectF(pill.left() + 10.0,
                                       pill.center().y() - 3.0, 6.0, 6.0))
            painter.setPen(fg)
            painter.drawText(
                QRectF(pill.left() + 20.0, pill.top(),
                       pill.width() - 30.0, pill.height()),
                Qt.AlignVCenter | Qt.AlignLeft, shown)
        else:
            opt = QStyleOptionViewItem(option)
            opt.state &= ~QStyle.State_Selected
            opt.state &= ~QStyle.State_MouseOver
            opt.backgroundBrush = QBrush(Qt.NoBrush)
            super().paint(painter, opt, index)

        # 行底分隔线
        painter.setPen(QPen(QColor(theme.COLORS["divider"]), 1.0))
        painter.drawLine(option.rect.left(), option.rect.bottom(),
                         option.rect.right(), option.rect.bottom())
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 28))
        return size


# --------------------------------------------------------------------------- #
# 空状态插画
# --------------------------------------------------------------------------- #
class EmptyArt(QWidget):
    """
    表格空状态插画：一叠视频卡片 + 播放圆钮。

    三张卡片错位叠放，正好对应"把散落的视频集中到一处"这件事；
    纯 QPainter 绘制，颜色全部取色板，切主题自动跟着变。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(112, 88)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        card_w, card_h = 72.0, 56.0
        base_x, base_y = 6.0, self.height() - card_h - 12.0

        # 后面两张卡片：只要轮廓和淡色，暗示"一叠"
        for index in (2, 1):
            offset = index * 7.0
            rect = QRectF(base_x + offset, base_y - offset, card_w, card_h)
            painter.setPen(QPen(QColor(theme.COLORS["border"]), 1.0))
            fill = QColor(theme.COLORS["panel_deep"])
            fill.setAlpha(235)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 11, 11)

        # 最前面的卡片：白底 + 品牌渐变描边
        front = QRectF(base_x, base_y, card_w, card_h)
        gradient = QLinearGradient(front.topLeft(), front.bottomRight())
        gradient.setColorAt(0.0, QColor(theme.COLORS["panel_solid"]))
        gradient.setColorAt(1.0, QColor(theme.COLORS["accent_soft"]))
        painter.setBrush(QBrush(gradient))
        pen_gradient = QLinearGradient(front.topLeft(), front.bottomRight())
        pen_gradient.setColorAt(0.0, QColor(theme.COLORS["accent"]))
        pen_gradient.setColorAt(1.0, QColor(theme.COLORS["accent_2"]))
        painter.setPen(QPen(QBrush(pen_gradient), 1.2))
        painter.drawRoundedRect(front, 11, 11)

        # 播放圆钮
        center = front.center()
        radius = 15.0
        halo = QColor(theme.COLORS["accent"])
        halo.setAlpha(18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(center, radius + 5.0, radius + 5.0)

        ring = QColor(theme.COLORS["accent_soft"])
        painter.setBrush(ring)
        painter.drawEllipse(center, radius, radius)

        painter.setBrush(QColor(theme.COLORS["accent"]))
        tri = 7.6
        painter.drawPolygon(QPolygonF([
            QPointF(center.x() - tri * 0.62, center.y() - tri),
            QPointF(center.x() + tri, center.y()),
            QPointF(center.x() - tri * 0.62, center.y() + tri),
        ]))
        painter.end()

    def apply_theme(self):
        self.update()


# --------------------------------------------------------------------------- #
# 运行指示器 / 计数徽章
# --------------------------------------------------------------------------- #
class PulseDot(QWidget):
    """任务运行时的小圆点（静态，不再呼吸闪烁）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(8, 8)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.COLORS["accent_2"]))
        painter.drawEllipse(QRectF(0, 0, 8, 8))
        painter.end()


class Badge(QLabel):
    """标题后的小计数徽章。"""

    def __init__(self, text="0", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(22)
        self.setMinimumWidth(26)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        radius = rect.height() / 2.0
        painter.setPen(QPen(QColor(theme.COLORS["accent"]), 1.0))
        painter.setBrush(QColor(theme.COLORS["accent_soft"]))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.setPen(QColor(theme.COLORS["accent"]))
        font = _font(12, QFont.DemiBold)
        painter.setFont(font)
        # 按墨迹包围盒居中：数字的上下留白和汉字不同，只用 AlignCenter
        # 会让"0"看起来偏下偏左，这里把字形的实际笔画框对到胶囊中心
        ink = QFontMetricsF(font).tightBoundingRect(self.text() or "0")
        baseline = QPointF(rect.center().x() - ink.center().x(),
                           rect.center().y() - ink.center().y())
        painter.drawText(baseline, self.text())
        painter.end()

    def apply_theme(self):
        self.update()


# --------------------------------------------------------------------------- #
# 分隔线
# --------------------------------------------------------------------------- #
class AutoHideLabel(QLabel):
    """文本为空时自动隐藏：预览面板里那些"暂时没有内容"的信息行，
    留在布局里会白占三行高度，撑出一块空洞。"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setVisible(bool(text and text.strip()))

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text and text.strip()))


class ElideLabel(QLabel):
    """单行省略标签：侧栏窄，长文本换行会把高度撑开，截断又难看。

    存一份完整文本，按当前宽度用 elidedText 重新省略；完整内容放 tooltip，
    鼠标悬停就能看到被省略的部分。
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setWordWrap(False)
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._refresh()

    def full_text(self):
        return self._full

    def _refresh(self):
        width = self.width()
        if width <= 0 or not self._full:
            super().setText(self._full)
            return
        super().setText(
            self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def apply_theme(self):
        self._refresh()


class Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setStyleSheet("background-color: %s;" % theme.COLORS["divider"])

    def apply_theme(self):
        self.setStyleSheet("background-color: %s;" % theme.COLORS["divider"])
