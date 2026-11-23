# -*- coding: utf-8 -*-
"""
app.py — 数据加工仓的应用状态：草稿区（输入 + 规则 + 增量维护的结果）
与版本仓库（输入和规则一起发布；发布失败保留上一版可查结果）。

被 server.py（HTTP）与测试共用，不依赖网络。
"""

from __future__ import annotations

import json
import os
import time

import engine
from samples import SAMPLES


def _sort_tuples(ts):
    """元组里可能混有字符串与数字，统一按文本表示排序（避免 TypeError）。"""
    return sorted((list(t) for t in ts), key=lambda t: [str(x) for x in t])


class App:
    def __init__(self, persist_path=None):
        self.persist_path = persist_path
        # 草稿区
        self.edb = {}                 # pred -> {tuple}
        self.rules_text = ""
        self.rules = []               # 最近一次通过校验的规则
        self.rules_error = None       # 当前 rules_text 的校验错误（None 表示有效）
        self.rules_error_detail = None
        self.state = None             # 与 self.rules 对应的增量维护状态
        # 版本仓库
        self.versions = []            # 每个版本：输入+规则快照 + 状态（成功时）
        self.next_vid = 1
        if persist_path and os.path.exists(persist_path):
            self._load()

    # ------------------------------------------------------------------ 工具
    def _edb_add(self, pred, tup):
        self.edb.setdefault(pred, set()).add(tuple(tup))

    def _snapshot_edb(self):
        return {p: set(ts) for p, ts in self.edb.items()}

    def _ensure_state(self):
        if self.state is None and not self.rules_error:
            self.state = engine.evaluate_full(self._snapshot_edb(), self.rules)

    # ------------------------------------------------------------------ 数据
    def import_facts(self, text):
        """导入事实文本（pred(a, b). 每行一条），增量合并进草稿。"""
        facts = engine.parse_facts(text)
        added = set()
        for pred, tup in facts:
            if tup not in self.edb.get(pred, set()):
                added.add((pred, tup))
        return self._apply(added, set())

    def add_fact(self, pred, tup):
        tup = tuple(tup)
        if tup in self.edb.get(pred, set()):
            return {"added": [], "removed": [], "stats": {"noop": True}}
        return self._apply({(pred, tup)}, set())

    def retract_fact(self, pred, tup):
        tup = tuple(tup)
        if tup not in self.edb.get(pred, set()):
            return {"added": [], "removed": [], "stats": {"noop": True}}
        return self._apply(set(), {(pred, tup)})

    def _apply(self, added, removed):
        """EDB 增删 → 增量维护（DRed / 继续迭代），返回派生事实变化。"""
        self._ensure_state()
        for pred, tup in added:
            self._edb_add(pred, tup)
        for pred, tup in removed:
            if pred in self.edb:
                self.edb[pred].discard(tup)
        if self.state is None:
            delta = {"added": set(added), "removed": set(removed), "stats": {}}
        else:
            delta = engine.apply_changes(self.state, added, removed)
        self._persist()
        return self._delta_json(delta)

    # ------------------------------------------------------------------ 规则
    def set_rules(self, text):
        """录入规则（Datalog 子集）。校验失败则拒绝应用：保留上一份有效规则
        与对应结果，返回错误（含负向环的循环路径）。"""
        self.rules_text = text
        try:
            rules = engine.parse_program(text)
            engine.check_safety(rules)
            engine.stratify(rules)  # 分层检查（含负向环检测）
        except engine.StratificationError as e:
            self.rules_error = str(e)
            self.rules_error_detail = {"cycle": e.cycle, "rules": e.cycle_rules}
            self._persist()
            return {"ok": False, "error": self.rules_error,
                    "detail": self.rules_error_detail}
        except (engine.ParseError, engine.SafetyError) as e:
            self.rules_error = str(e)
            self.rules_error_detail = None
            self._persist()
            return {"ok": False, "error": self.rules_error}
        self.rules = rules
        self.rules_error = None
        self.rules_error_detail = None
        # 规则变化不做增量，整版重算草稿状态
        self.state = engine.evaluate_full(self._snapshot_edb(), self.rules)
        self._persist()
        return {"ok": True, "strata": self.strata_info(),
                "stats": self.state.stats}

    def strata_info(self):
        if not self.rules:
            return []
        strata, _ = engine.stratify(self.rules)
        out = []
        for i, rids in enumerate(strata):
            out.append({
                "level": i,
                "preds": sorted({self.rules[r].head.pred for r in rids}),
                "rules": [self.rules[r].text() for r in rids],
            })
        return out

    # ------------------------------------------------------------------ 发布
    def publish(self, note=""):
        """把当前输入和规则一起发布为一个版本。
        失败（规则无效/求值异常）时记录失败版本，上一版结果仍可查。"""
        vid = self.next_vid
        self.next_vid += 1
        ver = {
            "id": vid,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
            "edb": self._snapshot_edb(),
            "rules_text": self.rules_text,
            "status": "ok",
            "error": None,
            "state": None,
            "summary": None,
        }
        if self.rules_error:
            ver["status"] = "failed"
            ver["error"] = self.rules_error
        else:
            try:
                st = engine.evaluate_full(ver["edb"], self.rules)
                ver["state"] = st
                ver["summary"] = {
                    p: len(ts) for p, ts in sorted(st.relations().items())
                }
                # 草稿继续在发布结果的一份副本上做增量维护
                self.state = st.clone()
            except Exception as e:  # 求值异常 → 失败版本
                ver["status"] = "failed"
                ver["error"] = f"{type(e).__name__}: {e}"
        self.versions.append(ver)
        self._persist()
        return self._version_json(ver)

    def get_version(self, vid):
        for v in self.versions:
            if v["id"] == vid:
                return v
        return None

    # ------------------------------------------------------------------ 查询
    def compare(self):
        """比较草稿的增量维护结果与整版重算结果。"""
        self._ensure_state()
        if self.state is None:
            return {"ok": False, "error": "当前没有有效规则，无法比较"}
        full = engine.evaluate_full(self._snapshot_edb(), self.rules)
        only_inc, only_full = engine.diff_states(self.state, full)
        return {
            "ok": True,
            "equal": not only_inc and not only_full,
            "only_incremental": sorted(engine.fact_text(k) for k in only_inc),
            "only_full": sorted(engine.fact_text(k) for k in only_full),
            "incremental_facts": sum(len(t) for t in self.state.relations().values()),
            "full_facts": sum(len(t) for t in full.relations().values()),
            "full_stats": full.stats,
        }

    def explain(self, pred, tup):
        self._ensure_state()
        if self.state is None:
            return {"exists": False, "derivations": []}
        return engine.explain(self.state, pred, tuple(tup))

    def explain_version(self, vid, pred, tup):
        v = self.get_version(vid)
        if not v or v["status"] != "ok":
            return {"exists": False, "derivations": []}
        return engine.explain(v["state"], pred, tuple(tup))

    # ------------------------------------------------------------------ 样例
    def load_sample(self, name):
        s = SAMPLES[name]
        self.edb = {}
        self.rules_text = s["rules"]
        self.rules = []
        self.rules_error = None
        self.rules_error_detail = None
        self.state = None
        facts = engine.parse_facts(s["facts"])
        for pred, tup in facts:
            self._edb_add(pred, tup)
        r = self.set_rules(s["rules"])
        self._persist()
        return {"ok": r["ok"], "title": s["title"], "description": s["description"]}

    def reset(self):
        self.edb = {}
        self.rules_text = ""
        self.rules = []
        self.rules_error = None
        self.rules_error_detail = None
        self.state = None
        self._persist()

    # ------------------------------------------------------------------ 序列化
    def state_json(self):
        self._ensure_state()
        rels = self.state.relations() if self.state else {p: set(t) for p, t in self.edb.items()}
        idb = set(self.state.idb_preds()) if self.state else set()
        return {
            "edb": {p: _sort_tuples(ts) for p, ts in sorted(self.edb.items())},
            "relations": {p: _sort_tuples(ts) for p, ts in sorted(rels.items())},
            "idb_preds": sorted(idb),
            "rules_text": self.rules_text,
            "rules_error": self.rules_error,
            "rules_error_detail": self.rules_error_detail,
            "strata": self.strata_info() if not self.rules_error else [],
            "versions": [self._version_json(v, with_state=False) for v in self.versions],
            "stats": self.state.stats if self.state else None,
        }

    def version_results_json(self, vid):
        v = self.get_version(vid)
        if not v:
            return None
        out = self._version_json(v, with_state=False)
        if v["status"] == "ok":
            out["relations"] = {
                p: _sort_tuples(ts)
                for p, ts in sorted(v["state"].relations().items())
            }
            out["idb_preds"] = v["state"].idb_preds()
        return out

    def _version_json(self, v, with_state=False):
        return {
            "id": v["id"], "ts": v["ts"], "note": v.get("note", ""),
            "status": v["status"], "error": v["error"],
            "summary": v["summary"],
            "rules_text": v["rules_text"],
            "edb": {p: _sort_tuples(ts) for p, ts in sorted(v["edb"].items())},
        }

    def _delta_json(self, delta):
        return {
            "added": sorted(engine.fact_text(k) for k in delta["added"]),
            "removed": sorted(engine.fact_text(k) for k in delta["removed"]),
            "stats": delta["stats"],
        }

    # ------------------------------------------------------------------ 持久化
    def _persist(self):
        if not self.persist_path:
            return
        data = {
            "edb": {p: [list(t) for t in ts] for p, ts in self.edb.items()},
            "rules_text": self.rules_text,
            "next_vid": self.next_vid,
            "versions": [
                {"id": v["id"], "ts": v["ts"], "note": v.get("note", ""),
                 "status": v["status"], "error": v["error"],
                 "rules_text": v["rules_text"],
                 "edb": {p: [list(t) for t in ts] for p, ts in v["edb"].items()}}
                for v in self.versions
            ],
        }
        tmp = self.persist_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.persist_path)

    def _load(self):
        with open(self.persist_path, encoding="utf-8") as f:
            data = json.load(f)
        self.edb = {p: {tuple(t) for t in ts} for p, ts in data["edb"].items()}
        self.rules_text = data["rules_text"]
        self.next_vid = data["next_vid"]
        if self.rules_text.strip():
            self.set_rules(self.rules_text)  # 重建规则与草稿状态
        for vd in data["versions"]:
            ver = {"id": vd["id"], "ts": vd["ts"], "note": vd.get("note", ""),
                   "status": vd["status"], "error": vd["error"],
                   "rules_text": vd["rules_text"],
                   "edb": {p: {tuple(t) for t in ts} for p, ts in vd["edb"].items()},
                   "state": None, "summary": None}
            if ver["status"] == "ok":
                try:
                    rules = engine.parse_program(ver["rules_text"])
                    engine.check_safety(rules)
                    st = engine.evaluate_full(ver["edb"], rules)
                    ver["state"] = st
                    ver["summary"] = {p: len(ts) for p, ts in sorted(st.relations().items())}
                except Exception as e:
                    ver["status"] = "failed"
                    ver["error"] = f"重放失败: {e}"
            self.versions.append(ver)
