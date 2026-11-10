# -*- coding: utf-8 -*-
"""AI 适配层：意图解析 + 回答组织。

三条硬规则
----------
1. **AI 不碰算术**：数字一律来自 rules.py 的确定性计算，模型只负责把结果讲成人话。
2. **AI 不碰事实**：所有法规表述必须来自检索命中的条文，模型不得自行补充条文内容。
3. **不知道就说不知道**：检索不命中直接拒答，绝不"编一个最像的"。

无 API Key 时自动降级为模板模式，离线也能跑完整 Demo。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

SYSTEM_PROMPT = """你是「票准」的中小企业财税合规助手。你的回答必须遵守以下规则：

1. 只能使用<依据>中给出的条文。不得引用、复述或推断任何未在<依据>中出现的法规内容。
2. 不得进行任何税额计算。所有数字由系统的计算引擎给出，你只负责解释这些数字的含义。
3. 如果<依据>不足以回答问题，直接回答"现有的法规库里没有找到明确依据"，并说明需要补充什么材料。
4. 回答用简体中文，先给结论，再给依据，最后给需要用户确认的信息。不超过 200 字。
5. 不得使用"包过""保证没问题""一定可以"这类表述。合规建议必须留出个案判断空间。
"""


# --------------------------------------------------------------------------
# LLM 适配（OpenAI 兼容接口，可指向 DeepSeek / 通义 / 智谱）
# --------------------------------------------------------------------------
def _load_env_file() -> None:
    """读取项目根目录的 .env（如果有）。

    只补齐当前进程里还没有的变量，因此命令行设置的环境变量优先级更高。
    .env 是明文密钥，已在 .gitignore 中排除，不要提交。
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip().lstrip("export ").strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_env_file()


def llm_enabled() -> bool:
    return bool(os.environ.get("TAXSURE_API_KEY"))


def call_llm(messages: list[dict], max_tokens: int = 600, timeout: float = 12.0) -> str | None:
    if not llm_enabled():
        return None
    base = os.environ.get("TAXSURE_API_BASE", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TAXSURE_MODEL", "deepseek-chat")
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["TAXSURE_API_KEY"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        # 任何网络/鉴权/格式问题都静默降级，保证 Demo 不会因为没网而卡死
        return None


# --------------------------------------------------------------------------
# 意图解析：先规则、后模型
# --------------------------------------------------------------------------
MONEY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|w|W)?\s*(元|块钱)?")
CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

INTENT_KEYWORDS = {
    "calc": ("交多少", "要交", "多少税", "怎么算", "税负", "算一下", "试算", "应纳税"),
    # 注意：只靠"发票"两个字不能判为体检类，否则"餐饮发票能不能抵扣"会被误路由
    "audit": ("台账", "体检", "校验", "重复开票", "清单", "批量", "逐行", "上传"),
}


def _to_amount(text: str) -> float | None:
    m = MONEY_RE.search(text)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2):
        val *= 10000
    return val


def parse_intent(question: str) -> dict:
    """返回 {intent, params, confidence}。params 直接喂给 rules.compute。"""
    q = question.strip()
    params: dict = {}
    confidence = 0.5

    if "一般纳税人" in q or "专票抵扣" in q or "进项" in q:
        params["taxpayer_type"] = "general"
    elif "小规模" in q:
        params["taxpayer_type"] = "small"

    amount = _to_amount(q)
    if amount is None:
        for cn, num in CN_NUM.items():
            if f"{cn}万" in q:
                amount = num * 10000
                break
    if amount is not None:
        params["quarter_sales"] = amount
        confidence = 0.7

    if "专票" in q or "专用发票" in q:
        params["special_invoice_sales"] = amount or 0.0
    if "利润" in q or "所得额" in q:
        profit = _to_amount(q.split("利润")[-1]) if "利润" in q else None
        if profit is None and "所得额" in q:
            profit = _to_amount(q.split("所得额")[-1])
        if profit:
            params["profit"] = profit
    if "市区" in q or "县城" in q:
        params["city"] = "县城" if "县城" in q else "市区"

    if any(k in q for k in INTENT_KEYWORDS["calc"]):
        intent = "calc"
    elif any(k in q for k in INTENT_KEYWORDS["audit"]) and "多少税" not in q:
        intent = "audit"
    else:
        intent = "qa"

    # 有模型时用模型复核意图，但只在规则判不准时介入
    if llm_enabled() and confidence < 0.7:
        raw = call_llm([
            {"role": "system", "content":
                '你是意图分类器。只输出 JSON：{"intent":"calc|audit|qa","params":{...}}。'
                'params 只允许字段 taxpayer_type, quarter_sales, special_invoice_sales, '
                'profit, city。金额单位：元。不要输出任何解释。'},
            {"role": "user", "content": q},
        ], max_tokens=160)
        if raw:
            try:
                m = re.search(r"\{.*\}", raw, re.S)
                obj = json.loads(m.group()) if m else {}
                if obj.get("intent") in ("calc", "audit", "qa"):
                    intent = obj["intent"]
                    params.update({k: v for k, v in (obj.get("params") or {}).items()
                                   if k in ("taxpayer_type", "quarter_sales",
                                            "special_invoice_sales", "profit", "city")})
                    confidence = 0.9
            except Exception:
                pass

    return {"intent": intent, "params": params, "confidence": confidence}


# --------------------------------------------------------------------------
# 回答组织
# --------------------------------------------------------------------------
def _cite(hit: dict) -> str:
    art = f"第{hit['article'].strip('第条')}条" if hit.get("article") else ""
    head = f"《{hit['title']}》"
    doc = f"（{hit['doc_no']}{'，' + art if art else ''}）" if hit.get("doc_no") else ""
    return f"{head}{doc}"


def _evidence_block(hits: list[dict]) -> str:
    lines = []
    for i, h in enumerate(hits, 1):
        lines.append(f"[{i}] {h['doc_no']} {h.get('article', '')}：{h['text']}")
    return "\n".join(lines)


def compose_answer(question: str, hits: list[dict], calc: dict | None,
                   gaps: list[str], intent: str) -> dict:
    """生成最终回答。有模型时让模型组织语言，否则走模板，两者引用同一条文。"""
    if intent == "audit" and not calc:
        return {
            "answer": "发票台账体检需要具体数据。请到「发票体检」页上传台账（Excel 或 CSV），"
                      "我会逐行比对发票号码、税率、金额、备注栏，并给出问题清单和整改建议。",
            "source": "template", "citations": [],
        }

    if not hits:
        return {
            "answer": "现有的法规库里没有找到明确依据。\n\n"
                      "为了定位到具体条文，请补充三条信息：① 业务实质（卖货物还是提供服务，"
                      "具体是什么服务）② 纳税人身份（小规模 / 一般纳税人）③ 金额是否含专票。"
                      "补齐后我再检索一次——不确定的问题我不会给你一个看起来像答案的答案。",
            "source": "template", "citations": [],
        }

    if llm_enabled():
        sys_msg = SYSTEM_PROMPT + "\n\n<依据>\n" + _evidence_block(hits) + "\n</依据>"
        user_msg = question
        if calc:
            user_msg += "\n\n<计算引擎结果>\n" + json.dumps(
                {"total": calc["total"], "lines": calc["lines"]}, ensure_ascii=False) + "\n</计算引擎结果>"
        text = call_llm([{"role": "system", "content": sys_msg},
                         {"role": "user", "content": user_msg}])
        if text:
            return {"answer": text, "source": "llm", "citations": [_cite(h) for h in hits]}

    # -------- 模板模式 --------
    return {
        "answer": _template_answer(question, hits, calc, gaps, intent),
        "source": "template",
        "citations": [_cite(h) for h in hits],
    }


def _template_answer(question, hits, calc, gaps, intent) -> str:
    parts = []
    top = hits[0]
    top_concl = top.get("conclusion") or top["text"]

    if calc:
        parts.append(
            f"按你给的口径试算：本季应纳税费合计 **{calc['total']:,.2f} 元**，"
            f"综合税负率约 **{calc['effective_rate']:.2%}**。"
        )
        detail = "；".join(
            f"{l['item']} {l['amount']:,.2f} 元（{l['note']}）"
            for l in calc["lines"] if abs(l["amount"]) > 0.001
        )
        if detail:
            parts.append("明细：" + detail + "。")
        parts.append(f"这么算的依据是：{top_concl}")
    else:
        parts.append(f"结论：{top_concl}")

    if len(hits) > 1:
        # 只补充"和首条依据相关度接近"的条文，避免把不相关的法条硬塞进回答
        limit = 1 if calc else 2
        top_score = hits[0].get("_score", 0) or 1
        extras = []
        for h in hits[1:1 + limit]:
            if (h.get("_score", 0) or 0) < top_score * 0.6:
                continue
            c = h.get("conclusion") or h["text"]
            if c and c != top_concl:
                extras.append(f"另外，{c}")
        if extras:
            parts.append("\n\n".join(extras))

    for n in (calc or {}).get("notes", [])[:2]:
        parts.append(f"提醒：{n['text']}")

    if gaps:
        parts.append("需要你确认：" + " ".join(gaps[:2]))

    parts.append("以上为规则试算，不构成税务意见；具体以主管税务机关口径为准。"
                 if calc else
                 "以上条文均来自法规库原文，具体适用请结合个案判断；不构成税务意见。")
    return "\n\n".join(parts)
