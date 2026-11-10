r"""生成一张架构总览图（PNG），输出 docs/images/architecture.png。

纯 Pillow 绘制，无外部依赖；字体用系统微软雅黑，2 倍超采样保证投影/印刷清晰。
运行：.\.venv\Scripts\python.exe scripts\make_architecture_png.py
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 2200, 1480
M = 60                      # 左右外边距
SS = 2                      # 超采样倍数
INNER_W = W - 2 * M

BG = "#F4F7FB"
INK = "#10243E"
GRAY = "#6B7787"
LIGHT = "#8A94A6"
CARD = "#FFFFFF"
CARD_LINE = "#DCE4F0"
TINT = "#F2F6FC"
TINT_LINE = "#CBD8EA"

C_IN = "#7C8FA8"        # 外部输入
C_FE = "#D9534F"        # 前端
C_BE = "#2B6CB0"        # 后端编排
C_DATA = "#6B46C1"      # 数据与模型
C_AUD = "#B7791F"       # 审计

_FONTS: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def F(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _FONTS:
        path = "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"
        _FONTS[key] = ImageFont.truetype(path, size * SS)
    return _FONTS[key]


def s(v: float) -> int:
    return int(round(v * SS))


def box(d, x0, y0, x1, y1, r=14, fill=CARD, outline=CARD_LINE, width=2):
    d.rounded_rectangle((s(x0), s(y0), s(x1), s(y1)), radius=s(r), fill=fill, outline=outline, width=s(width))


def text(d, x, y, txt, size=15, bold=False, fill=INK, anchor="la"):
    d.text((s(x), s(y)), txt, font=F(size, bold), fill=fill, anchor=anchor)


def vline_arrow(d, x, y0, y1, color):
    d.line((s(x), s(y0), s(x), s(y1 - 12)), fill=color, width=s(2.5))
    d.polygon([(s(x - 9), s(y1 - 16)), (s(x + 9), s(y1 - 16)), (s(x), s(y1))], fill=color)


def hline_arrow(d, y, x0, x1, color):
    d.line((s(x0), s(y), s(x1 - 10), s(y)), fill=color, width=s(2.5))
    d.polygon([(s(x1 - 14), s(y - 7)), (s(x1 - 14), s(y + 7)), (s(x1), s(y))], fill=color)


def layer(d, y, h, title, note, color):
    box(d, M, y, W - M, y + h, r=18)
    d.rounded_rectangle((s(M), s(y + 16), s(M + 7), s(y + h - 16)), radius=s(3), fill=color)
    text(d, M + 30, y + 18, title, 21, True, color)
    if note:
        text(d, W - M - 24, y + 24, note, 14, False, LIGHT, "ra")


def main() -> Path:
    img = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(img)

    # ---------- 标题 ----------
    text(d, M, 40, "RFQ 询价分析与报价智能体 · 架构总览", 38, True, INK)
    text(d, M, 100, "报价即提交（Quote-as-Commit）：每一次报价都是一次可追溯、可回滚、可重放的数据提交",
         19, False, GRAY)

    # ---------- L1 外部输入 ----------
    y1, h1 = 150, 140
    layer(d, y1, h1, "外部输入", "未经清洗的原始询价，一律视为不可信外部内容", C_IN)
    cards = [
        ("询价邮件 .eml", "正文 + Excel / PDF 附件"),
        ("Excel 附件", "合并单元格 · 多工作表 · 报价清单"),
        ("PDF 附件", "文本层解析 · 扫描件走视觉模型"),
    ]
    gap = 32
    cw = (INNER_W - 40 - gap * 2) / 3
    for i, (t1, t2) in enumerate(cards):
        x0 = M + 30 + i * (cw + gap)
        box(d, x0, y1 + 56, x0 + cw, y1 + h1 - 18, r=12, fill=TINT, outline=TINT_LINE, width=2)
        text(d, x0 + 20, y1 + 68, t1, 17, True)
        text(d, x0 + 20, y1 + 96, t2, 13, False, GRAY)
    vline_arrow(d, W / 2, y1 + h1, y1 + h1 + 40, C_IN)

    # ---------- L2 前端 ----------
    y2, h2 = 330, 160
    layer(d, y2, h2, "前端 · Streamlit 工作台", "Railway 对外唯一端口", C_FE)
    tabs = [("① 接收询价", "样例 / 上传 / 进度"), ("② 报价与审批", "DIFF · 改价 · MERGE"),
            ("③ 决策审计", "节点时间线 · 重放"), ("④ 记忆版本", "分支 · 快照 · 回滚"),
            ("⑤ 记忆网关", "隔离 · 评审 · 升级"), ("⑥ 规则沙盒", "分支回测 · 发布"),
            ("⑦ 模型路由", "任务映射 · 成本")]
    tgap = 22
    tw = (INNER_W - 40 - tgap * 6) / 7
    for i, (t1, t2) in enumerate(tabs):
        x0 = M + 20 + i * (tw + tgap)
        box(d, x0, y2 + 56, x0 + tw, y2 + h2 - 18, r=12, fill=TINT, outline=TINT_LINE, width=2)
        text(d, x0 + tw / 2, y2 + 74, t1, 16, True, anchor="ma")
        text(d, x0 + tw / 2, y2 + 102, t2, 12, False, GRAY, "ma")
    vline_arrow(d, W / 2, y2 + h2, y2 + h2 + 40, C_FE)
    text(d, W / 2 + 16, y2 + h2 + 8, "HTTP → 127.0.0.1:8000", 13, False, LIGHT)

    # ---------- L3 后端编排 ----------
    y3, h3 = 530, 300
    layer(d, y3, h3, "后端 · FastAPI + LangGraph 编排", "仅监听回环地址，不对外暴露", C_BE)
    nodes = [("intake", "进件", "冻结表级快照"), ("parse", "解析邮件·Excel", "·PDF 表格"),
             ("memory_gate", "信任分级", "拦截投毒"), ("normalize", "SKU 匹配", "置信度·来源"),
             ("price ×3", "争单/均衡/利润", "三个数据分支"), ("risk", "低毛利·账期", "异常·高风险"),
             ("approval", "人工审批", "（中断点）"), ("commit", "MERGE / PICK", "落主线"),
             ("render", "Excel · PDF", "HTML · 邮件草稿")]
    ngap = (INNER_W - 40 - 9 * 196) / 8
    for i, (t1, l1, l2) in enumerate(nodes):
        x0 = M + 20 + i * (196 + ngap)
        hot = t1 in ("memory_gate", "approval")
        box(d, x0, y3 + 56, x0 + 196, y3 + 174, r=12,
            fill="#EAF1FA" if hot else TINT, outline=C_BE if hot else TINT_LINE, width=3 if hot else 2)
        text(d, x0 + 98, y3 + 84, t1, 15, True, C_BE if hot else INK, "ma")
        text(d, x0 + 98, y3 + 118, l1, 12, False, GRAY, "ma")
        text(d, x0 + 98, y3 + 142, l2, 12, False, GRAY, "ma")
        if i < len(nodes) - 1:
            hline_arrow(d, y3 + 115, x0 + 196 + 2, x0 + 196 + ngap - 2, C_BE)

    box(d, M + 20, y3 + 196, W - M - 20, y3 + h3 - 18, r=12, fill="#FBFCFE", outline=TINT_LINE, width=2)
    text(d, M + 40, y3 + 212, "approval ── approve ──→ commit ──→ render　｜　── reject ──→ 丢弃分支，不进报价主线",
         14, False, INK)
    text(d, M + 40, y3 + 240, "price 产出三个真实数据分支 ql_<id>_win_first / _balanced / _margin_first，"
                              "审批后整单 MERGE 或逐行 PICK，失败可整体回滚", 14, False, GRAY)
    vline_arrow(d, W / 2, y3 + h3, y3 + h3 + 40, C_BE)

    # ---------- L4 数据与模型 ----------
    y4, h4 = 870, 300
    layer(d, y4, h4, "数据与模型", "两种「版本控制」：业务数据用 Git4Data，长期记忆用 Memoria", C_DATA)
    cols = [
        ("MatrixOne · 业务库", C_DATA, [
            "客户 · 联系人 · 物料主数据",
            "成本价 cost_price · 定价规则 pricing_rule",
            "历史报价 · 报价单 · 报价行",
            "审计事件 audit_event",
            "配置与模型路由 model_route",
        ]),
        ("Git4Data · 数据版本", C_DATA, [
            "SNAPSHOT  进件即冻结成本与规则",
            "BRANCH  三档策略 / 规则沙盒",
            "DIFF  分支与主线逐行比较",
            "MERGE / PICK  审批后提交",
            "ROLLBACK  回到报价当时",
        ]),
        ("Memoria · 记忆版本", "#2E8B57", [
            "main 主线：客户偏好 · 料号映射",
            "inbox_<rfq> 隔离分支：外部声明",
            "snapshot / diff / rollback",
            "MemoryGate 分级 T1–T4",
            "未验证内容永不进主线",
        ]),
        ("ModelRouter · 模型路由", "#B7791F", [
            "fast  抽取 · 澄清 · 摘要",
            "strong  复杂推理 · 高风险复核",
            "vision  PDF / 图片票据",
            "置信度不足 → rule-engine 降级",
            "按模型记录调用与成本",
        ]),
    ]
    cgap = 26
    kw = (INNER_W - 40 - cgap * 3) / 4
    for i, (title, color, lines) in enumerate(cols):
        x0 = M + 20 + i * (kw + cgap)
        box(d, x0, y4 + 56, x0 + kw, y4 + h4 - 18, r=12, fill=TINT, outline=TINT_LINE, width=2)
        text(d, x0 + 20, y4 + 70, title, 16, True, color)
        d.line((s(x0 + 20), s(y4 + 98), s(x0 + kw - 20), s(y4 + 98)), fill=TINT_LINE, width=s(1.5))
        for j, ln in enumerate(lines):
            text(d, x0 + 20, y4 + 108 + j * 30, ln, 13, False, INK)
    vline_arrow(d, W / 2, y4 + h4, y4 + h4 + 40, C_DATA)

    # ---------- L5 审计 ----------
    y5, h5 = 1210, 210
    layer(d, y5, h5, "贯穿全链路 · 决策审计与重放", "AuditLogger.wrap 包装每个节点，异常也留痕", C_AUD)
    box(d, M + 20, y5 + 56, M + 520, y5 + h5 - 18, r=12, fill="#FDF8EC", outline="#E8D9B0", width=2)
    text(d, M + 40, y5 + 74, "AuditLogger.wrap", 16, True, C_AUD)
    text(d, M + 40, y5 + 104, "输入摘要 · 输出 · 模型 ·", 13, False, INK)
    text(d, M + 40, y5 + 128, "提示词/规则版本 · 证据", 13, False, INK)
    hline_arrow(d, y5 + 110, M + 530, M + 570, C_AUD)
    box(d, M + 580, y5 + 56, M + 1440, y5 + h5 - 18, r=12, fill="#FDF8EC", outline="#E8D9B0", width=2)
    text(d, M + 600, y5 + 74, "audit_event 表：每一次决策都留下可复核的字段", 16, True, C_AUD)
    text(d, M + 600, y5 + 106, "节点 · 模型 · 提示词/规则版本 · 数据快照 · 记忆快照", 13, False, INK)
    text(d, M + 600, y5 + 130, "证据 · 置信度 · 耗时 · 成本 · 人工动作", 13, False, INK)
    hline_arrow(d, y5 + 110, M + 1450, M + 1490, C_AUD)
    outs = ["重放：用当时的快照重算差异", "影响分析：从证据反查受影响报价", "成本统计：按模型汇总调用"]
    for j, t in enumerate(outs):
        yy = y5 + 56 + j * 46
        box(d, M + 1500, yy, W - M - 20, yy + 38, r=10, fill="#FFFFFF", outline="#E8D9B0", width=2)
        text(d, M + 1520, yy + 11, f"{j + 1}  {t}", 13, False, INK)

    # ---------- 脚注 ----------
    text(d, M, y5 + h5 + 18, "公网只暴露 Streamlit；FastAPI、数据库、记忆服务均在容器内或远端内网；"
                             "外部邮件内容默认不可信，价格由确定性引擎计算，LLM 只负责抽取与措辞。", 13, False, LIGHT)

    out = Path(__file__).resolve().parents[1] / "docs" / "images" / "architecture.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W, H), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out


if __name__ == "__main__":
    p = main()
    print(f"saved: {p} ({os.path.getsize(p) / 1024:.0f} KB)")
