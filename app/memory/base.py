from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass

# 业务记忆类型 -> Memoria 记忆类型（官方仅 semantic/profile/procedural/working/tool_result；业务类型保留在内容标签中）
TYPE_MAP = {"profile": "profile", "alias": "semantic", "procedural": "procedural",
            "episodic": "semantic", "semantic": "semantic", "working": "working"}
# 信任等级：T1 主数据/人工确认 · T2 系统内审批产生 · T3 评审通过的外部信息 · T4 未经验证
TRUST_ORDER = {"T1": 1, "T2": 2, "T3": 3, "T4": 4}

_TAG = re.compile(r"^\[(\w+):([^\]]*)\]")


@dataclass
class MemoryItem:
    memory_id: str
    content: str
    memory_type: str = "semantic"
    customer_id: str | None = None
    trust_tier: str = "T3"
    source: str = ""
    score: float = 0.0
    created_at: str = ""


def encode(content: str, kind: str, customer_id: str | None, trust: str, source: str) -> str:
    """把业务元数据编码进内容前缀，保证在 Memoria 中也可过滤与追溯。"""
    tags = f"[kind:{kind}]" + (f"[customer:{customer_id}]" if customer_id else "") + \
           f"[trust:{trust}]" + (f"[src:{source}]" if source else "")
    return f"{tags} {content}"


def decode(raw: str) -> tuple[dict, str]:
    meta, s = {}, raw.strip()
    while (m := _TAG.match(s)):
        meta[m.group(1)] = m.group(2)
        s = s[m.end():]
    return meta, s.strip()


def tokens(text: str) -> set[str]:
    text = text.lower()
    words = set(re.findall(r"[a-z0-9][a-z0-9\-\.]+", text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return words | {a + b for a, b in zip(cjk, cjk[1:])}


def rank(items: list[MemoryItem], query: str, customer_id: str | None, top_k: int) -> list[MemoryItem]:
    """词重合度 + 同客户加分，同分按时间倒序。本地与 Git4Data 后端共用。"""
    q = tokens(query)
    scored = []
    for it in items:
        overlap = len(q & tokens(it.content))
        bonus = 1.0 if customer_id and it.customer_id == customer_id else 0.0
        if overlap or bonus:
            scored.append((overlap + bonus, it.created_at, it))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [MemoryItem(**{**it.__dict__, "score": s}) for s, _, it in scored[:top_k]]


class MemoryStore:
    backend = ""
    current_branch = "main"

    def store(self, content: str, kind: str = "semantic", customer_id: str | None = None,
              trust_tier: str = "T3", source: str = "") -> str: ...
    def retrieve(self, query: str, customer_id: str | None = None, top_k: int = 5,
                 kinds: list[str] | None = None) -> list[MemoryItem]: ...
    def list(self, limit: int = 100) -> list[MemoryItem]: ...
    def forget(self, memory_id: str, reason: str = "") -> None: ...
    def snapshot(self, name: str, description: str = "") -> str: ...
    def snapshots(self) -> list[dict]: ...
    def rollback(self, name: str) -> None: ...
    def branch(self, name: str) -> None: ...
    def checkout(self, name: str) -> None: ...
    def diff(self, name: str) -> list[dict]: ...
    def merge(self, name: str) -> None: ...
    def delete_branch(self, name: str) -> None: ...

    @contextmanager
    def on_branch(self, name: str):
        """临时切到指定分支读写，退出时切回原分支。"""
        prev = self.current_branch
        self.checkout(name)
        try:
            yield self
        finally:
            self.checkout(prev)

    @staticmethod
    def _filter(items: list[MemoryItem], customer_id: str | None, kinds: list[str] | None) -> list[MemoryItem]:
        out = []
        for it in items:
            if customer_id and it.customer_id and it.customer_id != customer_id:
                continue
            if kinds and it.memory_type not in kinds:
                continue
            out.append(it)
        return out


def trusted(items: list[MemoryItem], max_tier: str) -> list[MemoryItem]:
    """只保留信任等级不低于 max_tier 的记忆（T1 最高）。"""
    return [it for it in items if TRUST_ORDER.get(it.trust_tier, 4) <= TRUST_ORDER[max_tier]]
