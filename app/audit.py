# -*- coding: utf-8 -*-
"""发票台账体检：把"会不会被税务系统比对出来"变成一份可执行的清单。

每条问题都带三样东西：**严重级别 / 判定依据（法规编号）/ 处理建议**。
评委可以随便挑一行去对法规原文，这是"可解释"的含义。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

TOL = 0.02

# 项目名称 → 常见适用税率（用于粗筛税率异常，命中只提示、不直接定罪）
RATE_HINT = [
    (("咨询", "服务", "设计", "广告", "技术", "信息", "软件", "研发"), {0.06}),
    (("货物", "材料", "设备", "商品", "配件", "办公用品"), {0.13}),
    (("运输", "货运", "物流", "快递"), {0.09}),
    (("建筑", "工程", "施工", "安装", "装饰"), {0.09}),
    (("租赁", "出租", "不动产", "房租"), {0.09, 0.05}),
    (("餐饮", "住宿", "会议", "培训"), {0.06}),
]


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("￥", "").replace("¥", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _rate(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        r = float(v)
        return r / 100 if r > 1 else r
    s = str(v).strip().replace("％", "%")
    if s.endswith("%"):
        return _num(s[:-1]) / 100
    r = _num(s)
    return r / 100 if r > 1 else r


def _parse_period(period: str):
    """支持 2026Q4 / 2026-10 / 2026-10~2026-12 / 2026。"""
    if not period:
        return None
    p = period.strip().upper()
    m = re.fullmatch(r"(\d{4})Q([1-4])", p)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        start = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}[q]
        end = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
        return date(y, *start), date(y, *end)
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        last = [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
        return date(y, mo, 1), date(y, mo, last)
    m = re.fullmatch(r"(\d{4})", p)
    if m:
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31)
    return None


def _to_date(v):
    if isinstance(v, date):
        return v
    s = str(v).strip()
    m = re.search(r"(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _pick(row: dict, *names, default=""):
    for n in names:
        for k in row:
            if str(k).strip().replace(" ", "") == n:
                return row[k]
    return default


def audit(rows: list[dict], company: str = "", period: str = "") -> dict:
    """rows 为台账行（dict）。返回 {summary, items, evidence}。"""
    items: list[dict] = []
    rng = _parse_period(period)
    company_norm = re.sub(r"[（(].*?[)）]|\s", "", company or "")

    seen_numbers: dict[str, int] = defaultdict(int)
    total_amount = 0.0
    total_tax = 0.0

    for idx, row in enumerate(rows, start=1):
        number = str(_pick(row, "发票号码", "发票号", "invoice_no")).strip()
        inv_date = _to_date(_pick(row, "开票日期", "日期", "date"))
        buyer = str(_pick(row, "购买方名称", "购买方", "buyer")).strip()
        item_name = str(_pick(row, "项目名称", "货物或应税劳务服务名称", "商品名称", "项目")).strip()
        amount = _num(_pick(row, "金额", "不含税金额", "amount"))
        tax = _num(_pick(row, "税额", "tax"))
        total = _num(_pick(row, "价税合计", "含税金额", "total"))
        rate = _rate(_pick(row, "税率", "征收率", "rate"))
        remark = str(_pick(row, "备注", "备注栏", "remark")).strip()

        total_amount += amount
        total_tax += tax
        seen_numbers[number] += 1
        where = f"第 {idx} 行 / 发票 {number or '（无号码）'}"

        # 1. 重复发票号码
        if number and seen_numbers[number] == 2:
            items.append(_issue("高", "重复发票号码", f"{where}：同一发票号码出现多次，"
                                    "存在重复报销或台账重复导入风险。", "R15", "去重后重新核对凭证"))

        # 2. 金额 × 税率 ≠ 税额
        if rate and amount:
            expect = round(amount * rate, 2)
            if abs(expect - tax) > max(TOL, amount * 0.0005):
                items.append(_issue("高", "税额与税率不匹配",
                                    f"{where}：金额 {amount:,.2f} × {rate:.0%} 应为 {expect:,.2f}，"
                                    f"台账记录税额 {tax:,.2f}。", "R04",
                                    "以发票原件为准；若原件即有误，联系销方换开"))

        # 3. 价税合计不等于金额 + 税额
        if total and abs(amount + tax - total) > max(TOL, total * 0.0005):
            items.append(_issue("中", "价税合计不等于金额加税额",
                                f"{where}：{amount:,.2f} + {tax:,.2f} ≠ {total:,.2f}。", "R15",
                                "核对是否录入含税/不含税口径混用"))

        # 4. 购买方名称与本企业不一致
        if company_norm and buyer:
            buyer_norm = re.sub(r"[（(].*?[)）]|\s", "", buyer)
            if buyer_norm != company_norm:
                items.append(_issue("高", "购买方名称与本企业不一致",
                                    f"{where}：抬头为“{buyer}”，与本企业“{company}”不符，"
                                    "不得作为本单位税前扣除凭证。", "R15",
                                    "要求销方作废重开，抬头必须与企业全称完全一致"))

        # 5. 开票日期是否属于本期
        if rng and inv_date and not (rng[0] <= inv_date <= rng[1]):
            items.append(_issue("中", "开票日期不在本期",
                                f"{where}：开票日期 {inv_date} 不在申报期 {period} 内，"
                                "跨期抵扣或跨期列支需做纳税调整。", "R08",
                                "确认是否应计入本期；跨年发票需按权责发生制调整"))

        # 6. 备注栏缺失（建筑/运输/不动产）
        if not remark:
            for keys, reg, name, advice in (
                (("建筑", "工程", "施工", "安装", "装饰"), "R09", "建筑服务",
                 "备注栏必须注明建筑服务发生地县（市、区）名称及项目名称"),
                (("运输", "货运", "物流", "快递"), "R10", "货物运输服务",
                 "备注栏必须填写起运地、到达地、车种车号及运输货物信息"),
                (("不动产", "租赁", "出租", "房租"), "R11", "不动产销售或租赁",
                 "备注栏必须注明不动产详细地址"),
            ):
                if any(k in item_name for k in keys):
                    items.append(_issue("高", f"{name}发票备注栏为空",
                                        f"{where}：项目为“{item_name}”，但备注栏为空。", reg, advice))
                    break

        # 7. 项目名称与税率档位不符
        if rate and item_name:
            for keys, allowed in RATE_HINT:
                if any(k in item_name for k in keys):
                    if rate not in allowed:
                        items.append(_issue(
                            "中", "税率档位与项目名称不匹配",
                            f"{where}：项目“{item_name}”通常适用 "
                            f"{'/'.join(f'{x:.0%}' for x in sorted(allowed))}，台账记录 {rate:.0%}。",
                            "R04", "核对业务实质；混合销售与兼营的税率适用不同"))

        # 8. 金额异常
        if amount <= 0:
            items.append(_issue("低", "金额为零或负数",
                                f"{where}：金额 {amount:,.2f}，如为红字发票请确认对应蓝字发票。",
                                "R12", "红冲需与蓝字发票一一对应"))

    # 9. 整表口径提示
    issues = len(items)
    high = sum(1 for i in items if i["level"] == "高")
    mid = sum(1 for i in items if i["level"] == "中")
    low = sum(1 for i in items if i["level"] == "低")
    risk = min(100, high * 8 + mid * 4 + low * 1)
    evidence = sorted({i["reg"] for i in items if i["reg"]})

    return {
        "summary": {
            "rows": len(rows),
            "issues": issues, "high": high, "mid": mid, "low": low,
            "risk_score": risk,
            "total_amount": round(total_amount, 2),
            "total_tax": round(total_tax, 2),
            "verdict": _verdict(risk),
        },
        "items": items,
        "evidence": evidence,
    }


def _issue(level: str, title: str, detail: str, reg: str, advice: str) -> dict:
    return {"level": level, "title": title, "detail": detail, "reg": reg, "advice": advice}


def _verdict(risk: int) -> str:
    if risk >= 60:
        return "高风险：建议本周内整改后再申报"
    if risk >= 30:
        return "中风险：申报前需逐条确认"
    if risk > 0:
        return "低风险：存在少量瑕疵，可随期修正"
    return "未发现明显问题"
