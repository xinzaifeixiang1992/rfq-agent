"""表结构定义。DDL 同时兼容 SQLite 与 MatrixOne（MySQL 语法子集）。"""
from __future__ import annotations

from app.db.conn import Database

# 参与 Git4Data 版本管理的表及其主键（DIFF/MERGE/PICK 按主键对齐）
VERSIONED_PK: dict[str, str] = {
    "quote_line": "line_key",
    "cost_price": "sku",
    "pricing_rule": "rule_key",
    "model_route": "task",
    "mem_main": "memory_id",
}

DDL: dict[str, str] = {
    "customer": """
        CREATE TABLE IF NOT EXISTS customer (
            customer_id VARCHAR(32) PRIMARY KEY,
            name VARCHAR(128),
            email_domain VARCHAR(128),
            tier VARCHAR(16),
            region VARCHAR(32),
            payment_terms VARCHAR(32),
            win_rate DOUBLE
        )""",
    "product_sku": """
        CREATE TABLE IF NOT EXISTS product_sku (
            sku VARCHAR(32) PRIMARY KEY,
            name VARCHAR(256),
            spec VARCHAR(256),
            unit VARCHAR(16),
            category VARCHAR(64),
            aliases TEXT
        )""",
    "cost_price": """
        CREATE TABLE IF NOT EXISTS cost_price (
            sku VARCHAR(32) PRIMARY KEY,
            unit_cost DOUBLE,
            currency VARCHAR(8),
            supplier VARCHAR(64),
            updated_at VARCHAR(32)
        )""",
    "quote_history": """
        CREATE TABLE IF NOT EXISTS quote_history (
            id VARCHAR(48) PRIMARY KEY,
            customer_id VARCHAR(32),
            sku VARCHAR(32),
            qty DOUBLE,
            unit_price DOUBLE,
            currency VARCHAR(8),
            quoted_at VARCHAR(32),
            won INT,
            deal_price DOUBLE
        )""",
    "fx_rate": """
        CREATE TABLE IF NOT EXISTS fx_rate (
            currency VARCHAR(8) PRIMARY KEY,
            rate_to_cny DOUBLE,
            updated_at VARCHAR(32)
        )""",
    "pricing_rule": """
        CREATE TABLE IF NOT EXISTS pricing_rule (
            rule_key VARCHAR(64) PRIMARY KEY,
            rule_value VARCHAR(256),
            version VARCHAR(16),
            note VARCHAR(256)
        )""",
    "model_catalog": """
        CREATE TABLE IF NOT EXISTS model_catalog (
            model VARCHAR(64) PRIMARY KEY,
            kind VARCHAR(16),
            cost_per_1k DOUBLE
        )""",
    "model_route": """
        CREATE TABLE IF NOT EXISTS model_route (
            task VARCHAR(64) PRIMARY KEY,
            primary_model VARCHAR(64),
            escalate_model VARCHAR(64),
            fallback_model VARCHAR(64),
            confidence_threshold DOUBLE,
            version VARCHAR(16)
        )""",
    "rfq_record": """
        CREATE TABLE IF NOT EXISTS rfq_record (
            rfq_id VARCHAR(48) PRIMARY KEY,
            customer_id VARCHAR(32),
            subject VARCHAR(512),
            status VARCHAR(32),
            header_json TEXT,
            data_snapshot VARCHAR(128),
            memory_snapshot VARCHAR(128),
            created_at VARCHAR(32),
            updated_at VARCHAR(32)
        )""",
    "quote_line": """
        CREATE TABLE IF NOT EXISTS quote_line (
            line_key VARCHAR(96) PRIMARY KEY,
            rfq_id VARCHAR(48),
            line_no INT,
            sku VARCHAR(32),
            description VARCHAR(512),
            qty DOUBLE,
            unit VARCHAR(16),
            unit_cost DOUBLE,
            unit_price DOUBLE,
            currency VARCHAR(8),
            margin DOUBLE,
            strategy VARCHAR(32),
            win_prob DOUBLE,
            rationale TEXT
        )""",
    "decision_log": """
        CREATE TABLE IF NOT EXISTS decision_log (
            decision_id VARCHAR(48) PRIMARY KEY,
            rfq_id VARCHAR(48),
            node VARCHAR(32),
            model VARCHAR(256),
            prompt_ver VARCHAR(16),
            rule_ver VARCHAR(16),
            data_snapshot VARCHAR(128),
            memory_snapshot VARCHAR(128),
            input_digest VARCHAR(64),
            evidence_json TEXT,
            output_json TEXT,
            confidence DOUBLE,
            latency_ms INT,
            tokens INT,
            cost DOUBLE,
            human_action VARCHAR(32),
            override_json TEXT,
            created_at VARCHAR(32)
        )""",
    # 本地记忆后端（MEMORY_BACKEND=local）使用；Memoria 后端时不使用
    "memory_item": """
        CREATE TABLE IF NOT EXISTS memory_item (
            memory_id VARCHAR(48),
            branch VARCHAR(64),
            memory_type VARCHAR(16),
            content TEXT,
            customer_id VARCHAR(32),
            trust_tier VARCHAR(8),
            source VARCHAR(128),
            created_at VARCHAR(32),
            PRIMARY KEY (memory_id, branch)
        )""",
    "memory_snapshot": """
        CREATE TABLE IF NOT EXISTS memory_snapshot (
            snapshot_name VARCHAR(128),
            memory_id VARCHAR(48),
            memory_type VARCHAR(16),
            content TEXT,
            customer_id VARCHAR(32),
            trust_tier VARCHAR(8),
            source VARCHAR(128),
            created_at VARCHAR(32),
            PRIMARY KEY (snapshot_name, memory_id)
        )""",
    # Git4Data 记忆后端（MEMORY_BACKEND=git4data）：记忆主线是一张受版本管理的表，
    # 分支 = DATA BRANCH 表，快照 = 表级 SNAPSHOT，回滚 = RESTORE
    "mem_main": """
        CREATE TABLE IF NOT EXISTS mem_main (
            memory_id VARCHAR(48) PRIMARY KEY,
            memory_type VARCHAR(16),
            content TEXT,
            customer_id VARCHAR(32),
            trust_tier VARCHAR(8),
            source VARCHAR(128),
            created_at VARCHAR(32)
        )""",
    "mem_snapshot_meta": """
        CREATE TABLE IF NOT EXISTS mem_snapshot_meta (
            name VARCHAR(128) PRIMARY KEY,
            vcs_name VARCHAR(64),
            description VARCHAR(512),
            created_at VARCHAR(32)
        )""",
    # MemoryGate 评审队列：每个外部候选/异常内部写入一行，blocked 的不落记忆、只留证据
    "memory_review": """
        CREATE TABLE IF NOT EXISTS memory_review (
            review_id VARCHAR(48) PRIMARY KEY,
            rfq_id VARCHAR(48),
            branch VARCHAR(64),
            memory_id VARCHAR(48),
            decision VARCHAR(16),
            risk VARCHAR(16),
            claim VARCHAR(32),
            content TEXT,
            source VARCHAR(128),
            reasons TEXT,
            status VARCHAR(16),
            reviewer VARCHAR(64),
            created_at VARCHAR(32),
            decided_at VARCHAR(32)
        )""",
}


def init_schema(db: Database) -> None:
    for ddl in DDL.values():
        db.execute(ddl)
