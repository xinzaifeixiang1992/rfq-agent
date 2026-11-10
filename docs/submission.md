# RFQ 询价分析与报价智能体 · 说明文档

把一封询价邮件（正文 / Excel / PDF）变成可解释、可审批、可审计、可回滚的报价单。

| 项目 | 地址 |
|---|---|
| 在线体验 | https://rfq-agent-production.up.railway.app |
| 存活检查 | https://rfq-agent-production.up.railway.app/_stcore/health （返回 `ok`） |
| 演示视频 | 录制上传后回填 |
| 代码仓库 | https://github.com/xinzaifeixiang1992/rfq-agent |
| 访问密码 | 无需账号或密码 |

> 公共环境为共享演示实例，请勿上传真实客户资料；写操作按客户端 IP 限流。

## 一、问题

外贸询价以邮件为入口，格式高度不一致，报价决策又高度依赖个人经验，导致三个真实痛点：

1. **进件不可靠**：同一张询价单可能藏在邮件正文、合并单元格 Excel 或跨页 PDF 里；客户料号与内部 SKU 也不一致，人工抄录易漏项。
2. **决策不可解释**：报价依赖销售经验，事后无法回答“当时为什么报这个价”。规则和客户记忆一旦被改坏，无法还原到出问题前的状态。
3. **Agent 引入新风险**：大模型可以加速抽取和措辞，但客户邮件是外部不可信输入。一次提示注入若能写成长期记忆，就会污染此后每一次报价，且难以发现和召回。

目标用户是外贸销售、报价专员、销售主管和财务/审计人员。Agent 自动完成高重复的解析、查价、策略比较和文档生成，但报价进入主线前始终保留人工审批。

## 二、方案

核心理念是 **报价即提交（Quote-as-Commit）**：报价、规则和记忆都像代码一样经过 `SNAPSHOT → BRANCH → DIFF → MERGE / PICK / ROLLBACK`。

```text
浏览器
  │
  ▼
Streamlit 工作台（Railway 对外唯一端口）
  │  容器内 API_BASE=http://127.0.0.1:8000
  ▼
FastAPI ── LangGraph
  intake → parse → MemoryGate → normalize → price×3 → risk → 人工审批 → commit → render
     │         │            │              │                         │          │
     │         │            └─ inbox 分支   └─ 三个 quote_line 分支   └─ MERGE/PICK
     │         └─ Excel / PDF / 正文
     ├─ ModelRouter：按任务、复杂度和置信度选择模型，可降级规则引擎
     └─ AuditHook：输入摘要、输出、模型、提示词/规则版本、证据、快照、人工动作

MatrixOne / Git4Data                       Memoria
业务表、成本、历史、规则、报价、审计        客户画像、料号映射、程序性/情景记忆
SNAPSHOT / BRANCH / DIFF / PICK / MERGE    branch / diff / snapshot / rollback
```

### 一次报价的生命周期

1. `intake`：接收邮件和附件，创建 RFQ ID，并冻结数据与记忆版本。
2. `parse / normalize`：抽取客户、币种、贸易术语、交期和物料；料号匹配保留方法与分数。
3. `MemoryGate`：邮件是外部不可信输入，候选长期记忆必须经过 block / review / skip 判定。
4. `price / risk`：在三个数据分支生成三档报价，确定性引擎计算价格，LLM 只做字段抽取和文本生成。
5. `interrupt`：LangGraph 在人工审批前暂停；销售可选策略、选行、改价或驳回。
6. `commit / render`：整单有效则 MERGE，部分采用则 PICK；生成 Excel、PDF、HTML 和回复邮件草稿。
7. `learn`：成交/丢单和人工改价形成候选经验，经分支评审后才能进入记忆主线。

### 四项赛题核心能力的业务落点

| 核心能力 | 业务实现 | 关键代码 |
|---|---|---|
| Git for Data | 进件即冻结成本和规则；三档策略各开分支；审批使用 MERGE/PICK；规则在分支沙盒回测 | `app/vcs/`、`app/workflow.py` |
| 决策审计 | 每个节点记录模型、提示词/规则版本、快照、证据、置信度、耗时、成本和人工动作 | `app/audit/hook.py` |
| 记忆版本控制 | 学习记忆先入分支，支持 DIFF、评审、快照、回滚；与报价审计互相引用 | `app/memory/` |
| 模型路由 | 路由配置存数据库，按任务选择 fast / strong / vision；LLM 不可用时降级规则引擎 | `app/router/` |

## 三、技术栈

- Python 3.13、LangGraph、Pydantic 2
- FastAPI、Uvicorn、Streamlit
- MatrixOne / Git4Data、PyMySQL
- Memoria REST（Git for Memory）
- OpenAI 兼容 LLM / MOI Genesis
- `openpyxl`、`pdfplumber`、`reportlab`、Jinja2、pandas
- pytest、Docker、Railway

## 四、创新点

1. **报价即提交**：一份报价不是一个数据库行，而是一次数据提交。三档策略天然是三个分支，审批动作直接映射为 `MERGE`（整单采用）或 `PICK`（按行采用），驳回即丢弃分支，改动可回滚。
2. **MemoryGate 把“记忆投毒”变成可治理边界**：位于“外部内容 → 长期记忆”的强制关卡。`block` 拦截隐藏指令、零宽字符、伪造角色、低于成本的协议价与超限折扣账期；`review` 把可验证声明写入 `inbox_<rfq>` 隔离分支（T4），人工批准后才升级 T3；`skip` 去重。合并前自动创建 `pre_merge_*` 快照，存量扫描前创建 `pre_quarantine_*` 快照，`GET /memory/{id}/impact` 可从审计证据反查受影响的全部报价单。
3. **价格由确定性引擎给出，模型只做抽取与措辞**：成本、历史成交、趋势、客户画像、数量阶梯共同决定价格，模型故障时报价链路仍可稳定产出。
4. **节点级决策审计 + 时间旅行重放**：按报价当时的快照重放，能解释“当时与现在的差异”，而不只是留下一条日志。
5. **规则沙盒**：目标毛利等规则改动在隔离分支回测、看 DIFF 再合并；试错风险被限制在分支内，不会污染生产。

### 记忆信任分级

- T1：主数据/人工确认；T2：内部审批产生；T3：外部声明经评审通过；T4：未验证、永不参与决策。
- Memoria 云分支 DIFF 若出现 lineage 异常或超时，在 `pre_merge` 快照保护下使用可审计降级路径，不静默放行。

## 五、使用说明

打开在线工作台后，按页面顶部“评委演示指引”操作，三分钟可走完全链路：

1. 点击 **Excel 样例**：解析合并单元格附件，完成物料标准化，并在三个数据分支并行生成争单、均衡、利润三档报价。
2. 进入 **报价与审批**：比较三档 DIFF；修改一行价格并批准，观察整单 `MERGE` 或按行 `PICK`。
3. 进入 **决策审计**：查看模型、规则版本、数据/记忆快照、证据与人工动作；按报价时快照重放。
4. 点击 **投毒邮件**：在 **记忆网关** 查看提示注入、隐藏文字、虚假折扣和账期如何被拦截；正常声明进入隔离分支等待评审。
5. 在 **记忆版本** 创建快照、写入记忆并回滚；在 **规则沙盒** 修改目标毛利、回测、DIFF 后再合并。
6. 在 **模型路由** 查看每个节点选用的模型、token、成本、耗时和置信度。

### 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python.exe scripts\make_samples.py
.\.venv\Scripts\python.exe scripts\seed.py
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --port 8000
# 另开终端
.\.venv\Scripts\python.exe -m streamlit run web/app.py --server.port 8501
```

浏览器打开 `http://localhost:8501`。默认示例配置可使用 SQLite + Git4Data 仿真 + 规则引擎，不需要云端凭据。

## 六、验证结果

- 常规自动化测试：`169 passed, 8 skipped`（local / git4data-sim / fake Memoria）。
- 真实 MatrixOne Git4Data：完整 8 用例历史通过；本次变更后核心快照、DIFF、MERGE/PICK 6 用例复验通过。
- 真实 MatrixOne + Memoria + LLM 在线冒烟：四封样例、MERGE/PICK/驳回、重放、规则沙盒、记忆回滚、MemoryGate 全链路 `ALL OK`。
- 投毒评测（`eval/poison_cases/cases.json`：16 条攻击 + 14 条良性样本）：

| 指标 | 结果 |
|---|---:|
| 攻击直接拦截率 | 87.5%（其余 2 条进入高风险人工评审，不进主线） |
| 攻击不进入主线比例 | 100% |
| 良性误拦截率 | 0% |
| 与预期判定一致率 | 100% |
| 端到端检查 | 13 / 13 通过 |
| 遗留投毒影响与恢复 | 0.4348 → 0.4134 → 隔离后 0.4348 |

- 安全回归：管理开关与 token、健康检查脱敏、写接口限流、上游超时 503、Memoria 快照列表超时降级均有测试覆盖。

## 七、数据合规与安全

- 客户、联系人、邮箱、物料、成本、历史报价和邮件均由 `app/seed.py`、`scripts/make_samples.py` 固定随机种子生成，不含真实业务数据；上传件按 RFQ 隔离存放在本地 `data/uploads/`。
- 外部邮件默认不可信；LLM 只负责抽取与措辞，价格由确定性引擎计算，所有长期记忆写入必须经过 MemoryGate。
- 报价必须人工审批后进入主线；关键动作可追溯到数据、规则和记忆版本，并支持回滚。
- 公网 API 不直接暴露：容器只公开 Streamlit 端口，FastAPI 仅监听 `127.0.0.1:8000`；管理接口需 `ALLOW_ADMIN_RESET=true` 与恒定时间比较的 `X-Admin-Token` 双重校验。
- `/health` 只返回组件类型和状态，不返回主机、数据库名、用户名、连接串、密钥或管理口令。
- 外部服务凭据只存放在本地 `.env` 或 Railway Variables，不写入代码、日志、截图或仓库；`.gitignore` / `.dockerignore` 排除 `.env`、本地数据库、上传件、输出、日志、缓存与 Memoria 参考源码。

## 八、已知限制

- 演示实例为单容器、单 worker；所有访问者共享演示数据。进程内限流适合比赛演示，不代替网关级限流。
- LangGraph checkpointer 当前为内存版，容器重启后未完成的审批状态可能丢失；已完成决策仍在数据库审计表中。
- 记忆后端的当前分支是实例状态，当前以单 worker 避免跨请求竞争；生产多租户需改为请求级分支上下文。
- Railway 本地文件系统非持久化，生成的报价文件在重启后丢失；正式环境应接入对象存储。
- Memoria 免费/当前服务有快照配额，客户端只回收本应用创建的最旧注册快照。
- **MatrixOne 表级快照配额（免费额度硬限制）**：当前使用的免费 Serverless 实例对表级快照（`SNAPSHOT ... FOR TABLE`）有账户级上限 **50 个**。本应用每进一张询价单会为 `cost_price` 与 `pricing_rule` 各建 1 个快照（`sn_<rfq>_<table>`），即 **2 个/单**，约 25 单即达到上限。超限后 `CREATE SNAPSHOT` 返回 `20101 ... has reached the limit of 50`，会被统一异常处理器转换为 **HTTP 503「数据库连接暂时不可用，请稍后重试」**——该提示与实际原因不一致，排查时须以服务端日志中的原始错误码为准。代码已实现自愈：`MatrixOneVCS.snapshot()` 捕获该错误后自动回收本库最旧的询价单快照（保留最近 30 个）并重试一次；也可手动执行 `python scripts/prune_snapshots.py --keep 0` 立即释放配额。
- MatrixOne Git4Data 与外部模型属于远程依赖，代码已配置有界超时和可审计降级，但公网延迟会影响单次处理时长。

本项目仅用于比赛演示与技术验证，不建议未经权限、持久化和并发改造直接用于生产报价。
