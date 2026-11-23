/* app.js — Datalog 数据加工仓前端 */
"use strict";

let STATE = null;      // /api/state 快照
let SAMPLES = {};      // /api/samples
let formRules = [];    // 结构化表单中的规则

// ---------------------------------------------------------------- 工具
async function api(path, method = "GET", body = null) {
  const opt = { method, headers: {} };
  if (body !== null) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function factText(pred, tuple) {
  const fmt = (a) => {
    if (typeof a === "number") return String(a);
    return /^[a-z][A-Za-z0-9_]*$/.test(a) ? a : `'${a}'`;
  };
  return `${pred}(${tuple.map(fmt).join(", ")})`;
}

function parseArg(s) {
  s = s.trim();
  if (/^-?\d+$/.test(s)) return Number(s);
  const m = s.match(/^'(.*)'$/) || s.match(/^"(.*)"$/);
  if (m) return m[1];
  return s;
}

function banner(kind, msg, detail = null, timeout = 6000) {
  const box = document.getElementById("banner");
  const item = el("div", { class: `banner-item ${kind}`, text: msg });
  if (detail) item.appendChild(el("div", { class: "detail", text: detail }));
  box.appendChild(item);
  setTimeout(() => item.remove(), timeout);
}

function deltaBanner(delta, label = "本次变更") {
  const adds = delta.added || [], dels = delta.removed || [];
  const st = delta.stats || {};
  const parts = [];
  if (st.over_deleted !== undefined) {
    parts.push(`过度删除 ${st.over_deleted} 条`);
    if (st.rederived) parts.push(`重推回 ${st.rederived} 条`);
    if (st.recomputed_strata) parts.push(`整层重算 ${st.recomputed_strata} 层`);
    if (st.ms !== undefined) parts.push(`${st.ms} ms`);
  }
  const box = el("div", { class: "banner-item info" },
    `${label}：新增 ${adds.length} 条、消失 ${dels.length} 条` +
    (parts.length ? `（${parts.join("，")}）` : ""));
  if (adds.length || dels.length) {
    const list = el("div", { class: "delta-list" });
    for (const f of adds) list.appendChild(el("div", { class: "add", text: "+ " + f }));
    for (const f of dels) list.appendChild(el("div", { class: "del", text: "− " + f }));
    box.appendChild(list);
  }
  document.getElementById("banner").appendChild(box);
  setTimeout(() => box.remove(), 9000);
}

// ---------------------------------------------------------------- 数据区
function renderData() {
  const root = document.getElementById("edb-tables");
  root.innerHTML = "";
  const edb = STATE.edb || {};
  const preds = Object.keys(edb);
  if (!preds.length) {
    root.appendChild(el("div", { class: "hint", text: "暂无数据。用上方表单添加，或从样例载入。" }));
    return;
  }
  for (const pred of preds) {
    const head = el("div", { class: "pred-head" },
      el("span", { class: "name", text: pred }),
      el("span", { class: "badge edb", text: `输入 ${edb[pred].length} 条` }));
    const table = el("table", { class: "facts" });
    for (const tup of edb[pred]) {
      const btn = el("button", {
        class: "small danger", title: "撤销这条输入（增量删除，其它证明路径仍有效）",
        onclick: () => retractFact(pred, tup),
      }, "✕ 撤销");
      const tr = el("tr", {},
        el("td", {}, el("span", { class: "mono", text: factText(pred, tup) }),
          el("span", { class: "row-actions" }, btn)));
      table.appendChild(tr);
    }
    root.appendChild(head);
    root.appendChild(table);
  }
}

async function retractFact(pred, tuple) {
  const delta = await api("/api/facts/retract", "POST", { pred, tuple });
  deltaBanner(delta, `撤销 ${factText(pred, tuple)}`);
  await refresh();
}

async function addFact() {
  const pred = document.getElementById("add-pred").value.trim();
  const args = document.getElementById("add-args").value.trim();
  if (!pred) return banner("warn", "请填写谓词名");
  const tuple = args ? args.split(",").map(parseArg) : [];
  const delta = await api("/api/facts/add", "POST", { pred, tuple });
  deltaBanner(delta, `添加 ${factText(pred, tuple)}`);
  await refresh();
}

// ---------------------------------------------------------------- 规则区
function renderRules() {
  const ta = document.getElementById("rules-text");
  if (document.activeElement !== ta) ta.value = STATE.rules_text || "";
  const errBox = document.getElementById("rules-error");
  errBox.innerHTML = "";
  if (STATE.rules_error) {
    const box = el("div", { class: "error-box" }, "规则被拒绝：" + STATE.rules_error);
    const detail = STATE.rules_error_detail;
    if (detail && detail.cycle && detail.cycle.length) {
      box.appendChild(el("div", { class: "cycle", text: "循环路径：" + detail.cycle.join(" → ") }));
      if (detail.rules) {
        box.appendChild(el("div", { class: "detail", text: "涉及规则：\n" + detail.rules.join("\n") }));
      }
    }
    errBox.appendChild(box);
  }
  const sr = document.getElementById("strata");
  sr.innerHTML = "";
  for (const s of STATE.strata || []) {
    sr.appendChild(el("div", { class: "stratum" },
      el("div", {}, el("span", { class: "lvl", text: `第 ${s.level} 层 ` }),
        el("span", { class: "mono", text: s.preds.join(", ") })),
      el("div", { class: "rules", text: s.rules.join("\n") })));
  }
  if (!(STATE.strata || []).length && !STATE.rules_error) {
    sr.appendChild(el("div", { class: "hint", text: "尚无有效规则。" }));
  }
}

async function saveRules() {
  const text = document.getElementById("rules-text").value;
  const r = await api("/api/rules", "POST", { text });
  if (r.ok) {
    banner("ok", `规则已应用：${r.stats ? `求值 ${r.stats.rounds} 轮 / ${r.stats.ms} ms` : "OK"}`);
  } else {
    banner("bad", "规则被拒绝（未改动现有结果）", r.error, 9000);
  }
  await refresh();
}

// 结构化表单
function renderFormRules() {
  const root = document.getElementById("form-rules");
  root.innerHTML = "";
  formRules.forEach((rule, ri) => {
    const box = el("div", { class: "form-rule" });
    const headPred = el("input", { value: rule.head.pred, placeholder: "head 谓词" });
    headPred.oninput = () => rule.head.pred = headPred.value.trim();
    const headArgs = el("input", { class: "args", value: rule.head.args.join(", "), placeholder: "X, Y" });
    headArgs.oninput = () => rule.head.args = headArgs.value.split(",").map(s => s.trim()).filter(Boolean);
    box.appendChild(el("div", {},
      el("b", { text: "头部 " }), headPred, document.createTextNode("( "), headArgs, document.createTextNode(" )"),
      el("button", { class: "small danger", style: "float:right", onclick: () => { formRules.splice(ri, 1); renderFormRules(); } }, "删除规则")));
    const bodyBox = el("div", { class: "lit-rows" });
    rule.body.forEach((lit, li) => {
      const neg = el("input", { type: "checkbox" });
      neg.checked = !!lit.negated;
      neg.onchange = () => lit.negated = neg.checked;
      const p = el("input", { value: lit.pred, placeholder: "谓词" });
      p.oninput = () => lit.pred = p.value.trim();
      const a = el("input", { class: "args", value: lit.args.join(", "), placeholder: "X, Y" });
      a.oninput = () => lit.args = a.value.split(",").map(s => s.trim()).filter(Boolean);
      bodyBox.appendChild(el("div", { class: "lit-row" },
        el("label", {}, neg, " 非 "), p, document.createTextNode("( "), a, document.createTextNode(" )"),
        el("button", { class: "small danger", onclick: () => { rule.body.splice(li, 1); renderFormRules(); } }, "✕")));
    });
    box.appendChild(bodyBox);
    box.appendChild(el("button", {
      class: "small", onclick: () => { rule.body.push({ pred: "edge", args: ["X", "Y"], negated: false }); renderFormRules(); }
    }, "＋ 加规则体文字"));
    root.appendChild(box);
  });
}

async function applyFormRules() {
  const r = await api("/api/rules/from-form", "POST", { rules: formRules });
  if (r.ok) {
    document.getElementById("rules-text").value = r.text;
    banner("ok", "表单规则已生成并应用");
    switchTab("text");
  } else {
    banner("bad", "表单规则被拒绝", r.error, 9000);
  }
  await refresh();
}

function switchTab(which) {
  document.getElementById("tab-text").classList.toggle("active", which === "text");
  document.getElementById("tab-form").classList.toggle("active", which === "form");
  document.getElementById("rules-text-pane").style.display = which === "text" ? "" : "none";
  document.getElementById("rules-form-pane").style.display = which === "form" ? "" : "none";
}

// ---------------------------------------------------------------- 结果区
function renderResults() {
  const root = document.getElementById("result-tables");
  root.innerHTML = "";
  const rels = STATE.relations || {};
  const idb = new Set(STATE.idb_preds || []);
  const stats = STATE.stats;
  const total = Object.values(rels).reduce((n, t) => n + t.length, 0);
  document.getElementById("result-stats").textContent =
    stats ? `共 ${total} 条 · 求值 ${stats.rounds} 轮 / ${stats.ms} ms` : "";
  const preds = Object.keys(rels).sort((a, b) => (idb.has(b) - idb.has(a)) || a.localeCompare(b));
  for (const pred of preds) {
    const isIdb = idb.has(pred);
    const head = el("div", { class: "pred-head" },
      el("span", { class: "name", text: pred }),
      el("span", { class: `badge ${isIdb ? "idb" : "edb"}`, text: isIdb ? `派生 ${rels[pred].length} 条` : `输入 ${rels[pred].length} 条` }));
    const table = el("table", { class: "facts" });
    for (const tup of rels[pred]) {
      const provCell = el("span", { class: "row-actions" });
      const btn = el("button", { class: "small", title: "展开这条结果的支持关系（证明）" }, "溯源 ▸");
      const provBox = el("div", {});
      let open = false;
      btn.onclick = async () => {
        open = !open;
        btn.textContent = open ? "溯源 ▾" : "溯源 ▸";
        provBox.innerHTML = "";
        if (open) {
          const info = await api(`/api/provenance?pred=${encodeURIComponent(pred)}&tuple=${encodeURIComponent(JSON.stringify(tup))}`);
          provBox.appendChild(renderProv(info, [pred + JSON.stringify(tup)], null));
        }
      };
      provCell.appendChild(btn);
      const tr = el("tr", { class: isIdb ? "derived" : "" },
        el("td", {}, el("span", { class: "mono", text: factText(pred, tup) }), provCell, provBox));
      table.appendChild(tr);
    }
    root.appendChild(head);
    root.appendChild(table);
  }
  if (!preds.length) root.appendChild(el("div", { class: "hint", text: "暂无结果。" }));
}

// 溯源树：懒展开，ancestorKeys 防止环上无限展开
function renderProv(info, ancestorKeys, versionId) {
  const box = el("div", { class: "prov" });
  if (!info.exists) {
    box.appendChild(el("span", { class: "neg", text: "（该事实当前不成立）" }));
    return box;
  }
  if (info.base) box.appendChild(el("div", { class: "base-tag", text: "● 基事实：直接输入" }));
  if (!info.derivations.length && !info.base) {
    box.appendChild(el("div", { class: "hint", text: "（无推导记录）" }));
  }
  info.derivations.forEach((d, i) => {
    const dbox = el("div", {},
      el("div", { class: "rule", text: `证明 ${i + 1} ⇐ ${d.rule}` }));
    if (d.pos.length) {
      const ul = el("ul", {});
      for (const ant of d.pos) {
        const key = ant.pred + JSON.stringify(ant.tuple);
        const li = el("li", {});
        if (ancestorKeys.includes(key)) {
          li.appendChild(el("span", { class: "cycle-mark mono", text: `${ant.text} ⟳（见上方，循环支持不构成证明）` }));
        } else {
          const link = el("a", { href: "#", class: "mono", text: ant.text });
          const sub = el("div", {});
          link.onclick = async (ev) => {
            ev.preventDefault();
            if (sub.dataset.loaded) { sub.style.display = sub.style.display === "none" ? "" : "none"; return; }
            const url = versionId == null
              ? `/api/provenance?pred=${encodeURIComponent(ant.pred)}&tuple=${encodeURIComponent(JSON.stringify(ant.tuple))}`
              : `/api/versions/${versionId}/provenance?pred=${encodeURIComponent(ant.pred)}&tuple=${encodeURIComponent(JSON.stringify(ant.tuple))}`;
            const subInfo = await api(url);
            sub.appendChild(renderProv(subInfo, [...ancestorKeys, key], versionId));
            sub.dataset.loaded = "1";
          };
          li.appendChild(link);
          li.appendChild(sub);
        }
        ul.appendChild(li);
      }
      dbox.appendChild(ul);
    }
    for (const n of d.neg) {
      dbox.appendChild(el("div", { class: "neg mono", text: `且 ${n.text} 不存在（否定条件成立）` }));
    }
    box.appendChild(dbox);
  });
  return box;
}

// ---------------------------------------------------------------- 对比
async function compareNow() {
  const out = document.getElementById("compare-out");
  out.innerHTML = "";
  const r = await api("/api/compare", "POST", {});
  if (!r.ok) return banner("warn", r.error || "无法比较");
  if (r.equal) {
    out.appendChild(el("div", { class: "banner-item ok" },
      `✔ 增量维护结果与整版重算一致：各 ${r.incremental_facts} 条事实` +
      `（整版重算 ${r.full_stats.rounds} 轮 / ${r.full_stats.ms} ms）`));
  } else {
    const box = el("div", { class: "banner-item bad" }, "✘ 两种结果不一致：");
    box.appendChild(el("div", { class: "detail", text:
      `仅增量有：\n${r.only_incremental.join("\n") || "(无)"}\n仅整版有：\n${r.only_full.join("\n") || "(无)"}` }));
    out.appendChild(box);
  }
}

// ---------------------------------------------------------------- 版本
function renderVersions() {
  const root = document.getElementById("version-list");
  root.innerHTML = "";
  if (!STATE.versions.length) {
    root.appendChild(el("span", { class: "hint", text: "还没有已发布的版本。" }));
    return;
  }
  for (const v of STATE.versions) {
    const card = el("div", { class: "version-card" + (v.status === "failed" ? " failed" : "") });
    card.appendChild(el("div", {},
      el("span", { class: "vid", text: `版本 #${v.id} ` }),
      el("span", { class: `badge ${v.status === "ok" ? "ok" : "bad"}`, text: v.status === "ok" ? "成功" : "失败" })));
    card.appendChild(el("div", { class: "ts", text: `${v.ts}${v.note ? " · " + v.note : ""}` }));
    if (v.status === "ok") {
      const s = Object.entries(v.summary || {}).map(([p, n]) => `${p}:${n}`).join("  ");
      card.appendChild(el("div", { class: "summary", text: s || "(空)" }));
      card.onclick = () => showVersion(v.id);
    } else {
      card.appendChild(el("div", { class: "summary", text: v.error || "" }));
    }
    root.appendChild(card);
  }
}

async function showVersion(vid) {
  const v = await api(`/api/versions/${vid}/results`);
  const modalRoot = document.getElementById("modal-root");
  modalRoot.innerHTML = "";
  const modal = el("div", { class: "modal" });
  const close = () => modalRoot.innerHTML = "";
  modal.appendChild(el("button", { class: "close", onclick: close }, "✕ 关闭"));
  modal.appendChild(el("h2", { text: `版本 #${v.id}（${v.ts}）${v.note ? " · " + v.note : ""}` }));
  if (v.status !== "ok") {
    modal.appendChild(el("div", { class: "error-box", text: "该版本发布失败：" + (v.error || "") }));
  } else {
    modal.appendChild(el("div", { class: "hint", text: "只读快照：该版本的输入 + 规则 + 结果。点击结果可展开当时的证明。" }));
    modal.appendChild(el("h3", { text: "规则" }));
    modal.appendChild(el("pre", { class: "mono", text: v.rules_text || "(无)" }));
    modal.appendChild(el("h3", { text: "结果" }));
    const idb = new Set(v.idb_preds || []);
    for (const pred of Object.keys(v.relations || {})) {
      modal.appendChild(el("div", { class: "pred-head" },
        el("span", { class: "name", text: pred }),
        el("span", { class: `badge ${idb.has(pred) ? "idb" : "edb"}`, text: `${v.relations[pred].length} 条` })));
      const table = el("table", { class: "facts" });
      for (const tup of v.relations[pred]) {
        const btn = el("button", { class: "small" }, "溯源 ▸");
        const provBox = el("div", {});
        let open = false;
        btn.onclick = async () => {
          open = !open;
          btn.textContent = open ? "溯源 ▾" : "溯源 ▸";
          provBox.innerHTML = "";
          if (open) {
            const info = await api(`/api/versions/${vid}/provenance?pred=${encodeURIComponent(pred)}&tuple=${encodeURIComponent(JSON.stringify(tup))}`);
            provBox.appendChild(renderProv(info, [pred + JSON.stringify(tup)], vid));
          }
        };
        table.appendChild(el("tr", { class: idb.has(pred) ? "derived" : "" },
          el("td", {}, el("span", { class: "mono", text: factText(pred, tup) }),
            el("span", { class: "row-actions" }, btn), provBox)));
      }
      modal.appendChild(table);
    }
  }
  const mask = el("div", { class: "modal-mask", onclick: (e) => { if (e.target === mask) close(); } }, modal);
  modalRoot.appendChild(mask);
}

async function publish() {
  const note = prompt("版本备注（可空）：", "");
  if (note === null) return;
  const v = await api("/api/publish", "POST", { note });
  if (v.status === "ok") {
    banner("ok", `版本 #${v.id} 发布成功`);
  } else {
    banner("bad", `版本 #${v.id} 发布失败，上一版结果仍可查`, v.error, 9000);
  }
  await refresh();
}

// ---------------------------------------------------------------- 样例
async function loadSample(name) {
  const r = await api(`/api/sample/${name}`, "POST", {});
  if (!r.ok) return banner("bad", "样例载入失败");
  banner("info", `已载入样例「${r.title}」：${r.description}`, null, 9000);
  await refresh();
  const s = SAMPLES[name];
  if (s && s.rules_v2) {
    const box = el("div", { class: "banner-item warn" },
      "该样例含屏蔽规则变更演示：先「发布版本」保存 v1，再点右侧按钮载入 v2 规则并再次发布。 ",
      el("button", {
        class: "small", onclick: async () => {
          if (s.facts_v2_extra) await api("/api/facts/import", "POST", { text: s.facts_v2_extra });
          document.getElementById("rules-text").value = s.rules_v2;
          banner("info", "已载入 v2 屏蔽规则到编辑框（新增 muted(vip). 数据），请点「校验并应用」后「发布版本」。");
          await refresh();
        }
      }, "载入 v2 屏蔽规则"));
    document.getElementById("banner").appendChild(box);
  }
}

const BAD_RULE_DEMOS = {
  negcycle: "% 负向环：p 依赖 not q，q 依赖 not p\nnode(a). node(b).\np(X) :- node(X), not q(X).\nq(X) :- node(X), not p(X).\n",
  unsafe: "% 变量 X 没有正向约束，取值域无限\nanything(X) :- not blocked(X).\n",
};

// ---------------------------------------------------------------- 刷新
async function refresh() {
  STATE = await api("/api/state");
  renderData();
  renderRules();
  renderResults();
  renderVersions();
}

async function main() {
  SAMPLES = await api("/api/samples");
  document.getElementById("btn-add-fact").onclick = addFact;
  document.getElementById("btn-import").onclick = async () => {
    const text = document.getElementById("import-text").value;
    if (!text.trim()) return;
    const delta = await api("/api/facts/import", "POST", { text });
    deltaBanner(delta, "批量导入");
    await refresh();
  };
  document.getElementById("btn-save-rules").onclick = saveRules;
  document.getElementById("btn-compare").onclick = compareNow;
  document.getElementById("btn-publish").onclick = publish;
  document.getElementById("btn-reset").onclick = async () => {
    if (!confirm("清空草稿区的数据与规则？（已发布版本保留）")) return;
    await api("/api/reset", "POST", {});
    await refresh();
  };
  document.getElementById("sample-select").onchange = (e) => {
    if (e.target.value) loadSample(e.target.value);
    e.target.value = "";
  };
  document.getElementById("bad-rule-demo").onchange = async (e) => {
    const v = e.target.value;
    e.target.value = "";
    if (!v) return;
    document.getElementById("rules-text").value = BAD_RULE_DEMOS[v];
    await saveRules();
  };
  document.getElementById("tab-text").onclick = () => switchTab("text");
  document.getElementById("tab-form").onclick = () => switchTab("form");
  document.getElementById("btn-form-add-rule").onclick = () => {
    formRules.push({ head: { pred: "reach", args: ["X", "Y"] }, body: [{ pred: "edge", args: ["X", "Y"], negated: false }] });
    renderFormRules();
  };
  document.getElementById("btn-form-apply").onclick = applyFormRules;
  formRules = [{ head: { pred: "reach", args: ["X", "Y"] }, body: [{ pred: "edge", args: ["X", "Y"], negated: false }] }];
  renderFormRules();
  await refresh();
}

main().catch(e => banner("bad", "初始化失败：" + e.message));
