// Checks of the dashboard (static/index.html + board.js) for the page of a Codex orchestrator, in Korean and English, against the third fixture of tools/make_synth_fixture.py
// (<prefix>_codex_state.json: a Codex thread with two native sub-agents, an approval-review thread, two `claude -p` runs and a `codex exec` run, a small debate).
// No browser, no npm: the fake page of state_checks.js. Expected words are read from the running page's own t('key').
//  - the agent list: a card for each of the five, none for the approval review; the name tag of a sub-agent is the end of its path, the state pill of each
//  - the conversation: the card of a sub-agent's spawn says the instruction is encrypted (from the dictionary, in the page's language) and shows nothing of the note beside the ciphertext;
//    its final report and the message in the middle of its work
//  - the drawer of the `codex exec` run: the link line names `exec` and its time (the launcher is Codex), not `Bash`
//  - the debate table: both seats of the folder the orchestrator's runs write in
//  - no [key], nothing missing from the dictionary, no Hangul outside data in English
// usage: node codex_checks.js <static dir> <fixture prefix>      (prints PASS/FAIL per check, exit code 1 if any FAIL; without the fixture it is skipped with a note, unless CI or REQUIRE_FIXTURES is set: then that is a failure)
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx] = process.argv.slice(2);
if (!fs.existsSync(fx + '_codex_state.json')) {
  const need = process.env.CI || process.env.REQUIRE_FIXTURES;       // a missing fixture must not let CI pass without the checks; on a laptop it is only a note
  console.log((need ? 'FAIL' : 'SKIP') + ' Codex page checks: no ' + fx + '_codex_state.json (make the fixture with tools/make_synth_fixture.py)');
  process.exit(need ? 1 : 0);
}
const J = n => JSON.parse(fs.readFileSync(fx + n + '.json', 'utf8'));
const STATE = J('_codex_state'), TALK = J('_codex_talk'), ATALK = J('_codex_atalk'), SESSIONS = J('_codex_sessions'), PLANS = J('_codex_plans'), AGENT = J('_codex_agent');
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
    if (p === 'api/agent') return q.get('id') === AGENT.id ? AGENT : { id: q.get('id'), orch_msgs: [], handbacks: [], texts: [], writes: [], reads: [], tool_counts: [], activity: [], spawn_ts: S0.now, spawn_prompt: '', link: null, partial: null };
    return {};
  };
  const ctx = { console, Date: FDate, Math, JSON, URL, URLSearchParams, Promise, Set, Map, Object, Array, String, Number, Infinity, isNaN, encodeURIComponent, decodeURIComponent, AbortController, Error,
    innerWidth: 1200, innerHeight: 900, scrollY: 0, scrollX: 0, location: { search: '?session=' + S0.session.id, hash: '', host: 'localhost:8790', href: 'http://x/' },
    localStorage: { _m: {}, getItem(k) { return this._m[k] ?? null; }, setItem(k, v) { this._m[k] = String(v); } },
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
const scan = (P, data = []) => {
  const names = P.run('I18N.langs.map(l => l.name)').concat(data).filter(Boolean).sort((a, b) => b.length - a.length);
  const visible = v => v.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');
  const texts = P.all().map(v => names.reduce((s, n) => s.split(n).join(''), visible(v)));
  const lone = x => { const i = x.search(HAN); return x.slice(Math.max(0, i - 25), i + 25); };
  return { han: texts.filter(v => HAN.test(v)).map(lone).slice(0, 3), keys: P.all().filter(v => KEY.test(v)).map(v => v.match(KEY)[0]).slice(0, 3), missing: P.run('[...I18N.missing]') };
};

const subs = STATE.agents.filter(a => a.origin === 'subagent'), exec = STATE.agents.find(a => a.origin === 'exec'), clis = STATE.agents.filter(a => a.origin === 'cli');
const byTag = tag => subs.find(a => a.tag === tag);

async function run(lang) {
  const ko = lang === 'ko', L = s => `${lang}: ${s}`;
  const P = makePage({ lang });
  await P.load();
  check(L('the Codex orchestrator page draws'), P.run('!!S') && !!P.html('#agentList') && !!P.html('#orchCard') && STATE.orch.provider === 'codex', P.html('#updated'));
  const sc = scan(P);
  check(L('no [key] and nothing missing from the dictionary') + (ko ? '' : ', no Hangul outside data'), !sc.keys.length && !sc.missing.length && (ko || !sc.han.length), sc);

  // ---------- the agent list ----------
  P.run("ui.agentFilter = 'all'; ui.oldOpen = true; renderAgents()");                                                       // the finished ones are folded away until they are opened
  const list = P.html('#agentList');
  check(L('five agents: two sub-agents, two `claude -p` runs and a `codex exec` run; the approval review has no card'),
    STATE.agents.length === 5 && subs.length === 2 && clis.length === 2 && !!exec && STATE.agents.every(a => a.parent === null), STATE.agents.map(a => [a.origin, a.tag]));
  const cards = (list.match(/data-agent="/g) || []).length;
  check(L('every agent has a card (and nothing else has one)'), cards === STATE.agents.length, cards);
  check(L('a sub-agent is named by the end of its path on its tag, and its nickname beside it'), ['s1', 's2'].every(t => subs.some(a => a.tag === t) && list.includes(`<span class="a-tag">${t}</span>`)) && subs.every(a => a.title && list.includes(esc(a.title))), subs.map(a => [a.tag, a.title]));
  const done = byTag('s1'), working = byTag('s2');
  check(L('the finished sub-agent says Done, the one still working Working'), !!done && !!working && done.status === 'done' && working.status === 'running'
    && shows(list, P.T('status.done')) && shows(list, P.T('status.running')), [done && done.status, working && working.status]);
  check(L('the diamond mark of a Codex agent is not drawn on a Codex page (the provider is the orchestrator\'s)'), !list.includes('class="cxg"'), list.slice(0, 200));

  // ---------- the conversation ----------
  const feed = STATE.feed, spawn = feed.filter(e => e.kind === 'spawn' && subs.some(a => a.id === e.agent));
  check(L('the spawn of each sub-agent names the key of its body and carries no note of the record'), spawn.length === 2 && spawn.every(e => e.text_i18n && e.text_i18n.key === 'event.encrypted.text' && !/\/root\//.test(e.text)), spawn.map(e => [e.text, e.text_i18n]));
  P.run('renderFeed(); renderAtalk()');
  const talk = P.html('#feedItems') + P.html('#atalkItems') + P.html('#talkItems') + P.all().join('\n');
  check(L('the card of a spawn says that the instruction is encrypted, in the page\'s language'), shows(talk, P.T('event.encrypted.text')), talk.slice(0, 200));
  check(L('the old Korean text of that body is not shown when the page can word it from the key') + (ko ? ' (here it is the same sentence)' : ''), ko || !talk.includes('암호화'), '');
  const hand = feed.filter(e => e.kind === 'handback' && e.agent === (done || {}).id), mid = feed.filter(e => e.kind === 'agent_msg' && e.agent === (done || {}).id);
  check(L('a sub-agent\'s final report and its message in the middle of the work are in the conversation'), hand.length === 1 && mid.length === 1 && hand[0].ts > mid[0].ts && shows(talk, hand[0].text.slice(0, 30)) && shows(talk, mid[0].text.slice(0, 30)), [hand.length, mid.length]);

  // ---------- the drawer of the `codex exec` run ----------
  await P.run(`openDrawer(${JSON.stringify(exec.id)})`);
  await sleep(40);
  const sub = P.html('#dSub'), at = P.hm(AGENT.link.bash_ts);
  check(L('the drawer of the exec run says how it was linked and when `exec` ran, not `Bash`'), AGENT.link.parent_kind === 'codex' && sub.includes(' · exec ' + at) && !sub.includes(' · Bash '), sub);
  check(L('the link line is certain (the prompt matched)'), shows(sub, P.T('board.link.line', { rule: P.T('board.link.prompt') })) && shows(sub, P.T('board.link.class.certain')), sub);

  // ---------- the debate table ----------
  const table = P.html('#debateBox') + P.html('#debates') + P.all().join('\n');
  const seats = STATE.debates.flatMap(d => d.topics).flatMap(t => t.rows.map(r => [r.p, r.cells.map(c => c.agent)]));
  check(L('both seats of the folder are taken by the two runs the orchestrator started'), seats.length === 2 && seats.every(([p, a]) => a.length === 1 && STATE.agents.some(x => x.id === a[0])) && ['A', 'B'].every(p => seats.some(s => s[0] === p)), seats);
}

(async () => {
  await run('en');
  await run('ko');
  console.log(fails ? `${fails} FAILED` : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})();
