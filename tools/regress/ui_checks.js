// Synthetic checks of static/index.html and static/game.html against one saved fixture. No npm.
//  - Async responses are held and released in reverse order; the page must keep showing what the user picked last.
//  - #agent= hash, Codex plan bar freshness, data-agent escaped, one usd format on both pages.
//  - Server restart (boot change) vs. in-flight talk/event answers and open modals;
//    periodic refreshes (drawer 5 s, timeline 15 s) must wait for their own slow request instead of being dropped.
//  - Agent talk card (#atalk*): sides/kinds/filters, XSS payloads, older-load, modal race, restart, follow/"new" button, hover -> GAME.highlight.
//    Those checks are new: they FAIL on the reference statics (no agent talk card there) by design.
// Run it on the reference statics too: the checks that a fix is meant to change must FAIL there and PASS on the new statics.
// usage: node ui_checks.js <static dir> <fixture prefix>      (exit code = number of failed checks; fixture: tools/make_synth_fixture.py)
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx] = process.argv.slice(2);
const J = n => JSON.parse(fs.readFileSync(fx + n + '.json', 'utf8'));
const STATE = J('_state'), FIXED = Math.round(STATE.now * 1000) + 5000;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + detail)); };

class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(FIXED); } static now() { return FIXED; } }

function makePage(file, opts = {}) {
  const html = fs.readFileSync(path.join(dir, file), 'utf8');
  const scripts = [...html.matchAll(/<script(?:\s+src="([^"]+)")?\s*>([\s\S]*?)<\/script>/g)].map(m => ({ src: m[1] || '', code: m[1] ? fs.readFileSync(path.join(dir, m[1]), 'utf8') : m[2] }));
  const els = new Map();
  function el(sel) {
    if (els.has(sel)) return els.get(sel);
    const e = { sel, innerHTML: '', textContent: '', hidden: false, dataset: {}, style: { setProperty() {} }, scrollTop: 0, scrollHeight: 0, clientWidth: 900, clientHeight: 300, offsetHeight: 30, offsetWidth: 100,
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); }, toggle() {} },
      querySelectorAll(sel) { if (sel !== '.tab') return []; const self = this; self.tabs = {};       // tabBar buttons (data-k): remember the click handlers so a check can press a tab
        return [...this.innerHTML.matchAll(/data-k="([^"]+)"/g)].map(m => ({ dataset: { k: m[1] }, set onclick(f) { self.tabs[m[1]] = f; } })); },
      querySelector: () => null, getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
      addEventListener(t, f) { (this.listeners = this.listeners || {})[t] = (this.listeners[t] || []).concat(f); }, insertAdjacentHTML(p, h) { this.innerHTML += h; }, appendChild() {}, append() {}, remove() {}, closest: () => null, isConnected: true, getContext: () => null };
    els.set(sel, e); return e;
  }
  const P = { els, el, held: [], hold: false, timers: [], fetched: [], i18nHold: opts.i18n === 'fetch', i18nHeld: [], fails: 0, sessions: opts.sessions, plans: opts.plans, down: false, hang: null, status: null };
  // what the (fake) server answers; each answer carries a marker so the test can tell which response ended up on screen
  const route = u => {
    const url = new URL('http://x/' + u.replace(/^\//, '')), q = url.searchParams, p = url.pathname.replace(/^\//, '');
    if (p === 'api/state') return P.stateOverride ? Object.assign({}, STATE, P.stateOverride) : STATE;
    if (p === 'api/sessions') return P.sessions || J('_sessions');
    if (p === 'api/talk' && q.get('scope') === 'agents') return q.get('before') ? { items: [{ idx: 100, kind: 'orch_msg', from: 'orch', to: A.id, ts: STATE.now, title: 'old', text: 'old 100', full_len: 7 }], more: false } : { items: AGENT_ITEMS, more: true };
    if (p === 'api/talk') return q.get('before') ? { items: [{ idx: 100, kind: 'user_say', ts: STATE.now, text: 'old 100', full_len: 6 }, { idx: 101, kind: 'orch_say', ts: STATE.now, text: 'old 101', full_len: 6 }], more: false } : J('_talk');
    if (p === 'api/timeline') return { since: +q.get('since'), lanes: [], orch: [], marker: 'TL-' + q.get('since') };
    if (p === 'api/plans') return P.plans || { now: STATE.now, claude: null, codex: STATE.codex_limit };
    if (p === 'api/agent') return { id: q.get('id'), orch_msgs: [], handbacks: [], texts: [], writes: [], reads: [], tool_counts: [['MARK-' + q.get('id'), 1]], activity: [], spawn_ts: STATE.now, spawn_prompt: '', link: null, partial: null };
    if (p === 'api/file') return { short: 'short-' + q.get('path'), text: 'TEXT-' + q.get('path'), mtime: STATE.now };
    if (p === 'api/event') return { text: 'FULL-' + q.get('idx') };
    return {};
  };
  const HOLD = /api\/(agent|file|event|state|talk|timeline)/;
  const ctx = { console, Date: FDate, Math, JSON, URL, URLSearchParams, Promise, Set, Map, Object, Array, String, Number, Infinity, isNaN, encodeURIComponent, decodeURIComponent, AbortController, Error,
    innerWidth: 1200, innerHeight: 900, scrollY: 0, scrollX: 0, location: { search: opts.search !== undefined ? opts.search : '?session=' + STATE.session.id, hash: opts.hash || '', href: 'http://x/' },
    localStorage: { _m: {}, getItem(k) { return this._m[k] ?? null; }, setItem(k, v) { this._m[k] = String(v); } },
    document: { querySelector: s => el(s), getElementById: s => el('#' + s), querySelectorAll: () => [], addEventListener() {}, createElement: t => el('new:' + t + ':' + els.size), head: el('head'), body: el('body'), hidden: false, title: '', documentElement: el('html'), fonts: null },
    fetch: (u, opt) => new Promise((resolve, reject) => {
      u = String(u); P.fetched.push(u);
      const dict = I18B.route(dir, u);                        // api/i18n and locales/<code>.json come from disk, whatever the data requests do (a server that is down later does not take the loaded dictionary away)
      if (dict) { const ans = () => resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(dict)) }); if (P.i18nHold) { P.i18nHeld.push(ans); return; } return ans(); }
      if (P.down) return reject(new TypeError('Failed to fetch'));                       // server off: fetch itself fails (no HTTP status)
      // connected but never answers: only an abort (the page's own time limit) ends it, like a browser fetch
      if (P.hang && P.hang.test(u)) return opt && opt.signal && opt.signal.addEventListener('abort', () => reject(Object.assign(new Error('The operation was aborted'), { name: 'AbortError' })));
      const bad = P.status && Object.entries(P.status).find(([re]) => new RegExp(re).test(u));   // per-path HTTP errors, e.g. { 'api/state': 404 }
      if (bad) return resolve({ ok: false, status: bad[1], text: async () => bad[1] === 403 ? 'ERR-403 plain\nsecond line' : JSON.stringify({ error: 'ERR-' + bad[1] }) });   // the real 403 body is text/plain, other errors JSON
      if (P.failTalkAgents && /scope=agents/.test(u)) return resolve({ ok: false, status: 400, json: async () => ({}) });
      const answer = () => resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(route(u))) });
      if (!P.hold || !HOLD.test(u)) return answer();
      const h = { url: u, aborted: false };
      h.release = () => { const i = P.held.indexOf(h); if (i >= 0) P.held.splice(i, 1); answer(); };
      if (opt && opt.signal) opt.signal.addEventListener('abort', () => { h.aborted = true; reject(new Error('aborted')); });
      P.held.push(h);
    }),
    setInterval: () => 0, setTimeout: (f, ms) => { const t = { id: P.timers.length + 1, f, ms: ms || 0 }; P.timers.push(t); return t.id; }, clearTimeout: id => { P.timers = P.timers.filter(t => t.id !== id); },
    requestAnimationFrame: () => 0, addEventListener() {}, matchMedia: () => ({ matches: false }), scrollBy() {},
    IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  P.ctx = ctx;
  if (/<select id="langSel"[^>]*\shidden/.test(html)) el('#langSel').hidden = true;          // the fake page is not parsed: what the HTML says about the selector is put in by hand
  P.run = code => vm.runInContext(code, ctx);
  // the dictionary is put in from disk right after i18n.js ran (the page then starts at once); opts.i18n = 'fetch' leaves it to the page's own fetch, which the check can hold and release
  P.load = async () => { scripts.forEach(s => { vm.runInContext(s.code, ctx); if (s.src === 'i18n.js' && opts.i18n !== 'fetch') I18B.boot(ctx, dir); }); await sleep(60); };
  P.releaseI18n = async () => { const hs = P.i18nHeld.splice(0); hs.forEach(f => f()); await sleep(30); };
  P.pending = re => P.held.filter(h => re.test(h.url));
  P.releaseAll = async () => { const hs = P.held.splice(0); hs.forEach(h => h.release()); await sleep(30); };
  // run the timers whose delay is within [minMs, maxMs] (the 30 s guards of held requests are left alone unless asked for)
  P.fire = (minMs, maxMs = Infinity) => { const hit = t => t.ms >= minMs && t.ms <= maxMs; const due = P.timers.filter(hit); P.timers = P.timers.filter(t => !hit(t)); due.forEach(t => t.f()); };
  // the server restarted: the next /api/state answer carries another boot id
  P.reboot = async boot => { P.stateOverride = { boot }; P.run('tick()'); await sleep(10); const st = P.pending(/api\/state/)[0]; if (st) st.release(); await sleep(30); };
  return P;
}

const agents = STATE.agents.filter(a => a.tag).slice(0, 2), A = agents[0], B = agents[1];
const q = s => JSON.stringify(s);
const S0 = i => STATE.agents.filter(a => a.tag)[i].id;
const hmStr = ts => new Date(ts * 1000).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false });
// what /api/talk?scope=agents answers: the six kinds, one of each side (peer = agent -> orch, agent_msg to another agent / to orch, xread)
const I0 = Math.max(0, ...STATE.feed.map(e => e.idx)) + 1000;   // newer than everything in the state feed, so only the checks' own events merge
const AGENT_ITEMS = [
  { idx: I0 + 0, kind: 'spawn', from: 'orch', to: A.id, agent: A.id, ts: STATE.now - 50, title: 'assign A', text: 'SPAWN-TEXT **bold**', full_len: 19 },
  { idx: I0 + 1, kind: 'orch_msg', from: 'orch', to: B.id, agent: B.id, ts: STATE.now - 40, title: '메시지', text: 'ORCHMSG-TEXT', full_len: 12 },
  { idx: I0 + 2, kind: 'handback', from: A.id, to: 'orch', agent: A.id, ts: STATE.now - 30, title: '최종 보고', text: 'HANDBACK-TEXT', full_len: 13 },
  { idx: I0 + 3, kind: 'peer', from: B.id, to: 'orch', agent: B.id, ts: STATE.now - 20, title: '메시지', text: 'PEER-TEXT', full_len: 9 },
  { idx: I0 + 4, kind: 'agent_msg', from: A.id, to: B.id, peer: B.id, agent: A.id, ts: STATE.now - 15, title: '메시지', text: 'AGENTMSG-TEXT', full_len: 13 },
  { idx: I0 + 5, kind: 'agent_msg', from: B.id, to: 'orch', peer: null, agent: B.id, ts: STATE.now - 12, title: '메시지', text: 'AGENTMSG-ORCH-TEXT', full_len: 18 },
  { idx: I0 + 6, kind: 'xread', from: A.id, to: B.id, agent: B.id, ts: STATE.now - 10, title: 'r1/A.md', text: '', full_len: 0 },
];

(async () => {
  // ---------- index.html ----------
  let P = makePage('index.html');
  await P.load();
  P.hold = true;
  const body = () => P.els.get('#dBody') ? P.els.get('#dBody').innerHTML : '';
  const dtitle = () => P.els.get('#dTitle') ? P.els.get('#dTitle').innerHTML : '';

  // 1) drawer A -> B, answers come back B then A: the drawer must show B
  P.run(`openDrawer(${q(A.id)})`); P.run(`openDrawer(${q(B.id)})`);
  await sleep(10);
  const dr = P.pending(/api\/agent/);
  check('drawer: two /api/agent requests held', dr.length === 2, 'held ' + dr.length);
  dr.find(h => h.url.includes(B.id)).release(); await sleep(30);
  dr.find(h => h.url.includes(A.id)).release(); await sleep(30);
  P.held.length = 0;
  check('drawer A->B, reversed answers: body is B', body().includes('MARK-' + B.id) && !body().includes('MARK-' + A.id), 'body has ' + (body().match(/MARK-\w+/) || ['none'])[0]);
  check('drawer A->B, reversed answers: DETAIL is B', P.run('DETAIL && DETAIL.id') === B.id, 'DETAIL.id=' + P.run('DETAIL && DETAIL.id'));

  // 2) drawer closed while the request is out: nothing may come back to life
  P.run('closeDrawer()');
  P.run(`openDrawer(${q(A.id)})`); await sleep(10); P.run('closeDrawer()');
  await P.releaseAll();
  check('drawer closed during request: DETAIL stays empty', P.run('DETAIL') === null, 'DETAIL=' + JSON.stringify(P.run('DETAIL')));
  check('drawer closed during request: drawer stays closed', !P.els.get('#drawer').classList.contains('open'), 'drawer open');

  // 3) B picked but its answer is still out, a state refresh redraws the drawer: no B header over A body
  P.run(`openDrawer(${q(A.id)})`); await P.releaseAll();
  P.run(`openDrawer(${q(B.id)})`); await sleep(10);
  P.run('renderAll()');
  check('drawer switch pending, renderAll: not B header over A body', !(dtitle().includes(esc(B.title)) && body().includes('MARK-' + A.id)), 'mixed header/body');
  await P.releaseAll(); P.run('closeDrawer()');

  // 4) document modal A -> B, reversed
  P.run(`openFile(${q('/x/A.md')})`); P.run(`openFile(${q('/x/B.md')})`); await sleep(10);
  const fl = P.pending(/api\/file/);
  fl.find(h => h.url.includes(encodeURIComponent('/x/B.md'))).release(); await sleep(30);
  fl.find(h => h.url.includes(encodeURIComponent('/x/A.md'))).release(); await sleep(30);
  P.held.length = 0;
  const mb = () => P.els.get('#mBody').innerHTML, ms = () => P.els.get('#mSub').textContent;
  check('file A->B, reversed answers: modal shows B', mb().includes('TEXT-/x/B.md') && !mb().includes('TEXT-/x/A.md') && ms().includes('short-/x/B.md'), 'body=' + mb() + ' sub=' + ms());

  // 5) modal closed during the request
  P.run(`openFile(${q('/x/A.md')})`); await sleep(10);
  P.els.get('#mClose').onclick();
  await P.releaseAll();
  check('file closed during request: modal stays empty and closed', mb() === '' && !P.els.get('#modalBg').classList.contains('open'), 'body=' + mb() + ' open=' + P.els.get('#modalBg').classList.contains('open'));

  // 6) talk message modal (full text is fetched second): A -> B, reversed
  P.run(`ui.talk = [{ idx: 9001, kind: 'user_say', ts: ${STATE.now}, text: 'short A', full_len: 900 }, { idx: 9002, kind: 'user_say', ts: ${STATE.now}, text: 'short B', full_len: 900 }]`);
  P.run('openTalkMsg(9001)'); P.run('openTalkMsg(9002)'); await sleep(10);
  const ev = P.pending(/api\/event/);
  check('talk modal: two /api/event requests held', ev.length === 2, 'held ' + ev.length);
  ev.find(h => h.url.includes('idx=9002')).release(); await sleep(30);
  ev.find(h => h.url.includes('idx=9001')).release(); await sleep(30);
  P.held.length = 0;
  check('talk modal A->B, reversed answers: modal shows B', mb().includes('FULL-9002') && !mb().includes('FULL-9001'), 'body=' + mb());
  P.els.get('#mClose').onclick();

  // 7) "older talk" clicked twice: no duplicated items
  P.run("ui.talk = [{ idx: 500, kind: 'user_say', ts: " + STATE.now + ", text: 'x', full_len: 1 }]; ui.talkLoaded = true");
  P.run('loadTalk(true)'); P.run('loadTalk(true)'); await sleep(10);
  await P.releaseAll();
  const idxs = P.run('ui.talk.map(x => x.idx)').join(',');
  check('older talk requested twice: no duplicates', idxs === '100,101,500', 'ui.talk idx = ' + idxs);

  // 8) timeline window switched twice, answers reversed: the last picked window wins
  P.run("ui.tlWin = '30m'; loadTimeline()"); P.run("ui.tlWin = '12h'; loadTimeline()"); await sleep(10);
  const tl = P.pending(/api\/timeline/);
  check('timeline: two requests held', tl.length === 2, 'held ' + tl.length);
  const since = h => +new URL('http://x/' + h.url).searchParams.get('since');
  const last = tl.reduce((m, h) => since(h) < since(m) ? h : m);   // the 12h window starts earliest
  const first = tl.find(h => h !== last);
  last.release(); await sleep(30); first.release(); await sleep(30);
  P.held.length = 0;
  check('timeline 30m then 12h, reversed answers: 12h data kept', P.run('TL && TL.since') === since(last), 'TL.since=' + P.run('TL && TL.since') + ' want ' + since(last));

  // 9) state polling must not overlap
  P.held.length = 0;
  P.run('tick()'); P.run('tick()'); await sleep(10);
  const st = P.pending(/api\/state/);
  check('tick x2: only one /api/state in flight', st.length === 1, 'in flight ' + st.length);
  await P.releaseAll();
  P.run('tick()'); await sleep(10);
  check('tick after the first finished: next request goes out', P.pending(/api\/state/).length === 1, 'in flight ' + P.pending(/api\/state/).length);
  // 10) a request that never answers is given up on, and polling goes on
  P.fire(30000); await sleep(30);
  check('tick: hung request is aborted', P.held.length === 1 && P.held[0].aborted, 'held ' + P.held.length + ' aborted ' + (P.held[0] && P.held[0].aborted));
  P.held.length = 0;
  P.run('tick()'); await sleep(10);
  check('tick after abort: next request goes out', P.pending(/api\/state/).length === 1, 'in flight ' + P.pending(/api\/state/).length);
  await P.releaseAll();

  // ---------- game.html ----------
  P = makePage('game.html');
  await P.load();
  P.hold = true;
  P.run('tick()'); P.run('tick()'); await sleep(10);
  check('game tick x2: only one /api/state in flight', P.pending(/api\/state/).length === 1, 'in flight ' + P.pending(/api\/state/).length);
  await P.releaseAll();
  P.run('tick()'); await sleep(10);
  P.fire(30000); await sleep(30);
  check('game tick: hung request is aborted', P.held.length === 1 && P.held[0].aborted, 'held ' + P.held.length);
  P.held.length = 0;
  P.run('tick()'); await sleep(10);
  check('game tick after abort: next request goes out', P.pending(/api\/state/).length === 1, 'in flight ' + P.pending(/api\/state/).length);
  await P.releaseAll();

  // ---------- #agent=<id> opens the drawer when the id is in S.agents (Codex UUID included) ----------
  const cx = STATE.agents.find(a => a.provider === 'codex'), cl = STATE.agents.find(a => a.provider !== 'codex');
  for (const [name, id, want] of [['Codex UUID', cx && cx.id, true], ['Claude id', cl && cl.id, true], ['unknown id', 'a0000000000000000', false], ['unknown UUID', '01a0f0c8-0000-7000-8000-000000000000', false], ['broken escape', '%E0%A4%A', false]]) {
    if (!id) { check('hash ' + name + ': fixture has such an agent', false, 'none'); continue; }
    P = makePage('index.html', { hash: '#agent=' + id });
    await P.load();
    const opened = P.run('ui.drawer') !== null && P.els.get('#drawer').classList.contains('open');
    check('hash #agent= ' + name + (want ? ': drawer opens' : ': drawer stays closed'), opened === want, 'ui.drawer=' + P.run('ui.drawer'));
  }
  P = makePage('index.html', { hash: '#agent=' }); await P.load();
  check('hash #agent= (empty): drawer stays closed', P.run('ui.drawer') === null, 'ui.drawer=' + P.run('ui.drawer'));

  // ---------- Codex weekly limit in the bottom bar: freshness first ----------
  P = makePage('index.html'); await P.load();
  const now = STATE.now, bar = () => P.els.get('#planBar').innerHTML;
  const cxBar = x => { P.run('renderPlanBar(' + JSON.stringify({ claude: null, codex: Object.assign({ plan_type: 'prolite', window_minutes: 10080, secondary: null, credits: null, as_of: now - 60 }, x) }) + ')'); return bar(); };
  let b = cxBar({ reached: true, stale: true, resets_at: now - 3600, used_percent: 100 });
  check('plan bar: expired "reached" (stale) shows no limit reached', !b.includes('한도 도달') && b.includes('(추정)'), b.slice(0, 200));
  b = cxBar({ reached: true, stale: false, resets_at: now - 3600, used_percent: 100 });
  check('plan bar: reached but reset time already passed shows no limit reached', !b.includes('한도 도달') && b.includes('(추정)'), b.slice(0, 200));
  b = cxBar({ reached: true, stale: false, resets_at: now + 3600, used_percent: 100 });
  check('plan bar: fresh reached still shows limit reached', b.includes('주간 한도 도달'), b.slice(0, 200));
  b = cxBar({ reached: false, stale: false, resets_at: now + 3600, used_percent: 42 });
  check('plan bar: fresh usage shows the percentage', b.includes('<b>42%</b>') && !b.includes('한도 도달'), b.slice(0, 200));
  b = cxBar({ reached: false, stale: true, resets_at: now - 3600, used_percent: 42 });
  check('plan bar: stale usage shows the estimate', b.includes('주간 <b>—</b>') && b.includes('(추정)'), b.slice(0, 200));

  // ---------- data-agent is escaped ----------
  P = makePage('index.html'); await P.load();
  P.run("ui.oldOpen = true; ui.agentFilter = 'all'; S.agents[0].id = 'x\"><b>'; renderAgents()");
  const al = P.els.get('#agentList').innerHTML;
  check('agent card: data-agent value is escaped', al.includes('data-agent="x&quot;&gt;&lt;b&gt;"') && !al.includes('data-agent="x"><b>"'), (al.match(/data-agent="x[^ ]*/) || ['no card'])[0]);

  // ---------- one usd (dashboard format) on both pages ----------
  P = makePage('index.html'); await P.load();
  const u1 = [P.run('usd(1.5)'), P.run('usd(150)'), P.run('usd(0)'), P.run('usd(1234.5)')].join(' ');
  check('index usd: $1.50 $150 $0.00 $1,235', u1 === '$1.50 $150 $0.00 $1,235', u1);
  P = makePage('game.html'); await P.load();
  const u2 = [P.run('usd(1.5)'), P.run('usd(150)'), P.run('usd(0)'), P.run('usd(1234.5)')].join(' ');
  check('game usd: $1.50 $150 $0.00 $1,235', u2 === '$1.50 $150 $0.00 $1,235', u2);
  const total = [STATE.orch.tokens, ...STATE.agents.map(a => a.tokens)].reduce((sum, t) => sum + ((t && t.cost) || 0), 0);
  check('game cost chip: same text as the dashboard chip', P.els.get('#cost').textContent === '누적 ' + P.run('usd(' + total + ')'), P.els.get('#cost').textContent);

  // ---------- 2-4: tab bars keep their HTML; the session <select> is not rewritten when nothing changed ----------
  P = makePage('index.html'); await P.load();
  P.run(`openDrawer(${q(A.id)})`); await sleep(10);
  const dt = P.els.get('#dTabs').innerHTML;
  check('drawer tabs: same HTML as before', dt.startsWith('<button class="tab on" data-k="overview">개요</button><button class="tab " data-k="activity">활동</button>'), dt.slice(0, 160));
  const sel = P.els.get('#sessionSel'); let writes = 0, cur = sel.innerHTML;
  Object.defineProperty(sel, 'innerHTML', { get: () => cur, set: v => { writes++; cur = v; } });
  await P.run('fillSessions()'); await P.run('fillSessions()');
  check('session select: not rewritten when the HTML is the same', writes === 0, 'writes ' + writes);

  // ---------- server restarted (boot changed) while talk/event requests are out ----------
  const talkItem = (idx, text = 'x', full = text.length) => `{ idx: ${idx}, kind: 'user_say', ts: ${STATE.now}, text: ${q(text)}, full_len: ${full} }`;
  const wantTalk = J('_talk').items.map(x => x.idx).join(',');
  const talkIdx = () => P.run('ui.talk.map(x => x.idx)').join(',');
  const modalOpen = () => P.els.get('#modalBg').classList.contains('open');
  const mBody = () => P.els.get('#mBody').innerHTML;
  for (const [name, start] of [['older-talk request', 'loadTalk(true)'], ['full talk load', 'loadTalk(false)']]) {
    P = makePage('index.html'); await P.load();
    P.hold = true;
    P.run(`ui.talk = [${talkItem(500)}]; ui.talkLoaded = true`);
    P.run(start); await sleep(10);
    const oldReq = P.pending(/api\/talk/)[0];
    await P.reboot('RESTARTED-' + name);
    check(`boot change (${name} out): talk cleared, reload scheduled`, P.run('ui.talkLoaded') === false && P.run('ui.talk.length') === 0, 'talkLoaded=' + P.run('ui.talkLoaded') + ' len=' + P.run('ui.talk.length'));
    oldReq.release(); await sleep(30);                           // the old answer comes back BEFORE the reload timer fired
    check(`boot change (${name} out): answer released before the timer is dropped`, P.run('ui.talk.length') === 0 && P.run('ui.talkLoaded') === false, 'ui.talk idx = ' + talkIdx().slice(0, 60));
    P.fire(0, 1000); await sleep(10);                            // the timer that starts loadTalk(false)
    const fresh = P.pending(/api\/talk/)[0];
    check(`boot change (${name} out): fresh talk request went out`, !!fresh, 'none');
    if (fresh) { fresh.release(); await sleep(30); }
    check(`boot change (${name} out): talk is the fresh answer only`, talkIdx() === wantTalk && P.run('ui.talkLoaded') === true, 'ui.talk idx = ' + talkIdx().slice(0, 60));
    await P.releaseAll();
  }
  // the old answer arriving after the fresh one must not win either
  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run(`ui.talk = [${talkItem(500)}]; ui.talkLoaded = true`); P.run('loadTalk(true)'); await sleep(10);
  const older2 = P.pending(/api\/talk/)[0];
  await P.reboot('RESTARTED-late'); P.fire(0, 1000); await sleep(10);
  const fresh2 = P.pending(/api\/talk/).find(h => h !== older2);
  if (fresh2) { fresh2.release(); await sleep(30); }
  older2.release(); await sleep(30);
  check('boot change: late answer after the fresh one is dropped', talkIdx() === wantTalk, 'ui.talk idx = ' + talkIdx().slice(0, 60));
  await P.releaseAll();

  // talk message modal with its full-text request out
  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run(`ui.talk = [${talkItem(9001, 'short A', 900)}]`);
  P.run('openTalkMsg(9001)'); await sleep(10);
  const evReq = P.pending(/api\/event/)[0];
  check('talk modal (restart case): open with the request out', modalOpen() && !!evReq, 'open=' + modalOpen());
  await P.reboot('RESTARTED-modal');
  check('boot change: talk message modal is closed', !modalOpen(), 'still open, body=' + mBody().slice(0, 60));
  evReq.release(); await sleep(30);
  check('boot change: old event answer does not reach the modal', !mBody().includes('FULL-9001') && !modalOpen(), 'body=' + mBody().slice(0, 60) + ' open=' + modalOpen());
  P.fire(0, 1000); await sleep(10);
  const fresh3 = P.pending(/api\/talk/)[0]; if (fresh3) { fresh3.release(); await sleep(30); }
  check('boot change: after the fresh talk arrived the old modal body is gone', !mBody().includes('short A') && !mBody().includes('FULL-9001') && !modalOpen(), 'body=' + mBody().slice(0, 60));
  await P.releaseAll();

  // whole-conversation modal, already loaded, gets closed too (its messages carry the old numbers)
  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run(`ui.talk = [${talkItem(9001, 'short A')}, ${talkItem(9002, 'short B')}]`);
  P.els.get('#talkBig').onclick(); await sleep(10);
  check('talk-big modal (restart case): open', modalOpen() && mBody().includes('data-idx="9001"'), 'body=' + mBody().slice(0, 80));
  await P.reboot('RESTARTED-big');
  check('boot change: whole-conversation modal is closed', !modalOpen(), 'still open');
  await P.releaseAll();

  // a document modal is opened by path, not by event number: it stays, and its answer still arrives
  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run(`openFile(${q('/x/A.md')})`); await sleep(10);
  await P.reboot('RESTARTED-file');
  check('boot change: document modal stays open', modalOpen(), 'closed');
  await P.releaseAll();
  check('boot change: document answer that was out still shows', mBody().includes('TEXT-/x/A.md') && modalOpen(), 'body=' + mBody().slice(0, 60) + ' open=' + modalOpen());
  await P.releaseAll();

  // ---------- periodic refreshes wait for their own slow request ----------
  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run(`openDrawer(${q(A.id)})`); await sleep(10);
  P.run('refreshDrawer()'); P.run('refreshDrawer()'); await sleep(10);          // two 5-second periods pass, the answer is still out
  const dr2 = P.pending(/api\/agent/);
  check('drawer periodic refresh: one request in flight while it is slow', dr2.length === 1, 'in flight ' + dr2.length);
  if (dr2[0]) dr2[0].release(); await sleep(30);
  check('drawer periodic refresh: the slow answer is applied', P.run('DETAIL && DETAIL.id') === A.id && body().includes('MARK-' + A.id), 'DETAIL.id=' + P.run('DETAIL && DETAIL.id'));
  P.run('refreshDrawer()'); await sleep(10);
  check('drawer periodic refresh: next period sends again', P.pending(/api\/agent/).length === 1, 'in flight ' + P.pending(/api\/agent/).length);
  P.fire(30000); await sleep(30);
  check('drawer periodic refresh: hung request is aborted', P.held.length >= 1 && P.held.every(h => h.aborted), 'held ' + P.held.length);
  P.held.length = 0;
  P.run('refreshDrawer()'); await sleep(10);
  check('drawer periodic refresh: next period after the abort sends', P.pending(/api\/agent/).length === 1, 'in flight ' + P.pending(/api\/agent/).length);
  // opening another agent still supersedes the slow one
  P.run(`openDrawer(${q(B.id)})`); await sleep(10);
  const dr3 = P.pending(/api\/agent/);
  check('drawer: another agent is requested while the first is slow', dr3.length === 2, 'in flight ' + dr3.length);
  dr3.find(h => h.url.includes(B.id)).release(); await sleep(30);
  dr3.find(h => h.url.includes(A.id)).release(); await sleep(30);
  check('drawer: the newer agent wins after reopening', P.run('DETAIL && DETAIL.id') === B.id && body().includes('MARK-' + B.id) && !body().includes('MARK-' + A.id), 'DETAIL.id=' + P.run('DETAIL && DETAIL.id'));
  await P.releaseAll();

  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run("ui.tlWin = '2h'"); P.run('loadTimeline()'); P.run('loadTimeline()'); await sleep(10);   // the 15-second period fires again while the answer is out
  const tl2 = P.pending(/api\/timeline/);
  check('timeline periodic refresh: one request in flight while it is slow', tl2.length === 1, 'in flight ' + tl2.length);
  if (tl2[0]) tl2[0].release(); await sleep(30);
  check('timeline periodic refresh: the slow answer is applied', tl2[0] && P.run('TL && TL.since') === since(tl2[0]), 'TL.since=' + P.run('TL && TL.since'));
  P.run('loadTimeline()'); await sleep(10);
  check('timeline periodic refresh: next period sends again', P.pending(/api\/timeline/).length === 1, 'in flight ' + P.pending(/api\/timeline/).length);
  P.fire(30000); await sleep(30);
  check('timeline periodic refresh: hung request is aborted', P.held.length >= 1 && P.held.every(h => h.aborted), 'held ' + P.held.length);
  P.held.length = 0;
  P.run('loadTimeline()'); await sleep(10);
  check('timeline periodic refresh: next period after the abort sends', P.pending(/api\/timeline/).length === 1, 'in flight ' + P.pending(/api\/timeline/).length);
  await P.releaseAll();

  // ---------- no empty class attribute on the timeline dots ----------
  P = makePage('index.html'); await P.load();
  P.run(`TL = { since: ${STATE.now - 7200}, lanes: [{ id: ${q(A.id)}, spawn_ts: ${STATE.now - 3600}, ticks: [], writes: [], handbacks: [] }], orch: [] }; ui.tlWin = '2h'; renderTimeline()`);
  const svg = P.els.get('#tlBox').innerHTML;
  check('timeline dots carry no empty class attribute', svg.includes('<circle') && !svg.includes('class=""'), (svg.match(/<circle[^>]*>/) || ['no circle'])[0]);

  // ---------- agent talk card ----------
  P = makePage('index.html'); await P.load();
  if (P.run('typeof ui.atalk') === 'undefined') {     // reference statics: no such card. One clean FAIL instead of a crash
    check('atalk: the agent talk card exists (reference statics do not have it)', false, 'ui.atalk is undefined');
    console.log('FAILED ' + fails); process.exit(fails ? 1 : 0);
  }
  const atalkHtml_ = () => P.els.get('#atalkList').innerHTML;
  const nameA = A.tag || A.title;
  P = makePage('index.html'); await P.load();
  check('atalk: first load asks /api/talk?scope=agents&limit=80', P.fetched.some(u => /api\/talk\?scope=agents&limit=80/.test(u)), P.fetched.filter(u => /talk/.test(u)).join(' '));
  check('atalk: loaded items are shown', P.run('ui.atalkLoaded') === true && P.run('ui.atalk.length') === AGENT_ITEMS.length, 'loaded=' + P.run('ui.atalkLoaded') + ' n=' + P.run('ui.atalk.length'));
  let h = atalkHtml_();
  const cls = idx => (h.match(new RegExp(`<div class="msg (\\w+)" data-idx="${idx}"`)) || [])[1];
  check('atalk: spawn / orch_msg (orch -> agent) sit on the orch side', cls(I0 + 0) === 'orch' && cls(I0 + 1) === 'orch', `${cls(I0 + 0)} ${cls(I0 + 1)}`);
  check('atalk: handback / peer / agent_msg -> orch sit on the agent side', cls(I0 + 2) === 'agent' && cls(I0 + 3) === 'agent' && cls(I0 + 5) === 'agent', `${cls(I0 + 2)} ${cls(I0 + 3)} ${cls(I0 + 5)}`);
  check('atalk: agent_msg to another agent sits in the middle (peer)', cls(I0 + 4) === 'peer', String(cls(I0 + 4)));
  check('atalk: xread is one dim line without a bubble', /<div class="xr" data-hl="[^"]*">/.test(h) && !h.includes(`data-idx="${I0 + 6}"`) && h.includes('r1/A.md를 읽음') && h.includes(esc(nameA)), (h.match(/<div class="xr".{0,200}/) || ['none'])[0]);
  check('atalk: kind labels 작업 배정 / 메시지 / 최종 보고', h.includes('>작업 배정<') && h.includes('>메시지<') && h.includes('>최종 보고<'), 'labels missing');
  check('atalk: body is escaped text, clamp class only (no html from the record)', h.includes('SPAWN-TEXT bold') && !h.includes('<b>bold'), 'body=' + (h.match(/SPAWN-TEXT[^<]*/) || ['none'])[0]);
  check('atalk: hover ids carry sender and receiver (not the user)', h.includes(`data-idx="${I0}" data-hl="orch,${A.id}"`) && h.includes(`data-idx="${I0 + 4}" data-hl="${A.id},${B.id}"`), (h.match(new RegExp(`data-idx="${I0}"[^>]*`)) || ['none'])[0]);
  check('atalk: header time is the last conversation (xread is not one)', P.els.get('#atalkTime').textContent.includes(hmStr(STATE.now - 12)), 'time=' + P.els.get('#atalkTime').textContent);
  check('atalk: older-conversation button while more', h.includes('data-old="1"'), 'no button');
  const tabs = P.els.get('#atalkTabs').innerHTML;
  check('atalk: three filter tabs, 전체 on', tabs.includes('>전체<') && tabs.includes('>오케↔에이전트<') && tabs.includes('>에이전트끼리<') && /tab on" data-k="all"/.test(tabs), tabs.slice(0, 120));
  // filters
  const shownIdx = f => { P.run(`ui.atalkFilter = ${q(f)}; renderAtalk({ force: true })`); return [...atalkHtml_().matchAll(/data-idx="(\d+)"/g)].map(m => +m[1]).join(',') + (atalkHtml_().includes('class="xr"') ? '+xr' : ''); };
  check('atalk filter 오케↔에이전트: orch-side and agent-side only', shownIdx('orch') === [0, 1, 2, 3, 5].map(n => I0 + n).join(','), shownIdx('orch'));
  check('atalk filter 에이전트끼리: agent_msg to an agent and xread', shownIdx('peer') === (I0 + 4) + '+xr', shownIdx('peer'));
  check('atalk filter 전체: everything', shownIdx('all') === [0, 1, 2, 3, 4, 5].map(n => I0 + n).join(',') + '+xr', shownIdx('all'));
  P.run("renderAtalk({ force: true })"); P.els.get('#atalkTabs').tabs.peer();
  check('atalk filter: pressing a tab filters and is remembered', P.run('ui.atalkFilter') === 'peer' && P.run("localStorage.getItem('ab.atalkFilter')") === '"peer"' && !atalkHtml_().includes(`data-idx="${I0}"`), String(P.run("localStorage.getItem('ab.atalkFilter')")));
  P.els.get('#atalkTabs').tabs.all();
  check('atalk filter: 전체 again shows everything', P.run('ui.atalkFilter') === 'all' && atalkHtml_().includes(`data-idx="${I0}"`), P.run('ui.atalkFilter'));

  // XSS: payload in text, title (xread), the agent's own name, and ids
  const PAY = '<img src=x onerror=window.__xss=1>', QPAY = '"><script>window.__xss=2</script>';
  P.run(`S.agents[0].tag = ${q(PAY)}; S.agents[1].title = ${q(PAY)}; S.agents[1].tag = ''`);
  P.run(`ui.atalk = [{ idx: 300, kind: 'orch_msg', from: 'orch', to: ${q(S0(0))}, ts: ${STATE.now}, title: ${q(PAY)}, text: ${q(PAY + '\n**b**\n```\n' + PAY + '\n```')}, full_len: 10 },
    { idx: 301, kind: 'xread', from: ${q(S0(0))}, to: ${q(S0(1))}, ts: ${STATE.now}, title: ${q(PAY)}, text: '', full_len: 0 },
    { idx: 302, kind: 'agent_msg', from: ${q(QPAY)}, to: ${q(QPAY)}, peer: 'z', ts: ${STATE.now}, title: ${q(PAY)}, text: ${q(PAY)}, full_len: 10 }]; renderAtalk({ force: true })`);
  h = atalkHtml_();
  check('atalk XSS: no raw tag from text / title / names / ids in the card', !/<img|<script/i.test(h) && h.includes('&lt;img'), (h.match(/<(img|script)[^>]*>/i) || ['none'])[0]);
  check('atalk XSS: attribute values cannot be broken out of', !h.includes('"><script') && !h.includes('data-hl=""><'), (h.match(/data-hl="[^>]{0,60}/) || ['none'])[0]);
  P.run('openAtalkMsg(300)'); await sleep(10);
  check('atalk XSS: modal body is escaped too', !/<img|<script/i.test(mBody()) && mBody().includes('&lt;img'), mBody().slice(0, 100));
  P.els.get('#mClose').onclick();
  P.run('ui.modalKind = null'); P.run("ui.atalkFilter = 'all'");
  P.run(`S.agents[0].tag = ${q(A.tag)}; S.agents[1].title = ${q(B.title)}; S.agents[1].tag = ${q(B.tag)}`);

  // "older" clicked twice: no duplicates, oldest first, one request per click but only the newest is used
  P = makePage('index.html'); await P.load();
  P.run('loadAtalk(true)'); P.run('loadAtalk(true)'); await sleep(10);
  await P.releaseAll();
  const aidx = () => P.run('ui.atalk.map(x => x.idx)').join(',');
  check('atalk older x2: no duplicated items, older ones in front', aidx() === '100,' + AGENT_ITEMS.map(x => x.idx).join(','), aidx());
  check('atalk older: request carries before=<first idx>', P.fetched.some(u => u.includes(`scope=agents&limit=80&before=${I0}`)), P.fetched.filter(u => /before/.test(u)).join(' '));
  check('atalk older: more flag from the answer (no more button)', P.run('ui.atalkMore') === false && !atalkHtml_().includes('data-old'), 'more=' + P.run('ui.atalkMore'));

  // merge: only the six kinds, only newer than the last loaded one, nothing before the first load
  P = makePage('index.html'); await P.load();
  P.run(`S.feed = S.feed.concat([{ idx: ${I0 + 900}, kind: 'handback', from: ${q(A.id)}, to: 'orch', ts: ${STATE.now}, title: '최종 보고', text: 'NEW-HB', full_len: 6 }, { idx: ${I0 + 901}, kind: 'notify', from: ${q(A.id)}, to: 'orch', ts: ${STATE.now}, title: '완료', text: 'NOT-A-TALK', full_len: 10 }, { idx: ${I0 + 902}, kind: 'user_say', from: 'user', to: 'orch', ts: ${STATE.now}, title: '지시', text: 'USER-SAY', full_len: 8 }]); renderAll()`);
  check('atalk merge: new handback appended, notify / user_say are not', aidx() === AGENT_ITEMS.map(x => x.idx).join(',') + ',' + (I0 + 900) && atalkHtml_().includes('NEW-HB') && !atalkHtml_().includes('NOT-A-TALK') && !atalkHtml_().includes('USER-SAY'), aidx());
  P.run('renderAll()'); P.run('renderAll()');
  check('atalk merge: repeated renders add nothing twice', aidx() === AGENT_ITEMS.map(x => x.idx).join(',') + ',' + (I0 + 900), aidx());
  P = makePage('index.html'); await P.load(); P.run('ui.atalkLoaded = false; ui.atalk = []; ui.atalkHtml = ""');
  P.run(`S.feed = S.feed.concat([{ idx: ${I0 + 900}, kind: 'handback', from: ${q(A.id)}, to: 'orch', ts: ${STATE.now}, title: '최종 보고', text: 'EARLY', full_len: 5 }]); renderAll()`);
  check('atalk merge: nothing is merged before the first load arrived', P.run('ui.atalk.length') === 0 && !atalkHtml_().includes('EARLY'), 'n=' + P.run('ui.atalk.length'));
  
  // modal: full text of a cut item comes second; reversed answers -> the last opened wins
  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run(`ui.atalk = [{ idx: 9001, kind: 'handback', from: ${q(A.id)}, to: 'orch', ts: ${STATE.now}, title: '최종 보고', text: 'short A', full_len: 900 }, { idx: 9002, kind: 'spawn', from: 'orch', to: ${q(B.id)}, agent: ${q(B.id)}, ts: ${STATE.now}, title: 'assign', text: 'short B', full_len: 900 }, { idx: 9003, kind: 'spawn', from: 'orch', to: ${q(B.id)}, agent: ${q(B.id)}, ts: ${STATE.now}, title: 'TITLE-ONLY', text: '', full_len: 0 }]`);
  P.run('openAtalkMsg(9001)'); P.run('openAtalkMsg(9002)'); await sleep(10);
  const aev = P.pending(/api\/event/);
  check('atalk modal: title is sender -> receiver · kind', P.els.get('#mTitle').textContent.includes('→') && P.els.get('#mTitle').textContent.includes('작업 배정'), P.els.get('#mTitle').textContent);
  check('atalk modal: two /api/event requests held', aev.length === 2, 'held ' + aev.length);
  aev.find(x => x.url.includes('idx=9002')).release(); await sleep(30);
  aev.find(x => x.url.includes('idx=9001')).release(); await sleep(30);
  check('atalk modal A->B reversed: modal shows B only', mBody().includes('FULL-9002') && !mBody().includes('FULL-9001'), mBody());
  P.run('openAtalkMsg(9003)'); await sleep(10);
  check('atalk modal: spawn without a body falls back to its description', mBody().includes('TITLE-ONLY') && P.pending(/api\/event/).length === 0, mBody());
  P.run(`openAtalkMsg(${I0 + 6})`); // xread has no body: nothing to open
  check('atalk modal: xread does not open a modal', mBody().includes('TITLE-ONLY'), mBody());
  P.els.get('#mClose').onclick(); await P.releaseAll();

  // wide window = the filtered list, names in it open the drawer (and close the modal)
  P = makePage('index.html'); await P.load();
  P.run("ui.atalkFilter = 'peer'"); P.els.get('#atalkBig').onclick(); await sleep(10);
  check('atalk wide window: filtered items with full bodies', modalOpen() && mBody().includes(`data-idx="${I0 + 4}"`) && !mBody().includes(`data-idx="${I0}"`) && mBody().includes('class="mt full md"') && P.els.get('#mSub').textContent.includes('에이전트끼리'), mBody().slice(0, 80) + ' | ' + P.els.get('#mSub').textContent);
  P.els.get('#mClose').onclick();

  // restart while the older-load is out: cleared, old answer dropped, fresh scope=agents request, talk modal closed
  P = makePage('index.html'); await P.load(); P.hold = true;
  P.run('loadAtalk(true)'); await sleep(10);
  const oldA = P.pending(/scope=agents/)[0];
  await P.reboot('RESTARTED-atalk');
  check('atalk boot change: cleared, reload scheduled', P.run('ui.atalkLoaded') === false && P.run('ui.atalk.length') === 0 && atalkHtml_().includes('불러오는 중'), 'loaded=' + P.run('ui.atalkLoaded') + ' n=' + P.run('ui.atalk.length'));
  oldA.release(); await sleep(30);
  check('atalk boot change: the old answer is dropped', P.run('ui.atalk.length') === 0 && P.run('ui.atalkLoaded') === false, 'n=' + P.run('ui.atalk.length'));
  P.fire(0, 1000); await sleep(10);
  const freshA = P.pending(/scope=agents/)[0];
  check('atalk boot change: a fresh scope=agents request went out', !!freshA && !/before=/.test(freshA.url), freshA ? freshA.url : 'none');
  if (freshA) { freshA.release(); await sleep(30); }
  check('atalk boot change: fresh items only', aidx() === AGENT_ITEMS.map(x => x.idx).join(','), aidx());
  await P.releaseAll();
  P.run(`ui.atalk = [${JSON.stringify(AGENT_ITEMS[2])}]`); P.run(`openAtalkMsg(${I0 + 2})`); await sleep(10);
  await P.reboot('RESTARTED-atalk-modal');
  check('atalk boot change: an open item modal is closed', !modalOpen(), 'still open');
  await P.releaseAll();

  // load failure: says so (not "no conversation"), retries by itself
  P = makePage('index.html'); P.failTalkAgents = true; await P.load();
  check('atalk load failure: error text, not "no conversation yet"', P.run('ui.atalkErr') === true && atalkHtml_().includes('불러오지 못했습니다') && !atalkHtml_().includes('아직 없습니다'), atalkHtml_().slice(0, 80));

  // follow: at the bottom -> keep following; scrolled up -> keep the place and show "새 대화 ↓"
  P = makePage('index.html'); await P.load();
  const box = P.els.get('#atalkList'), nb = P.els.get('#atalkNew');
  box.scrollHeight = 1000; box.clientHeight = 300;
  const addOne = async n => { const idx = I0 + n; P.run(`S.feed = S.feed.concat([{ idx: ${idx}, kind: 'peer', from: ${q(A.id)}, to: 'orch', ts: ${STATE.now}, title: '메시지', text: 'M${idx}', full_len: 4 }]); renderAll()`); await sleep(5); };
  P.run('ui.atalkStick = true'); await addOne(910);
  check('atalk follow: at the bottom -> stays at the bottom, no "new" button', box.scrollTop === 1000 && nb.hidden === true, `top=${box.scrollTop} hidden=${nb.hidden}`);
  P.run('ui.atalkStick = false'); box.scrollTop = 120; await addOne(911);
  check('atalk follow: scrolled up -> place kept, "새 대화 ↓" shown', box.scrollTop === 120 && nb.hidden === false && P.run('ui.atalkStick') === false, `top=${box.scrollTop} hidden=${nb.hidden}`);
  nb.onclick();
  check('atalk follow: clicking "새 대화 ↓" goes to the bottom and hides it', box.scrollTop === 1000 && nb.hidden === true && P.run('ui.atalkStick') === true, `top=${box.scrollTop} hidden=${nb.hidden}`);
  P.run('ui.atalkStick = false'); box.scrollTop = 120; nb.hidden = true;
  P.run(`S.agents[0].tag = 'renamed-A'; renderAll()`);
  check('atalk follow: a name change while reading up keeps the place and shows no "new"', box.scrollTop === 120 && nb.hidden === true, `top=${box.scrollTop} hidden=${nb.hidden}`);
  P.run('ui.atalkFilter = "orch"; ui.atalkStick = false; renderAtalk({ force: true })'); P.run('ui.atalkFilter = "all"');
  box.scrollTop = 120; nb.hidden = true; await addOne(912);
  P.run('ui.atalkFilter = "peer"; renderAtalk({ force: true })'); box.scrollTop = 120; nb.hidden = true; P.run('ui.atalkStick = false'); await addOne(913);
  check('atalk follow: a new item hidden by the filter does not raise "new"', box.scrollTop === 120 && nb.hidden === true, `top=${box.scrollTop} hidden=${nb.hidden}`);

  // hover -> office highlight (GAME.highlight), never during a demo
  P = makePage('index.html'); await P.load();
  P.run('window.__hl = []; GAME = { demoOn: false, highlight: ids => window.__hl.push(ids) }');
  const L = P.els.get('#atalkList').listeners || {};
  const over = hl => L.mouseover && L.mouseover[0]({ target: { closest: () => hl ? { dataset: { hl } } : null } });
  over(`orch,${A.id}`);
  check('atalk hover: mouseover on an item highlights [sender, receiver]', JSON.stringify(P.run('window.__hl[window.__hl.length - 1]')) === JSON.stringify(['orch', A.id]), JSON.stringify(P.run('window.__hl')));
  over(null);
  check('atalk hover: pointer off an item clears it', P.run('window.__hl[window.__hl.length - 1]') === null, JSON.stringify(P.run('window.__hl')));
  P.run('window.__hl = []'); L.mouseleave && L.mouseleave[0]();
  check('atalk hover: leaving the list clears it', JSON.stringify(P.run('window.__hl')) === '[null]', JSON.stringify(P.run('window.__hl')));
  P.run('window.__hl = []; GAME.demoOn = true'); over(`orch,${A.id}`);
  check('atalk hover: no highlight while the demo plays', P.run('window.__hl.length') === 0, JSON.stringify(P.run('window.__hl')));
  P.run('GAME = null'); over(`orch,${A.id}`);
  check('atalk hover: no office (folded before first draw) is fine', true, '');

  // ---------- release: first screen (nothing to open), solo conversations, plan bar with the usage API off, bundled fonts ----------
  // Like the atalk checks above these are new: they FAIL on the reference statics by design.
  {
  const diagEl = P_ => P_.els.get('#diag') || { hidden: true, innerHTML: '' }, diagHtml_ = P_ => diagEl(P_) ? diagEl(P_).innerHTML : '';
  const nodata = P_ => P_.els.get('body').classList.contains('nodata');
  const stateReqs = P_ => P_.fetched.filter(u => /api\/state/.test(u)).length, sessReqs = P_ => P_.fetched.filter(u => /api\/sessions/.test(u)).length;
  const poll5 = P_ => P_.timers.filter(t => t.ms === 5000).length;
  const SRC = (over = {}) => [Object.assign({ provider: 'claude', dir: '~/.claude', from: 'default', exists: false, sessions: 0, agent_sessions: 0 }, over.claude),
    Object.assign({ provider: 'codex', dir: '~/.codex', from: 'default', exists: false, sessions: 0, agent_sessions: null }, over.codex)];
  const EMPTY = over => ({ default: '', sessions: [], projects: [], sources: SRC(over) });

  // 1) fresh HOME: no /api/state (no 404), a diagnostic card instead of "서버 응답 없음 … grep 8790"
  P = makePage('index.html', { search: '', sessions: EMPTY() });
  P.status = { 'api/state': 404 };
  await P.load();
  check('first screen: diagnostic card is shown, other panels hidden by the nodata class', diagEl(P).hidden === false && nodata(P), 'hidden=' + diagEl(P).hidden + ' nodata=' + nodata(P));
  check('first screen: /api/state is not requested when there is nothing to open (no 404 in the console)', stateReqs(P) === 0, P.fetched.join(' '));
  check('first screen: each provider row has folder, origin, and count', ['Claude Code', '~/.claude/projects', '기본 위치', '폴더 없음', '◆ Codex', '~/.codex/sessions'].every(t => diagHtml_(P).includes(t)), diagHtml_(P).slice(0, 300));
  check('first screen: one line for "no folder" and a hint for --claude-config-dir / --codex-home', diagHtml_(P).includes('폴더가 없습니다') && diagHtml_(P).includes('--claude-config-dir') && diagHtml_(P).includes('--codex-home'), diagHtml_(P).slice(0, 300));
  check('first screen: no stale "서버 응답 없음 … 8790" text', !/8790|서버 응답 없음/.test(diagHtml_(P) + ((P.els.get('#summary') || {}).innerHTML || '') + P.els.get('#updated').innerHTML), P.els.get('#updated').innerHTML);
  check('first screen: header says it is waiting (not the red dead dot)', P.els.get('#updated').innerHTML.includes('세션 기다리는 중') && !P.els.get('#updated').innerHTML.includes('dead'), P.els.get('#updated').innerHTML);
  check('first screen: a 5 s recheck is scheduled (setTimeout chain, not the 3 s tick)', poll5(P) === 1, 'timers ' + poll5(P));
  P.run('tick()'); await sleep(20);
  check('first screen: the 3 s tick leaves the card alone (only the 5 s recheck asks)', sessReqs(P) === 1 && stateReqs(P) === 0, `sessions ${sessReqs(P)} state ${stateReqs(P)}`);
  P.fire(5000, 5000); await sleep(30);
  check('first screen: 5 s later /api/sessions is asked again, still empty -> card stays, next recheck scheduled', sessReqs(P) === 2 && stateReqs(P) === 0 && !diagEl(P).hidden && poll5(P) === 1, `sessions ${sessReqs(P)} state ${stateReqs(P)} timers ${poll5(P)}`);
  // a session appears: the page opens it by itself (SESSION from the list's default) and the card goes away
  P.sessions = J('_sessions'); P.status = null;
  P.fire(5000, 5000); await sleep(60);
  check('first screen: a session appears -> opened automatically with ?session=<default>', stateReqs(P) === 1 && P.fetched.some(u => /api\/state\?.*session=/.test(u) && u.includes(encodeURIComponent(P.sessions.default))) && P.run('!!S'), P.fetched.slice(-3).join(' '));
  check('first screen: card hidden and nodata cleared once the session opens', diagEl(P).hidden === true && !nodata(P) && poll5(P) === 0 && P.els.get('#updated').innerHTML.includes('실시간'), `hidden=${diagEl(P).hidden} nodata=${nodata(P)} timers ${poll5(P)} ${P.els.get('#updated').innerHTML}`);

  // 2) per-case hints: folder exists but no session, the origin of the folder (flag / env), Codex counts only the last 7 days
  P = makePage('index.html', { search: '', sessions: EMPTY({ claude: { exists: true, from: 'flag', dir: '~/work/cl' }, codex: { exists: true, from: 'env', dir: '~/cx' } }) });
  await P.load();
  check('first screen: folder exists but 0 sessions -> "세션 0" and the hint', diagHtml_(P).includes('세션 0') && diagHtml_(P).includes('폴더는 있지만 세션이 없습니다') && diagHtml_(P).includes('최근 7일 세션 0'), diagHtml_(P).slice(0, 400));
  check('first screen: origin is spelled out (flag / env with the variable name)', diagHtml_(P).includes('명령 인자 <code>--claude-config-dir</code>') && diagHtml_(P).includes('환경변수 <code>CODEX_HOME</code>') && diagHtml_(P).includes('~/work/cl/projects'), diagHtml_(P).slice(0, 400));
  const PX = makePage('index.html', { search: '', sessions: EMPTY({ claude: { dir: '<img src=x onerror=1>' } }) }); await PX.load();
  check('first screen: dir text is escaped', !diagHtml_(PX).includes('<img') && diagHtml_(PX).includes('&lt;img'), diagHtml_(PX).slice(0, 200));
  P = makePage('index.html', { search: '', sessions: Object.assign(EMPTY(), { sources: undefined }) });
  await P.load();
  check('first screen: an older server without sources still shows a card', !diagEl(P).hidden && diagHtml_(P).includes('아직 열 세션이 없습니다') && diagHtml_(P).includes('--claude-config-dir'), diagHtml_(P).slice(0, 200));

  // 3) real connection failure is told apart (server off) and keeps retrying; it opens by itself when the server is back
  P = makePage('index.html', { search: '', sessions: J('_sessions') });
  P.down = true; await P.load();
  check('connection failure: its own card ("서버에 연결할 수 없습니다"), not the "no session" one', diagHtml_(P).includes('서버에 연결할 수 없습니다') && !diagHtml_(P).includes('아직 열 세션이 없습니다') && !diagHtml_(P).includes('폴더 없음') && diagEl(P).hidden === false, diagHtml_(P).slice(0, 200));
  check('connection failure: header shows the dead dot and "서버 응답 없음"', P.els.get('#updated').innerHTML.includes('서버 응답 없음') && P.els.get('#updated').innerHTML.includes('dead'), P.els.get('#updated').innerHTML);
  check('connection failure: the text does not hard-code a port', !/8790|ss -ltnp/.test(diagHtml_(P)), diagHtml_(P));
  P.down = false; P.fire(5000, 5000); await sleep(60);
  check('connection failure: server is back -> the page opens by itself', diagEl(P).hidden === true && P.run('!!S') && !nodata(P), `hidden=${diagEl(P).hidden}`);
  P = makePage('index.html', { search: '', sessions: J('_sessions') });
  P.status = { 'api/sessions': 403 }; await P.load();
  check('server refuses (403, text/plain body): "서버가 요청을 받지 않았습니다 (403)" shows the server\'s text in the page language (a body has one line per language, the last is Korean; it carries the fix)', diagHtml_(P).includes('(403)') && diagHtml_(P).includes('second line') && !diagHtml_(P).includes('ERR-403 plain') && diagHtml_(P).includes('dg-detail'), diagHtml_(P).slice(0, 300));
  P = makePage('index.html', { search: '', sessions: J('_sessions') });
  P.status = { 'api/sessions': 500 }; await P.load();
  check('server error (500, JSON body): the "error" text is shown, no --allow-host hint', diagHtml_(P).includes('(500)') && diagHtml_(P).includes('ERR-500') && !diagHtml_(P).includes('--allow-host'), diagHtml_(P).slice(0, 300));

  // 3b) the server accepts the connection but never answers /api/sessions: the page gives up after 10 s, says so, and keeps asking every 5 s
  const guard10 = P_ => P_.timers.filter(t => t.ms === 10000).length;
  P = makePage('index.html', { search: '', sessions: J('_sessions') });
  P.hang = /api\/sessions/; await P.load();
  check('no answer: while waiting, nothing is drawn and the 10 s limit is armed', diagHtml_(P) === '' && guard10(P) === 1 && sessReqs(P) === 1 && stateReqs(P) === 0, `html=${diagHtml_(P).length} guard=${guard10(P)} sessions ${sessReqs(P)}`);
  P.run('tick()'); await sleep(20);
  check('no answer: the 3 s tick does not pile up a second request', sessReqs(P) === 1, 'sessions ' + sessReqs(P));
  P.fire(10000, 10000); await sleep(30);
  check('no answer: after 10 s a "서버가 응답하지 않습니다" card (not the empty-folders one), header "서버 응답 없음"', diagHtml_(P).includes('서버가 응답하지 않습니다') && diagHtml_(P).includes('10초') && !diagHtml_(P).includes('폴더 없음') && nodata(P) && (P.els.get('#updated') || { innerHTML: '' }).innerHTML.includes('서버 응답 없음') && (P.els.get('#updated') || { innerHTML: '' }).innerHTML.includes('dead'), diagHtml_(P).slice(0, 200) + ' | ' + (P.els.get('#updated') || { innerHTML: '' }).innerHTML);
  check('no answer: the 5 s retry is scheduled and the 10 s limit is cleared', poll5(P) === 1 && guard10(P) === 0, `poll ${poll5(P)} guard ${guard10(P)}`);
  P.fire(5000, 5000); await sleep(20);
  check('no answer: the retry asks again (still hanging -> its own 10 s limit)', sessReqs(P) === 2 && guard10(P) === 1 && stateReqs(P) === 0, `sessions ${sessReqs(P)} guard ${guard10(P)}`);
  P.fire(10000, 10000); await sleep(30);
  check('no answer: still hanging -> the card stays and the next 5 s retry is scheduled', diagHtml_(P).includes('서버가 응답하지 않습니다') && poll5(P) === 1, `poll ${poll5(P)}`);
  P.hang = null; P.fire(5000, 5000); await sleep(60);
  check('no answer: the server answers again -> the page opens by itself', stateReqs(P) === 1 && diagEl(P).hidden === true && !nodata(P) && P.run('!!S') && guard10(P) === 0, `state ${stateReqs(P)} hidden=${diagEl(P).hidden} guard ${guard10(P)}`);
  P = makePage('index.html'); await P.load();
  check('a normal answer clears the 10 s limit (no timer left behind)', guard10(P) === 0, 'guard ' + guard10(P));
  const GH = makePage('game.html', { search: '', sessions: J('_sessions') });
  GH.hang = /api\/sessions/; await GH.load();
  GH.fire(10000, 10000); await sleep(30);
  check('/game no answer: same card after 10 s, 5 s retry scheduled, no /api/state request', diagHtml_(GH).includes('서버가 응답하지 않습니다') && poll5(GH) === 1 && stateReqs(GH) === 0 && (GH.els.get('#upd') || { textContent: '' }).textContent.includes('서버 응답 없음'), diagHtml_(GH).slice(0, 120));
  GH.hang = null; GH.fire(5000, 5000); await sleep(60);
  check('/game no answer: the server answers again -> opened, card hidden', stateReqs(GH) === 1 && diagEl(GH).hidden === true && GH.run('!!S'), `state ${stateReqs(GH)} hidden=${diagEl(GH).hidden}`);

  // 4) a link to a session the server does not have: its own card, no repeated /api/state while it stays missing
  P = makePage('index.html', { search: '?session=deadbeef-0000-4000-8000-000000000000', sessions: J('_sessions') });
  P.status = { 'api/state': 404 }; await P.load();
  check('missing session: card names the session and offers the default session', diagHtml_(P).includes('세션을 열지 못했습니다') && diagHtml_(P).includes('deadbeef-0000') && diagHtml_(P).includes('href="?"'), diagHtml_(P).slice(0, 300));
  P.fire(5000, 5000); await sleep(40);
  check('missing session: the 5 s recheck does not ask /api/state again while the list lacks it', stateReqs(P) === 1 && poll5(P) === 1, `state ${stateReqs(P)} timers ${poll5(P)}`);

  // 5) once a session was shown, a later outage is still the old "연결 끊김" label (no diagnostic card)
  P = makePage('index.html'); await P.load();
  P.down = true; await P.run('tick()'); await sleep(20);
  check('later outage keeps the "연결 끊김" label and no diagnostic card', P.els.get('#updated').innerHTML.includes('연결 끊김') && diagEl(P).innerHTML === '' && !nodata(P), P.els.get('#updated').innerHTML);

  // 6) /game: same card, same 5 s recheck
  let G = makePage('game.html', { search: '', sessions: EMPTY() });
  G.status = { 'api/state': 404 }; await G.load();
  check('/game first screen: diagnostic card, no /api/state request, old red error hidden', !diagEl(G).hidden && diagHtml_(G).includes('폴더 없음') && stateReqs(G) === 0 && (!G.els.get('#err') || G.els.get('#err').style.display !== 'block'), `hidden=${diagEl(G).hidden} state ${stateReqs(G)}`);
  check('/game first screen: 5 s recheck scheduled', poll5(G) === 1, 'timers ' + poll5(G));
  G.sessions = J('_sessions'); G.status = null; G.fire(5000, 5000); await sleep(60);
  check('/game first screen: a session appears -> opened, card hidden', stateReqs(G) === 1 && diagEl(G).hidden === true && G.run('!!S'), `state ${stateReqs(G)} hidden=${diagEl(G).hidden}`);
  G = makePage('game.html', { search: '', sessions: J('_sessions') });
  G.down = true; await G.load();
  check('/game connection failure: its own card', diagHtml_(G).includes('서버에 연결할 수 없습니다') && !diagHtml_(G).includes('폴더 없음'), diagHtml_(G).slice(0, 160));

  // 7) solo sessions (Claude conversations without sub-agents) are grouped apart
  const SESS0 = J('_sessions'), mkSolo = (id, proj, title, age) => ({ id, project: '-x-' + proj, proj, mtime: STATE.now - age, agents: 0, agents_mtime: 0, provider: 'claude', active: 0, solo: true, title, last: STATE.now - age, rep: false });
  const cur = SESS0.sessions.find(x => x.id === STATE.session.id), SOLO_ONLY = [mkSolo('50100001-0000-4000-8000-000000000001', 'chatdir', 'solo chat A', 100), mkSolo('50100001-0000-4000-8000-000000000002', 'chatdir', 'solo chat B', 900)];
  const SOLO_IN = mkSolo('50100002-0000-4000-8000-000000000003', cur.proj, 'solo beside agents', 50);
  SOLO_ONLY[0].rep = true;
  const SESS_SOLO = Object.assign({}, SESS0, { sessions: SESS0.sessions.concat(SOLO_ONLY, [SOLO_IN]), projects: SESS0.projects.concat([{ name: 'chatdir', rep: SOLO_ONLY[0].id, n: 2, last: STATE.now - 100 }]) });
  const selHtml_ = P_ => P_.els.get('#sessionSel').innerHTML;
  P = makePage('index.html'); await P.load();
  check('no solo sessions: the session select has no groups (same as before)', !selHtml_(P).includes('optgroup') && !selHtml_(P).includes('대화 ·'), selHtml_(P).slice(0, 200));
  P = makePage('index.html', { sessions: SESS_SOLO }); await P.load();
  check('solo: select has an orchestration group and a "일반 대화" group', selHtml_(P).includes('<optgroup label="오케스트레이션">') && selHtml_(P).includes('<optgroup label="일반 대화 · 서브에이전트 없음">'), selHtml_(P).slice(0, 200));
  check('solo: solo-only project is in the 일반 대화 group, marked "대화 ·", with the server\'s n', /일반 대화[^]*<option value="50100001-0000-4000-8000-000000000001" >대화 · chatdir \(2\) · solo chat A<\/option><\/optgroup>$/.test(selHtml_(P)), selHtml_(P).slice(-200));
  check('solo: orchestration projects keep their option text; the open one stays selected', !/오케스트레이션">[^]*대화 ·[^]*<\/optgroup><optgroup/.test(selHtml_(P)) && selHtml_(P).includes(`value="${cur.id}" selected`), selHtml_(P).slice(0, 300));
  check('solo: other-session button counts orchestrations and conversations apart', P.els.get('#orchCard').innerHTML.includes('다른 대화 1 ▾') && !P.els.get('#orchCard').innerHTML.includes('다른 세션'), (P.els.get('#orchCard').innerHTML.match(/other-btn[^]*?<\/button>/) || ['no button'])[0]);
  const qs0 = P.ctx.document.querySelector; P.ctx.document.querySelector = s => s === '#otherMenu' ? null : qs0(s);   // the fake DOM always finds #otherMenu; here no menu is open yet
  P.run("openOtherMenu({ getBoundingClientRect: () => ({ left: 0, bottom: 0 }) })");
  const menu = [...P.els.entries()].filter(([k]) => k.startsWith('new:div')).map(([, e]) => e.innerHTML).find(h => h.includes('mp-head')) || '';
  check('solo: the other-session menu lists conversations in their own section after the orchestrations', menu.includes('같은 프로젝트의 다른 세션 1개') && menu.includes('일반 대화(서브에이전트 없음) 1개') && menu.includes(`?session=${SOLO_IN.id}`) && menu.includes('<span class="mp-m">대화 · ') && !menu.includes('에이전트 0'), menu.slice(0, 400));
  // open a solo session: orchestrator-only board, nothing breaks
  P = makePage('index.html', { search: '?session=' + SOLO_ONLY[0].id, sessions: SESS_SOLO });
  P.stateOverride = { agents: [], debates: [], feed: [], alerts: [], session: Object.assign({}, STATE.session, { id: SOLO_ONLY[0].id, title: 'solo chat A' }) };
  await P.load();
  check('solo session opens: board renders with no agents (no exception, live label)', P.run('S.agents.length') === 0 && P.els.get('#updated').innerHTML.includes('실시간') && P.els.get('#orchCard').innerHTML.includes('서브에이전트 없는 일반 대화') && P.els.get('#tokCard').innerHTML.includes('토큰 사용량'), P.els.get('#updated').innerHTML);
  check('solo session opens: the orchestrator card says it is a conversation without sub-agents, the select shows it selected under 일반 대화', P.els.get('#orchCard').innerHTML.includes('서브에이전트 없는 일반 대화') && !P.els.get('#orchCard').innerHTML.includes('진행 중인 서브에이전트 없음') && selHtml_(P).includes(`value="${SOLO_ONLY[0].id}" selected`), selHtml_(P).slice(-220));
  check('solo session opens: the empty debate area says why instead of "보고서 경로를 찾지 못했습니다"', P.els.get('#topics').innerHTML.includes('서브에이전트 없이 나눈 일반 대화입니다') && !P.els.get('#topics').innerHTML.includes('찾지 못했습니다'), P.els.get('#topics').innerHTML.slice(0, 160));
  const Q = makePage('index.html'); await Q.load();
  check('orchestration session: no "일반 대화" wording anywhere on the orchestrator card', !Q.els.get('#orchCard').innerHTML.includes('일반 대화') && Q.els.get('#orchCard').innerHTML.includes('서브에이전트'), Q.els.get('#orchCard').innerHTML.slice(0, 200));
  check('solo session opens: no "목록 밖 세션" option (it is in the list now)', !selHtml_(P).includes('목록 밖 세션'), selHtml_(P).slice(0, 120));

  // 8) plan bar with the usage API off (usage_api === false): gray cache label, never the yellow error
  const bar = c => { P.run('renderPlanBar(' + JSON.stringify({ now: STATE.now, claude: c, codex: null }) + ')'); return P.els.get('#planBar').innerHTML; };
  const W = (pct, d) => ({ percent: pct, resets_at: STATE.now + d });
  P = makePage('index.html'); await P.load();
  const base = { source: 'cache', error: null, plan: 'Max 5x', scoped: [], extra: null, hits: {} };
  let h = bar(Object.assign({}, base, { usage_api: false, as_of: STATE.now - 3000, five_hour: W(12, 3600), seven_day: W(40, 86400 * 3) }));
  check('plan bar, usage API off + cache: gray "기록 HH:MM · /usage로 갱신" always (not only when stale)', h.includes('class="faint"') && h.includes('기록 ' + hmStr(STATE.now - 3000) + ' · /usage로 갱신') && h.includes('5시간') && h.includes('12%'), h);
  h = bar(Object.assign({}, base, { usage_api: false, error: '조회 실패(HTTP 429)', as_of: STATE.now - 3000, five_hour: W(12, 3600), seven_day: W(40, 86400 * 3) }));
  check('plan bar, usage API off: no yellow error even if the server sent one', !h.includes('class="warn"') && !h.includes('조회 실패'), h);
  h = bar(Object.assign({}, base, { usage_api: false, plan: '', as_of: null, five_hour: null, seven_day: null }));
  check('plan bar, usage API off + no cache: one gray line, no dashes or error', h.includes('Claude Code에서 /usage를 열면 보입니다') && !h.includes('<b>—</b>') && !h.includes('class="warn"') && !h.includes('기록 '), h);
  h = bar(Object.assign({}, base, { usage_api: false, as_of: null, five_hour: null, seven_day: null, hits: { seven_day: { status: 'rejected', resets_at: STATE.now + 5000 } } }));
  check('plan bar, usage API off + no cache but a recorded limit hit: still shows the hit', h.includes('주간 한도 도달') && !h.includes('/usage를 열면 보입니다'), h);
  const live = { source: 'api', error: null, plan: 'Max 5x', scoped: [], extra: null, hits: {}, usage_api: true, as_of: STATE.now - 30, five_hour: W(12, 3600), seven_day: W(40, 86400 * 3) };
  h = bar(live);
  check('plan bar, usage API on: unchanged ("조회 HH:MM")', h.includes('조회 ' + hmStr(STATE.now - 30)) && !h.includes('/usage로 갱신'), h);
  h = bar(Object.assign({}, live, { source: 'cache', error: '조회 실패(HTTP 500)' }));
  check('plan bar, usage API on: the error is still shown (unchanged)', h.includes('class="warn"') && h.includes('조회 실패(HTTP 500)'), h);
  h = bar({ source: 'cache', error: null, plan: 'Pro', scoped: [], extra: null, hits: {}, as_of: STATE.now - 3000, five_hour: W(12, 3600), seven_day: W(40, 86400 * 3) });
  check('plan bar, no usage_api field (older server): same as usage API on', h.includes('기록 ' + hmStr(STATE.now - 3000)) && !h.includes('/usage로 갱신'), h);

  // ~/.claude.json missing -> the server sends claude: null. Only people who have Claude sessions get the one-line hint
  const withSrc = n => Object.assign({}, J('_sessions'), { sources: SRC({ claude: { exists: true, sessions: n }, codex: { exists: true } }) });
  P = makePage('index.html', { sessions: withSrc(5) }); await P.load();
  h = bar(null);
  check('plan bar, claude: null but Claude sessions exist: the gray "/usage를 열면 보입니다" line', h.includes('Claude Code에서 /usage를 열면 보입니다') && !h.includes('class="warn"'), h);
  P = makePage('index.html', { sessions: withSrc(0) }); await P.load();
  h = bar(null);
  check('plan bar, claude: null and no Claude sessions: no Claude segment (as before)', !h.includes('Claude Code') && h.includes('요금제·사용 한도 정보 없음'), h);

  // 9) monitor symbols and the legend (no look-alike of the vendors' logos)
  P = makePage('index.html'); await P.load();
  check('monitor symbols: 9x9, orange ">_" for Claude Code and white diamond for Codex', P.run(`(() => { const L = AgentGameArt.LOGO; return L.claude.map.length === 9 && L.codex.map.length === 9 && [...L.claude.map, ...L.codex.map].every(r => r.length === 9) && L.codex.map[4] === 'xxxxxxxxx' && L.codex.map[0] === '....x....' && L.claude.map[7] === 'xx..xxxxx' && L.claude.col === '#e8845f' && L.codex.col === '#f2f4f8'; })()`) === true, '');
  const leg = P.run('AgentGame.legendHtml(true)');
  check('legend: names the new symbols, no "불꽃"/"매듭"', leg.includes('모니터 주황 &gt;_ = Claude Code') && leg.includes('모니터 흰 ◆·안경 = Codex') && !/불꽃|매듭/.test(leg), leg.slice(-160));

  // 10) bundled fonts: no external request, the faces the pages use, the licence text
  const stat = f => path.join(dir, f), rd = f => fs.readFileSync(stat(f), 'utf8');
  const pages = ['index.html', 'game.html', 'board.css', 'fonts/galmuri.css', 'common.js', 'board.js', 'game.js', 'game-art.js', 'game-demo.js'].concat(['i18n.js', 'locales/en.json', 'locales/ko.json'].filter(f => fs.existsSync(stat(f))));
  check('fonts: no page, stylesheet or script names an external http(s) URL to load', pages.every(f => !/(?:href|src|url\()\s*=?\s*["']?https?:\/\//.test(rd(f))), pages.filter(f => /(?:href|src|url\()\s*=?\s*["']?https?:\/\//.test(rd(f))).join(','));
  check('fonts: both pages link fonts/galmuri.css (relative, so a path prefix keeps working)', ['index.html', 'game.html'].every(f => rd(f).includes('<link rel="stylesheet" href="fonts/galmuri.css">')), '');
  const faces = [...rd('fonts/galmuri.css').matchAll(/font-family:\s*(\w+);[^}]*?font-weight:\s*(\d+);[^}]*?url\('\.\/([\w.-]+)'\)/g)].map(m => m.slice(1, 4).join(' '));
  check('fonts: Galmuri11 400/700 and Galmuri9 400 only, each file exists and is woff2', JSON.stringify(faces) === JSON.stringify(['Galmuri11 400 Galmuri11.woff2', 'Galmuri11 700 Galmuri11-Bold.woff2', 'Galmuri9 400 Galmuri9.woff2']) && faces.every(f => fs.readFileSync(stat('fonts/' + f.split(' ')[2])).subarray(0, 4).toString() === 'wOF2'), JSON.stringify(faces));
  check('fonts: OFL.txt is the full licence text (not a pointer)', rd('fonts/OFL.txt').includes('SIL OPEN FONT LICENSE Version 1.1') && rd('fonts/OFL.txt').includes('PERMISSION & CONDITIONS') && rd('fonts/OFL.txt').includes('DISCLAIMER'), '');
  check('demo scenarios carry no home-directory paths', !/\/home\/[a-z]|\/Users\/[A-Za-z]/.test(rd('game-demo.js')), '');
  }

  // ---------- 11) language base (i18n.js): the page starts after the dictionary, the selector, ?lang= carried between pages ----------
  // New checks: they FAIL on statics without i18n.js (the reference copy) by design.
  if (!I18B.has(dir)) check('i18n: this static dir has i18n.js', false, 'reference statics: no i18n.js');
  else {
    const L = I18B.lang(), PK = I18B.packs(dir), M = PK[L].messages, other = L === 'en' ? 'ko' : 'en', sid = STATE.session.id, nonI18n = P_ => P_.fetched.filter(u => !/api\/i18n|locales\//.test(u)), rdi = f => fs.readFileSync(path.join(dir, f), 'utf8');
    for (const file of ['index.html', 'game.html']) {
      // the page asks for its own dictionary (held here): nothing else is requested and nothing is drawn until it arrives
      P = makePage(file, { i18n: 'fetch', search: '?session=' + sid + '&lang=ko' }); await P.load();
      check(file + ': before the dictionary arrives only api/i18n is requested (no sessions, state or plans)', P.fetched.length >= 1 && P.fetched.every(u => /api\/i18n/.test(u)) && P.i18nHeld.length === 1, P.fetched.join(' '));
      check(file + ': before the dictionary arrives nothing is drawn', !P.els.get('#orchCard') || P.els.get('#orchCard').innerHTML === '', 'orchCard drawn');
      for (let i = 0; i < 5 && P.i18nHeld.length; i++) await P.releaseI18n();
      await sleep(60);
      check(file + ': after it arrives the page starts (state drawn) in the language of ?lang=', P.run('!!S') && P.run('I18N.lang') === 'ko' && P.run("t('status.running')") === PK.ko.messages['status.running'], P.fetched.join(' '));
      check(file + ': requests go api/i18n, then the chosen language and English, then the data', JSON.stringify(P.fetched.filter(u => /i18n|locales/.test(u))) === JSON.stringify(['api/i18n', 'locales/ko.json', 'locales/en.json']) && /i18n|locales/.test(P.fetched[0]) && nonI18n(P).length > 0, P.fetched.join(' '));
      check(file + ': <html lang> follows the language and the wait class is gone', P.els.get('html').lang === 'ko' && !P.els.get('html').classList.contains('i18n-wait'), String(P.els.get('html').lang));
    }
    // no ?lang=, no stored choice, no browser language: English (the default language)
    P = makePage('index.html', { i18n: 'fetch', search: '?session=' + sid }); await P.load();
    for (let i = 0; i < 5 && P.i18nHeld.length; i++) await P.releaseI18n();
    await sleep(60);
    check('index.html: without ?lang=, a stored choice or a browser language the page starts in English', P.run('I18N.lang') === 'en' && P.run('!!S') && P.els.get('html').lang === 'en', P.run('I18N.lang'));

    for (const file of ['index.html', 'game.html']) {
      P = makePage(file); await P.load();
      const sel = P.els.get('#langSel');
      check(file + ': the selector is wired (current language, named by common.language) and shown in the header', sel.hidden === false && sel.value === L && sel.title === M['common.language'] && typeof sel.onchange === 'function' && !/<select id="langSel"[^>]*\shidden/.test(rdi(file)), JSON.stringify([sel.hidden, sel.value, sel.title]));
      sel.value = other; sel.onchange();
      check(file + ': choosing the other language stores only the choice, keeps ?session=, and reloads with ?lang=', P.ctx.localStorage._m['ab.lang'] === JSON.stringify(other) && P.ctx.location.href.endsWith('?session=' + sid + '&lang=' + other), P.ctx.location.href + ' ' + JSON.stringify(P.ctx.localStorage._m));
    }
    P = makePage('index.html'); await P.load();
    check('index.html: nothing the user did not choose is stored (the language came from the default, not a choice)', P.ctx.localStorage._m['ab.lang'] === undefined, JSON.stringify(P.ctx.localStorage._m));
    check('index.html: no ?lang= on the page: the link to /game is as before', P.els.get('#gameFull').href === 'game?session=' + sid, P.els.get('#gameFull').href);
    P = makePage('index.html', { search: '?session=' + sid + '&lang=en' }); await P.load();
    check('index.html: ?lang= is carried to the /game link (the shared link keeps its language across the pages)', P.els.get('#gameFull').href === 'game?session=' + sid + '&lang=en', P.els.get('#gameFull').href);
    P = makePage('game.html', { search: '?session=' + sid + '&lang=en' }); await P.load();
    check('game.html: ?lang= is carried to the link back to the dashboard', P.els.get('#back').href === './?session=' + sid + '&lang=en', P.els.get('#back').href);
    P = makePage('game.html'); await P.load();
    check('game.html: no ?lang=: the link back is as before', P.els.get('#back').href === './?session=' + sid, P.els.get('#back').href);
    check('t() is a global of the pages (common.js and the others call it)', P.run("typeof t === 'function' && t('common.you')") === M['common.you'], '');
    check('i18n.js is loaded first, before common.js', ['index.html', 'game.html'].every(f => { const h = rdi(f); return h.indexOf('<script src="i18n.js"></script>') > 0 && h.indexOf('<script src="i18n.js"></script>') < h.indexOf('<script src="common.js"></script>'); }), '');
  }

  console.log(fails ? 'FAILED ' + fails : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log('HARNESS ERROR', e); process.exit(2); });
