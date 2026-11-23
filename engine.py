"""
engine.py — 一个小型、安全的 Datalog 子集求值引擎。

特性：
- 明确的 Datalog 子集语法（无任意脚本、无函数符号、无算术）。
- 安全性检查：所有变量必须有正向约束（range-restricted），取值域有限。
- 分层否定：否定文字只能引用更低层（已完成）的关系；含负向环的规则集被拒绝，
  并给出循环路径。
- 正向递归按集合语义做朴素迭代到不动点：事实与推导依据都是集合，
  图有环也会终止，不会无限追加相同记录。
- 溯源（provenance）：每条派生事实记录全部推导（规则 + 前件），
  用于"展开支持关系"。
- 增量维护：插入用半朴素/朴素继续迭代；删除用 DRed（过度删除 + 重推），
  否定所依赖的关系发生变化时，仅整层重算受影响层。删除一条输入时，
  只有所有证明路径都失效，结果才会消失。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# 词项与语法树
# ---------------------------------------------------------------------------

class Var(str):
    """变量。名字以大写字母或下划线开头的标识符。"""
    __slots__ = ()


def is_var(t) -> bool:
    return isinstance(t, Var)


def is_anon(v) -> bool:
    """匿名变量 `_`（每次出现都是全新的），不参与安全约束、不绑定。"""
    return is_var(v) and v.startswith("_anon_")


@dataclass(frozen=True)
class Literal:
    pred: str
    args: tuple
    negated: bool = False

    def vars(self):
        return [a for a in self.args if is_var(a) and not is_anon(a)]


@dataclass
class Rule:
    head: Literal
    body: tuple  # tuple[Literal]
    rid: int = -1

    def text(self) -> str:
        s = lit_text(self.head)
        if self.body:
            s += " :- " + ", ".join(lit_text(l) for l in self.body)
        return s + "."


@dataclass(frozen=True)
class Derivation:
    """一条推导依据：由规则 rid 推出，正向依赖 pos 事实，且 neg 事实当时不存在。"""
    rid: int
    pos: tuple  # tuple[(pred, tuple), ...]
    neg: tuple  # tuple[(pred, tuple), ...]


def fmt_arg(a) -> str:
    if is_var(a):
        return str(a)
    if isinstance(a, int):
        return str(a)
    s = str(a)
    if re.fullmatch(r"[a-z][A-Za-z0-9_]*", s):
        return s
    return "'" + s.replace("'", "\\'") + "'"


def lit_text(l: Literal) -> str:
    s = l.pred
    if l.args:
        s += "(" + ", ".join(fmt_arg(a) for a in l.args) + ")"
    else:
        s += "()"
    return ("not " + s) if l.negated else s


def fact_text(key) -> str:
    pred, tup = key
    return lit_text(Literal(pred, tuple(tup)))


# ---------------------------------------------------------------------------
# 解析器（明确的 Datalog 子集，拒绝一切其它语法）
# ---------------------------------------------------------------------------

class ParseError(Exception):
    pass


class SafetyError(Exception):
    pass


class StratificationError(Exception):
    def __init__(self, message, cycle=None, cycle_rules=None):
        super().__init__(message)
        self.cycle = cycle or []            # ["p", "q", "p"]
        self.cycle_rules = cycle_rules or []  # 构成循环的规则文本


_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<comment>\%[^\n]*)
      | (?P<arrow>:-)
      | (?P<number>-?\d+)
      | (?P<dstring>"(?:[^"\\]|\\.)*")
      | (?P<sstring>'(?:[^'\\]|\\.)*')
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<punct>[(),.])
    )
""", re.VERBOSE)


def _tokenize(text: str):
    tokens = []
    pos = 0
    n = len(text)
    while pos < n:
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise ParseError(
                f"无法识别的语法（位置 {pos}）：{text[pos:pos+20]!r}。"
                "只允许 Datalog 子集：事实与规则，不支持脚本/算术/函数符号。")
        pos = m.end()
        if m.lastgroup == "comment":
            continue
        tokens.append((m.lastgroup, m.group()))
    return tokens


class _Parser:
    def __init__(self, text):
        self.tokens = _tokenize(text)
        self.i = 0
        self.anon_counter = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def expect(self, value):
        kind, val = self.next()
        if val != value:
            raise ParseError(f"期望 {value!r}，实际遇到 {val!r}")
        return val

    def parse_program(self):
        rules = []
        while self.i < len(self.tokens):
            rules.append(self.parse_statement())
        return rules

    def parse_statement(self) -> Rule:
        head = self.parse_atom()
        body = []
        kind, val = self.peek()
        if val == ":-":
            self.next()
            while True:
                negated = False
                kind, val = self.peek()
                if kind == "ident" and val == "not":
                    self.next()
                    negated = True
                lit = self.parse_atom()
                body.append(Literal(lit.pred, lit.args, negated))
                kind, val = self.peek()
                if val == ",":
                    self.next()
                    continue
                break
        self.expect(".")
        return Rule(head=head, body=tuple(body))

    def parse_atom(self) -> Literal:
        kind, val = self.next()
        if kind != "ident" or val == "not":
            raise ParseError(f"期望谓词名，实际遇到 {val!r}")
        pred = val
        args = []
        kind, val = self.peek()
        if val == "(":
            self.next()
            kind, val = self.peek()
            if val != ")":
                while True:
                    args.append(self.parse_term())
                    kind, val = self.next()
                    if val == ",":
                        continue
                    if val == ")":
                        break
                    raise ParseError(f"期望 ',' 或 ')'，实际遇到 {val!r}")
            else:
                self.next()
        return Literal(pred, tuple(args))

    def parse_term(self):
        kind, val = self.next()
        if kind == "number":
            return int(val)
        if kind in ("sstring", "dstring"):
            return val[1:-1]
        if kind == "ident":
            if val == "_":
                self.anon_counter += 1
                return Var(f"_anon_{self.anon_counter}")
            if val[0].isupper() or val[0] == "_":
                return Var(val)
            return val  # 小写标识符是常量
        raise ParseError(f"非法的词项：{val!r}")


def parse_program(text: str):
    """解析规则文本，返回 Rule 列表（rid 从 0 编号）。"""
    rules = _Parser(text).parse_program()
    for i, r in enumerate(rules):
        r.rid = i
    return rules


def parse_facts(text: str):
    """解析导入数据：只允许基事实（无规则体、无变量、无否定）。"""
    rules = parse_program(text)
    facts = set()
    for r in rules:
        if r.body:
            raise ParseError(f"导入数据只能是事实，不能含规则体：{r.text()}")
        if any(is_var(a) for a in r.head.args):
            raise ParseError(f"导入数据不能含变量：{r.text()}")
        facts.add((r.head.pred, tuple(r.head.args)))
    return facts


# ---------------------------------------------------------------------------
# 安全性检查：所有变量必须有正向约束（有限取值域）
# ---------------------------------------------------------------------------

def check_safety(rules):
    errors = []
    for r in rules:
        pos_vars = set()
        for lit in r.body:
            if not lit.negated:
                pos_vars.update(lit.vars())
        for v in r.head.vars():
            if v not in pos_vars:
                errors.append(
                    f"规则 {r.text()} 的头部变量 {v} 没有正向约束："
                    "它必须出现在某个非否定的规则体文字中，否则取值域无限。")
        for lit in r.body:
            if lit.negated:
                for v in lit.vars():
                    if v not in pos_vars:
                        errors.append(
                            f"规则 {r.text()} 的否定文字 {lit_text(lit)} 中的变量 {v} "
                            "没有正向约束。")
    if errors:
        raise SafetyError("\n".join(errors))


# ---------------------------------------------------------------------------
# 分层：否定只能引用更低层的关系；负向环被拒绝并给出循环路径
# ---------------------------------------------------------------------------

def stratify(rules):
    """返回 (strata, pred_stratum)。strata[i] 是第 i 层的规则下标列表，
    按求值顺序排列（被依赖的层在前）。负向环抛 StratificationError。"""
    idb = {r.head.pred for r in rules}
    # 依赖边：body_pred -> head_pred（仅 IDB 谓词之间）
    edges = []  # (src, dst, negated, rid)
    for r in rules:
        for lit in r.body:
            if lit.pred in idb:
                edges.append((lit.pred, r.head.pred, lit.negated, r.rid))

    # Tarjan SCC
    index_of, lowlink, on_stack = {}, {}, set()
    stack, sccs = [], []
    counter = [0]

    adj = {}
    for s, d, _, _ in edges:
        adj.setdefault(s, []).append(d)
    for p in idb:
        adj.setdefault(p, [])

    import sys
    sys.setrecursionlimit(10000)

    def strongconnect(v):
        index_of[v] = lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, []):
            if w not in index_of:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index_of[w])
        if lowlink[v] == index_of[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for p in list(idb):
        if p not in index_of:
            strongconnect(p)

    scc_of = {}
    for i, scc in enumerate(sccs):
        for p in scc:
            scc_of[p] = i

    # 检查 SCC 内部的否定边 → 负向环
    for s, d, neg, rid in edges:
        if neg and scc_of[s] == scc_of[d]:
            cycle = _find_cycle(adj, d, s)  # d ⇝ s，再加上否定边 s->d
            cyc_rules = sorted({rid} | {r for a, b, n, r in edges
                                        if scc_of[a] == scc_of[d] == scc_of[b]})
            raise StratificationError(
                "规则包含负向环（否定出现在递归依赖中），无法分层："
                + " → ".join(cycle + [cycle[0]]),
                cycle=cycle + [cycle[0]],
                cycle_rules=[rules[r].text() for r in cyc_rules])

    # 凝聚图拓扑排序（Kahn），被依赖者在前
    indeg = {i: 0 for i in range(len(sccs))}
    cadj = {i: set() for i in range(len(sccs))}
    for s, d, _, _ in edges:
        a, b = scc_of[s], scc_of[d]
        if a != b and b not in cadj[a]:
            cadj[a].add(b)
            indeg[b] += 1
    queue = [i for i in range(len(sccs)) if indeg[i] == 0]
    order = []
    while queue:
        x = queue.pop()
        order.append(x)
        for y in cadj[x]:
            indeg[y] -= 1
            if indeg[y] == 0:
                queue.append(y)

    strata = []
    pred_stratum = {}
    for level, scc_idx in enumerate(order):
        preds = set(sccs[scc_idx])
        rids = [r.rid for r in rules if r.head.pred in preds]
        strata.append(sorted(rids))
        for p in preds:
            pred_stratum[p] = level
    return strata, pred_stratum


def _find_cycle(adj, start, target):
    """在依赖图中找 start ⇝ target 的一条路径（用于展示循环）。"""
    path = [start]
    visited = {start}

    def dfs(u):
        if u == target:
            return True
        for w in adj.get(u, []):
            if w not in visited:
                visited.add(w)
                path.append(w)
                if dfs(w):
                    return True
                path.pop()
        return False

    dfs(start)
    return path


# ---------------------------------------------------------------------------
# 求值：集合语义的朴素不动点迭代（每层内），层间按拓扑序
# ---------------------------------------------------------------------------

class State:
    """一次求值的完整结果：输入、派生事实、溯源、分层信息。"""

    def __init__(self, edb, rules, strata, pred_stratum):
        self.edb = {p: set(ts) for p, ts in edb.items()}       # pred -> {tuple}
        self.rules = rules
        self.strata = strata
        self.pred_stratum = pred_stratum                        # IDB pred -> 层号
        self.derived = {}                                       # pred -> {tuple}
        self.prov = {}                                          # (pred, tuple) -> {Derivation}
        self.stats = {"rounds": 0, "derivations": 0, "ms": 0.0}

    # -- 关系访问 ----------------------------------------------------------
    def relations(self):
        rels = {p: set(ts) for p, ts in self.edb.items()}
        for p, ts in self.derived.items():
            rels.setdefault(p, set()).update(ts)
        return rels

    def tuples_of(self, pred):
        out = set(self.edb.get(pred, set()))
        out.update(self.derived.get(pred, set()))
        return out

    def idb_preds(self):
        return sorted({self.rules[r].head.pred for s in self.strata for r in s})

    def clone(self):
        c = State(self.edb, self.rules, self.strata, self.pred_stratum)
        c.derived = {p: set(ts) for p, ts in self.derived.items()}
        c.prov = {k: set(ds) for k, ds in self.prov.items()}
        c.stats = dict(self.stats)
        return c


def _plan_body(body):
    """对规则体文字排序：优先绑定变量多的正向文字；
    否定文字只有在全部变量被绑定后才可执行（安全性保证总能排出来）。"""
    bound = set()
    remaining = list(body)
    order = []
    while remaining:
        best, best_score = None, -1
        for lit in remaining:
            if lit.negated:
                ready = all((not is_var(a)) or is_anon(a) or a in bound for a in lit.args)
                if ready and best is None:
                    best = lit  # 否定文字作为兜底
            else:
                score = sum(1 for a in lit.args
                            if not is_var(a) or (a in bound and not is_anon(a)))
                if score > best_score:
                    best, best_score = lit, score
        if best is None:
            raise SafetyError("否定文字的变量没有被正向文字绑定（安全检查遗漏）")
        order.append(best)
        remaining.remove(best)
        if not best.negated:
            bound.update(best.vars())
    return order


def _join(rule, relations):
    """对一条规则求所有满足替换。产出 (头部元组, Derivation)。"""
    order = _plan_body(rule.body)
    results = []

    def rec(i, subst, pos, neg):
        if i == len(order):
            head = []
            for a in rule.head.args:
                if is_var(a):
                    if is_anon(a):
                        raise SafetyError(f"规则头部不允许匿名变量：{rule.text()}")
                    head.append(subst[a])
                else:
                    head.append(a)
            results.append((tuple(head), Derivation(rule.rid, tuple(pos), tuple(neg))))
            return
        lit = order[i]
        if lit.negated:
            key = []
            for a in lit.args:
                if is_var(a):
                    if is_anon(a):
                        # 否定中的匿名变量：存在任意取值即失败，需要特判
                        key.append(None)
                    else:
                        key.append(subst[a])
                else:
                    key.append(a)
            if any(k is None for k in key):
                # not p(_, X)：只要存在任意匹配元组即失败
                tup = tuple(key)
                hit = any(_tuple_matches(tup, t) for t in relations.get(lit.pred, ()))
                if not hit:
                    rec(i + 1, subst, pos, neg + [(lit.pred, tuple(
                        "_anon_" if k is None else k for k in key))])
            else:
                tup = tuple(key)
                if tup not in relations.get(lit.pred, set()):
                    rec(i + 1, subst, pos, neg + [(lit.pred, tup)])
            return
        for tup in relations.get(lit.pred, ()):
            s2 = _match(lit.args, tup, subst)
            if s2 is not None:
                rec(i + 1, s2, pos + [(lit.pred, tup)], neg)

    rec(0, {}, [], [])
    return results


def _tuple_matches(pattern, tup):
    return all(p is None or p == t for p, t in zip(pattern, tup))


def _match(args, tup, subst):
    if len(args) != len(tup):
        return None
    s2 = dict(subst)
    for a, v in zip(args, tup):
        if is_var(a):
            if is_anon(a):
                continue
            if a in s2:
                if s2[a] != v:
                    return None
            else:
                s2[a] = v
        else:
            if a != v:
                return None
    return s2


def _run_stratum(state, rids, relations):
    """对一层规则做朴素不动点迭代（集合语义：重复事实/推导不会重复加入）。
    返回 (新增事实集合, 迭代轮数)。"""
    new_facts = set()
    rounds = 0
    changed = True
    while changed:
        changed = False
        rounds += 1
        for rid in rids:
            rule = state.rules[rid]
            for head_tup, deriv in _join(rule, relations):
                key = (rule.head.pred, head_tup)
                if head_tup not in state.derived.setdefault(rule.head.pred, set()):
                    state.derived[rule.head.pred].add(head_tup)
                    changed = True
                    new_facts.add(key)
                bucket = state.prov.setdefault(key, set())
                if deriv not in bucket:
                    bucket.add(deriv)
                    changed = True
        relations = state.relations()
    return new_facts, rounds


def evaluate_full(edb, rules):
    """从零整版求值。edb: {pred: {tuple}}；rules: 已通过安全与分层检查。"""
    t0 = time.perf_counter()
    strata, pred_stratum = stratify(rules)
    state = State(edb, rules, strata, pred_stratum)
    total_rounds = 0
    for rids in strata:
        _, rounds = _run_stratum(state, rids, state.relations())
        total_rounds += rounds
    state.stats = {
        "rounds": total_rounds,
        "derivations": sum(len(v) for v in state.prov.values()),
        "ms": round((time.perf_counter() - t0) * 1000, 3),
    }
    return state


# ---------------------------------------------------------------------------
# 增量维护：插入 = 继续迭代；删除 = DRed（过度删除 + 重推）；
# 否定输入变化的层整层重算。
# ---------------------------------------------------------------------------

def apply_changes(state, added, removed):
    """在 state 上就地应用 EDB 增删。added/removed: {(pred, tuple)}。
    返回 delta: {"added": {key...}, "removed": {key...}, "stats": {...}}，
    覆盖所有关系（含派生关系）的变化。"""
    t0 = time.perf_counter()
    before = state.relations()

    # 1. 应用 EDB 变化
    for pred, tup in added:
        state.edb.setdefault(pred, set()).add(tup)
    for pred, tup in removed:
        if pred in state.edb:
            state.edb[pred].discard(tup)

    # 2. 已确认消失的键：纯基事实（没有任何推导）被删即消失；
    #    同时有推导的键交给各层的不动点去判定。
    gone = set()
    for key in removed:
        if not state.prov.get(key):
            gone.add(key)

    # 3. 逐层处理
    changed_preds = {p for p, _ in added} | {p for p, _ in removed}
    stats = {"over_deleted": 0, "rederived": 0, "recomputed_strata": 0}
    for level, rids in enumerate(state.strata):
        preds = {state.rules[r].head.pred for r in rids}
        neg_preds = {lit.pred for r in rids for lit in state.rules[r].body if lit.negated}
        if neg_preds & changed_preds:
            # 否定所依赖的关系变了：否定是非单调的，整层重算（有依据且可解释）
            _recompute_stratum(state, rids, preds)
            stats["recomputed_strata"] += 1
        else:
            # DRed：过度删除 + 重推
            d = _over_delete(state, preds, gone)
            # 无论是否有事实被删除，都必须清理本层引用了已消失事实的推导依据，
            # 否则陈旧推导会在后续更新中被误认为有效支持。
            # 只清理本层：更高层的失效推导会在处理该层时，基于传播而来的
            # gone 集合被正确地判定与清理。
            _purge(state, d, gone, preds)
            stats["over_deleted"] += len(d)
            new_facts, _ = _run_stratum(state, rids, state.relations())
            stats["rederived"] += len(new_facts & d) if d else 0
        # 汇总本层变化，传播给更高层
        for p in preds:
            now = state.tuples_of(p)
            was = before.get(p, set())
            if now != was:
                changed_preds.add(p)
                gone |= {(p, t) for t in was - now}

    after = state.relations()
    all_preds = set(before) | set(after)
    delta_added, delta_removed = set(), set()
    for p in all_preds:
        for t in after.get(p, set()) - before.get(p, set()):
            delta_added.add((p, t))
        for t in before.get(p, set()) - after.get(p, set()):
            delta_removed.add((p, t))
    stats["ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return {"added": delta_added, "removed": delta_removed, "stats": stats}


def _over_delete(state, preds, gone):
    """删除阶段（DRed 的有根支持变体）：

    1. 受影响集：在溯源图中传递依赖于 gone 的本层派生事实（局部性）。
    2. 有根支持：受影响集内，拥有"有限证明树"的事实——即存在一条推导，
       其全部正向前件要么未受影响，要么本身已有根支持（最小不动点，
       循环互证没有 base case，不会被误判为成立）。
    3. 被删除 = 受影响 − 有根支持。

    依据：删除只会缩小关系，旧溯源已记录全部可能推导；受影响集内
    "有根支持 ⇔ 仍属于新数据下的最小不动点"，因此删除是精确的，
    只有所有证明路径都失效的事实才会消失。"""
    affected = set()
    changed = True
    while changed:
        changed = False
        for p in preds:
            for tup in state.derived.get(p, ()):
                key = (p, tup)
                if key in affected:
                    continue
                for d in state.prov.get(key, ()):
                    if any(ant in gone or ant in affected for ant in d.pos):
                        affected.add(key)
                        changed = True
                        break
    supported = set()
    changed = True
    while changed:
        changed = False
        for key in affected - supported:
            for d in state.prov.get(key, ()):
                ok = True
                for ant in d.pos:
                    if ant in gone:
                        ok = False
                        break
                    if ant in affected and ant not in supported:
                        ok = False
                        break
                if ok:
                    supported.add(key)
                    changed = True
                    break
    return affected - supported


def _purge(state, deleted, gone, preds):
    """移除本层被过度删除的事实，并清理本层引用了消失事实的推导依据。
    只动 preds 指定的当前层，避免跨层清理绕过更高层自己的删除判定。"""
    invalid = deleted | gone
    for key in deleted:
        p, tup = key
        if p in state.derived:
            state.derived[p].discard(tup)
        state.prov.pop(key, None)
    for key in list(state.prov):
        if key[0] not in preds:
            continue
        ds = state.prov[key]
        ds = {d for d in ds if not any(ant in invalid for ant in d.pos)}
        if ds:
            state.prov[key] = ds
        else:
            # 防御性处理：有根支持判定保证本层保留的事实至少还有一条有效
            # 推导，正常不会走到这里；若走到，移除该派生事实。
            p, tup = key
            if p in state.derived:
                state.derived[p].discard(tup)
            state.prov.pop(key, None)


def _recompute_stratum(state, rids, preds):
    """整层重算（否定输入变化时）：清空本层派生事实与溯源后重新迭代到不动点。"""
    for p in preds:
        state.derived.pop(p, None)
    for key in list(state.prov):
        if key[0] in preds:
            state.prov.pop(key, None)
    _run_stratum(state, rids, state.relations())


# ---------------------------------------------------------------------------
# 溯源查询
# ---------------------------------------------------------------------------

def explain(state, pred, tup):
    """返回一条事实的直接支持：基事实 / 每条推导的规则与前件。"""
    key = (pred, tuple(tup))
    exists = tup in state.tuples_of(pred)
    out = {"fact": fact_text(key), "exists": exists, "base": False, "derivations": []}
    if not exists:
        return out
    if tup in state.edb.get(pred, set()):
        out["base"] = True
    for d in sorted(state.prov.get(key, ()), key=lambda d: d.rid):
        out["derivations"].append({
            "rule": state.rules[d.rid].text(),
            "rid": d.rid,
            "pos": [{"pred": p, "tuple": list(t), "text": fact_text((p, t))}
                    for p, t in d.pos],
            "neg": [{"pred": p, "tuple": [a for a in t], "text": "not " + fact_text((p, t))}
                    for p, t in d.neg],
        })
    return out


def diff_states(a: State, b: State):
    """比较两个状态的关系内容。"""
    ra, rb = a.relations(), b.relations()
    only_a, only_b = set(), set()
    for p in set(ra) | set(rb):
        for t in ra.get(p, set()) - rb.get(p, set()):
            only_a.add((p, t))
        for t in rb.get(p, set()) - ra.get(p, set()):
            only_b.add((p, t))
    return only_a, only_b
