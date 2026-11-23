# -*- coding: utf-8 -*-
"""
demo.py — 三个操作样例的命令行演示（不启动网页，直接驱动引擎与应用层）。

    python3 demo.py
"""

from app import App
from samples import SAMPLES, NEGATIVE_CYCLE_RULES, UNSAFE_RULES


def hr(title):
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)


def show_delta(delta):
    print(f"  增量变化：+{len(delta['added'])} / −{len(delta['removed'])}"
          f"（过度删除 {delta['stats'].get('over_deleted', 0)} 条，"
          f"整层重算 {delta['stats'].get('recomputed_strata', 0)} 层，"
          f"{delta['stats'].get('ms', '?')} ms）")
    for f in delta["removed"][:8]:
        print(f"    − {f}")
    for f in delta["added"][:8]:
        print(f"    + {f}")


def main():
    app = App()

    hr("样例①  循环图：正向递归按集合语义迭代到不动点，图有环也终止")
    app.load_sample("cyclic")
    st = app.state
    print("  输入：edge(a,b) edge(b,c) edge(c,a) edge(c,d)  ← a→b→c→a 是环")
    print(f"  规则：reach(X,Y) :- edge(X,Y).  /  reach(X,Y) :- reach(X,Z), edge(Z,Y).")
    print(f"  结果：reach 共 {len(st.tuples_of('reach'))} 条（有限！路径无限多也不会追加重复记录）")
    print(f"  求值：{st.stats['rounds']} 轮到不动点，{st.stats['ms']} ms")
    print("  撤销边 edge(c,a)（断环）：")
    show_delta(app.retract_fact("edge", ("c", "a")))
    cmp = app.compare()
    print(f"  增量 vs 整版重算：{'一致 ✔' if cmp['equal'] else '不一致 ✘'}")
    print("  加回 edge(c,a)：")
    show_delta(app.add_fact("edge", ("c", "a")))
    print(f"  增量 vs 整版重算：{'一致 ✔' if app.compare()['equal'] else '不一致 ✘'}")

    hr("样例②  两条独立证明：删除一条输入，另一条证明路径仍成立")
    app.load_sample("diamond")
    info = app.explain("reach", ("a", "c"))
    print(f"  reach(a,c) 的证明数：{len(info['derivations'])}")
    for i, d in enumerate(info["derivations"], 1):
        ants = " ∧ ".join(p["text"] for p in d["pos"])
        print(f"    证明{i} ⇐ {d['rule']}   前件: {ants}")
    print("  撤销 edge(a,b)：")
    show_delta(app.retract_fact("edge", ("a", "b")))
    info = app.explain("reach", ("a", "c"))
    print(f"  reach(a,c) 仍成立：{info['exists']}（剩余证明 {len(info['derivations'])} 条）")
    print("  再撤销 edge(a,d)：")
    show_delta(app.retract_fact("edge", ("a", "d")))
    info = app.explain("reach", ("a", "c"))
    print(f"  两条证明都失效后，reach(a,c) 成立？{info['exists']}")
    print(f"  增量 vs 整版重算：{'一致 ✔' if app.compare()['equal'] else '不一致 ✘'}")

    hr("样例③  继承标签 + 屏蔽规则（分层否定），规则随版本一起发布")
    app.load_sample("masking")
    print(f"  分层：{[(s['level'], s['preds']) for s in app.strata_info()]}")
    print(f"  visible（没有被屏蔽的结果）：{sorted(app.state.tuples_of('visible'))}")
    v1 = app.publish("v1：屏蔽规则只挡 blocked 的人")
    print(f"  发布版本 #{v1['id']}：{v1['status']}")
    print("  屏蔽 bob：")
    show_delta(app.add_fact("blocked", ("bob",)))
    print(f"  visible 现在：{sorted(app.state.tuples_of('visible'))}")
    app.retract_fact("blocked", ("bob",))

    s = SAMPLES["masking"]
    app.import_facts(s["facts_v2_extra"])
    app.set_rules(s["rules_v2"])
    v2 = app.publish("v2：屏蔽规则变化，vip 标签也被 muted")
    print(f"\n  修改屏蔽规则后发布版本 #{v2['id']}：{v2['status']}")
    r1 = app.version_results_json(v1["id"])
    r2 = app.version_results_json(v2["id"])
    print(f"  v1 visible：{sorted(map(tuple, r1['relations']['visible']))}")
    print(f"  v2 visible：{sorted(map(tuple, r2['relations']['visible']))}")
    print("  两个版本的结果都可查（输入+规则随版本冻结）")

    hr("附：非法规则被拒绝，失败发布保留上一版可查结果")
    r = app.set_rules(NEGATIVE_CYCLE_RULES)
    print(f"  负向环规则：{r['ok']=}")
    print(f"  错误：{r['error']}")
    print(f"  循环路径：{' → '.join(r['detail']['cycle'])}")
    v3 = app.publish("v3：负向环规则")
    print(f"  发布版本 #{v3['id']}：{v3['status']}（上一版结果不受影响）")
    r2_again = app.version_results_json(v2["id"])
    print(f"  版本 #{v2['id']} 仍可查：visible 共 {len(r2_again['relations']['visible'])} 条")
    r = app.set_rules(UNSAFE_RULES)
    print(f"  无正向约束规则被拒绝：{r['error'].splitlines()[0]}")


if __name__ == "__main__":
    main()
