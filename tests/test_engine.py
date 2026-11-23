# -*- coding: utf-8 -*-
"""引擎核心测试：解析、安全、分层、不动点终止、DRed 增量维护、溯源。"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine
from engine import (ParseError, SafetyError, StratificationError,
                    parse_program, parse_facts, check_safety, stratify,
                    evaluate_full, apply_changes, diff_states, explain)

REACH = """\
reach(X, Y) :- edge(X, Y).
reach(X, Y) :- reach(X, Z), edge(Z, Y).
"""


def edb_of(facts_text):
    edb = {}
    for pred, tup in parse_facts(facts_text):
        edb.setdefault(pred, set()).add(tup)
    return edb


def build(facts_text, rules_text):
    rules = parse_program(rules_text)
    check_safety(rules)
    return evaluate_full(edb_of(facts_text), rules)


class TestParser(unittest.TestCase):
    def test_parse_rule(self):
        rules = parse_program("reach(X, Y) :- edge(X, Y), not blocked(Y).")
        self.assertEqual(len(rules), 1)
        r = rules[0]
        self.assertEqual(r.head.pred, "reach")
        self.assertEqual(len(r.body), 2)
        self.assertTrue(r.body[1].negated)

    def test_constants(self):
        rules = parse_program("p(a, 1, 'x y').")
        self.assertEqual(rules[0].head.args, ("a", 1, "x y"))

    def test_reject_script(self):
        with self.assertRaises(ParseError):
            parse_program("p(X) :- import os; os.system('rm -rf /').")
        with self.assertRaises(ParseError):
            parse_program("p(X) :- X = 1 + 2.")
        with self.assertRaises(ParseError):
            parse_program("__import__('os').system('id')")

    def test_reject_function_symbols(self):
        with self.assertRaises(ParseError):
            parse_program("p(f(X)) :- q(X).")

    def test_comments(self):
        rules = parse_program("% 注释\np(a). % 行尾\n")
        self.assertEqual(len(rules), 1)


class TestSafety(unittest.TestCase):
    def test_unbound_head_var(self):
        rules = parse_program("p(X) :- q(Y).")
        with self.assertRaises(SafetyError):
            check_safety(rules)

    def test_unbound_negated_var(self):
        rules = parse_program("p(X) :- q(X), not r(Y).")
        with self.assertRaises(SafetyError):
            check_safety(rules)

    def test_negation_only_body_rejected(self):
        rules = parse_program("anything(X) :- not blocked(X).")
        with self.assertRaises(SafetyError):
            check_safety(rules)

    def test_safe_program_ok(self):
        rules = parse_program("p(X) :- q(X), not r(X).")
        check_safety(rules)  # 不抛异常

    def test_ground_fact_rule_ok(self):
        rules = parse_program("p(a).")
        check_safety(rules)


class TestStratification(unittest.TestCase):
    def test_negative_self_loop_rejected(self):
        rules = parse_program("p(X) :- q(X), not p(X).\nq(a).")
        with self.assertRaises(StratificationError) as cm:
            stratify(rules)
        self.assertIn("p", cm.exception.cycle)

    def test_negative_mutual_cycle_shows_path(self):
        rules = parse_program(
            "node(a).\np(X) :- node(X), not q(X).\nq(X) :- node(X), not p(X).")
        with self.assertRaises(StratificationError) as cm:
            stratify(rules)
        cycle = cm.exception.cycle
        # 循环路径首尾相同，且包含 p 和 q
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"p", "q"})
        self.assertTrue(cm.exception.cycle_rules)

    def test_stratified_negation_ok(self):
        rules = parse_program(
            "edge(a, b).\nreach(X, Y) :- edge(X, Y).\n"
            "ok(X) :- reach(_, X), not blocked(X).\nblocked(b).")
        strata, pred_stratum = stratify(rules)
        self.assertLess(pred_stratum["reach"], pred_stratum["ok"])

    def test_positive_recursion_same_stratum(self):
        rules = parse_program(REACH)
        strata, pred_stratum = stratify(rules)
        self.assertEqual(pred_stratum["reach"], 0)
        self.assertEqual(len(strata), 1)


class TestFixpoint(unittest.TestCase):
    def test_cyclic_graph_terminates(self):
        st = build("node(a). node(b). node(c). node(d).\n"
                   "edge(a,b). edge(b,c). edge(c,a). edge(c,d).", REACH)
        reach = st.tuples_of("reach")
        # 12 条：a/b/c 各能到 {a,b,c,d}，d 到不了任何点
        self.assertEqual(len(reach), 12)
        self.assertIn(("a", "a"), reach)  # 环上自可达
        self.assertNotIn(("d", "a"), reach)
        # 集合语义：无重复
        self.assertEqual(len(reach), len(set(reach)))

    def test_transitive_chain(self):
        st = build("edge(a,b). edge(b,c). edge(c,d).", REACH)
        reach = st.tuples_of("reach")
        self.assertEqual(reach, {("a","b"),("a","c"),("a","d"),
                                 ("b","c"),("b","d"),("c","d")})

    def test_inherited_tags(self):
        st = build(
            "follows(a,b). follows(b,c). badge(a,vip). badge(b,staff).",
            "reach(X,Y) :- follows(X,Y).\n"
            "reach(X,Y) :- reach(X,Z), follows(Z,Y).\n"
            "inherit(X,T) :- badge(X,T).\n"
            "inherit(Y,T) :- reach(X,Y), badge(X,T).\n")
        inh = st.tuples_of("inherit")
        self.assertIn(("c", "vip"), inh)    # 沿可达继承
        self.assertIn(("c", "staff"), inh)
        self.assertIn(("a", "vip"), inh)    # 本人标签
        self.assertNotIn(("a", "staff"), inh)

    def test_stratified_negation_filters(self):
        st = build(
            "person(a). person(b). person(c). blocked(c).",
            "ok(X) :- person(X), not blocked(X).\n")
        self.assertEqual(st.tuples_of("ok"), {("a",), ("b",)})

    def test_no_duplicate_derivation_explosion(self):
        # 环上推导依据也是有限集合
        st = build("edge(a,b). edge(b,a).", REACH)
        n_deriv = sum(len(v) for v in st.prov.values())
        self.assertLess(n_deriv, 20)


class TestProvenance(unittest.TestCase):
    def test_two_independent_proofs(self):
        st = build("edge(a,b). edge(b,c). edge(a,d). edge(d,c).", REACH)
        info = explain(st, "reach", ("a", "c"))
        self.assertTrue(info["exists"])
        self.assertEqual(len(info["derivations"]), 2)  # 两条独立证明
        ants = {tuple(p["pred"] for p in d["pos"]) for d in info["derivations"]}
        self.assertEqual(ants, {("reach", "edge"), ("reach", "edge")})

    def test_base_fact_explain(self):
        st = build("edge(a,b).", REACH)
        info = explain(st, "edge", ("a", "b"))
        self.assertTrue(info["base"])

    def test_every_derived_fact_has_derivation(self):
        st = build("edge(a,b). edge(b,c). edge(c,a). edge(c,d).", REACH)
        for pred in st.idb_preds():
            for tup in st.derived[pred]:
                self.assertTrue(st.prov.get((pred, tup)),
                                f"{pred}{tup} 缺少推导依据")


class TestIncremental(unittest.TestCase):
    DIAMOND = "edge(a,b). edge(b,c). edge(a,d). edge(d,c). edge(c,e)."

    def test_delete_one_of_two_proofs_keeps_fact(self):
        st = build(self.DIAMOND, REACH)
        delta = apply_changes(st, set(), {("edge", ("a", "b"))})
        self.assertIn(("a", "c"), st.tuples_of("reach"))  # 另一证明仍成立
        # reach(a,b) 的唯一证明（edge(a,b) 直达）失效，应当消失
        self.assertIn(("reach", ("a", "b")), delta["removed"])
        self.assertNotIn(("a", "b"), st.tuples_of("reach"))
        # 再删第二条路：reach(a,c) 才消失
        apply_changes(st, set(), {("edge", ("a", "d"))})
        self.assertNotIn(("a", "c"), st.tuples_of("reach"))
        self.assertNotIn(("a", "e"), st.tuples_of("reach"))

    def test_deep_strata_chain_deletion(self):
        """多层链删除传播（回归：跨层清理不能绕过各层自己的删除判定，
        否则顶层会残留陈旧事实）。"""
        rules_text = (REACH +
                      "t1(X) :- reach(a, X).\n"
                      "t2(X) :- t1(X).\n"
                      "t3(X) :- t2(X).\n")
        st = build("edge(a,b). edge(b,c). edge(c,d).", rules_text)
        self.assertEqual(st.tuples_of("t3"), {("b",), ("c",), ("d",)})
        apply_changes(st, set(), {("edge", ("b", "c"))})
        self.assertEqual(st.tuples_of("reach"), {("a", "b"), ("c", "d")})
        self.assertEqual(st.tuples_of("t1"), {("b",)})
        self.assertEqual(st.tuples_of("t2"), {("b",)})
        self.assertEqual(st.tuples_of("t3"), {("b",)})

    def test_edb_fact_on_idb_pred(self):
        """输入事实与派生谓词同名：撤销基事实后，派生证明仍让事实成立。"""
        st = build("edge(a,b). reach(x,y).", REACH)
        self.assertIn(("x", "y"), st.tuples_of("reach"))
        apply_changes(st, set(), {("reach", ("x", "y"))})
        self.assertNotIn(("x", "y"), st.tuples_of("reach"))
        # reach(a,b) 既是输入又可由 edge(a,b) 派生：撤销输入后仍成立
        st2 = build("edge(a,b). reach(a,b).", REACH)
        apply_changes(st2, set(), {("reach", ("a", "b"))})
        self.assertIn(("a", "b"), st2.tuples_of("reach"))

    def test_insert_incremental(self):
        st = build("edge(a,b). edge(c,d).", REACH)
        apply_changes(st, {("edge", ("b", "c"))}, set())
        self.assertIn(("a", "d"), st.tuples_of("reach"))

    def test_negation_stratum_recompute_on_block(self):
        st = build("person(a). person(b).",
                   "ok(X) :- person(X), not blocked(X).\n")
        self.assertEqual(st.tuples_of("ok"), {("a",), ("b",)})
        apply_changes(st, {("blocked", ("a",))}, set())
        self.assertEqual(st.tuples_of("ok"), {("b",)})
        apply_changes(st, set(), {("blocked", ("a",))})
        self.assertEqual(st.tuples_of("ok"), {("a",), ("b",)})

    def test_random_edits_match_full_recompute(self):
        """性质测试：随机增删后，增量维护结果必须等于整版重算。"""
        rng = random.Random(20261123)
        rules_text = (
            REACH +
            "inherit(Y, T) :- reach(X, Y), tag(X, T).\n"
            "inherit(X, T) :- tag(X, T).\n"
            "visible(X, T) :- inherit(X, T), not blocked(X).\n"
            "t1(X) :- visible(X, _).\n"
            "t2(X) :- t1(X).\n")
        rules = parse_program(rules_text)
        check_safety(rules)
        nodes = "abcdef"
        for trial in range(30):
            facts = set()
            for x in nodes:
                facts.add(("node", (x,)))
                if rng.random() < 0.3:
                    facts.add(("tag", (x, rng.choice(["vip", "staff"]))))
                if rng.random() < 0.2:
                    facts.add(("blocked", (x,)))
            for x in nodes:
                for y in nodes:
                    if rng.random() < 0.2:
                        facts.add(("edge", (x, y)))
            edb = {}
            for p, t in facts:
                edb.setdefault(p, set()).add(t)
            st = evaluate_full(edb, rules)
            all_facts = sorted(facts)
            for step in range(20):
                if rng.random() < 0.5 and all_facts:
                    f = all_facts[rng.randrange(len(all_facts))]
                    apply_changes(st, set(), {f})
                    edb.setdefault(f[0], set()).discard(f[1])
                else:
                    p = rng.choice(["edge", "tag", "blocked"])
                    if p == "edge":
                        f = ("edge", (rng.choice(nodes), rng.choice(nodes)))
                    elif p == "tag":
                        f = ("tag", (rng.choice(nodes), rng.choice(["vip", "staff"])))
                    else:
                        f = ("blocked", (rng.choice(nodes),))
                    apply_changes(st, {f}, set())
                    edb.setdefault(f[0], set()).add(f[1])
                full = evaluate_full(edb, rules)
                only_inc, only_full = diff_states(st, full)
                self.assertFalse(only_inc,
                                 f"trial{trial} step{step} 增量多出: {only_inc}")
                self.assertFalse(only_full,
                                 f"trial{trial} step{step} 增量缺失: {only_full}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
