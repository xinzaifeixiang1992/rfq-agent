"""Streamlit 工作台。启动：streamlit run web/app.py（需先启动 API）"""
from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import get_settings  # noqa: E402

CFG = get_settings()
API = os.getenv("API_BASE") or CFG.api_base
SAMPLES = ROOT / "samples"
QUICK = [("Excel 样例", "01_nordic_excel.eml", "合并单元格 Excel 附件"),
         ("PDF 样例", "02_pacific_pdf.eml", "跨页续表 PDF + 正文注入"),
         ("正文样例", "03_rhein_body.eml", "物料写在邮件正文"),
         ("投毒邮件", "04_sakura_poison.eml", "隐藏文字 + 虚假折扣/账期")]
LABEL = {"win_first": "争单优先", "balanced": "均衡", "margin_first": "利润优先"}
STATUS = {"parsing": "解析中", "pricing": "定价中", "pending_approval": "待审批", "quoted": "已报价",
          "rejected": "已驳回", "won": "已成交", "lost": "已丢单"}

st.set_page_config(page_title="RFQ 询价分析与报价智能体", layout="wide")
http = httpx.Client(base_url=API, timeout=180)


def _client_ip() -> str:
    """API 只在容器内可达，由此处代发；把浏览器真实 IP 传给 API 做限流。"""
    h = st.context.headers
    for v in (h.get("X-Real-IP"), h.get("X-Forwarded-For"), st.context.ip_address):
        if isinstance(v, str) and v.strip():
            return v.split(",")[0].strip()
    return "unknown"


def _admin_token() -> str:
    """本地开发直接使用配置口令；公网演示（DEMO_MODE）只有带 ?admin 访问并输入正确口令才进入管理员模式。"""
    if not CFG.demo_mode:
        return CFG.admin_token
    if "admin" not in st.query_params:
        return ""
    return st.sidebar.text_input("管理口令", type="password", key="admin_token_input")


TOKEN = _admin_token()
IS_ADMIN = bool(CFG.allow_admin_reset and CFG.admin_token and TOKEN
                and hmac.compare_digest(TOKEN.encode(), CFG.admin_token.encode()))
HEADERS = {"X-Client-IP": _client_ip(), **({"X-Admin-Token": TOKEN} if IS_ADMIN else {})}


def call(method: str, path: str, **kw):
    try:
        r = http.request(method, path, headers=HEADERS, **kw)
    except httpx.HTTPError as e:
        st.error(f"API 不可用：{type(e).__name__}" if CFG.demo_mode else f"API 不可用（{API}）：{e}")
        st.stop()
    if r.status_code >= 400:
        st.error(f"{r.status_code}: {r.json().get('detail') if 'json' in r.headers.get('content-type', '') else r.text}")
        return None
    return r.json() if "json" in r.headers.get("content-type", "") else r.content


health = call("GET", "/health")
with st.sidebar:
    st.title("RFQ Agent(询价分析与报价智能体)")
    st.caption("后端组件状态")
    st.markdown(f"- **DB**：{health['db']}\n- **VCS**：{health['vcs']}\n- **记忆**：{health['memory']}\n"
                f"- **LLM**：{'开' if health['llm'] else '规则引擎'}")
    rfqs = call("GET", "/rfq") or []
    opts = [r["rfq_id"] for r in rfqs]
    fmt = {r["rfq_id"]: f"{r['rfq_id'][-10:]} · {STATUS.get(r['status'], r['status'])} · {(r['subject'] or '')[:18]}"
           for r in rfqs}
    if "rfq" not in st.session_state and opts:
        st.session_state.rfq = opts[0]
    if opts:
        idx = opts.index(st.session_state.rfq) if st.session_state.get("rfq") in opts else 0
        st.session_state.rfq = st.selectbox("询价单", opts, index=idx, format_func=lambda x: fmt[x])
    if IS_ADMIN and st.button("重置演示数据", type="secondary"):
        call("POST", "/admin/reset")
        st.session_state.pop("rfq", None)
        st.rerun()
    if CFG.demo_mode:
        st.caption("公共演示环境：数据为合成数据，所有访问者共享；写操作按 IP 限流。")


def submit_sample(name: str) -> None:
    with st.spinner(f"{name}：解析 → 标准化 → 三分支定价 → 风险审核 …（约 30–90 秒）"):
        res = call("POST", "/rfq", files=[("eml", (name, (SAMPLES / name).read_bytes()))])
    if res:
        st.session_state.rfq = res["rfq_id"]
        st.session_state.flash = f"已生成 {res['rfq_id']}（{name}），请到「② 报价与审批」查看"
        st.rerun()


with st.expander("评委演示指引（约 3 分钟）", expanded=not opts):
    st.markdown(
        "1. 点击下方 **Excel 样例**：邮件附件解析（合并单元格）→ 物料标准化 → 三档策略在三个**数据分支**上并行定价。\n"
        "2. **② 报价与审批**：对比三档 DIFF 与风险；改一行价格后批准 = 整单 **MERGE**，取消勾选部分行 = **PICK**。\n"
        "3. **③ 决策审计**：查看每个节点的模型、规则版本、数据/记忆快照与证据；点「重放」按报价时快照时间旅行。\n"
        "4. 点击 **投毒邮件**，再到 **⑤ 记忆网关**：注入、隐藏文字、虚假折扣被拦截，其余声明进入隔离分支，评审后才合并。\n"
        "5. **④ 记忆版本**：创建快照 → 写入一条记忆 → 回滚到快照。\n"
        "6. **⑥ 规则沙盒**：在分支上改 `target_margin` → 回测 → MERGE；**⑦ 模型路由** 查看各节点模型与成本。")
    qc = st.columns(len(QUICK))
    for c, (label, name, hint) in zip(qc, QUICK):
        if c.button(label, key=f"quick_{name}", help=hint, width="stretch") and (SAMPLES / name).exists():
            submit_sample(name)
if msg := st.session_state.pop("flash", None):
    st.success(msg)

tab_in, tab_q, tab_audit, tab_mem, tab_gate, tab_rule, tab_route = st.tabs(
    ["① 接收询价", "② 报价与审批", "③ 决策审计", "④ 记忆版本", "⑤ 记忆网关", "⑥ 规则沙盒", "⑦ 模型路由"])

# ---------------- ① 接收询价 ----------------
with tab_in:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("上传邮件 / 附件")
        eml = st.file_uploader(".eml 邮件（附件自动拆分）", type=["eml"])
        files = st.file_uploader("或直接上传附件", type=["xlsx", "xlsm", "csv", "pdf", "png", "jpg"],
                                 accept_multiple_files=True)
        samples = sorted(p.name for p in SAMPLES.glob("*.eml")) if SAMPLES.exists() else []
        sample = st.selectbox("或选择样例邮件", ["-"] + samples)
    with c2:
        st.subheader("或粘贴邮件")
        sender = st.text_input("发件人", "John Smith <john@pacific-elec.com>")
        subject = st.text_input("主题", "RFQ - harness components")
        body = st.text_area("正文", height=200, value="Please quote FOB Shanghai, USD, delivery within 4 weeks.\n\n"
                            "PE-CAB-15BK PVC cable black: 2000 m\nPH1003-BL cable: 800 m\nCBL-0001 PVC cable: 300 m\n"
                            "XYZ-77777 special sensor: 50 pcs\n\nBest regards,\nJohn Smith\nPacific Electric Co.")
    if st.button("开始分析", type="primary"):
        up = [("files", (f.name, f.getvalue())) for f in files or []]
        if eml:
            up.append(("eml", (eml.name, eml.getvalue())))
        elif sample != "-":
            up.append(("eml", (sample, (SAMPLES / sample).read_bytes())))
        with st.spinner("解析 → 标准化 → 三分支定价 → 风险审核 …"):
            res = call("POST", "/rfq", data={"subject": subject, "sender": sender, "body": body}, files=up or None)
        if res:
            st.session_state.rfq = res["rfq_id"]
            st.success(f"已生成 {res['rfq_id']}，请到「报价与审批」查看")
            st.rerun()

rfq_id = st.session_state.get("rfq")
view = call("GET", f"/rfq/{rfq_id}") if rfq_id else None
S = (view or {}).get("state") or {}

# ---------------- ② 报价与审批 ----------------
with tab_q:
    if not view:
        st.info("暂无询价单")
    else:
        rec = view["record"] or {}
        h = S.get("header") or {}
        st.subheader(f"{rfq_id} · {STATUS.get(rec.get('status'), rec.get('status'))}")
        cols = st.columns(6)
        for c, (k, v) in zip(cols, [("客户", (S.get("customer") or {}).get("name") or h.get("customer_name")),
                                    ("币种", h.get("currency")), ("贸易术语", h.get("incoterm")),
                                    ("目的地", h.get("destination")), ("交期", h.get("delivery_deadline")),
                                    ("付款", h.get("payment_terms"))]):
            c.metric(k, v or "—")
        if S.get("clarification"):
            with st.expander(f"⚠ 缺少字段 {h.get('missing_fields')}，追问邮件草稿"):
                st.code(S["clarification"])
        with st.expander(f"物料识别（{len(S.get('matched') or [])} 行）", expanded=False):
            st.dataframe(pd.DataFrame(S.get("matched") or [])[
                ["line_no", "part_no", "description", "qty", "unit", "sku", "sku_name", "match_method", "match_score",
                 "source"]] if S.get("matched") else pd.DataFrame(), width="stretch")
            for n in S.get("parse_notes") or []:
                st.caption(n)
        g = S.get("gate") or {}
        if g.get("verdicts"):
            with st.expander(f"MemoryGate：拦截 {g['block']} · 待评审 {g['review']} · 无需记忆 {g['skip']}",
                             expanded=bool(g.get("injection"))):
                for v in g["verdicts"]:
                    (st.error if v["decision"] == "block" else st.warning if v["decision"] == "review" else st.caption)(
                        f"[{v['decision']}] {v['claim']} · {v['source']}：{v['content'][:120]} —— {'；'.join(v['reasons'])}")

        if view.get("pending"):
            p = view["pending"]
            st.markdown(f"#### 三档策略 = 三个数据分支　推荐：**{LABEL.get(p['recommended'])}**（{p.get('reason')}）")
            mc = st.columns(3)
            for c, s in zip(mc, ("win_first", "balanced", "margin_first")):
                sm, d = p["summary"][s], p["branch_diffs"][s]
                c.metric(f"{LABEL[s]}　{p['branches'][s]}", f"{h.get('currency')} {sm['amount']:,.2f}",
                         f"毛利 {sm['margin']:.1%} · 赢单 {sm['avg_win_prob']:.0%}")
                c.caption(f"期望毛利 {sm['expected_profit']:,.2f} · DIFF +{d['inserted']} ~{d['updated']} "
                          f"-{d['deleted']} · 高风险 {p['high_risks'][s]}")
            st.dataframe(pd.DataFrame(call("GET", f"/rfq/{rfq_id}/compare") or []), width="stretch")

            strategy = st.radio("采用策略", list(LABEL), index=list(LABEL).index(p["recommended"]),
                                format_func=LABEL.get, horizontal=True, key=f"strat_{rfq_id}")
            risks = (S.get("risks") or {}).get(strategy) or []
            for r in risks:
                (st.error if r["level"] == "high" else st.warning if r["level"] == "medium" else st.info)(r["message"])
            rows = (S.get("quotes") or {}).get(strategy) or []
            if not rows:
                st.warning(f"策略「{LABEL.get(strategy)}」暂无报价行，请切换其它策略后重试")
                lines = pd.DataFrame(columns=["line_key", "unit_price"])
                ed = pd.DataFrame({"采用": pd.Series(dtype=bool), "line_key": pd.Series(dtype=object),
                                   "改价": pd.Series(dtype=float), "unit_price": pd.Series(dtype=float)})
            else:
                lines = pd.DataFrame(rows)
                lines.insert(0, "采用", lines["unit_price"] > 0)
                lines["改价"] = lines["unit_price"]
                try:
                    # key 带 rfq_id：不同询价单行数不同，复用同一 key 会让表格拿上一张单的编辑状态去合并而报错
                    ed = st.data_editor(lines[["采用", "line_key", "sku", "description", "qty", "unit", "unit_cost",
                                               "unit_price", "改价", "margin", "win_prob", "rationale"]],
                                        disabled=["line_key", "sku", "description", "qty", "unit", "unit_cost",
                                                  "unit_price", "margin", "win_prob", "rationale"], width="stretch",
                                        key=f"ed_{rfq_id}_{strategy}")
                except Exception as e:  # noqa: BLE001 —— 表格状态异常不应让整个页签变空白
                    st.error(f"报价表渲染失败（{type(e).__name__}）：切换策略或刷新页面后重试。{e}")
                    ed = pd.DataFrame({"采用": pd.Series(dtype=bool), "line_key": pd.Series(dtype=object),
                                       "改价": pd.Series(dtype=float), "unit_price": pd.Series(dtype=float)})
            a1, a2, a3 = st.columns([2, 1, 1])
            comment = a1.text_input("审批意见")
            approver = a1.text_input("审批人", "sales")
            if a2.button("批准并提交（MERGE / PICK）", type="primary"):
                keys = ed.loc[ed["采用"], "line_key"].tolist()
                ov = {r.line_key: float(r.改价) for r in ed.itertuples() if abs(float(r.改价) - float(r.unit_price)) > 1e-9}
                all_valid = keys == [k for k, v in zip(lines["line_key"], lines["unit_price"]) if v > 0 or k in ov]
                call("POST", f"/rfq/{rfq_id}/approve", json={
                    "action": "approve", "strategy": strategy, "line_keys": None if all_valid else keys,
                    "overrides": ov, "approver": approver, "comment": comment})
                st.rerun()
            if a3.button("驳回（丢弃全部分支）"):
                call("POST", f"/rfq/{rfq_id}/approve", json={"action": "reject", "comment": comment,
                                                              "approver": approver})
                st.rerun()
        elif view.get("committed"):
            st.markdown("#### 已提交主线的报价")
            st.dataframe(pd.DataFrame(view["committed"]), width="stretch")
            files = (S.get("outputs") or {}).get("files") or {}
            dc = st.columns(4)
            for c, k in zip(dc, ("xlsx", "pdf", "html", "reply")):
                if k in files:
                    data = call("GET", f"/rfq/{rfq_id}/files/{k}")
                    if data:
                        c.download_button(f"下载 {k}", data, file_name=Path(files[k]).name)
            reply = (S.get("outputs") or {}).get("reply")
            if reply:
                with st.expander("回复邮件草稿", expanded=True):
                    st.text(f"Subject: {reply['subject']}\n\n{reply['body']}")
            if rec.get("status") == "quoted":
                o1, o2, o3 = st.columns([2, 1, 1])
                note = o1.text_input("成交/丢单说明")
                for c, won in ((o2, True), (o3, False)):
                    if c.button("标记成交" if won else "标记丢单"):
                        res = call("POST", f"/rfq/{rfq_id}/outcome", json={"won": won, "note": note})
                        if res:
                            if res.get("note_blocked"):
                                st.error(f"说明含提示注入特征（{'、'.join(res['note_blocked'])}），已被 MemoryGate 丢弃")
                            st.success(f"经验已写入记忆分支 {res['memory_branch']}，请到「记忆版本」评审合并")
        else:
            st.info(f"状态：{STATUS.get(rec.get('status'), rec.get('status'))}")

# ---------------- ③ 决策审计 ----------------
with tab_audit:
    if rfq_id:
        logs = call("GET", f"/rfq/{rfq_id}/audit") or []
        df = pd.DataFrame(logs)
        if not df.empty:
            st.dataframe(df[["created_at", "node", "model", "prompt_ver", "rule_ver", "data_snapshot",
                             "memory_snapshot", "confidence", "latency_ms", "tokens", "cost", "human_action"]],
                         width="stretch")
            for r in logs:
                with st.expander(f"{r['node']} · {r['model']} · {r['decision_id']}"):
                    st.json({"evidence": r["evidence_json"], "override": r.get("override_json")}, expanded=False)
                    st.json(r["output_json"], expanded=False)
        st.markdown("#### 时间旅行：按报价时的数据快照重放")
        if st.button("重放并对比当前数据"):
            rp = call("GET", f"/rfq/{rfq_id}/replay")
            if rp:
                st.caption(f"快照：{', '.join(rp['snapshots'])} · 策略 {LABEL.get(rp['strategy'])}")
                st.dataframe(pd.DataFrame(rp["lines"]), width="stretch")
                st.write("规则变化：", rp["rule_diff"] or "无")

# ---------------- ④ 记忆版本 ----------------
with tab_mem:
    m1, m2 = st.columns([3, 2])
    with m1:
        st.subheader("当前记忆（main）")
        st.dataframe(pd.DataFrame(call("GET", "/memory") or []), width="stretch")
        with st.form("add_mem"):
            st.caption("手工写入记忆")
            content = st.text_input("内容")
            k1, k2, k3 = st.columns(3)
            kind = k1.selectbox("类型", ["profile", "alias", "procedural", "episodic", "semantic"])
            cid = k2.text_input("客户 ID", "C002")
            trust = k3.selectbox("信任级", ["T1", "T2", "T3", "T4"], index=2)
            if st.form_submit_button("写入") and content:
                call("POST", "/memory", json={"content": content, "kind": kind, "customer_id": cid or None,
                                              "trust_tier": trust})
                st.rerun()
    with m2:
        st.subheader("快照 / 回滚")
        snaps = call("GET", "/memory/snapshots") or []
        names = [s.get("name") or s.get("snapshot_name") for s in snaps]
        if names:
            pick = st.selectbox("记忆快照", names)
            if st.button("回滚到该快照"):
                call("POST", f"/memory/rollback/{pick}")
                st.rerun()
        sn = st.text_input("新快照名", "manual_1")
        if st.button("创建快照"):
            call("POST", "/memory/snapshots", json={"name": sn})
            st.rerun()
        st.subheader("学习分支评审")
        br = st.text_input("分支名", f"learn_{rfq_id.lower()}" if rfq_id else "", key="mem_branch")
        b1, b2, b3 = st.columns(3)
        if b1.button("DIFF", key="mem_diff") and br:
            st.dataframe(pd.DataFrame(call("GET", f"/memory/branches/{br}/diff") or []))
        if b2.button("合并到 main", key="mem_merge") and br:
            call("POST", f"/memory/branches/{br}/merge")
            st.rerun()
        if b3.button("丢弃分支", key="mem_drop") and br:
            call("DELETE", f"/memory/branches/{br}")

# ---------------- ⑤ 记忆网关 ----------------
with tab_gate:
    st.caption("外部内容永远不直接进入记忆主线：注入/隐藏文字/无证据的敏感声明直接拦截；"
               "其余进入隔离分支 inbox_<rfq>（T4），评审通过后升级为 T3，先打 pre_merge 快照再合并。")
    queue = call("GET", "/memory/gate/queue") or []
    pending = [r for r in queue if r["status"] == "pending"]
    st.subheader(f"待评审（{len(pending)}）")
    for br in sorted({r["branch"] for r in pending}):
        rows = [r for r in pending if r["branch"] == br]
        with st.container(border=True):
            st.markdown(f"**{br}** · {rows[0]['rfq_id']}")
            ex = []
            for r in rows:
                keep = st.checkbox(f"[{r['risk']}] {r['claim']}：{r['content'][:140]}", value=r["risk"] != "high",
                                   key=f"k_{r['review_id']}", help="；".join(r["reasons"]))
                if not keep:
                    ex.append(r["review_id"])
            q1, q2, _ = st.columns([1, 1, 3])
            if q1.button("批准勾选项并合并", key=f"a_{br}", type="primary"):
                res = call("POST", f"/memory/gate/{br}/approve", json={"reviewer": "reviewer", "exclude": ex})
                if res:
                    st.success(f"已合并 {len(res['approved'])} 条（T3），快照 {res['snapshot']}")
                st.rerun()
            if q2.button("全部驳回", key=f"r_{br}"):
                call("POST", f"/memory/gate/{br}/reject", json={"reviewer": "reviewer"})
                st.rerun()
    st.subheader("拦截与处理记录")
    done = [r for r in queue if r["status"] != "pending"]
    if done:
        df = pd.DataFrame(done)
        df["reasons"] = df["reasons"].map("；".join)
        st.dataframe(df[["review_id", "rfq_id", "status", "decision", "risk", "claim", "content", "source", "reasons",
                         "reviewer", "decided_at"]].style.apply(
            lambda r: ["color: #c0392b" if r["status"] in ("blocked", "quarantined") else ""] * len(r), axis=1),
            width="stretch")

    st.subheader("事后复查：主线记忆体检 + 影响分析")
    sc_cols = st.columns(3 if IS_ADMIN else 2)
    s2, s3 = sc_cols[-2], sc_cols[-1]
    if IS_ADMIN and sc_cols[0].button("模拟历史遗留投毒（旧接口写入）"):
        res = call("POST", "/admin/demo/legacy-poison")
        if res:
            st.warning(f"已写入伪造 T2 改价经验 {res['memory_id']}，后续 CBL-0016 报价会被下调；先跑一单再扫描")
    if s2.button("扫描主线"):
        st.session_state.scan = call("POST", "/memory/gate/scan")
    if s3.button("扫描并隔离（先打快照）", type="primary"):
        st.session_state.scan = call("POST", "/memory/gate/scan", params={"apply": True})
    sc = st.session_state.get("scan")
    if sc:
        if not sc["findings"]:
            st.success("主线记忆未发现问题")
        for f in sc["findings"]:
            with st.container(border=True):
                st.error(f"{f['memory_id']} · {f['kind']} · {f['trust']} · 来源 {f['source']}：{f['content'][:160]}")
                st.caption("；".join(f["reasons"]))
                if f["impact"]:
                    st.markdown(f"受影响报价单 **{len(f['impact'])}** 张")
                    st.dataframe(pd.DataFrame(f["impact"]), width="stretch")
                else:
                    st.caption("尚无报价单引用过此记忆")
        if sc.get("applied"):
            st.info(f"已隔离 {len(sc['findings'])} 条，可回滚快照 {sc['snapshot']}")

# ---------------- ⑥ 规则沙盒 ----------------
with tab_rule:
    rules = call("GET", "/rules") or []
    st.dataframe(pd.DataFrame(rules), width="stretch")
    with st.form("rule_form"):
        name = st.text_input("分支名", "aggressive", key="rule_branch_name")
        keys = [r["rule_key"] for r in rules]
        r1, r2 = st.columns(2)
        k = r1.selectbox("规则", keys, index=keys.index("target_margin") if "target_margin" in keys else 0)
        v = r2.text_input("新值", "0.18")
        if st.form_submit_button("在数据分支上修改"):
            # 结果存在独立键：与 form 的 widget key 同名会报 StreamlitWidgetAlreadyInstantiatedError
            st.session_state.rule_result = (call("POST", "/rules/branches", json={"name": name, "changes": {k: v}})
                                            or {}).get("branch")
    rb = st.session_state.get("rule_result")
    if rb:
        st.markdown(f"**分支 `{rb}`**")
        st.json(call("GET", f"/rules/branches/{rb}/diff"), expanded=False)
        if st.button("用近期询价回测", key="rule_backtest"):
            bt = call("GET", f"/rules/branches/{rb}/backtest")
            if bt:
                st.write(bt["total"])
                st.dataframe(pd.DataFrame([{"rfq_id": r["rfq_id"], "主线金额": r["main"]["amount"],
                                            "分支金额": r["branch"]["amount"],
                                            "主线期望毛利": r["main"]["expected_profit"],
                                            "分支期望毛利": r["branch"]["expected_profit"]} for r in bt["rfqs"]]))
        c1, c2 = st.columns(2)
        if c1.button("MERGE 到主线", key="rule_merge", type="primary"):
            call("POST", f"/rules/branches/{rb}/merge")
            st.session_state.pop("rule_result", None)
            st.rerun()
        if c2.button("丢弃分支", key="rule_drop"):
            call("DELETE", f"/rules/branches/{rb}")
            st.session_state.pop("rule_result", None)
            st.rerun()

# ---------------- ⑦ 模型路由 ----------------
with tab_route:
    st.subheader("路由表（配置即数据）")
    st.dataframe(pd.DataFrame(call("GET", "/routes") or []), width="stretch")
    st.subheader("调用统计（来自决策审计）")
    st.dataframe(pd.DataFrame(call("GET", "/audit/stats") or []), width="stretch")
