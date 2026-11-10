"""Memoria REST 客户端（Git for Memory，https://thememoria.ai）。

按官方 api-reference（matrixorigin/Memoria skills/api-reference/SKILL.md）实现：
- 记忆：GET/POST /v1/memories（cursor 分页）、/retrieve、/{id}/correct、DELETE ?reason=、/purge、/{id}/feedback
- 快照：/v1/snapshots、/{name}/diff、/{name}/rollback；分支：/v1/branches、/{name}/checkout|diff|merge
- 治理：/v1/governance、/v1/consolidate（force 为 query 参数，有冷却期）
官方记忆类型只有 semantic/profile/procedural/working/tool_result；业务 alias/episodic 类型保留在内容标签中。
客户、信任等级、来源同时写原生 subject_id/trust_tier/source（服务端可过滤）及内容标签（兼容旧服务）。
每次读写显式携带 branch，避免依赖服务端全局 checkout 状态。按 API Key 限流，429 时退避重试。
"""
from __future__ import annotations

import re
import time

import httpx

from app.memory.base import TYPE_MAP, MemoryItem, MemoryStore, decode, encode


def _name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", s)[:64]


class MemoriaError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class MemoriaStore(MemoryStore):
    backend = "memoria"
    OWNED_PREFIXES = ("m_rfq", "m_r_smoke_", "pre_merge_", "pre_quarantine_")
    RECYCLE_BATCH = 5

    def __init__(self, base_url: str, api_key: str, timeout: float = 20, retries: int = 3) -> None:
        if not (base_url and api_key):
            raise ValueError("MEMORY_BACKEND=memoria 需要 MEMORIA_URL 与 MEMORIA_API_KEY（在 thememoria.ai/app 申请）")
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"})
        self.retries = retries
        self.current_branch = "main"
        self.list_timeout = min(timeout, 8)
        self._snap_cache: dict[str, dict] = {}
        self._snap_backoff = 0.0

    def _call(self, method: str, path: str, **kw):
        for i in range(self.retries + 1):
            r = self.http.request(method, path, **kw)
            if r.status_code == 429 and i < self.retries:
                time.sleep(float(r.headers.get("Retry-After") or 2 ** i))
                continue
            break
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise MemoriaError(r.status_code, f"Memoria {method} {path} -> {r.status_code}: {str(detail)[:300]}")
        try:
            return r.json() if r.content else None
        except ValueError:
            return r.text

    @staticmethod
    def _items(payload) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for k in ("items", "results", "memories", "snapshots", "branches", "changes", "diff", "data"):
                v = payload.get(k)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):  # {"data": {"items": [...]}}
                    return MemoriaStore._items(v)
        return []

    @staticmethod
    def _id(d) -> str:
        if isinstance(d, dict):
            for k in ("memory_id", "id"):
                if d.get(k):
                    return str(d[k])
            for k in ("memory", "data"):
                if isinstance(d.get(k), dict):
                    return MemoriaStore._id(d[k])
        return ""

    @staticmethod
    def _to_item(d: dict) -> MemoryItem:
        meta, text = decode(d.get("content", ""))
        return MemoryItem(memory_id=MemoriaStore._id(d), content=text,
                          memory_type=meta.get("kind", d.get("memory_type", "semantic")),
                          customer_id=meta.get("customer", d.get("subject_id")),
                          trust_tier=meta.get("trust", d.get("trust_tier", "T3")),
                          source=meta.get("src", d.get("source", "")),
                          score=float(d.get("retrieval_score") or d.get("score") or d.get("similarity") or 0),
                          created_at=str(d.get("created_at", "") or d.get("observed_at", "")))

    # ---------- 记忆读写 ----------
    def store(self, content, kind="semantic", customer_id=None, trust_tier="T3", source=""):
        d = self._call("POST", "/v1/memories", json={
            "content": encode(content, kind, customer_id, trust_tier, source),
            "memory_type": TYPE_MAP.get(kind, "semantic"), "subject_id": customer_id,
            "trust_tier": trust_tier, "source": source or None, "branch": self.current_branch})
        return self._id(d)

    def retrieve(self, query, customer_id=None, top_k=5, kinds=None):
        body = {"query": query, "top_k": top_k * 3, "branch": self.current_branch}
        if customer_id:
            body["subject_id"] = customer_id
        # alias/episodic 被映射成 semantic；混合 kinds 时服务端原生过滤会把本地类型映射丢掉，故只在无冲突时下推
        mapped = [TYPE_MAP.get(k, "semantic") for k in kinds] if kinds else []
        if mapped and len(set(mapped)) == len(mapped):
            body["memory_types"] = mapped
        items = [self._to_item(x) for x in self._items(self._call("POST", "/v1/memories/retrieve", json=body))]
        return self._filter(items, customer_id, kinds)[:top_k]

    def list(self, limit=100):
        out, cursor = [], None
        while len(out) < limit:
            params = {"limit": min(limit - len(out), 100), "branch": self.current_branch}
            if cursor:
                params["cursor"] = cursor
            d = self._call("GET", "/v1/memories", params=params)
            page = self._items(d)
            out += [self._to_item(x) for x in page]
            cursor = d.get("next_cursor") if isinstance(d, dict) else None
            if not (cursor and page):
                break
        return out[:limit]

    def forget(self, memory_id, reason=""):
        params = {"branch": self.current_branch}
        if reason:  # 当前服务端忽略 reason，但保留参数以兼容 api-reference
            params["reason"] = reason
        self._call("DELETE", f"/v1/memories/{memory_id}", params=params)

    def purge(self, memory_ids: list[str], reason: str = "") -> dict:
        """批量删除，服务端自动建安全快照：返回 {purged, snapshot_name}。"""
        return self._call("POST", "/v1/memories/purge", json={"memory_ids": memory_ids, "reason": reason})

    def correct(self, memory_id: str, new_content: str, reason: str = "") -> None:
        self._call("PUT", f"/v1/memories/{memory_id}/correct", json={"new_content": new_content, "reason": reason})

    def feedback(self, memory_id: str, signal: str, context: str = "") -> str:
        """signal: useful / irrelevant / outdated / wrong，影响服务端检索排序。"""
        return self._id(self._call("POST", f"/v1/memories/{memory_id}/feedback",
                                   json={"signal": signal, "context": context})) or ""

    # ---------- Git for Memory ----------
    def snapshot(self, name, description=""):
        safe_name = _name(name)
        body = {"name": safe_name, "description": description}
        try:
            self._call("POST", "/v1/snapshots", json=body)
        except MemoriaError as exc:
            if exc.status != 400 or "snapshot limit reached" not in str(exc).lower():
                raise
            # 云端每用户最多 20 个快照；只回收本应用创建且已注册的最旧快照，不触碰用户手工快照。
            # 一次回收一批，避免之后每次建快照都要重新扫描列表。
            def owned(items):
                return [s for s in items
                        if s.get("registered", True) and str(s.get("name", "")).startswith(self.OWNED_PREFIXES)
                        and s.get("name") != safe_name]

            try:
                found = self._scan_snapshots(oldest_first=True, budget=45,
                                             enough=lambda xs: len(owned(xs)) >= self.RECYCLE_BATCH)
            except (httpx.TimeoutException, MemoriaError):
                found = []
            seen = {str(s.get("name")) for s in found}
            found += [s for s in self._snap_cache.values() if str(s.get("name")) not in seen]
            victims = sorted(owned(found), key=lambda s: str(s.get("created_at") or s.get("timestamp") or ""))
            if not victims:
                raise
            for s in victims[: self.RECYCLE_BATCH]:
                try:
                    self.delete_snapshot(str(s["name"]))
                except MemoriaError as del_exc:
                    if del_exc.status != 404:
                        raise
                    self._snap_cache.pop(str(s["name"]), None)
            self._call("POST", "/v1/snapshots", json=body)
        self._snap_cache[safe_name] = {"name": safe_name, "description": description, "registered": True,
                                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        return safe_name

    def _snapshot_page(self, limit: int, offset: int, timeout: float) -> tuple[list[dict], int | None]:
        d = self._call("GET", "/v1/snapshots", params={"limit": limit, "offset": offset}, timeout=timeout)
        total = d.get("total") if isinstance(d, dict) else None
        return self._items(d), (int(total) if total is not None else None)

    def _scan_snapshots(self, oldest_first=False, budget=12.0, item_timeout=3.0, enough=None) -> list[dict]:
        """逐条（limit=1）读取快照列表，跳过服务端统计条目数时超时的个别快照；列表按创建时间倒序。"""
        deadline = time.monotonic() + budget
        first, total = self._snapshot_page(1, 0, item_timeout)
        if total is None:  # 服务端未分页，已返回全量
            return first
        out: list[dict] = []
        for off in (range(total - 1, -1, -1) if oldest_first else range(total)):
            if time.monotonic() > deadline or (enough and enough(out)):
                break
            if off == 0:
                out += first
                continue
            try:
                out += self._snapshot_page(1, off, item_timeout)[0]
            except httpx.TimeoutException:
                continue
        return out

    def snapshots(self):
        """云端快照列表中个别条目统计很慢会拖住整页：先短超时取整页，失败则逐条读取并跳过慢条目；
        仍失败时 60 秒内用本进程已知列表兜底，避免拖垮页面。"""
        if time.monotonic() < self._snap_backoff:
            return list(self._snap_cache.values())
        try:
            items = self._items(self._call("GET", "/v1/snapshots", params={"limit": 100},
                                           timeout=self.list_timeout))
        except (httpx.TimeoutException, MemoriaError) as exc:
            if isinstance(exc, MemoriaError) and exc.status < 500:
                raise
            self._snap_backoff = time.monotonic() + 60
            try:
                self._snap_cache.update({str(s.get("name")): s for s in self._scan_snapshots()})
            except (httpx.TimeoutException, MemoriaError):
                pass
            return list(self._snap_cache.values())
        self._snap_cache = {str(s.get("name")): s for s in items}
        return items

    def snapshot_diff(self, name: str, limit: int = 50):
        return self._call("GET", f"/v1/snapshots/{_name(name)}/diff", params={"limit": limit})

    def delete_snapshot(self, name: str) -> None:
        self._call("DELETE", f"/v1/snapshots/{_name(name)}")
        self._snap_cache.pop(_name(name), None)

    def rollback(self, name):
        self._call("POST", f"/v1/snapshots/{_name(name)}/rollback")

    def branch(self, name):
        self._call("POST", "/v1/branches", json={"name": _name(name)})

    def branches(self) -> list[str]:
        """云端分支名列表（服务端未返回时为空列表）。"""
        items = self._items(self._call("GET", "/v1/branches"))
        out = []
        for b in items:
            name = b.get("name") if isinstance(b, dict) else b
            if name:
                out.append(str(name))
        return out

    def checkout(self, name):
        self._call("POST", f"/v1/branches/{_name(name)}/checkout")
        self.current_branch = name

    def diff(self, name, limit: int = 100):
        out = []
        d = self._call("GET", f"/v1/branches/{_name(name)}/diff-items", params={"limit": limit})
        groups = (("add", "added"), ("update", "updated"), ("delete", "removed"))
        rows = [(op, x) for op, key in groups for x in (d.get(key, []) if isinstance(d, dict) else [])]
        if not rows:  # 兼容旧服务或假服务返回 changes/items
            rows = [("add", x) for x in self._items(d)]
        for op, x in rows:
            meta, text = decode(x.get("content", "")) if isinstance(x, dict) else ({}, str(x))
            out.append({"op": op, "memory_id": self._id(x), "content": text,
                        "memory_type": meta.get("kind", x.get("memory_type") if isinstance(x, dict) else None),
                        "customer_id": meta.get("customer"), "trust_tier": meta.get("trust"),
                        "source": meta.get("src")})
        return out

    def merge(self, name):
        self._call("POST", f"/v1/branches/{_name(name)}/merge", json={"strategy": "accept"})

    def delete_branch(self, name):
        if self.current_branch == name:
            self.checkout("main")
        self._call("DELETE", f"/v1/branches/{_name(name)}")

    # ---------- 自治理（服务端：低置信度隔离、矛盾检测；有冷却期） ----------
    def governance(self, force: bool = False):
        return self._call("POST", "/v1/governance", params={"force": str(force).lower()}, json={})

    def consolidate(self, force: bool = False):
        return self._call("POST", "/v1/consolidate", params={"force": str(force).lower()}, json={})
