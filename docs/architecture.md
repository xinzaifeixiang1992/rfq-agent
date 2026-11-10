# 架构图

三张图分别回答：**系统怎么分层**、**一张询价单怎么走完全程**、**三档策略与 Git4Data 怎么落地**。图用 Mermaid 编写，GitHub 直接渲染；需要 PNG 时可粘贴到 <https://mermaid.live> 导出。

## 一、系统分层

```mermaid
flowchart TB
    subgraph IN["外部输入"]
        I1["询价邮件 .eml<br/>含 Excel / PDF 附件"]
        I2["邮件正文 / 直接上传附件"]
    end

    subgraph FE["前端 · Streamlit（唯一公网端口）"]
        F1["① 接收询价　② 报价与审批　③ 决策审计"]
        F2["④ 记忆版本　⑤ 记忆网关　⑥ 规则沙盒　⑦ 模型路由"]
    end

    subgraph BE["后端 · FastAPI（127.0.0.1:8000，不对外暴露）"]
        API["REST 接口<br/>写接口限流 · 管理接口双因子鉴权"]
        LG["LangGraph 编排<br/>intake → parse → memory_gate → normalize<br/>→ price → risk → approval → commit / reject → render"]
        MR["ModelRouter<br/>primary → escalate → fallback"]
        AU["决策审计 AuditLogger<br/>模型 · 提示词/规则版本 · 快照 · 证据 · 置信度 · 耗时 · 成本 · 人工动作"]
    end

    subgraph MODELS["模型侧"]
        LLM["LLM 网关<br/>fast / strong / vision"]
        RULE["rule-engine<br/>规则兜底"]
    end

    subgraph DATA["数据侧"]
        MO["MatrixOne<br/>业务表 + Git4Data 数据分支/表级快照"]
        MEM["Memoria<br/>记忆主线 main + 学习/隔离分支"]
    end

    I1 --> FE
    I2 --> FE
    FE -->|"HTTP"| API
    API --> LG
    LG --> MR
    MR --> LLM
    MR --> RULE
    LG -->|"报价即提交"| MO
    LG -->|"记忆读写"| MEM
    LG -->|"每个节点留痕"| AU
    AU -->|"审计表"| MO
```

## 二、一张询价单的完整链路

```mermaid
flowchart TD
    S["进件 intake<br/>冻结 cost_price / pricing_rule 表级快照<br/>写入 RFQ 记录"] --> P["解析 parse<br/>邮件正文 · 合并单元格 Excel · 跨页 PDF"]
    P --> G{"MemoryGate<br/>外部内容信任分级"}
    G -->|"注入 / 隐藏指令 / 无证据声明"| BLOCK["拦截并标记高风险<br/>不进入记忆主线"]
    G -->|"可验证声明"| INBOX["T4 隔离分支 inbox_rfq<br/>人工评审后升级 T3"]
    G -->|"主线记忆"| N["标准化 normalize<br/>SKU 匹配 + 匹配方式 + 置信度 + 来源"]
    N --> PR["定价 price<br/>确定性引擎 × 三档策略"]
    PR --> B1["数据分支 ql_id_win_first"]
    PR --> B2["数据分支 ql_id_balanced"]
    PR --> B3["数据分支 ql_id_margin_first"]
    B1 --> RISK["风险 risk<br/>低毛利 / 低置信度 / 超期账期"]
    B2 --> RISK
    B3 --> RISK
    RISK --> APV["人工审批 approval<br/>DIFF 对比 + 逐行改价"]
    APV -->|"整单采用"| MERGE["MERGE 到主线"]
    APV -->|"部分行采用"| PICK["PICK 选中行"]
    APV -->|"驳回"| DROP["丢弃分支"]
    MERGE --> OUT["render<br/>Excel / PDF / HTML / 回复邮件草稿"]
    PICK --> OUT
    BLOCK --> AUD["决策审计表<br/>可重放、可追溯"]
    DROP --> AUD
    OUT --> AUD
```

## 三、审计与重放为什么可信

```mermaid
flowchart LR
    N1["任一节点执行"] --> N2["AuditLogger.wrap"]
    N2 --> N3["记录<br/>节点名 · 模型 · 提示词/规则版本<br/>数据快照 · 记忆快照 · 证据<br/>置信度 · 耗时 · 成本"]
    N3 --> N4["audit_event 表"]
    N4 --> R1["③ 决策审计：逐节点时间线"]
    N4 --> R2["重放：用当时的快照重算<br/>解释「当时 vs 现在」差异"]
    N4 --> R3["MemoryGate 影响分析<br/>从证据反查受影响的报价单"]
    N4 --> R4["⑦ 模型路由统计<br/>按模型汇总调用与成本"]
```

## 关键设计取舍

| 取舍 | 选择 | 原因 |
|---|---|---|
| 谁算价 | 确定性引擎算价，LLM 只做抽取与措辞 | 价格必须可复现、可审计；LLM 输出不可作为定价依据 |
| 外部内容能否直接进记忆 | 一律先入隔离分支，评审后升级 | 防止记忆投毒污染后续所有报价 |
| 三档策略怎么存 | 三个真实数据分支，不是三份临时表 | 比较、回滚、合并都走 Git4Data 原生语义，而不是应用层模拟 |
| 未审批能否出价 | 不能，`approval` 是图上的中断点 | 报价必须人工确认后才落主线并生成对外文件 |
| 模型挂了怎么办 | 路由降级到 rule-engine，不阻断业务 | 外部模型不可用时仍能产出报价，审计里如实记录降级 |
| 前后端如何暴露 | 只公开 Streamlit 端口，FastAPI 仅监听回环地址 | 减小公网攻击面，管理接口再加 token 校验 |

## 对应代码

| 图上模块 | 代码位置 |
|---|---|
| LangGraph 编排 | `app/graph/builder.py`、`app/graph/nodes.py` |
| 审计留痕 | `app/audit/hook.py` |
| 模型路由 | `app/router/router.py`、`app/router/llm.py` |
| Git4Data 分支与快照 | `app/vcs/matrixone.py`、`app/vcs/sqlite_sim.py` |
| 记忆与网关 | `app/memory/memoria.py`、`app/memory/gate.py` |
| 定价与风险 | `app/pricing/engine.py`、`app/pricing/rules.py`、`app/pricing/risk.py` |
| 解析器 | `app/parsers/` |
| 前端 | `web/app.py` |
| REST 接口 | `app/api/main.py` |
