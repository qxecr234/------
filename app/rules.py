# -*- coding: utf-8 -*-
"""确定性税务计算引擎。

核心原则：**算术不交给大模型。**
AI 负责听懂问题、找依据、写解释；每一个数字都由这里的规则算出，可复算、可审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

CITY_RATE = {"市区": 0.07, "县城": 0.05, "镇": 0.05, "其他": 0.01}
EDU_RATE = 0.03          # 教育费附加
LOCAL_EDU_RATE = 0.02    # 地方教育附加
SMALL_Q_LIMIT = 300_000  # 小规模纳税人季度免税额度（月10万 ×3）
SMALL_VAT_RATE = 0.01    # 现行 3% 减按 1%
NORMAL_SMALL_RATE = 0.03
CIT_SMALL_RATE = 0.05    # 小微实际税负（减按25%计入 × 20%）
CIT_STD_RATE = 0.25
STAMP_SALE = 0.0003      # 买卖合同万分之三
SMALL_PROFIT_LIMIT = 3_000_000


@dataclass
class CalcInput:
    taxpayer_type: str = "small"      # small / general
    quarter_sales: float = 0.0        # 本季不含税销售额
    special_invoice_sales: float = 0.0  # 其中开具专票的部分（小规模不得免税）
    vat_rate: float = 0.06            # 一般纳税人适用税率
    input_vat: float = 0.0            # 一般纳税人当期可抵扣进项税额
    profit: float = 0.0               # 应纳税所得额
    city: str = "市区"
    contract_amount: float = 0.0      # 本期买卖合同金额（印花税）
    six_tax_half: bool = True         # 是否享受"六税两费"减半
    small_profit: bool = True         # 是否符合小型微利企业条件
    high_tech: bool = False           # 是否高新技术企业


@dataclass
class Line:
    item: str
    base: float
    rate: str
    amount: float
    reg: str = ""
    note: str = ""


@dataclass
class CalcResult:
    lines: list[Line] = field(default_factory=list)
    total: float = 0.0
    effective_rate: float = 0.0
    notes: list[dict] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lines": [asdict(x) for x in self.lines],
            "total": round(self.total, 2),
            "effective_rate": round(self.effective_rate, 4),
            "notes": self.notes,
            "assumptions": self.assumptions,
        }


def _round2(x: float) -> float:
    return round(x + 1e-9, 2)


def compute(p: CalcInput | dict) -> CalcResult:
    if isinstance(p, dict):
        known = {f for f in CalcInput.__dataclass_fields__}
        p = CalcInput(**{k: v for k, v in p.items() if k in known})

    r = CalcResult()
    r.assumptions = [
        "金额均为不含税金额；如手头是含税价，请先除以（1+税率）。",
        "小规模纳税人按季度申报口径计算（季度免税额度 30 万元）。",
        "未考虑地方性财政返还、税收优惠备案差异等个案因素。",
    ]

    sales = max(float(p.quarter_sales or 0), 0.0)
    special = min(max(float(p.special_invoice_sales or 0), 0.0), sales)
    vat = 0.0

    # ---------------- 增值税 ----------------
    if p.taxpayer_type == "small":
        if sales <= SMALL_Q_LIMIT:
            vat = _round2(special * SMALL_VAT_RATE)
            r.lines.append(Line(
                item="增值税（普通发票部分）", base=_round2(sales - special), rate="免征",
                amount=0.0, reg="R01",
                note=f"季度销售额 {sales:,.0f} 元 ≤ 30 万元，符合免征条件"))
            if special > 0:
                r.lines.append(Line(
                    item="增值税（专用发票部分）", base=special, rate="1%",
                    amount=vat, reg="R01/R02/R25",
                    note="专票部分不适用免税，须按 1% 缴纳"))
        else:
            vat = _round2(sales * SMALL_VAT_RATE)
            r.lines.append(Line(
                item="增值税", base=sales, rate="1%", amount=vat, reg="R02",
                note="季度销售额超过 30 万元，全额按 1% 征收"))
            r.notes.append({"level": "高", "reg": "R01",
                            "text": "已超过季度 30 万元免征线，本季全额计税；"
                                    "若为跨季集中开票，建议核对是否存在跨期开票问题。"})
    else:
        output_vat = _round2(sales * float(p.vat_rate))
        vat = _round2(max(output_vat - float(p.input_vat or 0), 0))
        r.lines.append(Line(item="销项税额", base=sales, rate=f"{p.vat_rate:.0%}",
                            amount=output_vat, reg="R04"))
        r.lines.append(Line(item="进项税额（抵扣）", base=float(p.input_vat or 0),
                            rate="—", amount=-_round2(float(p.input_vat or 0)), reg="R05/R06"))
        r.lines.append(Line(item="应纳增值税", base=sales, rate="销项-进项", amount=vat, reg="R05"))
        if output_vat - float(p.input_vat or 0) < 0:
            r.notes.append({"level": "中", "reg": "R05",
                            "text": "当期进项大于销项，形成留抵税额，可结转下期继续抵扣。"})

    # ---------------- 附加税费 ----------------
    city_rate = CITY_RATE.get(p.city, 0.07)
    surcharge_rate = city_rate + EDU_RATE + LOCAL_EDU_RATE
    half = 0.5 if p.six_tax_half else 1.0
    surcharge = _round2(vat * surcharge_rate * half)
    r.lines.append(Line(
        item="附加税费（城建税+教育费附加+地方教育附加）", base=vat,
        rate=f"{surcharge_rate:.0%}" + ("，减半" if p.six_tax_half else ""),
        amount=surcharge, reg="R22/R23/R17",
        note="以实际缴纳的增值税为计征依据"))

    # ---------------- 企业所得税 ----------------
    profit = float(p.profit or 0)
    cit = 0.0
    if profit <= 0:
        r.lines.append(Line(item="企业所得税", base=profit, rate="—", amount=0.0,
                            reg="R20", note="当期亏损，可结转以后年度弥补（最长 5 年）"))
    else:
        if p.high_tech:
            cit = _round2(profit * 0.15)
            r.lines.append(Line(item="企业所得税", base=profit, rate="15%（高新）",
                                amount=cit, reg="R19"))
        elif p.small_profit and profit <= SMALL_PROFIT_LIMIT:
            cit = _round2(profit * CIT_SMALL_RATE)
            r.lines.append(Line(item="企业所得税", base=profit, rate="实际 5%（小微）",
                                amount=cit, reg="R16",
                                note="减按 25% 计入应纳税所得额，按 20% 税率，实际税负 5%"))
        else:
            cit = _round2(profit * CIT_STD_RATE)
            r.lines.append(Line(item="企业所得税", base=profit, rate="25%",
                                amount=cit, reg="R18",
                                note="不符合小型微利企业条件，适用法定税率"))

    # ---------------- 印花税 ----------------
    stamp = 0.0
    if float(p.contract_amount or 0) > 0:
        stamp = _round2(float(p.contract_amount) * STAMP_SALE * half)
        r.lines.append(Line(item="印花税（买卖合同）", base=float(p.contract_amount),
                            rate="0.3‰" + ("，减半" if p.six_tax_half else ""),
                            amount=stamp, reg="R24/R17"))

    r.total = _round2(sum(x.amount for x in r.lines))
    r.effective_rate = (r.total / sales) if sales > 0 else 0.0

    # ---------------- 临界点提示 ----------------
    if p.taxpayer_type == "small":
        gap = SMALL_Q_LIMIT - sales
        if 0 < gap <= 30_000:
            after_tax = _round2((SMALL_Q_LIMIT + 1) * SMALL_VAT_RATE)
            r.notes.append({
                "level": "中", "reg": "R01",
                "text": f"距季度免征线只剩 {gap:,.0f} 元。一旦跨过，是全额计税而不是只对超出部分计税——"
                        f"30 万元线外哪怕 1 元，也要按 30 万元缴约 {after_tax:,.0f} 元。"})
        if sales > SMALL_Q_LIMIT:
            r.notes.append({
                "level": "中", "reg": "R01",
                "text": "若单个客户一次性大额开票导致跳档，可考虑按合同约定分月开票，平滑季度销售额。"})
    if p.taxpayer_type == "general" and p.vat_rate not in (0.13, 0.09, 0.06, 0.03):
        r.notes.append({"level": "低", "reg": "R04", "text": "适用税率不在法定档位内，请核对业务类型。"})

    return r


def explain_data_gaps(p: CalcInput | dict) -> list[str]:
    """指出用户没说清楚、但会影响结果的关键信息。"""
    if isinstance(p, dict):
        known = {f for f in CalcInput.__dataclass_fields__}
        p = CalcInput(**{k: v for k, v in p.items() if k in known})
    gaps = []
    if p.taxpayer_type == "small" and p.special_invoice_sales == 0:
        gaps.append("是否有开具增值税专用发票？专票部分即使未超 30 万元也不能免税。")
    if p.taxpayer_type == "general" and p.input_vat == 0:
        gaps.append("当期是否有可抵扣的进项专票？进项为 0 会让应纳增值税被高估。")
    if p.profit == 0:
        gaps.append("应纳税所得额是多少？该项为 0 时企业所得税按 0 计算。")
    return gaps
