// Checks of the office card's first state (static/index.html + board.js), in English and Korean, against the first fixture of tools/make_synth_fixture.py.
// No browser, no npm: the fake page of state_checks.js, with the window width and the saved choices set per page.
//  - from 768 px wide (a tablet: 834, 1194, and the 1500 px the old rule needed) the office starts open and says nothing; below it (a phone: 390, 767) it starts collapsed and the card says so
//    in one line (page.game.folded)
//  - a choice the user made (localStorage `ab.gameOpen`) wins over the width, both ways
//  - pressing the line opens the office (the same as the Expand button: the choice is saved), the line goes away, and collapsing brings it back; the buttons keep their words
//  - the line is in both dictionaries, and in the page as a button with its English text for a page whose dictionary did not load
// usage: node office_fold_checks.js <static dir> <fixture prefix>      (prints PASS/FAIL per check, exit code 1 if any FAIL; without the fixture it is skipped with a note, unless CI or REQUIRE_FIXTURES is set: then that is a failure)
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx] = process.argv.slice(2);
if (!fs.existsSync(fx + '_state.json')) {
  const need = process.env.CI || process.env.REQUIRE_FIXTURES;       // a missing fixture must not let CI pass without the checks; on a laptop it is only a note
  console.log((need ? 'FAIL' : 'SKIP') + ' office fold checks: no ' + fx + '_state.json (make the fixture with tools/make_synth_fixture.py)');
  process.exit(need ? 1 : 0);
}
const J = n => JSON.parse(fs.readFileSync(fx + n + '.json', 'utf8'));
const STATE = J('_state'), TALK = J('_talk'), ATALK = J('_atalk'), SESSIONS = J('_sessions'), PLANS = J('_plans');
const FIXED = Math.round(STATE.now * 1000) + 5000;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const HAN = /[ㄱ-ㆎ가-힣]/;
const KEY = /\[(?:common|status|kind|time|unit|board|office|demo|diag|page|cli|alert|event|plan)\.[\w.]+\]/i;
let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)))); };
const shows = (html, text) => !!text && (html.includes(text) || html.includes(esc(text)));
const clone = o => JSON.parse(JSON.stringify(o));

class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(FIXED); } static now() { return FIXED; } }

function makePage(opts = {}) {
  const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
  const scripts = [...html.matchAll(/<script(?:\s+src="([^"]+)")?\s*>([\s\S]*?)<\/script>/g)].map(m => ({ src: m[1] || '', code: m[1] ? fs.readFileSync(path.join(dir, m[1]), 'utf8') : m[2] }));
  const els = new Map(), log = [];
  const record = (sel, prop, v) => { if (typeof v === 'string' && v) log.push({ sel, prop, v }); };
  function el(sel) {
    if (els.has(sel)) return els.get(sel);
    const e = { sel, hidden: false, dataset: {}, style: { setProperty() {} }, scrollTop: 0, scrollHeight: 0, clientWidth: 900, clientHeight: 300, offsetHeight: 30, offsetWidth: 100,
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); }, toggle() {} },
      querySelectorAll(s) { if (s !== '.tab') return []; const self = this; self.tabs = {};
        return [...this.innerHTML.matchAll(/data-k="([^"]+)"/g)].map(m => ({ dataset: { k: m[1] }, set onclick(f) { self.tabs[m[1]] = f; } })); },
      querySelector: () => null, getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
      addEventListener(t, f) { (this.listeners = this.listeners || {})[t] = (this.listeners[t] || []).concat(f); }, insertAdjacentHTML(p, h) { this.innerHTML += h; },
      appendChild() {}, append() {}, remove() {}, closest: () => null, isConnected: true, getContext: () => null };
    for (const prop of ['innerHTML', 'textContent', 'title']) { let v = ''; Object.defineProperty(e, prop, { get() { return v; }, set(x) { v = x; record(sel, prop, x); }, enumerable: true }); }
    els.set(sel, e); return e;
  }
  const S0 = opts.state || STATE;
  const P = { els, el, log, fetched: [], timers: [], agent: null, talk: opts.talk || TALK };
  const route = u => {
    const url = new URL('http://x/' + u.replace(/^\//, '')), q = url.searchParams, p = url.pathname.replace(/^\//, '');
    if (p === 'api/state') return S0;
    if (p === 'api/sessions') return SESSIONS;
    if (p === 'api/talk' && q.get('scope') === 'agents') return ATALK;
    if (p === 'api/talk') return P.talk;
    if (p === 'api/timeline') return { since: +q.get('since'), lanes: [], orch: [] };
    if (p === 'api/plans') return PLANS;
    if (p === 'api/agent') return { id: q.get('id'), orch_msgs: [], handbacks: [], texts: [], writes: [], reads: [], tool_counts: [], activity: [], spawn_ts: S0.now, spawn_prompt: '', link: null, partial: null };
    return {};
  };
  const ctx = { console, Date: FDate, Math, JSON, URL, URLSearchParams, Promise, Set, Map, Object, Array, String, Number, Infinity, isNaN, encodeURIComponent, decodeURIComponent, AbortController, Error,
    innerWidth: opts.width || 1200, innerHeight: 900, scrollY: 0, scrollX: 0, location: { search: '?session=' + S0.session.id, hash: '', host: 'localhost:8790', href: 'http://x/' },
    localStorage: { _m: Object.assign({}, opts.saved || {}), getItem(k) { return this._m[k] ?? null; }, setItem(k, v) { this._m[k] = String(v); } },
    document: { querySelector: s => el(s), getElementById: s => el('#' + s), querySelectorAll: () => [], addEventListener() {}, createElement: t => el('new:' + t + ':' + els.size), head: el('head'), body: el('body'), hidden: false,
      get title() { return this._title || ''; }, set title(x) { this._title = x; record('document', 'title', x); }, documentElement: el('html'), fonts: null },
    fetch: (u) => new Promise(resolve => {
      u = String(u); P.fetched.push(u);
      const dict = I18B.route(dir, u);
      if (dict) return resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(dict)) });
      resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(route(u))) });
    }),
    setInterval: () => 0, setTimeout: (f, ms) => { const t = { id: P.timers.length + 1, f, ms: ms || 0 }; P.timers.push(t); return t.id; }, clearTimeout: id => { P.timers = P.timers.filter(t => t.id !== id); },
    requestAnimationFrame: () => 0, addEventListener() {}, matchMedia: () => ({ matches: false }), scrollBy() {},
    IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  P.ctx = ctx;
  P.run = code => vm.runInContext(code, ctx);
  P.load = async () => { scripts.forEach(s => { vm.runInContext(s.code, ctx); if (s.src === 'i18n.js') I18B.boot(ctx, dir, opts.lang || 'en'); }); await sleep(80); };
  P.T = (key, params) => P.run(`I18N.has(${JSON.stringify(key)}) ? t(${JSON.stringify(key)}, ${JSON.stringify(params || {})}) : null`);
  P.hm = ts => P.run(`hm(${JSON.stringify(ts)})`);
  P.html = sel => (els.get(sel) || { innerHTML: '' }).innerHTML;
  P.text = sel => (els.get(sel) || { textContent: '' }).textContent;
  P.all = () => log.map(x => x.v);
  return P;
}
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const open = P => P.run('ui.gameOpen');
const state = P => ({ open: open(P), box: P.el('#gameBox').hidden, line: P.el('#gameFolded').hidden, toggle: P.el('#gameToggle').textContent });

async function run(lang) {
  const L = s => `${lang}: ${s}`;
  const boot = async (width, saved) => { const P = makePage({ lang, width, saved }); await P.load(); return P; };

  // ---------- the first state by width ----------
  for (const w of [834, 1194, 1500, 768]) {
    const P = await boot(w);
    const s = state(P);
    check(L(`${w} px: the office starts open, the box shows and the one-line note does not`), s.open === true && s.box === false && s.line === true, s);
    check(L(`${w} px: the toggle button says Collapse`), s.toggle === P.T('common.collapse'), s.toggle);
  }
  for (const w of [390, 767]) {
    const P = await boot(w);
    const s = state(P);
    check(L(`${w} px: the office starts collapsed and the card says so in one line`), s.open === false && s.box === true && s.line === false, s);
    check(L(`${w} px: the toggle button says Expand`), s.toggle === P.T('common.expand'), s.toggle);
  }

  // ---------- a choice of the user wins ----------
  let P = await boot(1194, { 'ab.gameOpen': 'false' });
  check(L('a collapse chosen before stays collapsed on a wide window, with the note'), state(P).open === false && state(P).line === false, state(P));
  P = await boot(390, { 'ab.gameOpen': 'true' });
  check(L('an expand chosen before stays open on a narrow window, with no note'), state(P).open === true && state(P).line === true, state(P));

  // ---------- pressing the line ----------
  P = await boot(390);
  const press = P.el('#gameFolded');
  check(L('the line is pressable (a handler is set)'), typeof press.onclick === 'function', typeof press.onclick);
  press.onclick();
  const after = state(P);
  check(L('pressing the line opens the office, hides the line and the box shows'), after.open === true && after.box === false && after.line === true, after);
  check(L('the choice is kept (saved as the Expand button does)'), P.run("store.get('gameOpen', null)") === true, P.run("localStorage.getItem('ab.gameOpen')"));
  check(L('the toggle button now says Collapse'), after.toggle === P.T('common.collapse'), after.toggle);
  P.el('#gameToggle').onclick();
  const back = state(P);
  check(L('collapsing brings the line back (the button keeps its behaviour)'), back.open === false && back.line === false && back.box === true && back.toggle === P.T('common.expand'), back);
  P.el('#gameToggle').onclick();
  check(L('the Expand button opens it again and the line goes'), state(P).open === true && state(P).line === true, state(P));
  const again = await boot(390, P.ctx.localStorage._m);
  check(L('a page opened again with that saved choice starts open on the phone'), state(again).open === true && state(again).line === true, state(again));

  // ---------- the words ----------
  const text = P.T('page.game.folded');
  check(L('the line has its words in the dictionary of the page, and no [key]'), !!text && text.length > 8 && !KEY.test(text), text);
  check(L('the line is a button of the page that carries its key and the English words as the fallback'), /<button[^>]*id="gameFolded"[^>]*data-i18n="page\.game\.folded"[^>]*>The office is collapsed · Expand<\/button>/.test(html), '');
  check(L('the line is in the language of the page') , lang === 'ko' ? HAN.test(text) : !HAN.test(text), text);
}

(async () => {
  await run('en');
  await run('ko');
  console.log(fails ? `${fails} FAILED` : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})();
