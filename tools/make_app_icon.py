#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成两端共用的应用图标
======================

一套矢量几何（圆角方块 + 品牌渐变 + 播放三角）渲染出不同尺寸，
并针对小尺寸做光学补偿（更小的圆角、更粗的三角），输出：

    pyVideo/assets/app.ico            多尺寸 Windows 图标（16/20/24/32/40/48/64/128/256）
    pyVideo/assets/app.png            512×512 主图（窗口/文档用）
    pyVideo/assets/app.svg            矢量源文件（方便以后改色改形）
    VideoCollectorWeb/res/app.ico     同上一份，供 windres 编进 exe
    VideoCollectorWeb/res/app.png     同上

配色取自 pyVideo/app/theme.py 的 grad_1 / grad_2 / grad_3：
    #6b6bef → #4f8dff → #12b6c9

用法：
    python tools/make_app_icon.py
    python tools/make_app_icon.py --preview <目录>   # 额外导出各尺寸 png 便于目视检查
"""

import os
import struct
import sys
from math import acos, sqrt, tan

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QBuffer, QPointF, QRectF, Qt  # noqa: E402
from PyQt5.QtGui import (  # noqa: E402
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(ROOT, "pyVideo", "assets")
WEB_DIR = os.path.join(ROOT, "VideoCollectorWeb", "res")

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

# 与界面主题一致的品牌渐变
STOPS_LARGE = [(0.0, "#6b6bef"), (0.55, "#4f8dff"), (1.0, "#12b6c9")]
# 小尺寸提高对比、压缩色阶，避免 16px 下糊成一片
STOPS_SMALL = [(0.0, "#5a5ae4"), (0.5, "#3f83ff"), (1.0, "#08c6d8")]


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def _unit(dx, dy):
    length = sqrt(dx * dx + dy * dy)
    if length == 0:
        return 0.0, 0.0
    return dx / length, dy / length


def rounded_polygon(points, radius):
    """给多边形的每个顶点做圆角（切线长度会自动收敛，避免自交）。"""
    path = QPainterPath()
    n = len(points)
    if radius <= 0 or n < 3:
        path.moveTo(points[0])
        for pt in points[1:]:
            path.lineTo(pt)
        path.closeSubpath()
        return path

    corners = []
    for i in range(n):
        vertex = points[i]
        prev_pt = points[i - 1]
        next_pt = points[(i + 1) % n]
        u1 = _unit(prev_pt.x() - vertex.x(), prev_pt.y() - vertex.y())
        u2 = _unit(next_pt.x() - vertex.x(), next_pt.y() - vertex.y())
        cos_v = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        angle = acos(cos_v)
        dist = 0.0 if angle < 1e-4 else radius / tan(angle / 2.0)
        # 切线不能超过相邻边的一半
        dist = min(
            dist,
            sqrt((prev_pt.x() - vertex.x()) ** 2 + (prev_pt.y() - vertex.y()) ** 2) / 2.0,
            sqrt((next_pt.x() - vertex.x()) ** 2 + (next_pt.y() - vertex.y()) ** 2) / 2.0,
        )
        t1 = QPointF(vertex.x() + u1[0] * dist, vertex.y() + u1[1] * dist)
        t2 = QPointF(vertex.x() + u2[0] * dist, vertex.y() + u2[1] * dist)
        corners.append((vertex, t1, t2))

    path.moveTo(corners[0][2])
    for i in range(1, n + 1):
        vertex, t1, t2 = corners[i % n]
        path.lineTo(t1)
        path.quadTo(vertex, t2)
    path.closeSubpath()
    return path


def geometry(size):
    """返回该尺寸下的（圆角半径, 三角路径）。"""
    small = size < 48
    radius = size * (0.19 if small else 0.225)
    # 三角高宽比固定 1.15：太瘦会像箭头，太扁又没有"播放"的分量感
    tri_h = size * (0.52 if small else 0.50)
    tri_w = tri_h / 1.15
    # 视觉居中：三角形重心偏左，整体右移一点点才不显偏
    cx = size * 0.5 + tri_w * 0.075
    cy = size * 0.5
    left = cx - tri_w / 2.0
    right = cx + tri_w / 2.0
    top = cy - tri_h / 2.0
    bottom = cy + tri_h / 2.0
    corner = 0.0 if size <= 24 else size * (0.018 if small else 0.045)
    triangle = rounded_polygon(
        [QPointF(left, top), QPointF(right, cy), QPointF(left, bottom)], corner
    )
    return radius, triangle


def render_icon(size):
    """按目标像素尺寸渲染一张透明边角的图标。"""
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)

    small = size < 48
    radius, triangle = geometry(size)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    plate = QPainterPath()
    plate.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setClipPath(plate)

    gradient = QLinearGradient(0, 0, size, size)
    for pos, color in (STOPS_SMALL if small else STOPS_LARGE):
        gradient.setColorAt(pos, QColor(color))
    painter.fillRect(QRectF(0, 0, size, size), gradient)

    # 顶部高光：小尺寸会显脏，只给大图标加
    if not small:
        sheen = QLinearGradient(0, 0, 0, size * 0.55)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 34))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(QRectF(0, 0, size, size), sheen)

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#ffffff"))
    painter.drawPath(triangle)

    if not small:
        painter.setClipping(False)
        edge = QPainterPath()
        edge.addRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), max(0.0, radius - 0.5),
                            max(0.0, radius - 0.5))
        painter.setPen(QColor(255, 255, 255, 26))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(edge)

    painter.end()
    return img


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
def png_bytes(img):
    buffer = QBuffer()
    buffer.open(QBuffer.WriteOnly)
    img.save(buffer, "PNG")
    return bytes(buffer.data())


def dib_bytes(img):
    """32bpp BGRA DIB（含 AND 掩码）：ICO 小尺寸用它，兼容性最好。"""
    img = img.convertToFormat(QImage.Format_ARGB32)
    width, height = img.width(), img.height()
    stride = img.bytesPerLine()
    row_bytes = width * 4
    ptr = img.constBits()
    ptr.setsize(img.byteCount())
    raw = bytes(ptr)

    rows = [raw[y * stride:y * stride + row_bytes] for y in range(height)]
    rows.reverse()  # DIB 自下而上

    mask_row = ((width + 31) // 32) * 4
    mask = b"\x00" * (mask_row * height)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,           # biSize
        width,        # biWidth
        height * 2,   # biHeight：XOR 位图 + AND 掩码
        1,            # biPlanes
        32,           # biBitCount
        0,            # BI_RGB
        row_bytes * height,
        0, 0, 0, 0,
    )
    return header + b"".join(rows) + mask


def ico_bytes(images):
    """images: [(size, payload)]，尺寸升序。"""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        payload += data
    return header + entries + payload


# --------------------------------------------------------------------------- #
# SVG 源文件：用同一套几何序列化，避免源文件和位图不一致
# --------------------------------------------------------------------------- #
def path_to_svg(path):
    """
    把 QPainterPath 序列化成 SVG path 的 d 属性。

    注意：QPainterPath 里的 quadTo 会被 Qt 展开成三次贝塞尔
    （CurveTo 控制点 + 两个 CurveToData），所以这里按后面跟几个
    Data 元素来判断是二次还是三次曲线，不能一律当成 Q。
    """
    parts = []
    count = path.elementCount()
    i = 0
    while i < count:
        element = path.elementAt(i)
        if element.type == QPainterPath.MoveToElement:
            parts.append("M{0:.2f} {1:.2f}".format(element.x, element.y))
            i += 1
        elif element.type == QPainterPath.LineToElement:
            parts.append("L{0:.2f} {1:.2f}".format(element.x, element.y))
            i += 1
        elif element.type == QPainterPath.CurveToElement:
            n = 0
            while i + 1 + n < count and \
                    path.elementAt(i + 1 + n).type == QPainterPath.CurveToDataElement:
                n += 1
            if n >= 2:
                c1 = element
                c2 = path.elementAt(i + 1)
                end = path.elementAt(i + 2)
                parts.append("C{0:.2f} {1:.2f} {2:.2f} {3:.2f} {4:.2f} {5:.2f}".format(
                    c1.x, c1.y, c2.x, c2.y, end.x, end.y))
            elif n == 1:
                end = path.elementAt(i + 1)
                parts.append("Q{0:.2f} {1:.2f} {2:.2f} {3:.2f}".format(
                    element.x, element.y, end.x, end.y))
            i += 1 + max(n, 1)
        else:
            i += 1
    return " ".join(parts) + " Z"


SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#6b6bef"/>
      <stop offset="0.55" stop-color="#4f8dff"/>
      <stop offset="1" stop-color="#12b6c9"/>
    </linearGradient>
    <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="0.55" spreadMethod="pad">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.133"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="{radius:.2f}" fill="url(#brand)"/>
  <rect width="512" height="512" rx="{radius:.2f}" fill="url(#sheen)"/>
  <path d="{triangle}" fill="#ffffff"/>
</svg>
"""


def main(argv):
    # 允许被其它脚本 import 后调用：已有 QGuiApplication 时不再重复创建
    QGuiApplication.instance() or QGuiApplication(sys.argv[:1])

    preview_dir = None
    if "--preview" in argv:
        preview_dir = argv[argv.index("--preview") + 1]
        os.makedirs(preview_dir, exist_ok=True)

    big = render_icon(512)
    png512 = png_bytes(big)

    entries = []
    for size in ICO_SIZES:
        img = render_icon(size)
        payload = png_bytes(img) if size >= 128 else dib_bytes(img)
        entries.append((size, payload))
        if preview_dir:
            img.save(os.path.join(preview_dir, "{0:03d}.png".format(size)), "PNG")

    ico = ico_bytes(entries)

    for folder in (PY_DIR, WEB_DIR):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "app.ico"), "wb") as fp:
            fp.write(ico)
        with open(os.path.join(folder, "app.png"), "wb") as fp:
            fp.write(png512)

    radius, triangle = geometry(512)
    svg = SVG_TEMPLATE.format(radius=radius, triangle=path_to_svg(triangle))
    with open(os.path.join(PY_DIR, "app.svg"), "w", encoding="utf-8") as fp:
        fp.write(svg)

    print("app.ico: {0} 字节 / {1} 个尺寸 -> {2}".format(len(ico), len(ICO_SIZES), PY_DIR))
    print("app.png: {0} 字节 (512×512)".format(len(png512)))
    print("app.svg: {0} 字节".format(len(svg)))


if __name__ == "__main__":
    main(sys.argv[1:])
