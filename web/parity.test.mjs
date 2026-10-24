// Parity test: runs the JS core from web/app.html against tests/vectors.json (made by the Python library).
// Usage: node web/parity.test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "app.html"), "utf8");
const m = html.match(/<script id="core">([\s\S]*?)<\/script>/);
if (!m) { console.error("core script block not found"); process.exit(1); }

// The core gets no DOM, no fetch, no timers: it must be pure.
const ctx = vm.createContext({ Math, Number, String, Object, Array, Error, RegExp, JSON, isFinite, parseInt });
vm.runInContext(m[1] + "\n;globalThis.__core = { filterDocuments, parseQuery, csvCell, toCsv, addDays, safeUrl, isIso, coerceDataset, daysBetween };", ctx);
const core = ctx.__core;
const v = JSON.parse(readFileSync(join(here, "..", "tests", "vectors.json"), "utf8"));

let checks = 0, failures = 0;
const fail = (path, msg) => { failures++; console.error(`FAIL ${path}: ${msg}`); };
const clone = (x) => JSON.parse(JSON.stringify(x));

// Exact structural equality, including key sets (no tolerance: everything here is strings and integers).
function same(expected, actual, path) {
  checks++;
  const e = JSON.stringify(sortKeys(expected)), a = JSON.stringify(sortKeys(clone(actual)));
  if (e !== a) fail(path, `\n  expected ${e.slice(0, 400)}\n  got      ${a.slice(0, 400)}`);
}
function sortKeys(x) {
  if (Array.isArray(x)) return x.map(sortKeys);
  if (x && typeof x === "object") return Object.fromEntries(Object.keys(x).sort().map((k) => [k, sortKeys(x[k])]));
  return x;
}

for (const c of v.cases) {
  const res = core.filterDocuments(clone(v.documents), clone(c.query));
  same(c.result, res, `case.${c.name}`);
  const byNum = Object.fromEntries(v.documents.map((d) => [d.document_number, d]));
  checks++;
  const csv = core.toCsv(res.numbers.map((n) => byNum[n]));
  if (csv !== c.csv) fail(`case.${c.name}.csv`, "CSV differs");
}
for (const p of v.parse) same(p.result, core.parseQuery(p.q), `parse.${JSON.stringify(p.q)}`);
for (const c of v.cells) { checks++; const out = core.csvCell(c.v); if (out !== c.out) fail(`cell.${JSON.stringify(c.v)}`, `${JSON.stringify(out)} != ${JSON.stringify(c.out)}`); }
for (const d of v.add_days) { checks++; const out = core.addDays(d.a, d.n); if (out !== d.out) fail(`addDays(${d.a},${d.n})`, `${out} != ${d.out}`); }
for (const u of v.urls) { checks++; const out = core.safeUrl(u.v); if (out !== u.out) fail(`safeUrl(${JSON.stringify(u.v)})`, `${out} != ${u.out}`); }
for (const u of v.iso) { checks++; const out = core.isIso(u.v); if (out !== u.out) fail(`isIso(${JSON.stringify(u.v)})`, `${out} != ${u.out}`); }
for (const d of v.datasets) {
  let out;
  try { out = core.coerceDataset(clone(d.input)); }
  catch (e) {
    checks++;
    if (d.error === undefined) fail(`dataset.${d.name}`, `unexpected error ${e.message}`);
    else if (e.message !== d.error) fail(`dataset.${d.name}`, `error "${e.message}" != "${d.error}"`);
    continue;
  }
  if (d.error !== undefined) { fail(`dataset.${d.name}`, `expected error "${d.error}"`); continue; }
  same(d.out, out, `dataset.${d.name}`);
}
// Round trip: every fixture document survives the page's defensive loader unchanged.
same(v.documents.slice(0, -2), core.coerceDataset({ documents: clone(v.documents.slice(0, -2)) }).docs, "dataset.fixtures_roundtrip");

// Calendar arithmetic against the JS Date implementation over 30 years (independent of Python).
let z = Date.UTC(2010, 0, 1);
const iso = (t) => new Date(t).toISOString().slice(0, 10);
for (let i = 0; i < 365 * 30; i += 7) {
  const a = iso(z + i * 864e5);
  for (const n of [0, 1, 14, 30, 365, -1]) {
    checks++;
    const want = iso(z + (i + n) * 864e5);
    if (core.addDays(a, n) !== want) { fail(`addDays(${a},${n})`, `${core.addDays(a, n)} != ${want}`); break; }
  }
  checks++;
  if (core.daysBetween("2010-01-01", a) !== i) fail(`daysBetween(${a})`, "mismatch");
}

if (failures) { console.error(`${failures} failure(s) in ${checks} checks`); process.exit(1); }
console.log(`parity ok: ${v.cases.length} filter cases, ${v.parse.length} parse, ${v.cells.length} cells, ${v.urls.length} urls, ${v.iso.length} dates, ${v.datasets.length} datasets, ${checks} checks (exact match)`);

// ---------------------------------------------------------------- UI rendering with hostile data
// Runs the UI script against a tiny fake DOM. Any innerHTML write throws; every rendered string must
// arrive through textContent, and every link must be an allowed https URL.
{
  const ui = html.match(/<script id="ui">([\s\S]*?)<\/script>/)[1];
  const created = [];
  class El {
    constructor(tag) { this.tagName = tag; this.children = []; this._text = ""; this.className = ""; this.attrs = {}; this.value = ""; this.checked = false; this.listeners = {};
      this.classList = { _s: new Set(), add: (c) => this.classList._s.add(c), remove: (c) => this.classList._s.delete(c), toggle: (c, on) => on ? this.classList._s.add(c) : this.classList._s.delete(c) };
      created.push(this); }
    set innerHTML(v) { throw new Error("innerHTML used"); }
    set outerHTML(v) { throw new Error("outerHTML used"); }
    insertAdjacentHTML() { throw new Error("insertAdjacentHTML used"); }
    set textContent(v) { this._text = String(v); this.children = []; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
    get childNodes() { return this.children; }
    appendChild(c) { if (c.isFrag) { this.children.push(...c.children); } else this.children.push(c); return c; }
    addEventListener(t, f) { this.listeners[t] = f; }
    set href(v) { this.attrs.href = v; } get href() { return this.attrs.href; }
    focus() {} select() {}
  }
  const ids = {};
  for (const [, tag, id] of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)) {
    const e = new El(tag); ids[id] = e;
    if (id === "f-window") e.value = "14";
    if (id === "f-sort") e.value = "newest";
  }
  const hostile = {
    updated: "2026-10-21", complete_since: "2026-01-01", retention_days: 365,
    agencies: [{ key: "EPA<img>", label: "<b>x</b>", feed: "feed-../../x.xml" }, { key: "EPA", label: "EPA", feed: "feed-epa.xml" }],
    documents: [
      { document_number: "2026-1", title: "<img src=x onerror=alert(1)>", type: "Rule", publication_date: "2026-10-20", comments_close_on: "2026-10-25",
        html_url: "javascript:alert(1)", pdf_url: "https://www.govinfo.gov/ok.pdf", watch: ["EPA<script>"], abstract: "<script>alert(2)</script>",
        agencies: [{ name: "<svg onload=alert(3)>", slug: "x" }], docket_ids: ["<i>"], regulation_id_numbers: [], significant: true },
    ],
  };
  ids["snapshot"]._text = JSON.stringify(hostile);
  const doc = {
    getElementById: (id) => ids[id] || null,
    createElement: (t) => new El(t),
    createDocumentFragment: () => { const f = new El("#frag"); f.isFrag = true; return f; },
    querySelector: (sel) => (sel.includes(":checked") ? (ids["v-all"].checked ? ids["v-all"] : ids["v-closing"]) : null),
  };
  ids["v-all"].value = "all"; ids["v-closing"].value = "closing";
  let liveData = hostile;
  const fakeGlobal = { document: doc, window: {}, navigator: {}, setTimeout, clearTimeout, Promise, JSON, Math, Date, String, Number, Object, Array, Error, RegExp, isFinite, parseInt };
  const uctx = vm.createContext(fakeGlobal);
  vm.runInContext(m[1], uctx);
  // Live path: exercise feed links too.
  fakeGlobal.window.regWatchLoad = () => Promise.resolve({ data: liveData, base: "data/" });
  vm.runInContext(ui, uctx);
  await new Promise((r) => setTimeout(r, 20));
  checks++;
  const titles = created.filter((e) => e.className === "doc-title");
  if (!titles.length || titles[0].textContent !== "<img src=x onerror=alert(1)>") fail("ui.title", "hostile title not rendered as text");
  for (const e of created) {
    if (e.tagName === "a") {
      checks++;
      const h = e.attrs.href;
      if (!/^(https:\/\/(www\.)?(federalregister|govinfo)\.gov\/|data\/feed(-[a-z0-9_-]+)?\.xml$)/.test(h)) fail("ui.href", `unsafe href ${h}`);
      if (e.rel !== "noopener noreferrer") fail("ui.rel", "link missing rel");
    }
  }
  checks++;
  if (created.some((e) => e.tagName === "a" && /javascript:|\.\.\//.test(e.attrs.href || ""))) fail("ui.href", "javascript or traversal link created");
  checks++;
  if (!ids["data-note"].textContent.startsWith("Updated 2026-10-21")) fail("ui.note", ids["data-note"].textContent);
  checks++;
  if (ids["data-tag"].textContent !== "Live data") fail("ui.tag", ids["data-tag"].textContent);
  checks++;
  if (!/published since 2026-01-01\./.test(ids["data-note"].textContent)) fail("ui.since", ids["data-note"].textContent);
  // Review fix: a complete_since older than the retention window is shown as the retention floor.
  liveData = Object.assign({}, hostile, { complete_since: "2024-05-05" });
  vm.runInContext(ui, uctx);
  await new Promise((r) => setTimeout(r, 20));
  checks++;
  if (!/published since 2025-10-21\./.test(ids["data-note"].textContent)) fail("ui.since.floor", ids["data-note"].textContent);
  // Review fix: a seed store without complete_since must not be tagged "Live data".
  liveData = Object.assign({}, hostile, { complete_since: null });
  vm.runInContext(ui, uctx);
  await new Promise((r) => setTimeout(r, 20));
  checks++;
  if (ids["data-tag"].textContent !== "Sample data") fail("ui.tag.sample", ids["data-tag"].textContent);
  // Review fix: when data/documents.json fails, the error note must say the snapshot is shown and its date.
  fakeGlobal.window.regWatchLoad = () => Promise.reject(new Error("HTTP 404"));
  vm.runInContext(ui, uctx);
  await new Promise((r) => setTimeout(r, 20));
  checks++;
  const fb = ids["data-note"].textContent;
  if (!/HTTP 404/.test(fb) || !/embedded snapshot as of 2026-10-21/.test(fb) || !/not current/.test(fb)) fail("ui.fallback", fb);
  checks++;
  if (!/^Example data: snapshot 2026-10-21$/.test(ids["data-tag"].textContent)) fail("ui.fallback.tag", ids["data-tag"].textContent);
  console.log(`ui ok: hostile document rendered as text only, ${created.filter((e) => e.tagName === "a").length} links checked, fallback labeled`);
}
if (failures) { console.error(`${failures} failure(s) in ${checks} checks`); process.exit(1); }
