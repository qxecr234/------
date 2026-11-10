# -*- coding: utf-8 -*-
"""「票准」本地服务：Python 标准库 HTTP 服务 + 三个 JSON 接口。

零第三方依赖（Excel 解析除外，用 pandas/openpyxl，已随环境提供）。
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import audit as audit_mod          # noqa: E402
from app import llm, rules                 # noqa: E402
from app.rag import RegulationIndex, has_evidence  # noqa: E402

WEB = BASE / "web"
DATA = BASE / "data"
INDEX = RegulationIndex(DATA / "regulations.jsonl")

SAMPLE_COLUMNS = ["发票号码", "开票日期", "购买方名称", "项目名称", "金额", "税率", "税额", "价税合计", "备注"]


def _read_table(raw: bytes, filename: str) -> list[dict]:
    """把上传的上传文件读成 dict 列表。支持 xlsx / xls / csv。"""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xls")):
        import pandas as pd
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
        return df.fillna("").to_dict(orient="records")
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法识别文件编码，请用 UTF-8 或 GBK 保存")
    import csv
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


class Handler(BaseHTTPRequestHandler):
    server_version = "PiaoZhun/0.9"

    # ---------------- 基础 ----------------
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---------------- GET ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_file(WEB / "index.html")
            if path == "/api/health":
                return self._json({"ok": True, "regs": len(INDEX.docs),
                                   "llm": llm.llm_enabled()})
            if path == "/api/regs":
                return self._json({"ok": True, "items": [
                    {k: d.get(k, "") for k in ("id", "title", "doc_no", "article", "text", "tags")}
                    for d in INDEX.docs]})
            if path == "/api/sample":
                return self._json({"ok": True, "columns": SAMPLE_COLUMNS,
                                   "rows": _sample_rows()})
            if path.startswith("/static/"):
                return self._serve_file(WEB / path.split("/static/", 1)[1])
            return self._err("未知路径 " + path, 404)
        except Exception:
            traceback.print_exc()
            return self._err("服务内部错误", 500)

    def _serve_file(self, p: Path):
        if not p.exists() or not p.is_file():
            return self._err("文件不存在", 404)
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if p.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            raw = self._body()
            if path == "/api/ask":
                return self._handle_ask(raw)
            if path == "/api/calc":
                return self._handle_calc(raw)
            if path == "/api/audit":
                return self._handle_audit(raw)
            return self._err("未知路径 " + path, 404)
        except Exception as exc:
            traceback.print_exc()
            return self._err(f"处理失败：{exc}", 500)

    # ---------------- 问答 ----------------
    def _handle_ask(self, raw: bytes):
        payload = json.loads(raw or b"{}")
        question = (payload.get("question") or "").strip()
        if not question:
            return self._err("问题不能为空")

        parsed = llm.parse_intent(question)
        intent = parsed["intent"]
        params = parsed["params"]

        calc = None
        gaps: list[str] = []
        if intent == "calc":
            payload_params = dict(params)
            payload_params.setdefault("taxpayer_type", "small")
            payload_params.setdefault("quarter_sales", 0.0)
            payload_params.setdefault("profit", 0.0)
            gaps = rules.explain_data_gaps(payload_params)
            calc = rules.compute(payload_params).to_dict()

        hits = INDEX.search(question, top_k=3)
        if not has_evidence(hits):
            hits = []

        result = llm.compose_answer(question, hits, calc, gaps, intent)
        self._json({
            "ok": True,
            "intent": intent,
            "params": params,
            "calc": calc,
            "gaps": gaps,
            "answer": result["answer"],
            "source": result["source"],
            "citations": result["citations"],
            "hits": [{k: h.get(k, "") for k in ("id", "title", "doc_no", "article", "text", "conclusion")}
                     | {"score": h["_score"]} for h in hits],
        })

    # ---------------- 试算 ----------------
    def _handle_calc(self, raw: bytes):
        payload = json.loads(raw or b"{}")
        params = payload.get("params") or payload
        result = rules.compute(params)
        self._json({"ok": True, "calc": result.to_dict(),
                    "gaps": rules.explain_data_gaps(params)})

    # ---------------- 体检 ----------------
    def _handle_audit(self, raw: bytes):
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            payload = json.loads(raw or b"{}")
            rows = payload.get("rows") or []
        else:
            filename = self.headers.get("X-Filename") or "upload.csv"
            rows = _read_table(raw, filename)
            payload = {"company": "", "period": ""}
            # 其余参数放在 header 里，避免 multipart 解析
            payload["company"] = _unquote(self.headers.get("X-Company") or "")
            payload["period"] = _unquote(self.headers.get("X-Period") or "")

        if not rows:
            return self._err("台账为空，请检查文件")

        result = audit_mod.audit(rows, payload.get("company", ""), payload.get("period", ""))
        regs = {d["id"]: d for d in INDEX.docs}
        for item in result["items"]:
            r = regs.get(item["reg"])
            item["reg_title"] = f"《{r['title']}》" if r else ""
            item["reg_doc"] = r["doc_no"] if r else ""
        self._json({"ok": True, **result})


def _unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)


def _sample_rows() -> list[dict]:
    """内置样例台账：故意埋了 6 类常见问题，用于一键演示。"""
    return [
        {"发票号码": "26500000123456789012", "开票日期": "2026-10-08", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "信息技术服务", "金额": 10000.00, "税率": "6%", "税额": 600.00, "价税合计": 10600.00, "备注": ""},
        {"发票号码": "26500000123456789013", "开票日期": "2026-10-09", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "咨询服务费", "金额": 20000.00, "税率": "13%", "税额": 2600.00, "价税合计": 22600.00, "备注": ""},
        {"发票号码": "26500000123456789013", "开票日期": "2026-10-09", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "咨询服务费", "金额": 20000.00, "税率": "13%", "税额": 2600.00, "价税合计": 22600.00, "备注": ""},
        {"发票号码": "26500000123456789014", "开票日期": "2026-07-15", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "办公用品", "金额": 5000.00, "税率": "13%", "税额": 650.00, "价税合计": 5650.00, "备注": ""},
        {"发票号码": "26500000123456789015", "开票日期": "2026-11-02", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "建筑安装工程服务", "金额": 80000.00, "税率": "9%", "税额": 7200.00, "价税合计": 87200.00, "备注": ""},
        {"发票号码": "26500000123456789016", "开票日期": "2026-11-05", "购买方名称": "山城智造科技",
         "项目名称": "货物运输服务", "金额": 3000.00, "税率": "9%", "税额": 270.00, "价税合计": 3270.00, "备注": ""},
        {"发票号码": "26500000123456789017", "开票日期": "2026-11-06", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "餐饮服务", "金额": 1200.00, "税率": "6%", "税额": 72.00, "价税合计": 1290.00, "备注": ""},
        {"发票号码": "26500000123456789018", "开票日期": "2026-11-08", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "设备租赁", "金额": 15000.00, "税率": "9%", "税额": 1350.00, "价税合计": 16350.00, "备注": ""},
        {"发票号码": "26500000123456789019", "开票日期": "2026-11-10", "购买方名称": "重庆山城智造科技有限公司",
         "项目名称": "办公设备采购", "金额": 10000.00, "税率": "13%", "税额": 1000.00, "价税合计": 11000.00, "备注": ""},
    ]


def serve(port: int | None = None, host: str = "127.0.0.1"):
    port = port or int(os.environ.get("TAXSURE_PORT", "8848"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print("=" * 56)
    print("  票准 TaxSure · 本地演示服务")
    print(f"  打开： {url}")
    print(f"  法规库：{len(INDEX.docs)} 条     LLM：{'已接入' if llm.llm_enabled() else '未配置（模板模式）'}")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
