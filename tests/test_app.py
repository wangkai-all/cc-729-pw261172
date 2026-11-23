# -*- coding: utf-8 -*-
"""应用层测试：规则拒绝、发布版本、失败保留上一版、样例流程。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import App
from samples import SAMPLES, NEGATIVE_CYCLE_RULES, UNSAFE_RULES


class TestRules(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_negative_cycle_rejected_with_path(self):
        r = self.app.set_rules(NEGATIVE_CYCLE_RULES)
        self.assertFalse(r["ok"])
        self.assertIn("负向环", r["error"])
        cycle = r["detail"]["cycle"]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"p", "q"})
        # 拒绝后旧规则仍然有效可查
        self.assertIsNotNone(self.app.rules_error)

    def test_unsafe_rule_rejected(self):
        r = self.app.set_rules(UNSAFE_RULES)
        self.assertFalse(r["ok"])
        self.assertIn("正向约束", r["error"])

    def test_invalid_rules_keep_previous_state(self):
        self.app.load_sample("cyclic")
        before = self.app.state.relations()
        r = self.app.set_rules(NEGATIVE_CYCLE_RULES)
        self.assertFalse(r["ok"])
        # 结果仍对应上一份有效规则
        self.assertEqual(self.app.state.relations(), before)


class TestPublish(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_publish_and_versions(self):
        self.app.load_sample("masking")
        v1 = self.app.publish("第一版")
        self.assertEqual(v1["status"], "ok")
        # 修改屏蔽规则 → 第二版
        s = SAMPLES["masking"]
        self.app.import_facts(s["facts_v2_extra"])
        self.app.set_rules(s["rules_v2"])
        v2 = self.app.publish("屏蔽规则变化：vip 也屏蔽")
        self.assertEqual(v2["status"], "ok")
        # 两版结果不同且都可查
        r1 = self.app.version_results_json(v1["id"])
        r2 = self.app.version_results_json(v2["id"])
        vis1 = {tuple(t) for t in r1["relations"]["visible"]}
        vis2 = {tuple(t) for t in r2["relations"]["visible"]}
        self.assertIn(("alice", "vip"), vis1)
        self.assertNotIn(("alice", "vip"), vis2)  # vip 被 muted
        self.assertNotEqual(vis1, vis2)

    def test_failed_publish_keeps_previous_queryable(self):
        self.app.load_sample("cyclic")
        v1 = self.app.publish("好版本")
        self.assertEqual(v1["status"], "ok")
        # 录入负向环规则（被拒绝）后发布 → 失败版本
        self.app.set_rules(NEGATIVE_CYCLE_RULES)
        v2 = self.app.publish("坏版本")
        self.assertEqual(v2["status"], "failed")
        self.assertIn("负向环", v2["error"])
        # 上一版结果仍然可查
        r1 = self.app.version_results_json(v1["id"])
        self.assertEqual(len(r1["relations"]["reach"]), 12)


class TestSamplesFlow(unittest.TestCase):
    def test_cyclic_terminates(self):
        app = App()
        app.load_sample("cyclic")
        self.assertEqual(len(app.state.tuples_of("reach")), 12)

    def test_diamond_two_proofs(self):
        app = App()
        app.load_sample("diamond")
        info = app.explain("reach", ("a", "c"))
        self.assertEqual(len(info["derivations"]), 2)
        # 撤销一条边：还有一条证明
        app.retract_fact("edge", ("a", "b"))
        info = app.explain("reach", ("a", "c"))
        self.assertTrue(info["exists"])
        self.assertEqual(len(info["derivations"]), 1)
        # 再撤销一条：消失
        app.retract_fact("edge", ("a", "d"))
        info = app.explain("reach", ("a", "c"))
        self.assertFalse(info["exists"])
        # 增量结果与整版重算一致
        cmp = app.compare()
        self.assertTrue(cmp["equal"])

    def test_masking_block_unblock(self):
        app = App()
        app.load_sample("masking")
        self.assertIn(("bob", "staff"), app.state.tuples_of("visible"))
        app.add_fact("blocked", ("bob",))
        self.assertNotIn(("bob", "staff"), app.state.tuples_of("visible"))
        app.retract_fact("blocked", ("bob",))
        self.assertIn(("bob", "staff"), app.state.tuples_of("visible"))
        self.assertTrue(app.compare()["equal"])

    def test_compare_after_random_edits(self):
        app = App()
        app.load_sample("diamond")
        app.retract_fact("edge", ("c", "e"))
        app.add_fact("edge", ("e", "a"))  # 造环
        app.add_fact("edge", ("e", "f"))
        cmp = app.compare()
        self.assertTrue(cmp["equal"], cmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
