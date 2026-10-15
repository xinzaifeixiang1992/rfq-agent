"""按官方 api-reference 实现的 Memoria 内存版假服务（httpx.MockTransport），用于无 API Key 时校验客户端协议。

严格校验：Bearer 鉴权、store 请求体字段与 memory_type 取值、分支/合并请求体、governance 的 force 为 query 参数；
首次请求返回 429 以覆盖限流重试。响应格式中官方未写明的部分（branch diff 条目）按客户端的宽松解析约定。
"""
from __future__ import annotations

import copy
import itertools
import json
import uuid

import httpx

from app.memory.base import tokens

TYPES = {"semantic", "profile", "procedural", "working", "tool_result"}


class FakeMemoria(httpx.MockTransport):
    def __init__(self) -> None:
        super().__init__(self.handle)
        self.branches: dict[str, dict[str, dict]] = {"main": {}}
        self.current = "main"
        self.snaps: dict[str, dict[str, dict]] = {}
        self.snap_seq: dict[str, int] = {}
        self._seq = itertools.count()
        self.snap_limit = 20
        self.slow_snaps: set[str] = set()  # 模拟云端统计条目数很慢、整页读取超时的快照
        self.calls: list[tuple[str, str]] = []
        self.throttled = False

    @staticmethod
    def _r(status: int, body=None) -> httpx.Response:
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization") != "Bearer test-key":
            return self._r(401, {"detail": "invalid api key"})
        if not self.throttled:
            self.throttled = True
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "rate limited"})
        m, p, q = req.method, req.url.path, req.url.params
        body = json.loads(req.content) if req.content else {}
        self.calls.append((m, p))
        mem = self.branches[self.current]
        parts = p.strip("/").split("/")

        if p == "/v1/memories" and m == "POST":
            if not set(body) <= {"content", "memory_type", "session_id", "subject_id", "trust_tier", "source", "branch"} or \
                    body.get("memory_type", "semantic") not in TYPES:
                return self._r(422, {"detail": f"invalid body {sorted(body)} / {body.get('memory_type')}"})
            mem = self.branches[body.get("branch") or "main"]
            mid = uuid.uuid4().hex
            mem[mid] = {"memory_id": mid, "content": body["content"],
                        "memory_type": body.get("memory_type", "semantic"), "subject_id": body.get("subject_id"),
                        "trust_tier": body.get("trust_tier", "T1"), "source": body.get("source"),
                        "created_at": f"{len(mem):08d}"}
            return self._r(201, mem[mid])
        if p == "/v1/memories" and m == "GET":
            mem = self.branches[q.get("branch") or "main"]
            items = list(mem.values())
            start, limit = int(q.get("cursor") or 0), int(q.get("limit") or 50)
            if limit > 100:
                return self._r(422, {"detail": "limit"})
            nxt = start + limit
            return self._r(200, {"items": items[start:nxt], "next_cursor": str(nxt) if nxt < len(items) else None})
        if p == "/v1/memories/retrieve" and m == "POST":
            mem = self.branches[body.get("branch") or "main"]
            qt, types = tokens(body["query"]), set(body.get("memory_types") or TYPES)
            subject = body.get("subject_id")
            scored = [(len(qt & tokens(x["content"])) + (1 if subject and x.get("subject_id") == subject else 0), x)
                      for x in mem.values()
                      if x["memory_type"] in types and (not subject or x.get("subject_id") == subject)]
            scored = sorted([s for s in scored if s[0]], key=lambda s: s[0], reverse=True)[: body.get("top_k", 10)]
            return self._r(200, [{**x, "retrieval_score": s} for s, x in scored])
        if parts[:2] == ["v1", "memories"] and len(parts) == 3 and m == "DELETE":
            mem = self.branches[q.get("branch") or "main"]
            return self._r(204) if mem.pop(parts[2], None) else self._r(404, {"detail": "not found"})

        if p == "/v1/snapshots" and m == "POST":
            if len(self.snaps) >= self.snap_limit:
                return self._r(400, {"detail": f"Snapshot limit reached ({self.snap_limit}) for user u. "
                                               "Delete old snapshots first."})
            self.snaps[body["name"]] = copy.deepcopy(self.branches["main"])
            self.snap_seq[body["name"]] = next(self._seq)
            return self._r(201, {"name": body["name"]})
        if p == "/v1/snapshots" and m == "GET":  # 与云端一致：按创建倒序，limit 默认 20，带 total
            names = sorted(self.snaps, key=lambda n: self.snap_seq[n], reverse=True)
            limit, offset = int(q.get("limit") or 20), int(q.get("offset") or 0)
            page = names[offset: offset + limit]
            if self.slow_snaps & set(page):
                raise httpx.ReadTimeout("snapshot memory_count backfill", request=req)
            return self._r(200, {"snapshots": [{"name": n, "registered": True, "created_at": f"{self.snap_seq[n]:08d}"}
                                               for n in page], "total": len(names), "limit": limit, "offset": offset})
        if parts[:2] == ["v1", "snapshots"] and len(parts) == 3 and m == "DELETE":
            self.snap_seq.pop(parts[2], None)
            return self._r(204) if self.snaps.pop(parts[2], None) is not None else self._r(404, {"detail": "not found"})
        if parts[:2] == ["v1", "snapshots"] and parts[-1] == "rollback":
            if parts[2] not in self.snaps:
                return self._r(404, {"detail": "snapshot not found"})
            self.branches["main"] = copy.deepcopy(self.snaps[parts[2]])
            return self._r(200, {"ok": True})

        if p == "/v1/branches" and m == "POST":
            if set(body) != {"name"}:
                return self._r(422, {"detail": "branch body"})
            self.branches[body["name"]] = copy.deepcopy(self.branches["main"])
            return self._r(201, {"name": body["name"]})
        if parts[:2] == ["v1", "branches"] and len(parts) >= 3:
            name = parts[2]
            if name not in self.branches:
                return self._r(404, {"detail": "branch not found"})
            act = parts[3] if len(parts) > 3 else ""
            if act == "checkout":
                self.current = name
                return self._r(200, {"branch": name})
            if act in ("diff", "diff-items"):
                main = self.branches["main"]
                added = [x for k, x in self.branches[name].items() if k not in main]
                return self._r(200, {"added": added, "updated": [], "removed": []})
            if act == "merge":
                if body != {"strategy": "accept"}:
                    return self._r(422, {"detail": "merge body"})
                for k, x in self.branches[name].items():
                    self.branches["main"].setdefault(k, x)
                return self._r(200, {"merged": True})
            if m == "DELETE" and name != "main":
                self.branches.pop(name)
                return self._r(204)
        if p in ("/v1/governance", "/v1/consolidate") and m == "POST":
            return self._r(200, {"ok": True}) if "force" in q and body == {} else self._r(422, {"detail": "force"})
        return self._r(404, {"detail": f"no route {m} {p}"})
