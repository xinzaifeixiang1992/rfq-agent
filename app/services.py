"""依赖容器：统一装配数据库、Git4Data、记忆、模型路由与审计。"""
from __future__ import annotations

from dataclasses import dataclass

from app.audit.hook import AuditLogger
from app.config import Settings, get_settings
from app.db.conn import Database, create_database
from app.db.schema import init_schema
from app.memory import MemoryStore, create_memory
from app.pricing.rules import rule_version
from app.router.llm import LLMClient
from app.router.router import ModelRouter
from app.vcs import DataVCS, create_vcs


@dataclass
class Services:
    settings: Settings
    db: Database
    vcs: DataVCS
    memory: MemoryStore
    router: ModelRouter
    audit: AuditLogger


def build_services(settings: Settings | None = None) -> Services:
    s = settings or get_settings()
    db = create_database(s)
    init_schema(db)
    llm = LLMClient(s) if s.llm_enabled else None
    return Services(settings=s, db=db, vcs=create_vcs(db), memory=create_memory(s, db),
                    router=ModelRouter(db, llm), audit=AuditLogger(db, lambda: rule_version(db)))
