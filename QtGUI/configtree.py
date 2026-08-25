"""Config Browser tree model (Plan §2.4).

QAbstractItemModel over the live ODrive object graph for the QML TreeView
(qml/ConfigBrowserDialog.qml). Structure is walked lazily per subtree
expansion via ``dir()`` + ``getattr``; scalar values are read in the same
walk and cached until Refresh or a write through ``invalidate()``.
Editable = leaves under a ``.config`` object that are bool/int/float —
everything else is read-only display. The filter matches names only: it
classifies branches via fibre class-member introspection without reading
scalar endpoint values over USB. Transport failures anywhere in the walk
drop the link exactly like the poll does (Plan §4.1).

# ponytail: the EXPANSION walk getattr()s every child, which also reads
# scalar values (~40 reads per expansion). Fine at the measured ~3.8 kHz
# read rate (Plan §3.4); the filter scan is name-only (see _peek).
"""

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Slot

from errors import LINK_FAILURES

try:
    from fibre.libfibre import RemoteAttribute
except ImportError:  # mocks/tests run without pyfibre on sys.path
    RemoteAttribute = None

MAX_DEPTH = 7  # deepest fw chain is 5 levels (Plan §2.4); +2 slack

LEAF_TYPES = (bool, int, float, str)
EDITABLE_TYPES = (bool, int, float)  # str config leaves stay read-only

_NAME_ROLE = Qt.ItemDataRole.UserRole + 1
_VALUE_ROLE = _NAME_ROLE + 1
_PATH_ROLE = _NAME_ROLE + 2
_EDITABLE_ROLE = _NAME_ROLE + 3


class _Node:
    __slots__ = ("built", "children", "depth", "in_config", "name", "obj",
                 "parent", "path", "row", "value")

    def __init__(self, name, obj, parent, row, depth, in_config):
        self.name = name
        self.path = None       # dotted path below the device root ("" = root)
        self.obj = obj         # device object for branches, None for leaves
        self.parent = parent
        self.row = row         # row index within parent.children
        self.depth = depth     # path components below the device root
        self.in_config = in_config
        self.children = []
        self.built = False
        self.value = None


class ConfigTreeModel(QAbstractItemModel):
    """Read/write view of the connected ODrive's object graph."""

    def __init__(self, backend):
        super().__init__(backend)
        self._backend = backend
        self._filter = ""
        self._root = _Node("", None, None, 0, 0, False)

    # -- lifecycle ------------------------------------------------------

    @Slot()
    def reset(self):
        """Re-root on the current device and drop all caches."""
        self._rebuild(self._backend.odrive)

    @Slot(str)
    def set_filter(self, text):
        text = text.strip().lower()
        if text == self._filter:
            return
        self._filter = text
        self._rebuild(self._root.obj)

    def _rebuild(self, root_obj):
        self.beginResetModel()
        self._root = _Node("", root_obj, None, 0, 0, False)
        self.endResetModel()

    # -- structure walk -------------------------------------------------

    def _public_names(self, obj):
        try:
            return sorted(n for n in dir(obj) if not n.startswith("_"))
        except LINK_FAILURES as e:
            self._backend._drop_link("configBrowser", e)
            return []
        except (AttributeError, TypeError):
            return []  # object lost / torn down mid-walk: nothing to list

    def _build_children(self, node):
        if node.built or node.obj is None:
            node.built = True
            return
        node.built = True
        row = 0
        base_path = node.path
        for name in self._public_names(node.obj):
            try:
                value = getattr(node.obj, name)
            except LINK_FAILURES as e:
                self._backend._drop_link("configBrowser", e)
                return  # stop the walk; link is down, reconnect pending
            except (AttributeError, TypeError):
                continue  # endpoint vanished / unreadable: leave it out
            if callable(value):
                continue
            child_path = f"{base_path}.{name}" if base_path else name
            in_config = node.in_config or name == "config"
            if isinstance(value, LEAF_TYPES):
                child = _Node(name, None, node, row, node.depth + 1, in_config)
                child.value = value
            elif node.depth < MAX_DEPTH:
                child = _Node(name, value, node, row, node.depth + 1, in_config)
            else:
                continue  # depth cap: not traversed
            child.path = child_path
            if self._keep(child):
                node.children.append(child)
                row += 1

    def _keep(self, child):
        """Filter gate: name matches, or a descendant name matches."""
        t = self._filter
        if not t or t in child.name.lower():
            return True
        if child.obj is None:
            return False
        return self._descendant_match(child.obj, t, child.depth + 1)

    def _descendant_match(self, obj, t, depth):
        if depth > MAX_DEPTH + 1:
            return False
        for name in self._public_names(obj):
            if t in name.lower():
                return True
            v = self._peek(obj, name)
            if v is not None and self._descendant_match(v, t, depth + 1):
                return True
        return False

    def _peek(self, obj, name):
        """Branch-child getter that never reads scalar endpoint VALUES.

        Fibre exposes attribute types on the class: a RemoteAttribute with
        `_magic_getter` set is a scalar endpoint (reading it costs USB
        traffic) -> skipped. Without it the attribute is object-typed and
        its proxy loads locally, no device round-trip. Plain/mock objects
        (and fibre members of unknown kind) classify via getattr.
        """
        cls_member = getattr(type(obj), name, None)
        if (RemoteAttribute is not None and isinstance(cls_member, RemoteAttribute)
                and cls_member._magic_getter):
            return None  # scalar: name already match-checked above
        try:
            v = getattr(obj, name)
        except LINK_FAILURES as e:
            self._backend._drop_link("configBrowser", e)
            return None
        except (AttributeError, TypeError):
            return None
        return None if callable(v) or isinstance(v, LEAF_TYPES) else v

    # -- model plumbing --------------------------------------------------

    def roleNames(self):
        return {
            _NAME_ROLE: b"name",
            _VALUE_ROLE: b"value",
            _PATH_ROLE: b"path",
            _EDITABLE_ROLE: b"editable",
        }

    def columnCount(self, parent=QModelIndex()):
        return 1

    def index(self, row, col, parent=QModelIndex()):
        pnode = parent.internalPointer() if parent.isValid() else self._root
        self._build_children(pnode)
        if 0 <= row < len(pnode.children):
            return self.createIndex(row, col, pnode.children[row])
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        p = node.parent
        if p is None or p.parent is None:
            return QModelIndex()
        return self.createIndex(p.row, 0, p)

    def rowCount(self, parent=QModelIndex()):
        node = parent.internalPointer() if parent.isValid() else self._root
        self._build_children(node)
        return len(node.children)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == _NAME_ROLE:
            return node.name
        if role == _VALUE_ROLE:
            return str(node.value) if node.obj is None else ""
        if role == _PATH_ROLE:
            return node.path or ""
        if role == _EDITABLE_ROLE:
            return (node.obj is None and node.in_config
                    and isinstance(node.value, EDITABLE_TYPES))
        return None

    # -- write support ----------------------------------------------------

    def invalidate(self, path, value):
        """Update one leaf's cached value after an external write."""
        node = self._node_at(path)
        if node is None or node.obj is not None:
            return
        node.value = value
        idx = self._index_of(node)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def _node_at(self, path):
        """Walk `path` down the built tree; None when absent/unfiltered."""
        node = self._root
        for part in path.split("."):
            self._build_children(node)
            node = next((c for c in node.children if c.name == part), None)
            if node is None:
                return None
        return node

    def _index_of(self, node):
        chain = []
        n = node
        while n.parent is not None:
            chain.append(n)
            n = n.parent
        idx = QModelIndex()
        for anc in reversed(chain):
            idx = self.index(anc.row, 0, idx)
            if not idx.isValid():
                return QModelIndex()
        return idx

    def resolve(self, path):
        """Re-walk `path` from the live device root.

        Returns (container_obj, attr_name, current_value), or None when the
        path doesn't exist, isn't under .config, or isn't editable-typed.
        """
        parts = path.split(".")
        if "config" not in parts:
            return None
        obj = self._root.obj
        if obj is None:
            return None
        try:
            for part in parts[:-1]:
                obj = getattr(obj, part)
            cur = getattr(obj, parts[-1])
        except LINK_FAILURES as e:
            self._backend._drop_link("configBrowser", e)
            return None
        except (AttributeError, TypeError):
            return None
        if isinstance(cur, EDITABLE_TYPES):
            return obj, parts[-1], cur
        return None
