// Checks of the dashboard (static/index.html + board.js) for work that stopped, waits or nests (limits, interrupted runs, grandchildren, diagnostics), in Korean and English, against the second fixture of tools/make_synth_fixture.py
// (<prefix>_stopped_state.json: a usage limit the orchestrator waits on, runs that stopped for each reason, a paused debate cell, grandchildren, system lines, diagnostics).
// No browser, no npm: the fake page of ui_checks_en.js. Expected words are read from the running page's own t('key'), so what is checked is that the right key and the right numbers
// reach the screen, and that nothing else does (a [key], and in English any Hangul that is not data).
//  - orchestrator card: limit_wait with and without a reset time and "continues by itself"
//  - agent list: the pill of each state and reason, the sentence under it, held agents in the main list, no NaN in the sort, a grandchild one step in under its launcher
//  - header: the interrupted and unknown chips, the diagnostics chip (hidden at 0, and for a server that does not send it)
//  - debate table: a paused cell and the stage of its topic
//  - rooms: a folder of people who work together (one result column, or participants only), in both languages
//  - message flow and conversation card: system lines (dimmed, no sender and receiver), the last speaker ignores them
//  - alerts: the grouped limit alert in the page's language and time; an old server's Korean title as the fallback
//  - drawer: why it stopped, how it was linked (rule and how sure), runs and who handed the last one over, launched by
//  - diagnostics list: a sentence per code, who it is about, how serious; a code this page has no sentence for shows as the code
//  - an old server (none of the new fields) still draws
// usage: node state_checks.js <static dir> <fixture prefix>      (prints PASS/FAIL per check, exit code 1 if any FAIL; without the second fixture it is skipped with a note, unless CI or REQUIRE_FIXTURES is set: then that is a failure)
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx] = process.argv.slice(2);
if (!fs.existsSync(fx + '_stopped_state.json')) {
  const need = process.env.CI || process.env.REQUIRE_FIXTURES;       // a missing fixture must not let CI pass without the checks; on a laptop it is only a note
  console.log((need ? 'FAIL' : 'SKIP') + ' state checks: no ' + fx + '_stopped_state.json (make the fixture with tools/make_synth_fixture.py)');
  process.exit(need ? 1 : 0);
}
const J = n => JSON.parse(fs.readFileSync(fx + n + '.json', 'utf8'));
const STATE = J('_stopped_state'), TALK = J('_stopped_talk'), ATALK = J('_stopped_atalk'), SESSIONS = J('_stopped_sessions'), PLANS = J('_stopped_plans'), DIAG = J('_stopped_diag');
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
  const P = { els, el, log, fetched: [], timers: [], diag: opts.diag || DIAG, agent: null, talk: opts.talk || TALK };
  const route = u => {
    const url = new URL('http://x/' + u.replace(/^\//, '')), q = url.searchParams, p = url.pathname.replace(/^\//, '');
    if (p === 'api/state') return S0;
    if (p === 'api/sessions') return SESSIONS;
    if (p === 'api/talk' && q.get('scope') === 'agents') return ATALK;
    if (p === 'api/talk') return P.talk;
    if (p === 'api/timeline') return { since: +q.get('since'), lanes: [], orch: [] };
    if (p === 'api/plans') return PLANS;
    if (p === 'api/diag') return P.diag;
    if (p === 'api/agent') return P.agent ? P.agent(q.get('id')) : { id: q.get('id'), orch_msgs: [], handbacks: [], texts: [], writes: [], reads: [], tool_counts: [], activity: [], spawn_ts: S0.now, spawn_prompt: '', link: null, partial: null };
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

const agentsBy = f => STATE.agents.filter(f);
const interrupted = r => agentsBy(a => a.status === 'interrupted' && a.reason === r)[0];
const crash = agentsBy(a => a.status === 'ended' && a.reason === 'crash')[0];
const cli = agentsBy(a => a.origin === 'cli');
const grand = cli.find(a => a.parent && STATE.agents.some(p => p.id === a.parent && p.origin === 'subagent'));
const great = cli.find(a => a.parent === (grand || {}).id);
const rankOf = { running: 0, stalled: 1, interrupted: 2, unknown: 3, failed: 4, killed: 4, done: 5, ended: 6 };

async function run(lang) {
  const ko = lang === 'ko', L = s => `${lang}: ${s}`;
  const boot = async (o = {}) => { const P = makePage(Object.assign({ lang }, o)); await P.load(); return P; };
  const over = f => { const s = clone(STATE); f(s); return s; };
  let P = await boot();
  check(L('the stopped fixture draws (state, agent list, orchestrator card)'), P.run('!!S') && !!P.html('#agentList') && !!P.html('#orchCard'), P.html('#updated'));
  const s0 = scan(P, ko ? [] : []);
  check(L('no [key] and nothing missing from the dictionary') + (ko ? '' : ', no Hangul outside data'), !s0.keys.length && !s0.missing.length && (ko || !s0.han.length), s0);

  // ---------- orchestrator card ----------
  const o = STATE.orch;
  check(L('the orchestrator waits on the limit: state word, reset time + "resumes automatically", amber dot'),
    o.state === 'limit_wait' && shows(P.html('#orchCard'), P.T('board.orch.limit')) && shows(P.html('#orchCard'), P.T('board.orch.limit.auto', { time: P.hm(o.resets_at) })) && /dot interrupted/.test(P.html('#orchCard')), [o.state, P.html('#orchCard').slice(0, 400)]);
  for (const [at, auto, key] of [[o.resets_at, false, 'board.orch.limit.at'], [null, false, 'board.orch.limit.noTime'], [null, true, 'board.orch.limit.noTime.auto']]) {
    const Q = await boot({ state: over(s => Object.assign(s.orch, { resets_at: at, auto })) });
    check(L(`orchestrator limit_wait ${at ? 'with' : 'without'} a time, ${auto ? 'automatic' : 'manual'} -> ${key}`), shows(Q.html('#orchCard'), Q.T(key, { time: at ? Q.hm(at) : '' })), Q.html('#orchCard').slice(0, 300));
  }
  const W = await boot({ state: over(s => { s.orch.state = 'idle'; delete s.orch.resets_at; delete s.orch.auto; }) });
  check(L('without limit_wait the orchestrator card says nothing about a limit'), !shows(W.html('#orchCard'), P.T('board.orch.limit')), '');

  // ---------- agent list ----------
  P.run("ui.agentFilter = 'all'; ui.oldOpen = true; renderAgents();");
  const list = P.html('#agentList');
  for (const r of ['limit', 'api_error', 'time_limit', 'exited']) {
    const a = interrupted(r);
    check(L(`an agent interrupted by "${r}" shows ${P.T('status.interrupted.' + r)}`), !!a && list.includes(`<span class="pill interrupted" title="${esc(P.T('status.why.interrupted.' + r))}">${esc(P.T('status.interrupted.' + r))}</span>`), a ? '' : 'no such agent in the fixture');
  }
  check(L('an agent that ended without an answer and no process shows ' + P.T('status.ended.crash')), !!crash && list.includes(`>${esc(P.T('status.ended.crash'))}</span>`), '');
  const lim = interrupted('limit');
  check(L('the limit agent says when it resets'), !!lim && shows(list, P.T('board.agents.limitAt', { time: P.hm(lim.resets_at) })), '');
  check(L('the other interrupted agents say why, not a time'), shows(list, P.T('status.why.interrupted.time_limit')) && shows(list, P.T('status.why.interrupted.exited')), '');
  // held agents stay in the main list: with the finished group folded they are still there
  P.run("ui.oldOpen = false; renderAgents();");
  const folded = P.html('#agentList');
  const held = STATE.agents.filter(a => a.status === 'interrupted'), liveSet = a => ['running', 'stalled', 'interrupted', 'unknown'].includes(a.status);
  // a tree stays together: a finished run under a working launcher is shown with it; the finished group holds the trees of which nothing is working or held
  const treeOf = a => { let r = a; while (r.parent && STATE.agents.some(p => p.id === r.parent)) r = STATE.agents.find(p => p.id === r.parent); return r.id; };
  const onIds = new Set(STATE.agents.filter(liveSet).map(treeOf)), shown = STATE.agents.filter(a => onIds.has(treeOf(a)));
  check(L('interrupted agents are in the main list while the finished group is folded'), held.every(a => folded.includes(`data-agent="${a.id}"`)) && !folded.includes(`data-agent="${crash.id}"`), '');
  check(L('the finished count leaves out the agents that are shown (and the trees they belong to)'), shows(folded, P.T('board.agents.finished', { count: STATE.agents.length - shown.length })), [STATE.agents.length, shown.length]);
  // a grandchild sits one step in under its launcher (two steps for a great-grandchild), right after it
  P.run("ui.oldOpen = true; renderAgents();");
  const full = P.html('#agentList'), at = id => full.indexOf(`data-agent="${id}"`);
  const launcher = STATE.agents.find(a => a.id === (grand || {}).parent);
  check(L('a grandchild follows its launcher, one step in; its own child two steps'), !!grand && !!great && !!launcher && at(launcher.id) >= 0 && at(launcher.id) < at(grand.id) && at(grand.id) < at(great.id)
    && new RegExp(`data-agent="${grand.id}"[^>]*style="--depth:1"`).test(full) && new RegExp(`data-agent="${great.id}"[^>]*style="--depth:2"`).test(full) && /class="card agent[^"]*child"/.test(full), [at(launcher && launcher.id), at(grand && grand.id), at(great && great.id)]);
  check(L('a nested card has no "launched by" line (the step in says it)'), !full.includes('a-under'), full.match(/a-under[^<]*<[^<]*/));
  check(L('a finished run stays under its working launcher (it is not cut off into the finished group)'), P.html('#agentList').indexOf(`data-agent="${great.id}"`) < P.html('#agentList').indexOf('id="restToggle"') || !P.html('#agentList').includes('id="restToggle"'), '');
  const Q1 = await boot({ state: over(s => { s.agents.find(a => a.id === great.id).parent = 'a0000000000000000'; }) });
  Q1.run("ui.agentFilter = 'all'; ui.oldOpen = true; renderAgents();");
  check(L('an agent whose launcher is not in the list stands on its own with a "launched by" line'), Q1.html('#agentList').includes('a-under') && shows(Q1.html('#agentList'), Q1.T('board.agents.launchedBy', { name: 'a0000000' })), Q1.html('#agentList').match(/a-under[^<]*<[^<]*/));
  // unknown (and a status this page has never heard of): drawn, sorted after interrupted, no NaN
  const roots = s => s.agents.filter(x => x.status === 'done' && !x.parent);
  const U = await boot({ state: over(s => { const a = roots(s)[0]; a.status = 'unknown'; a.reason = null; roots(s)[0].status = 'status-from-the-future'; }) });
  U.run("ui.agentFilter = 'all'; ui.oldOpen = true; renderAgents();");
  const uh = U.html('#agentList'), cards = uh.split('<div class="card agent').slice(1).map(c => ({ child: /^[^>]*\bchild\b/.test(c), st: (c.match(/class="pill ([\w-]+)"/) || [])[1] }));
  const seq = cards.filter(c => !c.child).map(c => c.st);
  check(L('unknown shows its word and a hollow dot; a status the page does not know shows as it came and goes last'), shows(uh, U.T('status.unknown')) && /dot unknown/.test(uh) && uh.includes('>status-from-the-future</span>') && !/NaN/.test(uh), seq);
  const part = seq.slice(0, seq.indexOf('done') < 0 ? seq.length : seq.indexOf('done'));
  check(L('the launchers of the list are ordered running, stalled, interrupted, unknown, then the finished ones'), seq.every((st, i) => i === 0 || (rankOf[st] ?? 9) >= (rankOf[seq[i - 1]] ?? 9) || !part.includes(st) && i < 0) && seq.indexOf('status-from-the-future') === seq.length - 1, seq);

  // ---------- header ----------
  const hdr = P.html('#counters'), nInt = held.length, nUnk = 0;
  check(L('the header counts the interrupted agents'), shows(hdr, P.T('status.interrupted')) && hdr.includes(`<b>${nInt}</b>`) && /dot interrupted/.test(hdr), hdr.slice(0, 300));
  check(L('no unknown chip while nothing is unknown'), !/dot unknown/.test(hdr), '');
  check(L('the diagnostics chip shows the count (amber while something is to check)'), shows(hdr, P.T('board.top.diag', { n: `<b>${STATE.diag.n}</b>` })) && hdr.includes('id="diagChip"') && /id="diagChip"[^>]*color:var\(--amber\)/.test(hdr), hdr.slice(-300));
  const D0 = await boot({ state: over(s => { s.diag = { n: 0, warn: 0, info: 0, by_code: {}, capped: false }; }) });
  check(L('no diagnostics chip at 0'), !D0.html('#counters').includes('diagChip'), '');
  const D1 = await boot({ state: over(s => { delete s.diag; }) });
  check(L('no diagnostics chip from a server that does not send the count'), !D1.html('#counters').includes('diagChip'), '');
  const D2 = await boot({ state: over(s => { s.diag = { n: 3, warn: 0, info: 3, by_code: {}, capped: false }; }) });
  check(L('only information: the chip is not amber'), D2.html('#counters').includes('id="diagChip"') && !/id="diagChip"[^>]*color:var\(--amber\)/.test(D2.html('#counters')), '');
  const UC = await boot({ state: over(s => { s.agents.find(a => a.status === 'done').status = 'unknown'; }) });
  check(L('the unknown chip appears with one unknown agent'), /dot unknown/.test(UC.html('#counters')) && shows(UC.html('#counters'), UC.T('status.unknown')), UC.html('#counters').slice(0, 300));

  // ---------- debate table ----------
  P.run('renderDebates(); renderSummary();');
  const deb = P.html('#topics');
  check(L('a paused cell: its own look, word and sentence'), deb.includes('class="cell c-paused"') && shows(deb, P.T('board.cell.paused')) && shows(deb, P.T('board.cell.paused.sub')), deb.match(/c-paused[^]{0,200}/));
  check(L('the topic with a paused cell is still in its round (the round is not done)'), /stage s-active/.test(deb), deb.match(/class="stage[^<]*</g));
  check(L('the progress card says the paused participant is the one the round waits for'), shows(P.html('#summary'), P.T('board.sum.paused')), P.html('#summary').slice(0, 300));

  // a draft whose agent is interrupted: no typing dots, the word of the state, and it is still an open round
  const cellsOf = () => STATE.debates.flatMap(d => d.topics).flatMap(tp => tp.rows).flatMap(r => r.cells);
  const heldCell = cellsOf().find(c => c.state === 'draft' && (agentsBy(a => a.id === c.agent)[0] || {}).status === 'interrupted');
  const heldHtml = (deb.match(/<div class="cell c-draft c-held"[^]*?<\/div><\/div>/) || [''])[0];
  check(L('the draft of an interrupted agent: "' + P.T('board.cell.draftHeld', { state: P.T('status.interrupted') }) + '", no typing dots, a sentence that it is not being written'),
    !!heldCell && heldHtml.includes(esc(P.T('board.cell.draftHeld', { state: P.T('status.interrupted') }))) && !heldHtml.includes('typing') && heldHtml.includes(esc(P.T('board.cell.draftHeld.title.interrupted'))), heldHtml || 'no such cell in the fixture');
  check(L('the draft of a running agent still types'), cellsOf().some(c => c.state === 'draft' && (agentsBy(a => a.id === c.agent)[0] || {}).status === 'running') ? /class="cell c-draft"[^]*?typing/.test(deb) : true, '');
  const openOf = state => P.run(`topicStage({ final: { exists: false }, deps: '', rows: [{ cells: [{ round: 1, state: ${JSON.stringify(state)}, agent: 'x' }, { round: 1, state: 'done', agent: 'y' }] }], rounds: [1] }).cls`);
  check(L('a round with a cell in a state this page does not know (or paused) is still open'), openOf('unknown') === 's-active' && openOf('paused') === 's-active' && openOf('done') !== 's-active', [openOf('unknown'), openOf('paused'), openOf('done')]);

  // ---------- rooms: people who work together in a folder, whatever the work is called ----------
  const live3 = STATE.agents.filter(a => a.status === 'running').slice(0, 2).concat(STATE.agents.filter(a => a.status === 'done').slice(0, 1));
  const cellOf = (a, st) => ({ round: 1, state: st, path: '/r/meeting/' + a.id + '.md', agent: a.id, writer: null, readers: [], lines: 4, mtime: STATE.now - 60, planned: false });
  const roomOf = (kind, ags, states) => ({ root: '/r/meeting', short: '~/r/meeting', name: 'meeting', title: 'Weekly sync', finals: [], last_ts: STATE.now, current: true, topics: [{
    dir: '/r/meeting', key: 'meeting', title: 'Weekly sync', name: '', deps: '', kind: 'rounds', room: kind, guide: '/r/meeting/agenda.md', final: { path: null, rel: null, exists: false, auto: false, mtime: null, lines: 0 },
    rounds: kind === 'cells' ? [1] : [], docs: [{ name: 'agenda.md', path: '/r/meeting/agenda.md' }], brief: true,
    rows: ags.map((a, i) => ({ p: kind === 'cells' ? 'ABC'[i] : (a.tag || a.id.slice(0, 6)), role: '', agents: [a.id], cells: kind === 'cells' ? [cellOf(a, states[i])] : [] })) }] });
  const withRoom = (kind, states) => over(s => { s.debates = [roomOf(kind, live3.map(a => s.agents.find(x => x.id === a.id)), states)]; });
  const RC = await boot({ state: withRoom('cells', ['draft', 'writing', 'done']) });
  const rc = RC.html('#topics');
  check(L('a room of cells: one result column (no round 1 and 2), the guide chip, the "meeting" key, its own stage line'),
    shows(rc, RC.T('board.room.col')) && !shows(rc, RC.T('board.round', { n: 1 })) && !shows(rc, RC.T('board.round', { n: 2 })) && shows(rc, RC.T('board.room.key')) && shows(rc, RC.T('board.foot.brief', { name: 'agenda.md' }))
    && shows(rc, RC.T('board.stage.room.active', { done: 1, total: 3 })), rc.slice(0, 600));
  check(L('a room of cells: a cell for each, the stepper is guide -> result -> final'), (rc.match(/class="cell c-/g) || []).length === 3 && shows(rc, RC.T('board.step.brief')) && shows(rc, RC.T('board.step.final')) && (rc.match(/class="step /g) || []).length === 3, rc.match(/class="step /g));
  const RD = await boot({ state: withRoom('cells', ['done', 'done', 'done']) });
  check(L('a room of cells whose every file is in but whose people still work: "all submitted"'), shows(RD.html('#topics'), RD.T('board.stage.room.ready', { total: 3 })), RD.html('#topics').match(/class="stage[^<]*</g));
  const ended = s => { s.agents.forEach(a => { if (a.status === 'running') a.status = 'done'; }); };
  const stageOf = html => { const m = html.match(/class="stage (s-[a-z]+)">([^<]*)</); return m ? m[1] + ' ' + m[2] : null; };      // the stage badge of the first topic: its class and its text
  const RF = await boot({ state: over(s => { s.debates = [roomOf('cells', live3.map(a => s.agents.find(x => x.id === a.id)), ['done', 'done', 'done'])]; ended(s); }) });
  const RT = await boot({ state: over(s => { const d = roomOf('cells', live3.map(a => s.agents.find(x => x.id === a.id)), ['done', 'done', 'done']); d.copies = 3; s.debates = [d]; ended(s); }) });
  const tip = RT.T('board.debate.copies', { count: 3 });
  check(L('a debate that stands for folded copies says how many, in the tab and in the title tooltip; one with none says nothing'), tip && RT.html('#debateTabs').includes('title="' + esc(tip) + '"') && RT.html('#debateMeta').includes(esc(tip)) && !RF.html('#debateTabs').includes('title='),
    [RT.html('#debateTabs').slice(0, 200), RT.html('#debateMeta').slice(0, 200)]);
  check(L('a room of cells whose every file is in and nobody works: "' + RF.T('board.stage.complete') + '" (no round to wait for), the green stage'), stageOf(RF.html('#topics')) === 's-final ' + RF.T('board.stage.complete'), stageOf(RF.html('#topics')));
  const RG = await boot({ state: over(s => { s.debates = [roomOf('cells', live3.map(a => s.agents.find(x => x.id === a.id)), ['done', 'missing', 'done'])]; ended(s); }) });
  check(L('a room with a seat that never handed in is stopped, not complete'), stageOf(RG.html('#topics')) === 's-ready ' + RG.T('board.stage.room.partial', { done: 2, total: 3 }), stageOf(RG.html('#topics')));
  const RH = await boot({ state: over(s => { const d = roomOf('cells', live3.map(a => s.agents.find(x => x.id === a.id)), ['done', 'done', 'done']); d.topics[0].final = { path: '/r/CLOSING.md', rel: '../CLOSING.md', exists: true, auto: true, mtime: STATE.now, lines: 30 }; s.debates = [d]; ended(s); }) });
  check(L('a room closed by the conclusion of its bundle above it: the final stage, and the document is named and opens'), stageOf(RH.html('#topics')) === 's-final ' + RH.T('board.stage.final') && shows(RH.html('#topics'), '../CLOSING.md') && RH.html('#topics').includes('data-path="/r/CLOSING.md"'), stageOf(RH.html('#topics')));
  // a flat review (its result files are declared, there is no round folder): its cells carry no round
  const flatOf = (ags, states) => ({ root: '/r/rev', short: '~/r/rev', name: 'rev', title: 'Release review', finals: [], last_ts: STATE.now, current: true, topics: [{
    dir: '/r/rev', key: 'rev', title: 'Release review', name: '', deps: '', kind: 'flat', final: { path: null, rel: null, exists: false, auto: false, mtime: null, lines: 0 }, rounds: [], docs: [{ name: 'brief.md', path: '/r/rev/brief.md' }], brief: true,
    rows: ags.map((a, i) => ({ p: ['sol', 'opus', 'mini'][i], role: '', agents: [a.id], cells: [{ round: null, state: states[i], path: '/r/rev/' + ['sol', 'opus', 'mini'][i] + '.md', agent: a.id, writer: null, readers: [], lines: 4, mtime: STATE.now - 60, planned: false }] })) }] });
  const withFlat = (states, end) => over(s => { s.debates = [flatOf(live3.map(a => s.agents.find(x => x.id === a.id)), states)]; if (end) ended(s); });
  const FD = await boot({ state: withFlat(['done', 'done', 'done'], true) });
  const fd = FD.html('#topics');
  check(L('a flat review whose every result is in and nobody works: complete, with one result column and a cell for each'), stageOf(fd) === 's-final ' + FD.T('board.stage.complete') && shows(fd, FD.T('board.room.col')) && !shows(fd, FD.T('board.round', { n: 1 })) && (fd.match(/class="cell c-done"/g) || []).length === 3, stageOf(fd));
  const FW = await boot({ state: withFlat(['done', 'draft', 'writing'], false) });
  check(L('a flat review with results still coming: working, in/total'), stageOf(FW.html('#topics')) === 's-active ' + FW.T('board.stage.room.active', { done: 1, total: 3 }), stageOf(FW.html('#topics')));
  const FP = await boot({ state: withFlat(['done', 'waiting', 'waiting'], true) });
  check(L('a flat review with only some results in and nobody working is stopped, not complete and not waiting'), stageOf(FP.html('#topics')) === 's-ready ' + FP.T('board.stage.room.partial', { done: 1, total: 3 }), stageOf(FP.html('#topics')));
  check(L('a debate with round folders keeps its stage: round done, next stage waits'), P.run(`topicStage({ final: { exists: false }, deps: '', kind: 'rounds', rows: [{ agents: [], cells: [{ round: 1, state: 'done', agent: 'x' }, { round: 2, state: 'done', agent: 'x' }] }], rounds: [1, 2] }).text`) === P.T('board.stage.ready', { n: 2 }), '');
  const RM = await boot({ state: withRoom('members', []) });
  const rm = RM.html('#topics');
  check(L('a room of participants only: no stepper, no round column, the stage says how many talk, a row for each'),
    !rm.includes('class="stepper"') && !shows(rm, RM.T('board.round', { n: 1 })) && !shows(rm, RM.T('board.room.col')) && !rm.includes('class="cell') && shows(rm, RM.T('board.stage.room.members', { count: 3 })) && live3.every(a => rm.includes(`data-agent="${a.id}"`)), rm.slice(0, 600));
  check(L('the participants of such a room are not "other work", and the progress card lists the room'), !shows(rm, RM.T('board.work.title')) || (rm.match(new RegExp('data-agent="' + live3[0].id + '"', 'g')) || []).length === 1,
    (rm.match(new RegExp('data-agent="' + live3[0].id + '"', 'g')) || []).length);
  check(L('they count as agents of the debate (the debate tab and the token card)'), live3.every(a => RM.run(`inDebate(S.agents.find(a => a.id === ${JSON.stringify(a.id)}), S.debates[0])`)), '');
  // the words that name the unit of work: a room is "this work", a debate is "this debate" (token card, the agent filter tab, the timeline window)
  const RU = await boot({ state: over(s => { s.debates = [roomOf('cells', live3.map(a => s.agents.find(x => x.id === a.id)), ['draft', 'writing', 'done'])]; live3.forEach(x => { s.agents.find(a => a.id === x.id).units = ['/r/meeting']; }); }) });
  const tokHead = (Pg, key) => { const d = Pg.run('currentDebate()'); return (Pg.T(key, { name: d.name, count: Pg.run('S.agents.filter(a => inDebate(a, currentDebate())).length'), input: '', output: '', cost: '' }) || '').split(':')[0]; };
  for (const [what, Pg] of [['cells', RU], ['participants only', RM]]) {
    const tk = Pg.html('#tokCard');
    check(L('a room of ' + what + ': the token card says "' + tokHead(Pg, 'board.tok.room') + '", not the debate wording'), shows(tk, tokHead(Pg, 'board.tok.room')) && !shows(tk, tokHead(Pg, 'board.tok.debate')), tk.slice(-700));
    check(L('a room of ' + what + ': the agent filter tab and the timeline window say "' + Pg.T('board.agents.tab.room') + '" / "' + Pg.T('board.tl.win.room') + '", not the debate words'),
      shows(Pg.html('#agentFilter'), Pg.T('board.agents.tab.room')) && !shows(Pg.html('#agentFilter'), Pg.T('board.agents.tab.debate')) && shows(Pg.html('#tlWin'), Pg.T('board.tl.win.room')) && !shows(Pg.html('#tlWin'), Pg.T('board.tl.win.debate')), [Pg.html('#agentFilter'), Pg.html('#tlWin')]);
  }
  check(L('a debate session keeps the debate words (token card, agent filter tab, timeline window)'), shows(P.html('#tokCard'), tokHead(P, 'board.tok.debate')) && !shows(P.html('#tokCard'), tokHead(P, 'board.tok.room'))
    && shows(P.html('#agentFilter'), P.T('board.agents.tab.debate')) && !shows(P.html('#agentFilter'), P.T('board.agents.tab.room')) && shows(P.html('#tlWin'), P.T('board.tl.win.debate')), [P.html('#agentFilter'), P.html('#tlWin')]);
  const RE = await boot({ state: over(s => { s.debates = [roomOf('members', live3.map(a => s.agents.find(x => x.id === a.id)), [])]; s.agents.forEach(a => { if (a.status === 'running') a.status = 'done'; }); }) });
  check(L('a room of participants only whose participants have all ended says so'), shows(RE.html('#topics'), RE.T('board.stage.room.membersEnded', { count: 3 })), RE.html('#topics').match(/class="stage[^<]*</g));
  const sr = scan(RC), sm = scan(RM);
  check(L('the rooms show no [key]') + (ko ? '' : ' and no Hangul outside data'), !sr.keys.length && !sr.missing.length && !sm.keys.length && !sm.missing.length && (ko || (!sr.han.length && !sm.han.length)), [sr, sm]);

  // ---------- message flow and conversation ----------
  P.run("ui.feedFilter = 'all'; renderFeed();");
  const feed = P.html('#feedItems'), sys = STATE.feed.filter(e => e.kind === 'sys');
  const rows = [...feed.matchAll(/<div class="ev k-sys"[^]*?<\/div><\/div><\/div>/g)].map(m => m[0]);
  check(L('the system lines are in the message flow, dimmed, with no sender or receiver'), sys.length > 0 && rows.length === sys.length && rows.every(r => !r.includes('who-b')), [sys.length, rows.length]);
  check(L('each system line is worded from its key and numbers (the time in this language)'), sys.every(e => shows(feed, P.T(e.title_i18n.key, Object.assign({}, e.title_i18n.params, e.title_i18n.params.at ? { at: P.hm(e.title_i18n.params.at) } : {})))), sys.map(e => e.title_i18n.key));
  check(L('the line is not an orchestrator speech'), !STATE.feed.some(e => e.kind === 'orch_say' && /hit your|rate_limit|Synthetic API error/.test(e.text || '')), '');
  P.run("ui.feedFilter = 'orch'; renderFeed();");
  check(L('the Orch tab has none of them; the Status tab has them'), !P.html('#feedItems').includes('k-sys'), '');
  P.run("ui.feedFilter = 'status'; renderFeed();");
  check(L('the Status tab has the system lines'), P.html('#feedItems').includes('k-sys'), '');
  const talk = P.html('#talkList'), talkSys = TALK.items.filter(e => e.kind === 'sys');
  check(L('the conversation card shows the system lines as dimmed notes, not as a bubble of either side'), talkSys.length > 0 && (talk.match(/class="sysline"/g) || []).length === talkSys.length && !/class="msg [^"]*"[^>]*>[^]{0,80}Synthetic/.test(talk), [talkSys.length, (talk.match(/class="sysline"/g) || []).length]);
  const lastSpeech = [...TALK.items].reverse().find(e => e.kind !== 'sys');
  check(L('the card header names the last thing somebody said, not the last system line'), TALK.items[TALK.items.length - 1].kind === 'sys' ? shows(P.text('#talkTime'), P.T('board.talk.last', { who: lastSpeech.kind === 'user_say' || lastSpeech.kind === 'user_answer' ? P.T('common.you') : P.T('common.orchestrator'), time: P.hm(lastSpeech.ts), ago: P.run(`ago(${lastSpeech.ts})`) })) : true, P.text('#talkTime'));

  // ---------- alerts ----------
  const al = STATE.alerts.find(a => a.id.startsWith('limit:'));
  const title = P.run(`alertTitle(S.alerts.find(a => a.id.startsWith('limit:')))`);
  check(L('the grouped limit alert is worded from its key with the reset time of this page'), !!al && title === P.T(al.title_i18n.key, { at: P.hm(al.title_i18n.params.at) }) && !title.includes(String(al.title_i18n.params.at)), title);
  if (!ko) check('en: the limit alert title is not the Korean one', !HAN.test(title), title);
  P.run('ui.alertsOpen = true; renderAlerts();');
  const alHtml = P.html('#alerts'), alRow = (alHtml.match(new RegExp('<div class="al [^"]*" data-id="' + al.id + '"[^]*?<div class="x"')) || [''])[0];
  check(L('a limit that resumes by itself is a note (' + P.T('board.alert.level.note') + '), not "' + P.T('board.alert.level.info') + '": nothing is asked of the user'),
    al.title_params.auto ? al.level === 'note' && alRow.includes(' note ') && alRow.includes(esc(P.T('board.alert.level.note')) + ' ·') && !alRow.includes(esc(P.T('board.alert.level.info'))) : al.level === 'check', [al.level, alRow.slice(0, 200)]);
  const Un = await boot({ state: over(s => { s.alerts.push({ id: 'x:1', level: 'from-the-future', title: 'T', text: 'x', ts: s.now, agent: null }); }) });
  Un.run('ui.alertsOpen = true; renderAlerts();');
  check(L('an alert level this page does not know still draws (an icon, no "undefined")'), Un.html('#alerts').includes('data-id="x:1"') && !/>undefined</.test(Un.html('#alerts')), '');
  const Old = await boot({ state: over(s => { s.alerts.forEach(a => { delete a.title_i18n; }); }) });
  check(L('an alert without title_i18n (an old server) shows its title as it came'), Old.run(`alertTitle(S.alerts.find(a => a.id.startsWith('limit:')))`) === al.title, '');

  // ---------- drawer ----------
  const detail = id => ({ id, orch_msgs: [], handbacks: [], texts: [], writes: [], reads: [], tool_counts: [], activity: [], spawn_ts: STATE.now, spawn_prompt: '', link: null, partial: null });
  P.agent = detail;
  await P.run(`openDrawer(${JSON.stringify(lim.id)})`); await sleep(30);
  check(L('the drawer of a stopped agent says why and when the limit resets'), shows(P.html('#dBody'), P.T('board.drawer.stateBox')) && shows(P.html('#dBody'), P.T('status.why.interrupted.limit')) && shows(P.html('#dBody'), P.T('board.agents.limitAt', { time: P.hm(lim.resets_at) })), P.html('#dBody').slice(0, 300));
  check(L('its title carries the state word'), shows(P.html('#dTitle'), P.T('status.interrupted.limit')), P.html('#dTitle'));
  // the summary of a tool call that is the board's own wording (SubagentHandback): the page words it, in the card's now-line and in the activity tab
  const handback = agentsBy(a => a.last_tool && a.last_tool.name === 'SubagentHandback')[0];
  check(L('the fixture has an agent whose last tool is SubagentHandback, and the server named the key'), !!handback && handback.last_tool.text_i18n && handback.last_tool.text_i18n.key === 'event.tool.handback', handback && handback.last_tool);
  if (handback) {
    P.agent = id => Object.assign(detail(id), { activity: [handback.last_tool] });
    await P.run(`openDrawer(${JSON.stringify(handback.id)}); ui.dTab = 'activity'; renderDrawer()`); await sleep(30);
    const act = P.html('#dBody');
    check(L('its activity tab says "' + P.T('event.tool.handback') + '" in this language'), shows(act, P.T('event.tool.handback')) && (ko || !HAN.test(act.replace(/\/\*[^]*?\*\//g, ''))), act.slice(0, 300));
    check(L('a tool entry without a key (an old server) shows its text as it came'), P.run(`toolText({ name: 'Read', text: 'a.py' })`) === 'a.py' && P.run(`toolText({ name: 'SubagentHandback', text: 'T-old', text_i18n: { key: 'no.such.key', params: {} } })`) === 'T-old', '');
    P.agent = detail;
  }
  const ck = cli.find(a => a.link.rule === 'content');
  await P.run(`openDrawer(${JSON.stringify(ck.id)})`); await sleep(30);
  check(L('a `claude -p` run: how it was linked and how sure that is'), shows(P.html('#dSub'), P.T('board.link.line', { rule: P.T('board.link.content') })) && shows(P.html('#dSub'), P.T('board.link.class.certain')) && P.html('#dSub').includes(esc(P.T('board.link.tip.content'))), P.html('#dSub'));
  await P.run(`openDrawer(${JSON.stringify(grand.id)})`); await sleep(30);
  check(L('a grandchild says who launched it'), shows(P.html('#dSub'), P.T('board.agents.launchedBy', { name: launcher.tag || launcher.title })), P.html('#dSub'));
  const R = await boot({ state: over(s => { const a = s.agents.find(x => x.id === ck.id); a.runs = [{ n: 1, start: s.now - 900, end: s.now - 600, kind: 'first', by: null }, { n: 2, start: s.now - 300, end: null, kind: 'process', by: [s.session.id, null] }]; a.by = [s.session.id, null]; a.link = { rule: 'content_short', certain: false, rule_class: 'guess', incomplete: true, tree: s.session.id }; }) });
  R.agent = detail;
  await R.run(`openDrawer(${JSON.stringify(ck.id)})`); await sleep(30);
  const sub = R.html('#dSub');
  check(L('a run handed over again: the count and who gave the last instruction'), shows(sub, R.T('board.drawer.runs', { count: 2 })) && shows(sub, R.T('board.drawer.run.by', { who: R.T('common.orchestrator') })), sub);
  check(L('a comparison cut short is not called a short instruction'), shows(sub, R.T('board.link.content_short.incomplete')) && !shows(sub, R.T('board.link.content_short')) && sub.includes(esc(R.T('board.link.tip.content_short.incomplete'))), sub);
  check(L('the runs are listed in the overview'), shows(R.html('#dBody'), R.T('board.drawer.runsBox')) && shows(R.html('#dBody'), R.T('board.drawer.run.process')), R.html('#dBody').slice(-300));
  R.run("ui.agentFilter = 'all'; ui.oldOpen = true; renderAgents();");
  check(L('the guess mark of that agent carries the "cut short" tip'), R.html('#agentList').includes(`title="${esc(R.T('board.link.tip.content_short.incomplete'))}"`), '');
  const linkOf = extra => boot({ state: over(s => { const a = s.agents.find(x => x.id === ck.id); a.by = [s.session.id, null]; a.link = Object.assign({ rule: 'content_short', certain: false, rule_class: 'guess', incomplete: false, tree: s.session.id }, extra); }) });
  const PU = await linkOf({ assumed: true });
  PU.agent = detail;
  await PU.run(`openDrawer(${JSON.stringify(ck.id)})`); await sleep(30);
  const unread = PU.html('#dSub');
  check(L('a guess made on a launching script nobody could read says so'), shows(unread, PU.T('board.link.content_short.assumed')) && !shows(unread, PU.T('board.link.content_short.incomplete')) && !shows(unread, PU.T('board.link.content_short'))
    && unread.includes(esc(PU.T('board.link.tip.content_short.assumed'))), unread);
  PU.run("ui.agentFilter = 'all'; ui.oldOpen = true; renderAgents();");
  check(L('the guess mark of that agent carries the "script not read" tip'), PU.html('#agentList').includes(`title="${esc(PU.T('board.link.tip.content_short.assumed'))}"`), '');
  const PB = await linkOf({ assumed: true, incomplete: true });
  PB.agent = detail;
  await PB.run(`openDrawer(${JSON.stringify(ck.id)})`); await sleep(30);
  check(L('a guess with both reasons names the cut comparison'), shows(PB.html('#dSub'), PB.T('board.link.content_short.incomplete')) && !shows(PB.html('#dSub'), PB.T('board.link.content_short.assumed')), PB.html('#dSub'));
  const PS = await linkOf({ assumed: false });
  PS.agent = detail;
  await PS.run(`openDrawer(${JSON.stringify(ck.id)})`); await sleep(30);
  check(L('a guess with no reason given is still a short instruction'), shows(PS.html('#dSub'), PS.T('board.link.content_short')) && PS.html('#dSub').includes(esc(PS.T('board.link.tip.content_short'))), PS.html('#dSub'));

  // ---------- diagnostics list ----------
  const G = await boot({ diag: Object.assign(clone(DIAG), { items: DIAG.items.concat([{ code: 'a_code_from_the_future', level: 'info', scope: 'session', agent: null, unit: '~/work/x/y', params: { n: 2 } },
    { code: 'parse_errors', level: 'warn', scope: 'session', agent: null, unit: null, params: { n: 2 } }]) }) });
  await G.run('openDiag()'); await sleep(30);
  const body = G.html('#mBody'), shownCodes = [...new Set(DIAG.items.map(x => x.code))];
  check(L('the diagnostics list: title, the counts, one sentence per code, no raw code'), shows(G.text('#mTitle'), G.T('board.diag.title')) && shows(G.text('#mSub'), G.T('board.diag.sub', { count: DIAG.n, warn: DIAG.warn }))
    && shownCodes.every(c => shows(body, G.T('board.diag.code.' + c)) && !body.includes('>' + c + '<')), [G.text('#mSub'), shownCodes]);
  check(L('each row has its severity; who it is about (an agent by name, the orchestrator, a folder, the session)'), shows(body, G.T('board.diag.level.warn')) && shows(body, G.T('board.diag.level.info')) && body.includes('data-agent=') && shows(body, G.T('common.orchestrator')) && shows(body, '~/work/x/y') && shows(body, G.T('board.diag.subject.session')), body.slice(0, 300));
  check(L('a code this page has no sentence for shows as the code'), body.includes('>a_code_from_the_future<'), '');
  check(L('the list carries no record text (only dictionary sentences, names and folders)'), !/Synthetic API error/.test(body) && !STATE.agents.some(a => a.description && a.description.length > 12 && body.includes(esc(a.description))), '');
  const E = await boot({ diag: Object.assign(clone(DIAG), { n: 0, warn: 0, info: 0, items: [] }) });
  await E.run('openDiag()'); await sleep(30);
  check(L('an empty list says there is nothing to report'), shows(E.html('#mBody'), E.T('board.diag.empty')), E.html('#mBody'));

  // ---------- an old server: none of the new fields ----------
  const old = await boot({ state: over(s => { delete s.diag; s.agents.forEach(a => { ['reason', 'resets_at', 'node', 'by', 'parent', 'runs', 'work_units'].forEach(k => delete a[k]); if (a.link) { delete a.link.rule_class; delete a.link.incomplete; delete a.link.assumed; delete a.link.tree; } }); s.feed = s.feed.filter(e => e.kind !== 'sys'); s.orch.state = 'idle'; }) });
  old.run("ui.agentFilter = 'all'; ui.oldOpen = true; renderAgents();");
  check(L('a state without any of the new fields still draws (status words as before, no step in)'), old.run('!!S') && old.html('#agentList').includes('class="card agent') && !old.html('#agentList').includes('child') && !/NaN|undefined/.test(old.html('#agentList')), old.html('#agentList').slice(0, 200));
  const so = scan(old);
  check(L('and shows no [key]') + (ko ? '' : ' and no Hangul outside data'), !so.keys.length && !so.missing.length && (ko || !so.han.length), so);
  const sa = scan(G);
  check(L('the diagnostics list shows no [key]') + (ko ? '' : ' and no Hangul outside data'), !sa.keys.length && !sa.missing.length && (ko || !sa.han.length), sa);
}

(async () => {
  if (!I18B.has(dir)) { check('i18n: this static dir has i18n.js', false, 'reference statics: no i18n.js'); process.exit(1); }
  await run('ko');
  await run('en');
  console.log(fails ? 'FAILED ' + fails : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})();
