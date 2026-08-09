"""StableNodeIDLayer — rename-safe graph node identity.

The legacy CodeGraph id scheme ``{type}_{name}_{path_sha8}`` bakes the name and
path INTO the id, so renaming a function mints a new id and every ``calls`` /
``called_by`` edge that referenced the old name dangles.

This layer makes the id a content-independent UUID with ``name``/``path`` as
*mutable properties* plus a redirect map.  Renaming updates one node; edges keyed
by the stable id survive.  Adopt it per-graph (CodeGraph first) behind a version
flag — it's a standalone component so existing graphs are untouched until they
opt in.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class StableNodeIDManager:
    """Registry of stable node ids with mutable name/path and redirects."""

    def __init__(self, path: Optional[Path] = None, persist: bool = False):
        self._persist = persist
        self._path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._nodes: Dict[str, Dict] = {}          # stable_id -> {name, path, type, ...}
        self._redirects: Dict[str, str] = {}       # old_id -> new_id
        self._name_index: Dict[str, List[str]] = {}  # name -> [stable_id]
        self._pathname_index: Dict[str, str] = {}  # "name\x00path" -> stable_id
        # legacy (name, path) keys -> sid, kept after a rename/move so captures
        # on divergent branches still resolve the pre-rename symbol to the same
        # id (the M5 rename gate). Persisted; rebuilt on load.
        self._aliases: Dict[str, str] = {}
        if self._persist and self._path and self._path.exists():
            self._load()

    # ── (name, path) → stable id (idempotent; reindex-preserving) ────────

    @staticmethod
    def _pn_key(name: str, path: str) -> str:
        return f"{name}\x00{path}"

    def id_for(self, name: str, path: str, type_: str = "") -> str:
        """Return the stable id for ``(name, path)``, creating + registering one
        on first sight.  Idempotent — the SAME symbol gets the SAME id across
        reindexes, which is what stops renames/edits from orphaning edges."""
        with self._lock:
            key = self._pn_key(name, path)
            sid = self._pathname_index.get(key) or self._aliases.get(key)
            if sid is not None and sid in self._nodes:
                return sid
            sid = self.generate_stable_id()
            self.register_node(sid, name, path, type_)
            return sid

    def to_dict(self) -> Dict:
        with self._lock:
            return {
                "nodes": self._nodes,
                "redirects": self._redirects,
                "aliases": self._aliases,
            }

    @classmethod
    def from_dict(cls, data: Dict) -> "StableNodeIDManager":
        mgr = cls()
        mgr._nodes = dict(data.get("nodes", {}))
        mgr._redirects = dict(data.get("redirects", {}))
        mgr._aliases = dict(data.get("aliases", {}))
        for sid, node in mgr._nodes.items():
            nm = node.get("name", "")
            mgr._name_index.setdefault(nm, []).append(sid)
            mgr._pathname_index[cls._pn_key(nm, node.get("path", ""))] = sid
        return mgr

    # ── id lifecycle ────────────────────────────────────────────────────

    @staticmethod
    def generate_stable_id() -> str:
        """A content-independent id — survives renames and path moves."""
        return f"node_{uuid.uuid4().hex}"

    def register_node(
        self, stable_id: str, name: str, path: str, type_: str = "", **meta
    ) -> None:
        with self._lock:
            self._nodes[stable_id] = {
                "stable_id": stable_id, "name": name, "path": path,
                "type": type_, "renamed_from": [], "path_history": [], **meta,
            }
            self._name_index.setdefault(name, []).append(stable_id)
            self._pathname_index[self._pn_key(name, path)] = stable_id
            self._save()

    def get_node(self, stable_id: str) -> Optional[Dict]:
        with self._lock:
            return self._nodes.get(self.resolve_node_id(stable_id))

    def by_name(self, name: str) -> List[str]:
        with self._lock:
            return [self.resolve_node_id(i) for i in self._name_index.get(name, [])]

    # ── mutation that edges survive ─────────────────────────────────────

    def rename_node(self, stable_id: str, new_name: str) -> bool:
        with self._lock:
            sid = self.resolve_node_id(stable_id)
            node = self._nodes.get(sid)
            if node is None:
                return False
            old = node["name"]
            if old == new_name:
                return True
            node.setdefault("renamed_from", []).append(old)
            node["name"] = new_name
            # update name index (id is UNCHANGED — edges keyed by id survive)
            self._name_index.get(old, [])
            if sid in self._name_index.get(old, []):
                self._name_index[old].remove(sid)
            self._name_index.setdefault(new_name, []).append(sid)
            # pathname index: point the NEW (name, path) key at the id. The OLD
            # key is KEPT as a legacy alias — captures on divergent branches
            # still resolve the pre-rename symbol to the same id (two agents
            # renaming the same function must land on ONE node — the M5 rename
            # gate). A future function reusing the old name aliases the same id
            # (rare, accepted).
            path = node.get("path", "")
            if path:
                # keep the pre-rename key as a persisted legacy alias
                self._aliases[self._pn_key(old, path)] = sid
                self._pathname_index[self._pn_key(new_name, path)] = sid
            self._save()
            return True

    def move_node(self, stable_id: str, new_path: str) -> bool:
        with self._lock:
            sid = self.resolve_node_id(stable_id)
            node = self._nodes.get(sid)
            if node is None:
                return False
            old_path = node["path"]
            if old_path == new_path:
                return True
            node.setdefault("path_history", []).append(old_path)
            node["path"] = new_path
            name = node.get("name", "")
            if name:
                # New-path key added; old-path key kept as a legacy alias
                # (same rationale as rename_node).
                self._aliases[self._pn_key(name, old_path)] = sid
                self._pathname_index[self._pn_key(name, new_path)] = sid
            self._save()
            return True

    def add_redirect(self, old_id: str, new_id: str) -> None:
        """Point ``old_id`` at ``new_id`` (e.g. after a merge/dedup)."""
        with self._lock:
            self._redirects[old_id] = new_id
            self._save()

    def resolve_node_id(self, node_id: str) -> str:
        seen = set()
        cur = node_id
        with self._lock:
            while cur in self._redirects and cur not in seen:
                seen.add(cur)
                cur = self._redirects[cur]
        return cur

    # ── persistence (optional) ──────────────────────────────────────────

    def _save(self) -> None:
        if not (self._persist and self._path):
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "nodes": self._nodes,
                "redirects": self._redirects,
                "aliases": self._aliases,
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as exc:
            logger.warning("[StableNodeIDLayer] _save failed: %s", exc)

    def _load(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            self._nodes = payload.get("nodes", {})
            self._redirects = payload.get("redirects", {})
            self._aliases = payload.get("aliases", {})
            for sid, node in self._nodes.items():
                nm = node.get("name", "")
                self._name_index.setdefault(nm, []).append(sid)
                # from_dict rebuilds this index; the file-load path MUST too, or
                # a reloaded manager mints NEW ids for existing symbols (breaks
                # idempotency — the semantic-VCS op-log depends on it).
                self._pathname_index[self._pn_key(nm, node.get("path", ""))] = sid
        except Exception as exc:
            logger.warning("[StableNodeIDLayer] _load failed: %s", exc)
