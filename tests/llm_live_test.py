# -*- coding: utf-8 -*-
"""在线实测：需要配置 TAXSURE_API_KEY，验证大模型分支真的能跑通。

与 smoke_test.py 的分工：
* smoke_test.py —— 离线回归，28 项，不需要网络，任何时候都能跑
* llm_live_test.py —— 在线实测，4 项，需要 Key，验证“模型组织语言”这一层

用法：
    python run.py                 # 启动服务（带 Key 的环境）
    python tests/llm_live_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8848"
PASS = 0
FAIL = 0


def call(path: str, payload: dict | None = None, timeout: float = 60):
    req = urllib.request.Request(BASE + path)
    if payload is not None:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    health = call("/api/health")
    if not health.get("llm"):
        print("跳过：服务未配置 TAXSURE_API_KEY，当前是模板模式。")
        print("设置方式： set TAXSURE_API_KEY=sk-***  然后重启 python run.py")
        return 2
    print("模型模式已启用，开始实测（每次调用约 2–3 秒）\n")

    cases = [
        ("我这个季度开了 28 万的普票，小规模纳税人，要交多少税？", True),
        ("建筑服务发票备注栏要写什么？", True),
        ("公司这个月有餐饮发票，进项能抵扣吗？", True),
        ("今天重庆天气怎么样，适合吃火锅吗", False),   # 应为拒答，走模板
    ]

    for q, expect_llm in cases:
        d = call("/api/ask", {"question": q})
        src = d.get("source")
        ans = d.get("answer", "")
        print(f"\n> {q}")
        print(f"  source={src}  len={len(ans)}  citations={len(d.get('citations') or [])}")
        if expect_llm:
            check("走大模型分支", src == "llm", f"(实际 {src})")
            check("回答非空（>40 字）", len(ans) > 40, f"({len(ans)} 字)")
            check("带引用条文", bool(d.get("citations")), f"({len(d.get('citations') or [])} 条)")
            check("未编造条文号", any(c and "《" in c for c in (d.get("citations") or [])),
                  "")
        else:
            check("无关问题不走模型、直接拒答", src == "template" and not d.get("hits"),
                  f"(source={src}, hits={len(d.get('hits') or [])})")

    # 延迟单独测一遍，因为报告里要写数字
    t = time.time()
    call("/api/ask", {"question": "小规模纳税人季度 30 万免税是怎么回事？"})
    dt = round(time.time() - t, 2)
    check("端到端延迟 < 15 秒", dt < 15, f"({dt}s)")

    print(f"\n结果：{PASS} 项通过，{FAIL} 项失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
