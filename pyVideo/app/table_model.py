"""表格的模型层：行数据、排序、筛选与缩略图缓存。

为什么不再用 QTableWidget：
原有的 `QTableWidget` 每个单元格是一个 `QTableWidgetItem` 对象，两万行就是
十二万个对象；排序要整表重排控件、筛选靠 `setRowHidden` 逐行开关，任务进行中
还要把排序关掉再打开，行数一多就会明显卡顿。

改成 Model/View 后：
* 行数据只在 `records` 字典里存一份，模型只负责"怎么取"，不复制数据；
* 排序由模型重排 uid 列表完成，视图只重绘可见区域；
* 筛选交给 `QSortFilterProxyModel`，不再逐行 setRowHidden；
* 缩略图以 QPixmap 挂在模型上并按上限淘汰，滚动长列表不会无限占内存。
"""

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)

from . import theme
from .utils import human_size
from .widgets import THUMB_ROLE, UID_ROLE, status_kind, thumb_placeholder

# 与视图约定的自定义角色（定义在 widgets 里，这里直接复用）
# UID_ROLE：行唯一 id；SORT_ROLE：排序键；THUMB_ROLE：缩略图 QPixmap
SORT_ROLE = Qt.UserRole + 2

# 勾选列与 C++ 版一样排在最左：这一列的"勾选态"就是行选中态，
# 勾选框由 StatusPillDelegate 按 QStyle.State_Selected 画，表头由
# CheckHeaderView 自绘，三者共用同一个状态，不需要第二套数据结构。
COLUMN_TITLES = ["", "序号", "预览", "文件名", "大小", "状态", "来源文件夹"]
COL_CHECK = 0
COL_SEQ = 1
COL_THUMB = 2
COL_NAME = 3
COL_SIZE = 4
COL_STATUS = 5
COL_DIR = 6


class VideoTableModel(QAbstractTableModel):
    """以 uid 为行的表格模型；数据本体留在 MainWindow.rows 里。"""

    # 状态文本 → 排序权重，让失败/跳过的行能聚在一起，而不是按拼音乱排
    _KIND_ORDER = {"fail": 0, "skip": 1, "pending": 2, "success": 3,
                   "inplace": 4, "deleted": 5}

    def __init__(self, records, parent=None):
        super().__init__(parent)
        self._records = records          # uid -> dict（外部持有）
        self._uids = []                  # 当前显示顺序
        self._index_of = {}              # uid -> 行号，增删/排序后重建
        self._thumbs = {}                # uid -> QPixmap（原始缓存图，未裁切）
        self._thumb_order = []           # 简化的 LRU 顺序
        self._thumb_limit = 400          # 最多缓存多少张，超出淘汰最旧的

    # -- Qt 模型接口 --------------------------------------------------- #
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._uids)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMN_TITLES)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMN_TITLES[section]
        if role == Qt.TextAlignmentRole:
            # 「大小」列数字右对齐，表头跟着右对齐才在同一条基线上；
            # 勾选列的表头是全选框，居中
            if section == COL_SIZE:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if section == COL_CHECK:
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row < 0 or row >= len(self._uids):
            return None
        uid = self._uids[row]
        record = self._records.get(uid) or {}

        if role == UID_ROLE:
            return uid
        if role == Qt.DisplayRole:
            if col == COL_CHECK:
                return None
            if col == COL_SEQ:
                return str(record.get("seq", row + 1))
            if col == COL_THUMB:
                return None
            if col == COL_NAME:
                return record.get("name", "")
            if col == COL_SIZE:
                return human_size(record.get("size", 0))
            if col == COL_STATUS:
                return record.get("status", "")
            if col == COL_DIR:
                return record.get("dir", "")
            return None
        if role == SORT_ROLE:
            if col == COL_SEQ:
                return int(record.get("seq", row + 1))
            if col == COL_NAME:
                return str(record.get("name", "")).lower()
            if col == COL_SIZE:
                return int(record.get("size", 0) or 0)
            if col == COL_STATUS:
                status = record.get("status", "")
                return (self._KIND_ORDER.get(status_kind(status), 9), status)
            if col == COL_DIR:
                return str(record.get("dir", "")).lower()
            # 勾选列不参与排序：点它只切换勾选，标题也不显示箭头
            return row
        if role == Qt.ToolTipRole:
            if col == COL_NAME:
                return record.get("name", "")
            if col == COL_STATUS:
                return record.get("status", "")
            if col == COL_DIR:
                return record.get("dir", "")
            if col == COL_THUMB:
                return "缩略图预览" if uid in self._thumbs else "正在生成缩略图…"
            if col == COL_CHECK:
                return "勾选（当前所选）"
            return None
        if role == Qt.TextAlignmentRole:
            if col == COL_SEQ or col == COL_CHECK:
                return int(Qt.AlignCenter)
            if col == COL_SIZE:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if col == COL_SEQ:
                return theme.COLORS["text_mute"]
            if col == COL_NAME:
                return theme.COLORS["text"]
            return None
        if role == THUMB_ROLE:
            return self._thumbs.get(uid) if col == COL_THUMB else None
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    # -- 数据维护 ------------------------------------------------------- #
    def reset_rows(self):
        self.beginResetModel()
        self._uids = []
        self._index_of = {}
        self._thumbs.clear()
        self._thumb_order = []
        self.endResetModel()

    def append_uids(self, uids):
        """批量追加，一次性完成插入通知（而不是逐条 insertRow）。"""
        uids = [uid for uid in uids if uid is not None]
        if not uids:
            return
        start = len(self._uids)
        self.beginInsertRows(QModelIndex(), start, start + len(uids) - 1)
        self._uids.extend(uids)
        for offset, uid in enumerate(uids):
            self._index_of[uid] = start + offset
        self.endInsertRows()

    def uid_at(self, row):
        if 0 <= row < len(self._uids):
            return self._uids[row]
        return None

    def row_of(self, uid):
        return self._index_of.get(uid)

    def record(self, uid):
        return self._records.get(uid)

    def set_status(self, uid, status, target=None):
        record = self._records.get(uid)
        if record is None:
            return
        record["status"] = status
        if target is not None:
            record["target"] = target or ""
        row = self._index_of.get(uid)
        if row is None:
            return
        left = self.index(row, COL_CHECK)
        right = self.index(row, COL_DIR)
        self.dataChanged.emit(
            left, right, [Qt.DisplayRole, Qt.ToolTipRole, SORT_ROLE])

    def set_thumb(self, uid, pixmap):
        if pixmap is None or pixmap.isNull():
            return
        if uid not in self._thumbs:
            self._thumb_order.append(uid)
            # 超出上限就丢最旧的：长列表滚动几万行也不会把内存吃满
            while len(self._thumb_order) > self._thumb_limit:
                old = self._thumb_order.pop(0)
                self._thumbs.pop(old, None)
        self._thumbs[uid] = pixmap
        row = self._index_of.get(uid)
        if row is not None:
            left = self.index(row, COL_THUMB)
            self.dataChanged.emit(left, left, [THUMB_ROLE, Qt.ToolTipRole])

    def forget_thumbs(self):
        self._thumbs.clear()
        self._thumb_order = []

    def refresh_theme(self):
        """换主题后把文字色、占位图重画一遍。"""
        if not self._uids:
            return
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(self._uids) - 1, len(COLUMN_TITLES) - 1),
            [Qt.ForegroundRole, THUMB_ROLE])

    def sort(self, column, order=Qt.AscendingOrder):
        """按某一列重排；视图点击表头时由代理转发过来。"""
        if not self._uids:
            return
        reverse = order == Qt.DescendingOrder

        def key(uid):
            record = self._records.get(uid) or {}
            if column == COL_SEQ:
                return int(record.get("seq", 0))
            if column == COL_NAME:
                return str(record.get("name", "")).lower()
            if column == COL_SIZE:
                return int(record.get("size", 0) or 0)
            if column == COL_STATUS:
                status = record.get("status", "")
                return (self._KIND_ORDER.get(status_kind(status), 9), status)
            if column == COL_DIR:
                return str(record.get("dir", "")).lower()
            return int(record.get("seq", 0))

        self.layoutAboutToBeChanged.emit()
        self._uids.sort(key=key, reverse=reverse)
        self._index_of = {uid: row for row, uid in enumerate(self._uids)}
        self.layoutChanged.emit()


class VideoFilterProxy(QSortFilterProxyModel):
    """关键字 + 状态胶囊双重筛选。

    筛选在代理层完成：源模型不增不删，撤销筛选是一次 `invalidateFilter`，
    比原来逐行 `setRowHidden` 快一个量级。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keyword = ""
        self._status = None
        self.setDynamicSortFilter(True)

    # -- 筛选条件 ------------------------------------------------------- #
    def set_filter(self, keyword="", status=None):
        keyword = (keyword or "").strip().lower()
        if keyword == self._keyword and status == self._status:
            return
        self._keyword = keyword
        self._status = status
        self.invalidateFilter()

    def keyword(self):
        return self._keyword

    def status_filter(self):
        return self._status

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        if model is None:
            return True
        uid = model.uid_at(source_row)
        record = model.record(uid) or {}
        if self._keyword:
            haystack = "{0}\n{1}\n{2}".format(
                record.get("name", ""), record.get("path", ""),
                record.get("status", "")).lower()
            if self._keyword not in haystack:
                return False
        if self._status and status_kind(record.get("status", "")) != self._status:
            return False
        return True

    # -- 排序转发 ------------------------------------------------------- #
    def sort(self, column, order=Qt.AscendingOrder):
        model = self.sourceModel()
        if model is not None:
            model.sort(column, order)

    # -- 行/uid 映射 ---------------------------------------------------- #
    def uid_at(self, proxy_row):
        model = self.sourceModel()
        if model is None or proxy_row < 0 or proxy_row >= self.rowCount():
            return None
        source_row = self.mapToSource(self.index(proxy_row, 0)).row()
        return model.uid_at(source_row)

    def proxy_row_of(self, uid):
        model = self.sourceModel()
        if model is None:
            return None
        source_row = model.row_of(uid)
        if source_row is None:
            return None
        index = self.mapFromSource(model.index(source_row, 0))
        return index.row() if index.isValid() else None


__all__ = [
    "COLUMN_TITLES", "COL_CHECK", "COL_SEQ", "COL_THUMB", "COL_NAME",
    "COL_SIZE", "COL_STATUS", "COL_DIR",
    "SORT_ROLE", "THUMB_ROLE", "UID_ROLE",
    "VideoTableModel", "VideoFilterProxy",
]
