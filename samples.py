# -*- coding: utf-8 -*-
"""
samples.py — 三个内置操作样例（网页一键载入，demo.py 也复用）。

1. cyclic   循环图：正向递归在环上按集合语义收敛到不动点。
2. diamond  两条独立证明：reach(a,c) 有两条互不依赖的证明路径，
            删除一条边后结果仍成立；再删一条才消失。
3. masking  屏蔽规则：派生可达关系、沿可达关系继承标签、
            再用分层否定过滤被屏蔽的结果；并演示修改屏蔽规则后发布新版本。
"""

COMMON_RULES = """\
% 可达关系：正向递归，集合语义，图有环也会终止
reach(X, Y) :- edge(X, Y).
reach(X, Y) :- reach(X, Z), edge(Z, Y).
"""

SAMPLES = {
    "cyclic": {
        "title": "循环图：递归在环上终止",
        "description": "a→b→c→a 构成环，c→d 引出。路径无限多，"
                       "但集合语义下 reach 只有 12 条记录，迭代到不动点即停。",
        "facts": """\
node(a). node(b). node(c). node(d).
edge(a, b). edge(b, c). edge(c, a). edge(c, d).
""",
        "rules": COMMON_RULES,
        "demo_steps": [
            {"op": "retract", "fact": ["edge", ["c", "a"]],
             "note": "撤销边 c→a（断环），增量维护（DRed）只删失去全部证明的派生事实"},
            {"op": "add", "fact": ["edge", ["c", "a"]],
             "note": "重新加回边 c→a，增量插入恢复结果"},
        ],
    },
    "diamond": {
        "title": "两条独立证明：删一条边结果仍在",
        "description": "reach(a,c) 有 a→b→c 与 a→d→c 两条独立证明。"
                       "删除 edge(a,b) 后 reach(a,c) 依然成立（还剩一条证明），"
                       "再删 edge(a,d) 才消失。",
        "facts": """\
node(a). node(b). node(c). node(d). node(e).
edge(a, b). edge(b, c). edge(a, d). edge(d, c). edge(c, e).
""",
        "rules": COMMON_RULES,
        "demo_steps": [
            {"op": "retract", "fact": ["edge", ["a", "b"]],
             "note": "撤销 edge(a,b)：reach(a,c) 少了一条证明，但另一条仍成立"},
            {"op": "retract", "fact": ["edge", ["a", "d"]],
             "note": "再撤销 edge(a,d)：两条证明都失效，reach(a,c) 才消失"},
            {"op": "add", "fact": ["edge", ["a", "b"]],
             "note": "加回 edge(a,b)"},
        ],
    },
    "masking": {
        "title": "继承标签与屏蔽规则（分层否定）",
        "description": "badge 沿 follows 可达关系向下继承（inherit），"
                       "visible 用否定过滤掉被屏蔽的人与被打入冷宫的标签。"
                       "修改屏蔽规则后发布新版本，旧版本结果仍可查。",
        "facts": """\
person(alice). person(bob). person(carol). person(dave).
follows(alice, bob). follows(bob, carol). follows(alice, dave).
badge(alice, vip). badge(bob, staff).
blocked(carol).
""",
        "rules": """\
% 可达关系
reach(X, Y) :- follows(X, Y).
reach(X, Y) :- reach(X, Z), follows(Z, Y).
% 继承标签：本人有的标签算继承，可达上游的标签也继承
inherit(X, T) :- badge(X, T).
inherit(Y, T) :- reach(X, Y), badge(X, T).
% 没有被屏蔽的结果：继承到的标签，且本人未被屏蔽
visible(X, T) :- inherit(X, T), not blocked(X).
""",
        "demo_steps": [
            {"op": "add", "fact": ["blocked", ["bob"]],
             "note": "屏蔽 bob：visible(bob,staff) 消失（否定输入变化 → 该层整层重算）"},
            {"op": "retract", "fact": ["blocked", ["bob"]],
             "note": "取消屏蔽 bob：visible(bob,staff) 恢复"},
        ],
        # 屏蔽规则变化：第二版把 vip 标签也屏蔽掉
        "rules_v2": """\
reach(X, Y) :- follows(X, Y).
reach(X, Y) :- reach(X, Z), follows(Z, Y).
inherit(X, T) :- badge(X, T).
inherit(Y, T) :- reach(X, Y), badge(X, T).
visible(X, T) :- inherit(X, T), not blocked(X), not muted(T).
""",
        "facts_v2_extra": "muted(vip).\n",
    },
}

# 负向环示例（应被拒绝并显示循环路径）
NEGATIVE_CYCLE_RULES = """\
node(a). node(b).
p(X) :- node(X), not q(X).
q(X) :- node(X), not p(X).
"""

# 不安全规则示例（变量没有正向约束）
UNSAFE_RULES = "anything(X) :- not blocked(X).\n"
