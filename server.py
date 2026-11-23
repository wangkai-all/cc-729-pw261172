# -*- coding: utf-8 -*-
"""
server.py — HTTP 服务（仅标准库）。提供 JSON API 与静态页面。

    python3 server.py [--port 8000] [--state warehouse_state.json]

API：
    GET  /api/state                     草稿全貌（数据/规则/结果/版本列表）
    POST /api/facts/import              {"text": "edge(a, b).\n..."}  导入事实
    POST /api/facts/add                 {"pred": "edge", "tuple": ["a","b"]}
    POST /api/facts/retract             {"pred": "edge", "tuple": ["a","b"]}  撤销
    POST /api/rules                     {"text": "reach(X,Y) :- ..."}  录入规则
    POST /api/publish                   {"note": "..."}  输入+规则一起发布
    GET  /api/versions/<id>/results     查看某版结果（失败版本返回错误）
    POST /api/compare                   增量维护结果 vs 整版重算
    GET  /api/provenance?pred=..&tuple=..   展开一条结果的支持关系
    GET  /api/versions/<id>/provenance?pred=..&tuple=..
    POST /api/sample/<name>             载入内置样例 cyclic|diamond|masking
    POST /api/reset                     清空草稿
"""

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from app import App
from samples import SAMPLES

STATIC_DIR = Path(__file__).resolve().parent / "static"

APP = None  # 在 main 中初始化


class Handler(BaseHTTPRequestHandler):
    server_version = "DatalogWarehouse/1.0"

    # ------------------------------------------------------------ 基础工具
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        types = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}
        if not path.is_file() or STATIC_DIR not in path.resolve().parents:
            self._send_json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args):  # 静默
        pass

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/" or path == "/index.html":
                return self._send_file(STATIC_DIR / "index.html")
            if path.startswith("/static/"):
                return self._send_file(STATIC_DIR / path[len("/static/"):])
            if path == "/api/state":
                return self._send_json(APP.state_json())
            if path == "/api/samples":
                return self._send_json({
                    k: {"title": v["title"], "description": v["description"],
                        "facts": v["facts"], "rules": v["rules"],
                        "rules_v2": v.get("rules_v2"),
                        "facts_v2_extra": v.get("facts_v2_extra"),
                        "demo_steps": v.get("demo_steps", [])}
                    for k, v in SAMPLES.items()})
            m = re.fullmatch(r"/api/versions/(\d+)/results", path)
            if m:
                out = APP.version_results_json(int(m.group(1)))
                return self._send_json(out if out else {"error": "版本不存在"},
                                       200 if out else 404)
            m = re.fullmatch(r"/api/versions/(\d+)/provenance", path)
            if m:
                q = parse_qs(url.query)
                pred = q["pred"][0]
                tup = json.loads(q["tuple"][0])
                return self._send_json(APP.explain_version(int(m.group(1)), pred, tup))
            if path == "/api/provenance":
                q = parse_qs(url.query)
                pred = q["pred"][0]
                tup = json.loads(q["tuple"][0])
                return self._send_json(APP.explain(pred, tup))
            return self._send_json({"error": "not found"}, 404)
        except Exception as e:
            return self._send_json({"error": f"{type(e).__name__}: {e}"}, 400)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        try:
            body = self._read_json()
            if path == "/api/facts/import":
                return self._send_json(APP.import_facts(body.get("text", "")))
            if path == "/api/facts/add":
                return self._send_json(APP.add_fact(body["pred"], body["tuple"]))
            if path == "/api/facts/retract":
                return self._send_json(APP.retract_fact(body["pred"], body["tuple"]))
            if path == "/api/rules":
                return self._send_json(APP.set_rules(body.get("text", "")))
            if path == "/api/rules/from-form":
                # 结构化表单 → Datalog 文本（同一套校验）
                text = rules_from_form(body.get("rules", []))
                r = APP.set_rules(text)
                r["text"] = text
                return self._send_json(r)
            if path == "/api/publish":
                return self._send_json(APP.publish(body.get("note", "")))
            if path == "/api/compare":
                return self._send_json(APP.compare())
            m = re.fullmatch(r"/api/sample/(\w+)", path)
            if m:
                name = m.group(1)
                if name not in SAMPLES:
                    return self._send_json({"error": "未知样例"}, 404)
                return self._send_json(APP.load_sample(name))
            if path == "/api/reset":
                APP.reset()
                return self._send_json({"ok": True})
            return self._send_json({"error": "not found"}, 404)
        except Exception as e:
            return self._send_json({"error": f"{type(e).__name__}: {e}"}, 400)


def rules_from_form(rules):
    """结构化表单 JSON → Datalog 文本。
    rules: [{"head": {"pred": "reach", "args": ["X","Y"]},
             "body": [{"pred": "edge", "args": ["X","Y"], "negated": false}, ...]}]
    """
    def fmt_atom(a):
        pred = str(a.get("pred", "")).strip()
        if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", pred):
            raise ValueError(f"非法谓词名：{pred!r}（需小写字母开头）")
        args = [str(x).strip() for x in a.get("args", [])]
        for x in args:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|-?\d+|'[^']*'", x):
                raise ValueError(f"非法参数：{x!r}")
        return pred + "(" + ", ".join(args) + ")"

    lines = []
    for r in rules:
        head = fmt_atom(r.get("head", {}))
        body = []
        for lit in r.get("body", []):
            s = fmt_atom(lit)
            if lit.get("negated"):
                s = "not " + s
            body.append(s)
        line = head + (" :- " + ", ".join(body) if body else "") + "."
        lines.append(line)
    return "\n".join(lines) + "\n"


def main():
    global APP
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--state", default="warehouse_state.json",
                    help="持久化文件（输入与规则版本；结果由求值重建）")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()
    APP = App(persist_path=None if args.no_persist else args.state)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"数据加工仓已启动: http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
