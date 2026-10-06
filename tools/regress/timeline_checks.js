// Checks of the activity timeline's window picker (static/index.html + board.js), in English and Korean, against the first fixture of tools/make_synth_fixture.py.
// No browser, no npm: the fake page of office_fold_checks.js with the elements of the picker, the window width and the saved choices set per page.
//  - the windows: 30m, 2h, this debate and 12h send `since` only (as they always did); 24h, 3d, 7d and All are tabs on a wide page and a box on a narrow one (390 px), the choice is kept,
//    and what is kept in the browser is only believed when it makes sense
//  - a range of your own: two date-time inputs, kept, checked (an end after the start, at most 31 days), sent as `since` and `until`
//  - the steps: ◀ moves the range by its own length into the past, ▶ toward now and stops at now; a range that ends in the past has no "now" line and the live refresh does not move it
//  - the ticks: over a day they carry the date and sit at local midnight and noon; a long range shows the 60 most recently active rows until "show all"; a short one shows all
//  - the page: the picker wraps (nothing is wider than the card) on a phone, in the style sheet
// usage: node timeline_checks.js <static dir> <fixture prefix>      (prints PASS/FAIL per check, exit code 1 if any FAIL; without the fixture it is skipped with a note, unless CI or REQUIRE_FIXTURES is set: then that is a failure)
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx] = process.argv.slice(2);
if (!fs.existsSync(fx + '_state.json')) {
  const need = process.env.CI || process.env.REQUIRE_FIXTURES;       // a missing fixture must not let CI pass without the checks; on a laptop it is only a note
  console.log((need ? 'FAIL' : 'SKIP') + ' timeline checks: no ' + fx + '_state.json (make the fixture with tools/make_synth_fixture.py)');
  process.exit(need ? 1 : 0);
}
const J = n => JSON.parse(fs.readFileSync(fx + n + '.json', 'utf8'));
const STATE = J('_state'), TALK = J('_talk'), ATALK = J('_atalk'), SESSIONS = J('_sessions'), PLANS = J('_plans');
const FIXED = Math.round(STATE.now * 1000) + 5000;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const HAN = /[ㄱ-ㆎ가-힣]/;
const KEY = /\[(?:common|status|kind|time|unit|board|office|demo|diag|page|cli|alert|event|plan)\.[\w.]+\]/i;
let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)))); };
const clone = o => JSON.parse(JSON.stringify(o));
const css = fs.readFileSync(path.join(dir, 'board.css'), 'utf8');

class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(FIXED); } static now() { return FIXED; } }

// A session with many agents: the first one copied, each less recently active than the one before
function crowded(n, units, base = STATE, name = 'Crowd') {
  const S = clone(base), a0 = S.agents[0];
  for (let i = 0; i < n; i++) S.agents.push({ ...clone(a0), id: 'zz-' + name.toLowerCase() + '-' + String(i).padStart(3, '0'), title: name + ' ' + String(i).padStart(3, '0'), tag: '', status: 'done', last_ts: STATE.now - 300 - i * 60,
    spawn_ts: STATE.now - 3600, first_ts: STATE.now - 3600, units: units || [], work_units: units || [], placed: null });      // (what the judgment ties an agent to is where it sits)
  return S;
}
// The agents of the first debate, moved to `hours` ago (a debate that is older than the server's limit for a range that is sent whole)
function older(hours, base = STATE) {
  const S = clone(base);
  S.agents.forEach((a, i) => { a.spawn_ts = a.first_ts = STATE.now - hours * 3600 - i * 60; a.last_ts = STATE.now - (hours - 1) * 3600; });
  return S;
}

function makePage(opts = {}) {
  const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
  const scripts = [...html.matchAll(/<script(?:\s+src="([^"]+)")?\s*>([\s\S]*?)<\/script>/g)].map(m => ({ src: m[1] || '', code: m[1] ? fs.readFileSync(path.join(dir, m[1]), 'utf8') : m[2] }));
  const els = new Map(), log = [];
  const record = (sel, prop, v) => { if (typeof v === 'string' && v) log.push({ sel, prop, v }); };
  function el(sel) {
    if (els.has(sel)) return els.get(sel);
    const e = { sel, hidden: false, disabled: false, value: '', max: '', attrs: {}, dataset: {}, style: { setProperty() {} }, scrollTop: 0, scrollHeight: 0, clientWidth: 900, clientHeight: 300, offsetHeight: 30, offsetWidth: 100,
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); }, toggle() {} },
      querySelectorAll(s) { if (s !== '.tab') return []; const self = this; self.tabs = {};
        return [...this.innerHTML.matchAll(/data-k="([^"]+)"/g)].map(m => ({ dataset: { k: m[1] }, set onclick(f) { self.tabs[m[1]] = f; } })); },
      querySelector(s) { return s === '#tlAll' && this.innerHTML.includes('id="tlAll"') ? el('#tlAll') : null; },
      setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
      getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
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
    if (p === 'api/timeline') {
      const since = +q.get('since'), until = q.get('until') == null ? null : +q.get('until'), end = until == null ? S0.now + 5 : until, span = end - since, binned = span > 13 * 3600;
      let lanes = S0.agents.map(a => ({ id: a.id, spawn_ts: a.spawn_ts, last: a.last_ts, ticks: [[since + span / 3, 'bash', 4], [since + span / 2, 'read']], writes: [], handbacks: [], received: [] }));
      const more = {};
      if (binned) {                                       // as the server does: the 60 most recently active lanes unless all are asked for, and how many there are
        lanes.sort((x, y) => (y.last || 0) - (x.last || 0)); more.lanes_total = lanes.length;
        if (q.get('all') !== '1') lanes = lanes.slice(0, 60);
      }
      lanes.forEach(l => delete l.last);
      return Object.assign({ since, now: S0.now, lanes, orch: [[since + span / 4, 'spawn']] }, until == null ? {} : { until }, binned ? { binned: true, bin: span / 1000 } : {}, more);
    }
    if (p === 'api/plans') return PLANS;
    if (p === 'api/agent') return { id: q.get('id'), orch_msgs: [], handbacks: [], texts: [], writes: [], reads: [], tool_counts: [], activity: [], spawn_ts: S0.now, spawn_prompt: '', link: null, partial: null };
    return {};
  };
  const ctx = { console, Date: FDate, Math, JSON, URL, URLSearchParams, Promise, Set, Map, Object, Array, String, Number, Infinity, isNaN, isFinite, encodeURIComponent, decodeURIComponent, AbortController, Error, RegExp,
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
  P.load = async () => { scripts.forEach(s => { vm.runInContext(s.code, ctx); if (s.src === 'i18n.js') I18B.boot(ctx, dir, opts.lang || 'en'); }); await sleep(120); };
  P.T = (key, params) => P.run(`I18N.has(${JSON.stringify(key)}) ? t(${JSON.stringify(key)}, ${JSON.stringify(params || {})}) : null`);
  P.html = sel => (els.get(sel) || { innerHTML: '' }).innerHTML;
  P.saved = k => P.ctx.localStorage._m['ab.' + k];
  // The timeline requests so far, as { since, until } (until null when there is none)
  P.reqs = () => P.fetched.filter(u => u.includes('api/timeline')).map(u => { const q = new URL('http://x/' + u.replace(/^\//, '')).searchParams; return { since: +q.get('since'), until: q.get('until') == null ? null : +q.get('until'), url: u }; });
  P.last = () => P.reqs().slice(-1)[0];
  P.now = () => P.run('now()');
  P.hm = ts => P.run(`hm(${JSON.stringify(ts)})`);
  P.tabKeys = () => [...P.html('#tlWin').matchAll(/data-k="([^"]+)"/g)].map(m => m[1]);
  P.click = async k => { P.el('#tlWin').tabs[k](); await sleep(40); };
  P.step = async dir => { P.el(dir < 0 ? '#tlPrev' : '#tlNext').listeners.click[0](); await sleep(40); };
  P.ticks = () => [...P.html('#tlBox').matchAll(/font-size="10\.5"[^>]*>([^<]*)<\/text>/g)].map(m => m[1]);
  P.tickXs = () => [...P.html('#tlBox').matchAll(/<text x="([\d.]+)" y="\d+" font-size="10\.5"[^>]*>([^<]*)<\/text>/g)].map(m => [+m[1], m[2]]);
  P.rows = () => [...P.html('#tlBox').matchAll(/font-size="11\.5" fill="var\(--text\)">([^<]*)<\/text>/g)].map(m => m[1]);
  P.setRange = async (from, to) => { P.el('#tlFrom').value = from; P.el('#tlTo').value = to; P.el('#tlFrom').listeners.change[0](); await sleep(40); };
  return P;
}
const inp = ts => { const d = new Date(ts * 1000), p = n => String(n).padStart(2, '0'); return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()); };
const near = (a, b, e = 1) => Math.abs(a - b) <= e;

async function run(lang) {
  const L = s => `${lang}: ${s}`;
  const boot = async (width, saved, state) => { const P = makePage({ lang, width, saved, state }); await P.load(); return P; };
  const W = { '30m': 1800, '2h': 7200, '12h': 43200, '24h': 86400, '3d': 259200, '7d': 604800 };

  // ---------- the windows ----------
  let P = await boot(1200);
  check(L('a wide page has every window as a tab, in order, and no box'), JSON.stringify(P.tabKeys()) === JSON.stringify(['30m', '2h', 'debate', '12h', '24h', '3d', '7d', 'all', 'custom']) && !P.html('#tlWin').includes('tlMore'), P.tabKeys());
  const labels = [...P.html('#tlWin').matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map(m => m[1]);
  const want = ['30m', '2h', 'debate', '12h', '24h', '3d', '7d', 'all', 'custom'].map(k => P.T('board.tl.win.' + k));
  check(L('the tabs carry the words of the dictionary, and no [key]'), JSON.stringify(labels) === JSON.stringify(want) && labels.every(x => x && !KEY.test(x)), [labels, want]);
  check(L('the words are in the language of the page'), lang === 'ko' ? HAN.test(labels.join('')) : !HAN.test(labels.join('')), labels);
  for (const k of ['30m', '2h', '12h', '24h', '3d', '7d']) {
    P = await boot(1200);
    await P.click(k);
    const r = P.last(), n = P.now();
    check(L(`${k}: the request is since now minus ${W[k]} s, and a window that ends now has no until`), r && near(r.since, n - W[k]) && r.until === null && !/until/.test(r.url), r);
    check(L(`${k}: the choice is kept (ab.tlWin) and the tab is on`), P.saved('tlWin') === JSON.stringify(k) && P.html('#tlWin').includes(`class="tab on" data-k="${k}"`), [P.saved('tlWin'), P.html('#tlWin')]);
    check(L(`${k}: the range line says nothing for a window that ends now`), P.html('#tlRange') === '' && P.el('#tlRange').textContent === '', P.el('#tlRange').textContent);
  }
  P = await boot(1200);
  await P.click('all');
  const started = STATE.session.started, ra = P.last();
  check(L('All: since the start of the session (a minute before), no until'), ra && started && near(ra.since, started - 60) && ra.until === null, [ra, started]);
  check(L('All: both step buttons are off'), P.el('#tlPrev').disabled === true && P.el('#tlNext').disabled === true, [P.el('#tlPrev').disabled, P.el('#tlNext').disabled]);
  const before = P.reqs().length;
  await P.step(-1);
  check(L('All: a press of the step button does nothing'), P.reqs().length === before && P.run('ui.tlWin') === 'all', P.reqs().length);
  P = await boot(1200);
  const r0 = P.last();
  check(L('the debate window is asked for as it was (since only)'), r0 && /api\/timeline\?since=\d+(&session=[\w-]+)?$/.test(r0.url) && !/until/.test(r0.url), r0);
  const s0 = [];
  for (const k of ['30m', '2h', '12h']) { await P.click(k); s0.push(P.last().url); }
  check(L('30m, 2h and 12h are asked for as they always were: ?since=<whole seconds> and nothing else'), s0.every(u => /api\/timeline\?since=\d+(&session=[\w-]+)?$/.test(u) && !/until/.test(u)), s0);

  // ---------- the narrow page ----------
  P = await boot(390);
  check(L('a phone (390 px) has the first four windows as tabs and the rest in one box'), JSON.stringify(P.tabKeys()) === JSON.stringify(['30m', '2h', 'debate', '12h']) && P.html('#tlWin').includes('id="tlMore"'), [P.tabKeys(), P.html('#tlWin')]);
  const opts = [...P.html('#tlWin').matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g)].map(m => [m[1], m[2]]);
  check(L('the box holds a prompt and 24h, 3d, 7d, All and Custom with the words of the dictionary'),
    JSON.stringify(opts) === JSON.stringify([['', P.T('board.tl.more')], ...['24h', '3d', '7d', 'all', 'custom'].map(k => [k, P.T('board.tl.win.' + k)])]), opts);
  check(L('the box is the width of its card at most (the style sheet)'), /\.tl-more\s*\{[^}]*max-width:\s*100%/.test(css), '');
  const box = P.el('#tlMore');
  box.value = '7d'; box.onchange(); await sleep(40);
  const rn = P.last(), n1 = P.now();
  check(L('picking 7d in the box asks for seven days and is kept'), near(rn.since, n1 - 604800) && rn.until === null && P.saved('tlWin') === '"7d"', [rn, P.saved('tlWin')]);
  check(L('with a long window on, the box is marked and shows it'), P.html('#tlWin').includes('tl-more on') && /<option value="7d" selected>/.test(P.html('#tlWin')), P.html('#tlWin'));
  P.el('#tlMore').value = ''; P.el('#tlMore').onchange(); await sleep(20);
  check(L('the prompt of the box picks nothing'), P.run('ui.tlWin') === '7d', P.run('ui.tlWin'));
  const again = await boot(390, P.ctx.localStorage._m);
  check(L('a page opened again keeps the long window and shows it in the box'), again.run('ui.tlWin') === '7d' && /<option value="7d" selected>/.test(again.html('#tlWin')), again.html('#tlWin'));
  check(L('the phone and the wide page do not both have the box: width 760 has all tabs'), (await boot(760)).tabKeys().length === 9 && (await boot(759)).tabKeys().length === 4, '');

  // ---------- what the browser kept ----------
  for (const [what, saved] of [['a window this page does not know', { 'ab.tlWin': '"90d"' }], ['a number', { 'ab.tlWin': '7' }], ['no JSON', { 'ab.tlWin': 'x' }], ['null', { 'ab.tlWin': 'null' }],
    ['a picked range with nothing picked', { 'ab.tlWin': '"custom"' }], ['a picked range that runs backward', { 'ab.tlWin': '"custom"', 'ab.tlFrom': '2000', 'ab.tlTo': '1000' }],
    ['a picked range over 31 days', { 'ab.tlWin': '"custom"', 'ab.tlFrom': '1000', 'ab.tlTo': String(1000 + 32 * 86400) }], ['a picked range that is text', { 'ab.tlWin': '"custom"', 'ab.tlFrom': '"a"', 'ab.tlTo': '"b"' }]]) {
    const Q = await boot(1200, saved);
    check(L(`${what}: the page starts on the default window (this debate)`), Q.run('ui.tlWin') === 'debate' && Q.html('#tlWin').includes('class="tab on" data-k="debate"') && !Q.last().url.includes('until'), [Q.run('ui.tlWin'), Q.last()]);
  }
  let Q = await boot(1200, { 'ab.tlWin': '"2h"', 'ab.tlEnd': '"soon"' });
  check(L('a step back that is not a time is dropped'), Q.run('ui.tlEnd') === null && Q.last().until === null, [Q.run('ui.tlEnd'), Q.last()]);
  Q = await boot(1200, { 'ab.tlWin': '"debate"', 'ab.tlEnd': '1000' });
  check(L('a step back kept for a window that has no length is dropped'), Q.run('ui.tlEnd') === null && Q.last().until === null, Q.run('ui.tlEnd'));

  // ---------- a range of your own ----------
  P = await boot(1200);
  check(L('the inputs are hidden until Custom is picked'), P.el('#tlCustom').hidden === true, P.el('#tlCustom').hidden);
  await P.click('custom');
  const n2 = P.now();
  check(L('Custom shows the inputs, set to the last 24 hours, to the minute (local time)'), P.el('#tlCustom').hidden === false && P.el('#tlFrom').value === inp(n2 - 86400) && P.el('#tlTo').value === inp(n2), [P.el('#tlFrom').value, P.el('#tlTo').value]);
  check(L('and asks for it: a range that ends now has no until'), near(P.last().since, n2 - 86400) && P.last().until === null, P.last());
  const base = Math.floor(n2 / 60) * 60 - 3 * 86400, f = base, t1 = base + 9 * 3600 + 15 * 60;       // three days ago, nine hours and a quarter long, to the minute
  await P.setRange(inp(f), inp(t1));
  check(L('a picked range is sent as since and until (the local time of the inputs)'), P.last().since === f && P.last().until === t1, [P.last(), f, t1]);
  check(L('the range is kept (ab.tlFrom, ab.tlTo, ab.tlWin) and the inputs hold it'), P.saved('tlFrom') === String(f) && P.saved('tlTo') === String(t1) && P.saved('tlWin') === '"custom"' && P.el('#tlFrom').value === inp(f), [P.saved('tlFrom'), P.saved('tlTo')]);
  check(L('the range on show is written out, with the dates'), P.el('#tlRange').textContent.includes(P.run(`dayTime(${f})`)) && P.el('#tlRange').textContent.includes(P.run(`dayTime(${t1})`)), P.el('#tlRange').textContent);
  check(L('the picked range ends in the past: no "now" line is drawn'), !P.html('#tlBox').includes('stroke-dasharray="3 3"') && P.html('#tlBox').includes('<svg'), P.html('#tlBox').slice(-200));
  const reopened = await boot(1200, P.ctx.localStorage._m);
  check(L('a page opened again shows the same range, in the inputs and in the request'), reopened.last().since === f && reopened.last().until === t1 && reopened.el('#tlFrom').value === inp(f) && reopened.el('#tlTo').value === inp(t1) && reopened.el('#tlCustom').hidden === false, [reopened.last(), reopened.el('#tlFrom').value]);
  const nreq = P.reqs().length;
  await P.setRange(inp(t1), inp(f));
  check(L('an end before the start is not sent: the hint says so and the inputs are marked'), P.reqs().length === nreq && P.el('#tlHint').textContent === P.T('board.tl.custom.bad') && P.el('#tlFrom').attrs['aria-invalid'] === 'true', [P.el('#tlHint').textContent, P.reqs().length - nreq]);
  check(L('and the range that was kept is the last good one'), P.saved('tlFrom') === String(f) && P.saved('tlTo') === String(t1), [P.saved('tlFrom'), P.saved('tlTo')]);
  await P.setRange(inp(f - 86400 * 31), inp(t1));
  check(L('a range over 31 days is not sent either'), P.reqs().length === nreq && P.el('#tlHint').textContent !== '', P.reqs().length - nreq);
  await P.setRange('', inp(t1));
  check(L('an input that was cleared is not sent'), P.reqs().length === nreq, P.reqs().length - nreq);
  await P.setRange(inp(f - 86400 * 30), inp(t1));
  check(L('thirty days and some hours is within the limit: sent, hint gone'), P.reqs().length === nreq + 1 && P.last().since === f - 86400 * 30 && P.el('#tlHint').textContent === '' && P.el('#tlFrom').attrs['aria-invalid'] === 'false', [P.last(), P.el('#tlHint').textContent]);
  await P.setRange(inp(f), inp(P.now() + 7200));
  check(L('an end in the future is the end of the range now: no until'), P.last().until === null && near(P.last().since, f), P.last());
  check(L('and the end input cannot be put after now (max)'), P.el('#tlTo').max === inp(P.now()), P.el('#tlTo').max);

  // ---------- the steps ----------
  P = await boot(1200);
  await P.click('2h');
  check(L('a window that ends now cannot be stepped forward'), P.el('#tlNext').disabled === true && P.el('#tlPrev').disabled === false, [P.el('#tlPrev').disabled, P.el('#tlNext').disabled]);
  let n3 = P.now();
  await P.step(-1);
  check(L('◀ from 2h: the two hours before, asked for with an until'), near(P.last().since, n3 - 14400) && near(P.last().until, n3 - 7200), [P.last(), n3]);
  check(L('the end of the step is kept (ab.tlEnd), the window is still 2h, the range line shows it, and ▶ is on'), near(+P.saved('tlEnd'), n3 - 7200) && P.saved('tlWin') === '"2h"' && P.el('#tlRange').textContent.includes(' – ') && P.el('#tlNext').disabled === false, [P.saved('tlEnd'), P.el('#tlRange').textContent]);
  check(L('a range that ends in the past draws no "now" line'), !P.html('#tlBox').includes('stroke-dasharray="3 3"'), '');
  await P.step(-1);
  check(L('◀ again: the two hours before that'), near(P.last().since, n3 - 21600) && near(P.last().until, n3 - 14400), P.last());
  await P.step(1);
  check(L('▶: back one step'), near(P.last().since, n3 - 14400) && near(P.last().until, n3 - 7200), P.last());
  await P.step(1);
  check(L('▶ again: the window that ends now, with no until, and ▶ is off'), near(P.last().since, n3 - 7200) && P.last().until === null && P.saved('tlEnd') === 'null' && P.el('#tlNext').disabled === true, [P.last(), P.saved('tlEnd')]);
  await P.step(1);
  check(L('▶ at now stops there'), P.last().until === null && P.run('ui.tlEnd') === null, P.last());
  await P.step(-1); await P.click('12h');
  check(L('picking a window after a step back goes to the window that ends now (the step is forgotten)'), P.last().until === null && near(P.last().since, P.now() - 43200) && P.run('ui.tlEnd') === null && P.saved('tlEnd') === 'null', [P.last(), P.run('ui.tlEnd')]);
  await P.click('2h');
  // the live refresh and a range in the past
  await P.step(-1);
  const reqsBefore = P.reqs().length, sinceBefore = P.run('TL.since');
  P.run('serverSkew += 900'); P.run('loadTimeline()'); await sleep(40); P.run('loadTimeline()'); await sleep(40);
  check(L('the live refresh does not move a range in the past, and does not ask again'), P.reqs().length === reqsBefore && P.run('TL.since') === sinceBefore, [P.reqs().length - reqsBefore, P.run('TL.since'), sinceBefore]);
  check(L('and the clock does not move it either (it ends where the step put it)'), P.run('tlRange().until') === P.run('ui.tlEnd') && near(P.run('tlRange().since'), P.run('ui.tlEnd') - 7200), P.run('JSON.stringify(tlRange())'));
  P = await boot(1200);
  await P.click('30m'); const c1 = P.reqs().length;
  P.run('serverSkew += 900'); P.run('loadTimeline()'); await sleep(40);
  check(L('a window that ends now is moved by the refresh as before'), P.reqs().length === c1 + 1 && near(P.last().since, P.now() - 1800), [P.reqs().length - c1, P.last()]);
  // steps of a range of your own
  P = await boot(1200);
  await P.click('custom'); await P.setRange(inp(f), inp(t1));
  const len = t1 - f;
  await P.step(-1);
  check(L('◀ from a picked range: the same length before it, kept as a picked range'), P.last().since === f - len && P.last().until === f && P.saved('tlFrom') === String(f - len) && P.saved('tlTo') === String(f) && P.run('ui.tlWin') === 'custom', [P.last(), len]);
  check(L('the inputs follow the step'), P.el('#tlFrom').value === inp(f - len) && P.el('#tlTo').value === inp(f), [P.el('#tlFrom').value, P.el('#tlTo').value]);
  await P.step(1); await P.step(1);
  check(L('▶ twice from there: the range after the picked one'), P.last().since === t1 && P.last().until === t1 + len || (P.last().until === null && near(P.last().since, P.now() - len)), P.last());
  const nowTo = P.now();
  await P.setRange(inp(nowTo - 3 * 3600), inp(nowTo - 3600));
  await P.step(1);
  check(L('▶ past now stops at now: the same length, ending now (no until)'), P.last().until === null && near(P.last().since, P.now() - 7200, 60), [P.last(), P.now()]);
  check(L('and ▶ is then off'), P.el('#tlNext').disabled === true, P.el('#tlNext').disabled);
  // the debate window
  P = await boot(1200);
  const d0 = P.run('tlRange()');
  await P.step(-1);
  const len2 = Math.min(P.now() - d0.since, 31 * 86400);
  check(L('◀ from the debate window makes it a picked range: the same length before its start'), P.run('ui.tlWin') === 'custom' && near(P.last().until, d0.since, 60) && near(P.last().since, d0.since - len2, 60) && P.run('ui.tlEnd') === null, [P.last(), d0]);
  // a month
  P = await boot(1200);
  await P.click('custom'); await P.setRange(inp(f - 86400 * 30), inp(t1));
  await P.step(-1);
  check(L('a step of a 30-day range is a 30-day range (within the limit)'), P.last().until - P.last().since <= 31 * 86400 && P.last().until === f - 86400 * 30, P.last());

  // ---------- the ticks ----------
  P = await boot(1200);
  await P.click('12h');
  const OLD = `(() => { const t0 = TL.since, t1 = now(), span = Math.max(60, t1 - t0), c = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600], step = c.find(s => span / s <= 8) || 21600, out = [];
    for (let tt = Math.ceil(t0 / step) * step; tt <= t1; tt += step) out.push(hm(tt)); return out; })()`;       // the ticks as they were drawn before the longer windows
  for (const k of ['30m', '2h', '12h']) {
    await P.click(k);
    const got = P.ticks(), old = P.run(OLD);
    check(L(`${k}: the ticks are exactly the ones the earlier drawing made (steps, places and labels)`), got.length >= 2 && JSON.stringify(got) === JSON.stringify(old), [got, old]);
  }
  await P.click('12h');
  const t12 = P.ticks();
  await P.click('3d');
  const t3 = P.ticks(), n4 = P.now(), tz = new Date(n4 * 1000).getTimezoneOffset();
  check(L('3d: every label has a date and a time, there are at most eight, and they are at local midnight and noon'), t3.length >= 5 && t3.length <= 8 && t3.every(x => /\d/.test(x) && /(00:00|12:00|오전 12:00|오후 12:00|12:00 AM|12:00 PM|0:00)/.test(x)), [t3, tz]);
  const dayPart = t3.map(x => x.replace(/\s*\d{1,2}:\d{2}.*$/, '').trim());
  check(L('3d: the date part is the date of the tick (at least two different days)'), new Set(dayPart).size >= 3 && dayPart.every(x => x.length >= 2), dayPart);
  await P.click('7d');
  const t7 = P.ticks();
  check(L('7d: one label a day at most, each a date (month and day), none with a time of day'), t7.length >= 5 && t7.length <= 8 && t7.every(x => /\d/.test(x) && !/\d:\d\d/.test(x)), t7);
  check(L('7d: the labels are the days in a row'), new Set(t7).size === t7.length, t7);
  await P.click('all');
  const ta = P.ticks();
  check(L('All: still at most eight labels, dates'), ta.length >= 1 && ta.length <= 8 && ta.every(x => /\d/.test(x)), ta);
  const N = await boot(390);
  N.el('#tlBox').clientWidth = 390;
  N.el('#tlMore').value = '7d'; N.el('#tlMore').onchange(); await sleep(40);
  const tn = N.tickXs();
  // How many fall in a week depends on the time zone (the two-day ticks are at the local midnights of the even days, three or four of them in seven days): what is checked is the room each one has,
  // at least the 44 px the page allows a label that is a date only, and that there are some
  const gaps = tn.slice(1).map((x, i) => x[0] - tn[i][0]);
  check(L('a phone: the labels of a week (dates only) have 44 px each at least, whatever the time zone puts in the week'), tn.length >= 2 && tn.every(x => /\d/.test(x[1]) && !/\d:\d\d/.test(x[1])) && gaps.every(g => g >= 44), [tn, gaps]);

  // ---------- the rows ----------
  const many = crowded(80);
  P = await boot(1200, undefined, many);
  await P.click('7d');
  const rows7 = P.rows();
  check(L('a long range with 85 agents shows the orchestrator and the 60 most recently active rows (and asks for no more)'), rows7.length === 61 && !/all=1/.test(P.last().url), [rows7.length, P.last().url]);
  check(L('the rows are the most recently active ones (the quietest agents are not there)'), rows7.includes('Crowd 000') && rows7.includes('Crowd 054') && !rows7.includes('Crowd 079'), rows7.filter(x => x.startsWith('Crowd')).length);
  const note = P.html('#tlBox');
  check(L('the note says how many of how many, and the button offers all of them'), note.includes(P.T('board.tl.capped', { shown: 60, count: 85 })) && note.includes(P.T('board.tl.showAll', { count: 85 })) && note.includes('id="tlAll"'), note.slice(-400));
  const askedBefore = P.reqs().length;
  P.el('#tlAll').onclick(); await sleep(40);
  const rowsAll = P.rows();
  check(L('Show all: asks the server for all of them (all=1), and shows every one of the 85 rows and the orchestrator'), P.reqs().length === askedBefore + 1 && /[?&]all=1/.test(P.last().url) && rowsAll.length === 86, [P.last(), rowsAll.length]);
  check(L('and the button now offers the short list, with no note of how many are not shown'), P.html('#tlBox').includes(P.T('board.tl.showFewer', { shown: 60 })) && !P.html('#tlBox').includes(P.T('board.tl.capped', { shown: 60, count: 85 })), P.html('#tlBox').slice(-300));
  P.el('#tlAll').onclick(); await sleep(40);
  check(L('pressing it again goes back to the 60 without asking (the answer has them all)'), P.rows().length === 61 && P.reqs().length === askedBefore + 1, [P.rows().length, P.reqs().length - askedBefore]);
  P.el('#tlAll').onclick(); await sleep(40);
  check(L('and to all of them again without asking'), P.rows().length === 86 && P.reqs().length === askedBefore + 1, [P.rows().length, P.reqs().length - askedBefore]);
  P.el('#tlAll').onclick(); await sleep(40);
  P.run('loadTimeline()'); await sleep(40);
  check(L('the refresh asks for the short list again (no all=1) when it is the short list that is on show'), !/all=1/.test(P.last().url) && P.rows().length === 61, [P.last().url, P.rows().length]);
  await P.click('3d'); P.el('#tlAll').onclick(); await sleep(40); await P.click('24h');
  check(L('picking another window forgets Show all'), P.run('ui.tlAll') === false && !/all=1/.test(P.last().url), [P.run('ui.tlAll'), P.last().url]);
  await P.click('12h');
  check(L('a window that is not long shows every row and has no button'), P.rows().length === 86 && !P.html('#tlBox').includes('id="tlAll"') && !P.html('#tlBox').includes(P.T('board.tl.showAll', { count: 85 })), P.rows().length);
  await P.click('2h');
  check(L('2h: the same'), P.rows().length === 86 && !P.html('#tlBox').includes('id="tlAll"'), P.rows().length);
  await P.click('7d');
  check(L('a quiet long range (few agents) has no note and no button'), (await (async () => { const Z = await boot(1200); await Z.click('7d'); return !Z.html('#tlBox').includes('id="tlAll"') && Z.rows().length === STATE.agents.length + 1; })()), '');
  check(L('a tick the server put several into carries how many (data-n), and the other marks carry their time and kind'), /data-ts="[\d.]+" data-k="bash" data-n="4"/.test(P.html('#tlBox')) && /data-ts="[\d.]+" data-k="read"\/>/.test(P.html('#tlBox')) && /data-k="ev:spawn"/.test(P.html('#tlBox')), P.html('#tlBox').slice(0, 600));
  // the tip: one set of handlers on the box, the text worked out on the pointer
  const ts0 = P.now() - 3600, mark = (k, extra) => ({ dataset: Object.assign({ ts: String(ts0), k }, extra || {}) });
  const over = m => { P.el('#tlBox').listeners.mouseover[0]({ target: { closest: () => m } }); return P.el('#tip').textContent; };
  check(L('the box has the handlers of the tip (not one set for each mark)'), ['mouseover', 'mousemove', 'mouseout'].every(e => (P.el('#tlBox').listeners[e] || []).length === 1), Object.keys(P.el('#tlBox').listeners || {}));
  check(L('the tip of a tool mark: the time and the kind; with a count: ×n'), over(mark('bash')) === P.hm(ts0) + ' · bash' && over(mark('bash', { n: '4' })) === P.hm(ts0) + ' · bash ×4' && P.el('#tip').style.display === 'block', [over(mark('bash', { n: '4' }))]);
  check(L('the tip of the other marks is in the words of the page'), over(mark('recv')) === P.hm(ts0) + ' · ' + P.T('board.tl.recv') && over(mark('handback')) === P.hm(ts0) + ' · ' + P.T('board.tl.handback')
    && over(mark('saved', { p: '~/x/r1/A.md' })) === P.hm(ts0) + ' · ' + P.T('board.tl.saved', { path: '~/x/r1/A.md' }) && over(mark('ev:spawn')) === P.hm(ts0) + ' · ' + P.T('board.tl.ev.spawn') && over(mark('ev:unknown_kind')) === P.hm(ts0) + ' · unknown_kind', '');
  P.el('#tlBox').listeners.mouseout[0]({ target: { closest: () => mark('bash') }, relatedTarget: null });
  check(L('leaving a mark hides the tip; moving to something inside it does not'), P.el('#tip').style.display === 'none', P.el('#tip').style.display);
  over(mark('bash')); P.el('#tlBox').listeners.mouseout[0]({ target: { closest: () => Object.assign(mark('bash'), { contains: () => true }) }, relatedTarget: {} });
  check(L('a pointer that goes to something inside the mark keeps the tip'), P.el('#tip').style.display === 'block', P.el('#tip').style.display);

  // ---------- this debate when it is older than the server sends whole ----------
  const debateRoot = STATE.debates[0].root, otherWork = '/tmp/other-work';
  const old40 = crowded(70, [otherWork], older(40));                      // the debate's five agents worked 40 hours ago; 70 agents of other work were active since
  P = await boot(1200, undefined, old40);
  check(L('this debate, 40 hours old: the request asks for every lane (all=1), the server would send the 60 most recently active, which are other work'), /[?&]all=1/.test(P.last().url) && P.run('ui.tlWin') === 'debate', P.last());
  const rowsD = P.rows();
  check(L('and the rows are the five of the debate and the orchestrator, none of the other work'), rowsD.length === 6 && !rowsD.some(x => x.startsWith('Crowd')) && STATE.agents.every(a => rowsD.some(r => r.includes((a.tag ? a.tag + ' ' : '') + a.title.slice(0, 8)) || r.startsWith(a.tag))), rowsD);
  check(L('with no note of how many rows there are (five are shown, five are all there is)'), !P.html('#tlBox').includes('id="tlAll"') && !P.html('#tlBox').includes(P.T('board.tl.capped', { shown: 5, count: 75 })), P.html('#tlBox').slice(-200));
  P = await boot(1200);
  check(L('this debate, two hours old: asked for as it always was, with no all=1'), !/all=1/.test(P.last().url), P.last());
  const big = crowded(70, STATE.agents[0].units, older(40), 'Own');          // seventy more agents of the debate itself, and thirty of other work
  const bigger = crowded(30, [otherWork], big, 'Other');
  P = await boot(1200, undefined, bigger);
  check(L('a debate with 75 agents shows the 60 most recently active of them, and the note counts the 75 of the debate (not the 105 of the session)'),
    P.rows().length === 61 && P.html('#tlBox').includes(P.T('board.tl.capped', { shown: 60, count: 75 })) && P.html('#tlBox').includes(P.T('board.tl.showAll', { count: 75 })) && !P.html('#tlBox').includes(P.T('board.tl.capped', { shown: 60, count: 105 })) && !P.html('#tlBox').includes(P.T('board.tl.showAll', { count: 105 })), [P.rows().length, P.html('#tlBox').slice(-300)]);
  P.el('#tlAll').onclick(); await sleep(40);
  check(L('and Show all has the 75 of the debate and asks for nothing more (the answer has all of them)'), P.rows().length === 76 && !/all=1/.test(P.last().url) === false && P.html('#tlBox').includes(P.T('board.tl.showFewer', { shown: 60 })), [P.rows().length, P.last().url]);

  // ---------- the rows of a range are the ones that worked in it (the server sends the lanes in that order: the 30 that worked, then the 70 that were there and did nothing) ----------
  P = await boot(1200);
  const asAgent = (id, last) => ({ id, spawn_ts: STATE.now - 3 * 86400, ticks: [], writes: [], handbacks: [], received: [] });
  const wide = crowded(100, [otherWork]);
  P = await boot(1200, undefined, wide);
  const idsOf = (from, n) => wide.agents.slice(from, from + n).map(a => a.id);
  const quiet = wide.agents.slice(5, 75), working = wide.agents.slice(75, 105);                       // 70 alive across the range with nothing in it, 30 with ticks in it
  const sinceR = STATE.now - 3 * 86400, untilR = STATE.now - 2 * 86400;
  P.run(`ui.tlAll = true; TL = ${JSON.stringify({ since: sinceR, until: untilR, now: STATE.now, binned: true, bin: 86, lanes_total: 100, orch: [],
    lanes: [...working.map((a, i) => ({ id: a.id, spawn_ts: sinceR + 10, ticks: [[sinceR + 1000 + i * 10, 'bash'], [sinceR + 2000 + i, 'read']], writes: [], handbacks: [], received: [] })), ...quiet.map(a => ({ id: a.id, spawn_ts: sinceR - 100, ticks: [], writes: [], handbacks: [], received: [] }))] })}; ui.tlAll = false; ui.tlWin = '7d'; renderTimeline()`);
  const shown = P.rows();
  check(L('a range in the past with 70 agents that did nothing in it (alive, active since) and 30 that worked: the 60 rows hold all 30 that worked'),
    working.every(a => shown.includes(a.title)) && shown.length === 61, [shown.filter(x => x.startsWith('Crowd')).length, shown.length]);

  // The ticks of a long range are the first of their column, so they cannot say which lane worked last: the page keeps the order the server sends (the work in the range, the latest first) when
  // it cuts the lanes to 60, and does not sort them by its own reading of the marks. One agent worked at +100 and +500 s of a column (its tick says +100), sixty at +201...+260 s.
  const solSince = STATE.now - 7 * 86400, sol = crowded(61, [otherWork], STATE, 'Sol');
  const solLane = (a, ticks) => ({ id: a.id, spawn_ts: solSince - 100, ticks, writes: [], handbacks: [], received: [] });
  const solAgents = sol.agents.filter(a => a.title.startsWith('Sol')), solA = solAgents[0], solRest = solAgents.slice(1);
  const solTL = () => JSON.stringify({ since: solSince, now: STATE.now, binned: true, bin: 604.8, lanes_total: 61, orch: [],
    lanes: [solLane(solA, [[solSince + 100, 'bash', 2]]), ...solRest.map((a, i) => solLane(a, [[solSince + 260 - i, 'bash']]))] });          // in the order the server gives them: the latest work first
  P = await boot(1200, undefined, sol);
  P.run(`ui.tlWin = '7d'; ui.tlAll = false; TL = ${solTL()}; renderTimeline()`);
  const solRows = P.rows(), solQuiet = solRest[solRest.length - 1].title;
  check(L('7d, 61 lanes: the 60 kept are the first 60 the server sent, the agent that worked last among them (its tick is the first of its column) and not the one that worked first'),
    solRows.length === 61 && solRows.includes(solA.title) && !solRows.includes(solQuiet), [solRows.length, solRows.includes(solA.title), solRows.includes(solQuiet)]);
  P.el('#tlAll').onclick(); await sleep(20);
  check(L('Show all: all 61'), P.rows().length === 62 && P.rows().includes(solQuiet), P.rows().length);
  P.el('#tlAll').onclick(); await sleep(20);
  check(L('Show fewer: back to the 60 the server would send, with the agent that worked last in them'), P.rows().length === 61 && P.rows().includes(solA.title) && !P.rows().includes(solQuiet), [P.rows().length, P.rows().includes(solA.title)]);
  const solDebate = crowded(61, STATE.agents[0].units, STATE, 'Deb');
  P = await boot(1200, undefined, solDebate);
  const debAgents = solDebate.agents.filter(a => a.title.startsWith('Deb'));
  P.run(`ui.tlWin = 'debate'; ui.tlAll = false; TL = ${JSON.stringify({ since: solSince, now: STATE.now, binned: true, bin: 604.8, lanes_total: 66, orch: [],
    lanes: [solLane(debAgents[0], [[solSince + 100, 'bash', 2]]), ...debAgents.slice(1).map((a, i) => solLane(a, [[solSince + 260 - i, 'bash']])), ...solDebate.agents.slice(0, 5).map(a => solLane(a, []))] })}; renderTimeline()`);
  check(L('this debate, long: the 60 kept of its 66 are the first 60 of the lanes the server sent (the same order), the one that worked last among them'),
    P.rows().length === 61 && P.rows().includes(debAgents[0].title) && !P.rows().includes(debAgents[debAgents.length - 1].title), [P.rows().length, P.rows().includes(debAgents[0].title)]);

  // ---------- the labels over a day, in a card of any width ----------
  const overlaps = P_ => { const t = P_.tickXs(); return t.filter((x, i) => i + 1 < t.length && t[i + 1][0] - x[0] < x[1].length * 6.3 + 3).length; };
  for (const width of [900, 390, 320]) {
    for (const k of ['24h', '3d', '7d', 'custom']) {
      P = await boot(1200);
      P.el('#tlBox').clientWidth = width;
      await P.click(k);
      const tt = P.tickXs();
      check(L(`${k} in a card ${width} px wide: no label over the next one (${tt.length} labels)`), tt.length >= 1 && overlaps(P) === 0, tt);
      if (k === '24h') check(L(`24h in ${width} px: the labels carry the date (the day goes across midnight)`), tt.every(x => /\d/.test(x[1])) && (tt.length < 2 || tt.every(x => P.T('time.dayTime', { date: '', time: '' }) == null || x[1].length > 6)), tt);
    }
  }
  P = await boot(1200); P.el('#tlBox').clientWidth = 900; await P.click('24h');
  check(L('24h on a wide card: eight labels at most, each with a date and a time'), P.ticks().length >= 3 && P.ticks().length <= 8 && P.ticks().every(x => /\d:\d\d/.test(x) && x.replace(/\d{1,2}:\d{2}.*/, '').trim().length >= 2), P.ticks());
  P = await boot(1200); P.el('#tlBox').clientWidth = 900; await P.click('12h');
  check(L('12h: the labels are not given dates by this (only those of other days, as before)'), P.run(OLD) && JSON.stringify(P.ticks()) === JSON.stringify(P.run(OLD)), P.ticks());

  // ---------- an open end goes on with the clock ----------
  P = await boot(1200);
  await P.click('custom');
  check(L('a picked range starts open: no end is kept (ab.tlTo is null)'), P.saved('tlTo') === 'null' && P.run('ui.tlTo') === null, P.saved('tlTo'));
  P.run('serverSkew += 7'); P.run('loadTimeline()'); await sleep(40);
  check(L('seven seconds later the request still has no until, and the range reaches the new now'), P.last().until === null && near(P.last().since, P.now() - 86400 - 7, 2), P.last());
  P.run('serverSkew += 600'); P.run('loadTimeline()'); await sleep(40);
  check(L('ten minutes later too'), P.last().until === null, P.last());
  const sinceBefore2 = P.last().since;
  P.el('#tlFrom').value = inp(sinceBefore2 - 3600); P.el('#tlFrom').listeners.change[0](); await sleep(40);
  check(L('changing only the start leaves the end open (the end input shows the minute it was set)'), P.run('ui.tlTo') === null && P.last().until === null && near(P.last().since, sinceBefore2 - 3600, 60), [P.run('ui.tlTo'), P.last()]);
  P.el('#tlTo').value = inp(P.now() - 7200); P.el('#tlTo').listeners.change[0](); await sleep(40);
  check(L('an end that is picked in the past closes it'), P.run('ui.tlTo') !== null && P.last().until !== null && near(P.last().until, P.now() - 7200, 60), [P.run('ui.tlTo'), P.last()]);
  P.el('#tlTo').value = inp(P.now()); P.el('#tlTo').listeners.change[0](); await sleep(40);
  check(L('an end that is this minute is open again, not a time that is past in a few seconds'), P.run('ui.tlTo') === null && P.last().until === null, [P.run('ui.tlTo'), P.last()]);
  P = await boot(1200);
  await P.click('2h'); await P.step(-1);
  P.run('serverSkew += 5');
  await P.step(1);
  check(L('◀ then ▶ with a few seconds between: back to the window that ends now (open), not frozen a few seconds ago'), P.run('ui.tlEnd') === null && P.last().until === null && P.saved('tlEnd') === 'null', [P.run('ui.tlEnd'), P.last()]);
  P.run('serverSkew += 9'); P.run('loadTimeline()'); await sleep(40);
  check(L('and it goes on with the clock'), near(P.last().since, P.now() - 7200, 2) && P.last().until === null, P.last());
  P = await boot(1200);
  const T0m = Math.floor(P.now() / 60) * 60;
  await P.click('custom'); await P.setRange(inp(T0m - 4 * 3600), inp(T0m - 2 * 3600));
  P.run('serverSkew += 4'); await P.step(1);
  check(L('a picked range stepped forward to within a minute of now (a few seconds after the minute) ends open, keeping its length'), P.run('ui.tlTo') === null && P.last().until === null && near(P.last().since, P.now() - 7200, 2), [P.run('ui.tlTo'), P.last()]);
  P = await boot(1200);
  await P.click('custom'); await P.setRange(inp(T0m - 4 * 3600), inp(T0m - 2 * 3600));
  P.run('serverSkew += 150'); await P.step(1);
  check(L('but one that lands two minutes before now stays a time (it is not snapped)'), P.run('ui.tlTo') !== null && P.last().until !== null && near(P.last().until, T0m, 2), [P.run('ui.tlTo'), P.last()]);
  P = await boot(1200);
  await P.click('2h'); await P.step(-1);
  const nowS = P.now(), reqsS = P.reqs().length;
  P.run('serverSkew += 120'); P.run('loadTimeline()'); await sleep(40); P.run('loadTimeline()'); await sleep(40);
  check(L('a range that ended two hours ago is not asked for again by the refresh (nothing is expected for it)'), P.reqs().length === reqsS, P.reqs().length - reqsS);
  P = await boot(1200);
  await P.click('custom'); await P.setRange(inp(T0m - 3600), inp(T0m - 180));
  const reqsR = P.reqs().length;
  P.run('serverSkew += 60'); P.run('loadTimeline()'); await sleep(40);
  check(L('a range that ended a few minutes ago is asked for again (records come late)'), P.reqs().length === reqsR + 1 && P.last().until !== null, [P.reqs().length - reqsR, P.last()]);
  P.run('serverSkew += 900'); P.run('loadTimeline()'); await sleep(40); const reqsL = P.reqs().length; P.run('loadTimeline()'); await sleep(40);
  check(L('and not any more once it ended a while ago'), P.reqs().length === reqsL, P.reqs().length - reqsL);

  // ---------- what a picked range may be ----------
  P = await boot(1200);
  await P.click('custom');
  const r1 = P.reqs().length, f1 = P.run('ui.tlFrom'), t1o = P.run('ui.tlTo');
  const tomorrow = P.now() + 86400;
  await P.setRange(inp(tomorrow), inp(tomorrow + 86400));
  check(L('a start in the future is an error: not sent, hint and marks, nothing saved'), P.reqs().length === r1 && P.el('#tlHint').textContent === P.T('board.tl.custom.bad') && P.el('#tlFrom').attrs['aria-invalid'] === 'true' && P.run('ui.tlFrom') === f1 && P.run('ui.tlTo') === t1o, [P.reqs().length - r1, P.run('ui.tlFrom')]);
  P.run('serverSkew += 3'); P.run('loadTimeline()'); await sleep(40);
  check(L('and a few seconds later no request has the start after the end'), P.reqs().every(r => r.until === null || r.since < r.until), P.reqs());
  await P.setRange(inp(P.now() - 3600), inp(tomorrow));
  check(L('an end in the future is cut to now first, then the range is checked: an hour up to now is fine'), P.run('ui.tlTo') === null && near(P.last().since, P.now() - 3600, 60) && P.last().until === null, P.last());
  await P.setRange(inp(P.now() + 120), inp(P.now() + 7200));
  check(L('a start a little after now is an error too'), P.el('#tlFrom').attrs['aria-invalid'] === 'true', P.el('#tlFrom').attrs);
  const inputs = P.el('#tlFrom');
  check(L('the hint is a status the screen reader announces, and both inputs point to it'), /id="tlHint" role="status"/.test(fs.readFileSync(path.join(dir, 'index.html'), 'utf8')) && (fs.readFileSync(path.join(dir, 'index.html'), 'utf8').match(/aria-describedby="tlHint"/g) || []).length === 2, '');
  const stored = (extra) => boot(1200, extra);
  for (const [what, saved] of [['a picked range with times Date cannot show', { 'ab.tlWin': '"custom"', 'ab.tlFrom': '1e15', 'ab.tlTo': String(1e15 + 600) }], ['a step back of -1e300', { 'ab.tlWin': '"12h"', 'ab.tlEnd': '-1e300' }],
    ['a step back before the year 2000', { 'ab.tlWin': '"12h"', 'ab.tlEnd': '1000' }], ['a picked range in the year 1970', { 'ab.tlWin': '"custom"', 'ab.tlFrom': '100', 'ab.tlTo': '500' }],
    ['a start that Date can show but is beyond 2100', { 'ab.tlWin': '"custom"', 'ab.tlFrom': '8e12', 'ab.tlTo': '8.0000001e12' }], ['a picked range that is open and starts in the future', { 'ab.tlWin': '"custom"', 'ab.tlFrom': String(STATE.now + 3 * 86400), 'ab.tlTo': 'null' }]]) {
    const Q = await stored(saved);
    const url = Q.last() && Q.last().url;
    check(L(`${what}: the page starts on the default window, the inputs show no NaN, and the request is one the server answers`), Q.run('ui.tlWin') === (what.includes('step back') && saved['ab.tlWin'] === '"12h"' ? '12h' : 'debate') && !/NaN/.test(Q.el('#tlFrom').value + Q.el('#tlTo').value) && Q.last() && isFinite(Q.last().since) && (Q.last().until === null || Q.last().since < Q.last().until), [Q.run('ui.tlWin'), Q.last()]);
  }
  P = await boot(1200);
  P.run('ui.tlWin = "custom"; ui.tlFrom = 1e15; ui.tlTo = 1e15 + 600');
  P.run('loadTimeline()'); await sleep(40);
  check(L('a range worked out from values that do not fit together goes back to the default window (and is saved so)'), P.run('ui.tlWin') === 'debate' && P.run('ui.tlFrom') === null && P.saved('tlWin') === '"debate"' && isFinite(P.last().since) && P.last().until === null, [P.run('ui.tlWin'), P.last()]);
  P.run('ui.tlWin = "12h"; ui.tlEnd = -1e300'); P.run('loadTimeline()'); await sleep(40);
  check(L('and so does a step back that is not a time'), P.last().until === null || P.last().since < P.last().until, P.last());

  // ---------- the box on a phone ----------
  P = await boot(390);
  check(L('the prompt of the box is disabled (it cannot be picked) and is the one shown while a window of the tabs is on'), /<option value="" disabled selected>/.test(P.html('#tlWin')), P.html('#tlWin'));
  P.el('#tlMore').value = '3d'; P.el('#tlMore').onchange(); await sleep(40);
  check(L('with a longer window on, the box shows it and the prompt is not the one selected'), /<option value="" disabled>/.test(P.html('#tlWin')) && /<option value="3d" selected>/.test(P.html('#tlWin')), P.html('#tlWin'));

  // ---------- the words ----------
  for (const k of ['board.tl.more', 'board.tl.capped', 'board.tl.showAll', 'board.tl.showFewer', 'board.tl.custom.bad', 'page.timeline.prev', 'page.timeline.next', 'page.timeline.from', 'page.timeline.to']) {
    const v = P.T(k, { shown: 60, count: 85 });
    check(L(`${k} has words in the dictionary of the page`), !!v && !KEY.test(v) && (lang === 'ko' ? HAN.test(v) : !HAN.test(v)), v);
  }
  const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
  check(L('the picker is in the page with the English words as its fallback'), /id="tlPrev"[^>]*>◀/.test(html) && /id="tlNext"[^>]*>▶/.test(html) && /data-i18n="page\.timeline\.from">From</.test(html) && /data-i18n="page\.timeline\.to">To</.test(html)
    && /type="datetime-local" id="tlFrom"/.test(html) && /type="datetime-local" id="tlTo"/.test(html) && /id="tlCustom" hidden/.test(html), '');
}

(async () => {
  await run('en');
  await run('ko');
  // ---------- the ticks of a day or more, where the clocks change ----------
  // A range that ends in the past (so the page's own clock does not matter) over a change of the clocks, seen from places where it happens: the date ticks are at local midnight, the half-day ones at
  // local midnight and noon, whatever the length of the day between them. The expected ones are counted here on the calendar, apart from the page.
  const midnights = (a, b, noon) => { const out = [], d = new Date(a * 1000); d.setHours(0, 0, 0, 0); for (let i = 0; i < 400 && d.getTime() / 1000 <= b + 86400; i++, d.setDate(d.getDate() + 1), d.setHours(0, 0, 0, 0)) {
    const hs = noon ? [0, 12] : [0]; hs.forEach(h => { const x = new Date(d); x.setHours(h, 0, 0, 0); const t = x.getTime() / 1000; if (t >= a && t <= b) out.push(t); }); } return out; };
  for (const [tz, day] of [['America/Los_Angeles', [2025, 11, 2]], ['America/Los_Angeles', [2025, 3, 9]], ['Europe/Berlin', [2025, 10, 26]], ['Australia/Adelaide', [2025, 4, 6]], ['Pacific/Auckland', [2025, 9, 28]], ['Asia/Kolkata', [2025, 11, 2]], ['UTC', [2025, 11, 2]]]) {
    process.env.TZ = tz;
    for (const lang of ['en', 'ko']) {
      const Pz = makePage({ lang, width: 1200 }); await Pz.load();
      Pz.el('#tlBox').clientWidth = 900;
      const t0 = new Date(day[0], day[1] - 1, day[2] - 3, 9, 15).getTime() / 1000;
      for (const [span, noon] of [[7 * 86400, false], [4 * 86400, true]]) {
        const t1 = t0 + span;
        Pz.run(`ui.tlWin = ${noon ? '"3d"' : '"7d"'}; TL = ${JSON.stringify({ since: t0, until: t1, now: t1, lanes: [], orch: [[t0 + 100, 'spawn']] })}; renderTimeline()`);
        const want = midnights(t0, t1, noon).map(tt => Pz.run(noon ? `dayTime(${tt})` : `monthDay(${tt})`)), got = Pz.ticks();
        check(`${tz} ${lang}: ${noon ? 'half-day' : 'day'} ticks over ${day.join('-')}: local ${noon ? 'midnight and noon' : 'midnight'}, ${want.length} of them, each with its own date`, got.length >= 4 && JSON.stringify(got) === JSON.stringify(want), [got, want]);
      }
      const Pt = Pz.run(`tlLocalTicks(${t0}, ${t0 + 10 * 86400}, 172800)`);
      check(`${tz} ${lang}: two-day ticks are at local midnight`, Pt.length >= 4 && Pt.every(tt => { const d = new Date(tt * 1000); return d.getHours() === 0 && d.getMinutes() === 0; }), Pt.map(tt => new Date(tt * 1000).toString()));
    }
  }
  process.env.TZ = 'UTC';

  // ---------- the page on a phone: the picker wraps ----------
  const rule = sel => { const m = css.match(new RegExp('(?:^|\\n)' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}')); return m ? m[1] : ''; };
  check('the head of the timeline wraps (flex-wrap) and its tabs may shrink', /flex-wrap:\s*wrap/.test(rule('.sec-head')) && /min-width:\s*0/.test(rule('.tl-head .tabs')) && /flex-wrap:\s*wrap/.test(rule('.tabs')), [rule('.sec-head'), rule('.tl-head .tabs')]);
  check('the picked range wraps, its labels and inputs are never wider than the card', /flex-wrap:\s*wrap/.test(rule('.tl-custom')) && /max-width:\s*100%/.test(rule('.tl-custom input')) && /min-width:\s*0/.test(rule('.tl-custom input')) && /max-width:\s*100%/.test(rule('.tl-custom label')), [rule('.tl-custom'), rule('.tl-custom input')]);
  check('the card and the drawing do not scroll sideways (svg width 100%, grid cells may shrink)', /width:\s*100%/.test(rule('.tl svg')) && /min-width:\s*0/.test(css.match(/\.now > \*[^{]*\{([^}]*)\}/)[1]), '');
  check('the step buttons can be disabled and look it', /:disabled/.test(css) && /\.tl-nav \.icon-btn:disabled/.test(css), '');
  console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
  process.exit(fails ? 1 : 0);
})();
