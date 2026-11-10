# -*- coding: utf-8 -*-
"""端到端自测：不依赖浏览器，直接打三个接口。

用法：先 `python run.py`，再 `python tests/smoke_test.py`
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8848"
PASS = 0
FAIL = 0


def call(path: str, payload: dict | None = None):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main():
    print("1. 健康检查")
    h = call("/api/health")
    check("服务在线", h.get("ok") is True)
    check("法规库已加载", h.get("regs", 0) >= 20, f"({h.get('regs')} 条)")

    print("\n2. 政策问答 · 应命中具体条文")
    cases = [
        ("我这个季度开了 28 万的普票，小规模纳税人，要交多少税？", ["R01", "R02"]),
        ("建筑服务发票备注栏要写什么？", ["R09"]),
        ("公司这个月有餐饮发票，进项能抵扣吗？", ["R26"]),
        ("年销售额 480 万，要不要转一般纳税人？", ["R07"]),
        ("小微企业实际税负 5% 是怎么回事？", ["R16"]),
        ("我开的是专票，还能享受季度 30 万免税吗？", ["R01", "R25"]),
    ]
    for q, expect in cases:
        d = call("/api/ask", {"question": q})
        got = [x["id"] for x in d.get("hits", [])]
        hit = any(e in got for e in expect)
        check(f"「{q[:16]}…」", hit, f"命中 {got} 期望 {expect}")
        check("  有回答文本", bool(d.get("answer")), f"({len(d.get('answer',''))} 字)")

    print("\n3. 拒答机制 · 无关问题不应硬答")
    d = call("/api/ask", {"question": "今天重庆天气怎么样，适合吃火锅吗"})
    check("拒答而不是编造", "没有找到明确依据" in d.get("answer", "") or not d.get("hits"),
          f"hits={[x['id'] for x in d.get('hits', [])]}")

    print("\n4. 发票体检 · 应查出预埋的问题")
    s = call("/api/sample")
    a = call("/api/audit", {"rows": s["rows"], "company": "重庆山城智造科技有限公司", "period": "2026Q4"})
    check("返回问题清单", a.get("ok") and a["summary"]["issues"] >= 5,
          f"({a['summary']['issues']} 条)")
    titles = {i["title"] for i in a["items"]}
    for t in ("重复发票号码", "税额与税率不匹配", "税率档位与项目名称不匹配",
              "购买方名称与本企业不一致", "开票日期不在本期", "建筑服务发票备注栏为空"):
        check(f"检出：{t}", t in titles)
    check("每条都有法规依据", all(i["reg"] for i in a["items"]))
    check("风险分合理", 0 < a["summary"]["risk_score"] <= 100, f"({a['summary']['risk_score']})")

    print("\n5. 税费试算 · 数字应可复算")
    c = call("/api/calc", {"params": {"taxpayer_type": "small", "quarter_sales": 280000,
                                      "profit": 200000, "city": "市区"}})
    vat = [l for l in c["calc"]["lines"] if l["item"].startswith("增值税")]
    check("28 万普票 · 增值税免征", all(l["amount"] == 0 for l in vat), f"{vat}")
    check("企业所得税按小微 5%", abs(c["calc"]["lines"][-1]["amount"] if False else
                                [l["amount"] for l in c["calc"]["lines"] if "所得税" in l["item"]][0]
                                - 10000) < 0.01, "(200000×5%=10000)")

    c2 = call("/api/calc", {"params": {"taxpayer_type": "small", "quarter_sales": 320000,
                                       "profit": 0}})
    vat2 = [l["amount"] for l in c2["calc"]["lines"] if l["item"] == "增值税"][0]
    check("32 万 · 全额按 1% 计税", abs(vat2 - 3200) < 0.01, f"({vat2})")

    c3 = call("/api/calc", {"params": {"taxpayer_type": "general", "quarter_sales": 1000000,
                                       "vat_rate": 0.06, "input_vat": 20000, "profit": 0}})
    vat3 = [l["amount"] for l in c3["calc"]["lines"] if l["item"] == "应纳增值税"][0]
    check("一般纳税人 · 销项减进项", abs(vat3 - 40000) < 0.01, f"(1000000×6%-20000=40000, 实际 {vat3})")

    print(f"\n结果：{PASS} 项通过，{FAIL} 项失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
