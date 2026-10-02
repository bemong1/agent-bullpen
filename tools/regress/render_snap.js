// Deterministic render of static/index.html's inline script against saved API JSON. No npm.
// usage: node render_snap.js <static dir> <fixture prefix> > out.json      (AB_LANG=en renders in English; default ko)
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx] = process.argv.slice(2);
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
// every <script src> (from the static dir) and inline <script>, in document order
const scripts = [...html.matchAll(/<script(?:\s+src="([^"]+)")?\s*>([\s\S]*?)<\/script>/g)].map(m => ({ src: m[1] || '', code: m[1] ? fs.readFileSync(path.join(dir, m[1]), 'utf8') : m[2] }));
const J = n => JSON.parse(fs.readFileSync(fx + n + '.json', 'utf8'));
const STATE = J('_state'), FIXED = Math.round(STATE.now * 1000) + 5000;
class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(FIXED); } static now() { return FIXED; } }
const els = new Map();
function el(sel) {
  if (els.has(sel)) return els.get(sel);
  const e = { sel, innerHTML: '', textContent: '', hidden: false, dataset: {}, style: { setProperty() {} }, scrollTop: 0, scrollHeight: 0, clientWidth: 900, clientHeight: 300, offsetHeight: 30, offsetWidth: 100,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); }, toggle() {} },
    querySelectorAll: () => [], querySelector: () => null, getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
    addEventListener() {}, insertAdjacentHTML(p, h) { this.innerHTML += h; }, appendChild() {}, append() {}, remove() {}, closest: () => null, isConnected: true, getContext: () => null };
  els.set(sel, e); return e;
}
const route = u => u.includes('api/state') ? STATE : u.includes('api/sessions') ? J('_sessions') : u.includes('api/talk') ? (u.includes('scope=agents') ? { items: STATE.feed.filter(e => ['spawn', 'orch_msg', 'handback', 'peer', 'agent_msg', 'xread'].includes(e.kind)).slice(-80), more: true } : J('_talk')) : u.includes('api/timeline') ? J('_timeline') : u.includes('api/plans') ? { now: STATE.now, claude: null, codex: STATE.codex_limit } : {};
const ctx = { console, Date: FDate, Math, JSON, URL, URLSearchParams, Promise, Set, Map, Object, Array, String, Number, Infinity, isNaN, encodeURIComponent, decodeURIComponent, AbortController,
  innerWidth: 1200, innerHeight: 900, scrollY: 0, scrollX: 0, location: { search: '?session=' + STATE.session.id, hash: '', href: 'http://x/' },
  localStorage: { _m: {}, getItem(k) { return this._m[k] ?? null; }, setItem(k, v) { this._m[k] = String(v); } },
  document: { querySelector: s => el(s), querySelectorAll: () => [], addEventListener() {}, createElement: t => el('new:' + t + ':' + els.size), head: el('head'), body: el('body'), hidden: false, title: '', documentElement: el('html'), fonts: null },
  fetch: async u => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(I18B.route(dir, u) || route(String(u)))) }),
  setInterval: () => 0, setTimeout: (f, ms) => 0, clearTimeout() {}, requestAnimationFrame: () => 0, addEventListener() {}, matchMedia: () => ({ matches: false }), scrollBy() {},
  IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
ctx.window = ctx; vm.createContext(ctx);
scripts.forEach(s => { vm.runInContext(s.code, ctx); if (s.src === 'i18n.js') I18B.boot(ctx, dir); });   // the page starts after I18N.init, which the loader does from disk
setTimeout(() => {   // let tick()/fillSessions()/loadTalk() promises settle
  const out = {}; for (const [k, e] of els) if (!k.startsWith('new:') && (e.innerHTML || e.textContent)) out[k] = e.innerHTML || e.textContent;
  out['document.title'] = ctx.document.title;
  process.stdout.write(JSON.stringify(out, null, 1));
}, 200);
