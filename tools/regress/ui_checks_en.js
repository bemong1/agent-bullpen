// English-screen checks of static/index.html and static/game.html on one saved fixture (the fake browser of ui_checks.js, no npm, no browser).
// The page is booted in English from the dictionary on disk and every text it writes to the screen is recorded (innerHTML, textContent, title, document.title).
//  - cards: the orchestrator, token, agent, feed, talk and agent-talk cards, the header chips, the alerts; the agent drawer in all four tabs
//  - the diagnosis card (empty HOME with every origin, server off, 403, 500, no answer, missing session) and the plan bar (errors by error_info, Codex limit)
//  - the new server fields: a title from title_i18n (the old Korean title is not shown), notify status, questions, alerts, error_info
//  - no Hangul outside data (data = text that came from the transcript or the fixture: Korean data is injected and must show unchanged), no [key], I18N.missing empty
// Expected words are never typed here: they are read from the running page's own t('key'), so the dictionary can be edited without touching this file;
// what is checked is that the right key reaches the screen and that nothing else (Hangul, a [key]) does.
// usage: node ui_checks_en.js <static dir> <fixture prefix>      (prints PASS/FAIL per check, exit code 1 if any FAIL; fixture: tools/make_synth_fixture.py)
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx] = process.argv.slice(2);
const J = n => JSON.parse(fs.readFileSync(fx + n + '.json', 'utf8'));
const STATE = J('_state'), TALK = J('_talk'), ATALK = J('_atalk'), SESSIONS = J('_sessions'), PLANS = J('_plans');
const FIXED = Math.round(STATE.now * 1000) + 5000;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const HAN = /[ㄱ-ㆎ가-힣]/;
const KEY = /\[(?:common|status|kind|time|unit|board|office|demo|diag|page|cli|alert|event|plan)\.[\w.]+\]/i;           // a drawn [key] of any dictionary area
let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)))); };
// the HTML holds dictionary text raw (t() does not escape) or escaped (when the page wraps it in esc()): either counts as the text being there
const shows = (html, text) => !!text && (html.includes(text) || html.includes(esc(text)));

class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(FIXED); } static now() { return FIXED; } }

function makePage(file, opts = {}) {
  const html = fs.readFileSync(path.join(dir, file), 'utf8');
  const scripts = [...html.matchAll(/<script(?:\s+src="([^"]+)")?\s*>([\s\S]*?)<\/script>/g)].map(m => ({ src: m[1] || '', code: m[1] ? fs.readFileSync(path.join(dir, m[1]), 'utf8') : m[2] }));
  const els = new Map(), log = [];                                                      // log: every non-empty text written to the screen
  const record = (sel, prop, v) => { if (typeof v === 'string' && v) log.push({ sel, prop, v }); };
  function el(sel) {
    if (els.has(sel)) return els.get(sel);
    const e = { sel, hidden: false, dataset: {}, style: { setProperty() {} }, scrollTop: 0, scrollHeight: 0, clientWidth: 900, clientHeight: 300, offsetHeight: 30, offsetWidth: 100,
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); }, toggle() {} },
      querySelectorAll(s) { if (s !== '.tab') return []; const self = this; self.tabs = {};              // tab bar buttons (data-k): keep the click handlers so a check can press a tab
        return [...this.innerHTML.matchAll(/data-k="([^"]+)"/g)].map(m => ({ dataset: { k: m[1] }, set onclick(f) { self.tabs[m[1]] = f; } })); },
      querySelector: () => null, getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
      addEventListener(t, f) { (this.listeners = this.listeners || {})[t] = (this.listeners[t] || []).concat(f); }, insertAdjacentHTML(p, h) { this.innerHTML += h; },
      appendChild() {}, append() {}, remove() {}, closest: () => null, isConnected: true, getContext: () => null };
    for (const prop of ['innerHTML', 'textContent', 'title']) { let v = ''; Object.defineProperty(e, prop, { get() { return v; }, set(x) { v = x; record(sel, prop, x); }, enumerable: true }); }
    els.set(sel, e); return e;
  }
  const P = { els, el, log, fetched: [], timers: [], status: null, down: false, hang: null, sessions: opts.sessions, plans: opts.plans, stateOverride: opts.state, talk: opts.talk };
  const route = u => {
    const url = new URL('http://x/' + u.replace(/^\//, '')), q = url.searchParams, p = url.pathname.replace(/^\//, '');
    if (p === 'api/state') return P.stateOverride ? Object.assign({}, STATE, P.stateOverride) : STATE;
    if (p === 'api/sessions') return P.sessions || SESSIONS;
    if (p === 'api/talk' && q.get('scope') === 'agents') return ATALK;
    if (p === 'api/talk') return P.talk || TALK;
    if (p === 'api/timeline') return { since: +q.get('since'), lanes: [], orch: [] };
    if (p === 'api/plans') return P.plans || PLANS;
    if (p === 'api/agent') return P.agent ? P.agent(q.get('id')) : { id: q.get('id'), orch_msgs: [], handbacks: [], texts: [], writes: [], reads: [], tool_counts: [], activity: [], spawn_ts: STATE.now, spawn_prompt: '', link: null, partial: null };
    if (p === 'api/file') return { short: 'short-' + q.get('path'), text: 'TEXT', mtime: STATE.now };
    if (p === 'api/event') return { text: 'FULL-' + q.get('idx') };
    return {};
  };
  const ctx = { console, Date: FDate, Math, JSON, URL, URLSearchParams, Promise, Set, Map, Object, Array, String, Number, Infinity, isNaN, encodeURIComponent, decodeURIComponent, AbortController, Error,
    innerWidth: 1200, innerHeight: 900, scrollY: 0, scrollX: 0, location: { search: opts.search !== undefined ? opts.search : '?session=' + STATE.session.id, hash: opts.hash || '', host: 'localhost:8790', href: 'http://x/' },
    localStorage: { _m: {}, getItem(k) { return this._m[k] ?? null; }, setItem(k, v) { this._m[k] = String(v); } },
    document: { querySelector: s => el(s), getElementById: s => el('#' + s), querySelectorAll: () => [], addEventListener() {}, createElement: t => el('new:' + t + ':' + els.size), head: el('head'), body: el('body'), hidden: false,
      get title() { return this._title || ''; }, set title(x) { this._title = x; record('document', 'title', x); }, documentElement: el('html'), fonts: null },
    fetch: (u, opt) => new Promise((resolve, reject) => {
      u = String(u); P.fetched.push(u);
      const dict = I18B.route(dir, u);                                                  // api/i18n and locales/<code>.json come from disk
      if (dict) return resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(dict)) });
      if (P.down) return reject(new TypeError('Failed to fetch'));
      if (P.hang && P.hang.test(u)) return opt && opt.signal && opt.signal.addEventListener('abort', () => reject(Object.assign(new Error('The operation was aborted'), { name: 'AbortError' })));
      const bad = P.status && Object.entries(P.status).find(([re]) => new RegExp(re).test(u));
      if (bad) return resolve({ ok: false, status: bad[1], text: async () => bad[1] === 403 ? 'ERR-403 plain\nsecond line' : JSON.stringify({ error: 'ERR-' + bad[1] }) });
      resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(route(u))) });
    }),
    setInterval: () => 0, setTimeout: (f, ms) => { const t = { id: P.timers.length + 1, f, ms: ms || 0 }; P.timers.push(t); return t.id; }, clearTimeout: id => { P.timers = P.timers.filter(t => t.id !== id); },
    requestAnimationFrame: () => 0, addEventListener() {}, matchMedia: () => ({ matches: false }), scrollBy() {},
    IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  P.ctx = ctx;
  P.run = code => vm.runInContext(code, ctx);
  P.load = async () => { scripts.forEach(s => { vm.runInContext(s.code, ctx); if (s.src === 'i18n.js') I18B.boot(ctx, dir, opts.lang || 'en'); }); await sleep(60); };
  P.fire = (minMs, maxMs = Infinity) => { const hit = t => t.ms >= minMs && t.ms <= maxMs; const due = P.timers.filter(hit); P.timers = P.timers.filter(t => !hit(t)); due.forEach(t => t.f()); };
  // the page's own t(): the words the screen should show for a key. A key that the dictionary lacks gives null, so a check on it fails (and I18N.missing only holds the page's own misses)
  P.T = (key, params) => P.run(`I18N.has(${JSON.stringify(key)}) ? t(${JSON.stringify(key)}, ${JSON.stringify(params || {})}) : null`);
  P.html = sel => (els.get(sel) || { innerHTML: '' }).innerHTML;
  P.text = sel => (els.get(sel) || { textContent: '' }).textContent;
  P.all = () => log.map(x => x.v);
  return P;
}

// what is wrong with everything the page wrote: Hangul that is not data (`data` = strings to take out first; the language names are data too), a [key], a key that no dictionary had
const scan = (P, data = []) => {
  const names = P.run('I18N.langs.map(l => l.name)').concat(data).filter(Boolean).sort((a, b) => b.length - a.length);
  const visible = v => v.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');                  // CSS and HTML comments are not on the screen
  const texts = P.all().map(v => names.reduce((s, n) => s.split(n).join(''), visible(v)));
  const lone = x => { const i = x.search(HAN); return x.slice(Math.max(0, i - 25), i + 25); };
  return { han: texts.filter(v => HAN.test(v)).map(lone).slice(0, 3), keys: P.all().filter(v => KEY.test(v)).map(v => v.match(KEY)[0]).slice(0, 3), missing: P.run('[...I18N.missing]') };
};
const clean = (name, P, data = []) => { const s = scan(P, data); check(name + ': no Hangul outside data, no [key], nothing missing from the dictionary', !s.han.length && !s.keys.length && !s.missing.length, s); };

const agents = STATE.agents.filter(a => a.tag), A = agents[0], B = agents[1];
const EMPTY = over => ({ default: '', sessions: [], projects: [], sources: [Object.assign({ provider: 'claude', dir: '~/.claude', from: 'default', exists: false, sessions: 0, agent_sessions: 0 }, over && over.claude),
  Object.assign({ provider: 'codex', dir: '~/.codex', from: 'default', exists: false, sessions: 0, agent_sessions: null }, over && over.codex)] });

(async () => {
  if (!I18B.has(dir)) { check('i18n: this static dir has i18n.js', false, 'reference statics: no i18n.js'); console.log('FAILED ' + fails); process.exit(1); }

  // ---------- 1) the first screen in English ----------
  let P = makePage('index.html');
  await P.load();
  check('en: the page runs in English (I18N.lang, <html lang>, the dictionary words)', P.run('I18N.lang') === 'en' && P.els.get('html').lang === 'en' && P.T('common.you') !== '[common.you]' && P.T('common.you') !== '', [P.run('I18N.lang'), P.els.get('html').lang]);
  check('en: the first screen is drawn from the state (S, orchestrator card, agent list, feed, header chips)', P.run('!!S') && !!P.html('#orchCard') && !!P.html('#agentList') && !!P.html('#feedItems') && !!P.html('#counters'), P.run('!!S'));
  check('en: the language selector is wired to the current language and named by common.language', P.els.get('#langSel').value === 'en' && P.els.get('#langSel').title === P.T('common.language') && typeof P.els.get('#langSel').onchange === 'function', [P.els.get('#langSel').value, P.els.get('#langSel').title]);
  check('en: <title> is the dashboard title; header and footer times are 24 h without AM/PM', P.ctx.document.title.includes(P.T('page.title.dashboard')) && /\d\d:\d\d:\d\d/.test(P.text('#updated') + P.html('#updated')) && !/[AP]M/.test(P.html('#updated') + P.html('#talkList')),
    [P.ctx.document.title, P.html('#updated')]);

  // ---------- 2) cards ----------
  const orch = P.html('#orchCard'), tok = P.html('#tokCard');
  check('card: orchestrator card says Orchestrator (common.orchestrator) and none of the status words is a key', shows(orch, P.T('common.orchestrator')) && shows(orch, P.T(STATE.orch.state === 'working' ? 'status.running' : 'status.idle')), orch.slice(0, 200));
  check('card: token card has its title (board.tok.title) and a dollar total', shows(tok, P.T('board.tok.title')) && /\$\d/.test(tok), tok.slice(0, 200));
  P.run("ui.oldOpen = true; ui.agentFilter = 'all'; renderAgents()");
  const list = P.html('#agentList');
  check('card: every agent card shows its status in English (status.<code>) and its own title as data', STATE.agents.every(a => shows(list, P.T('status.' + a.status)) && list.includes(esc(a.title))), list.slice(0, 200));
  const cnt = P.html('#counters');
  check('card: header chips carry dictionary words (agents/messages/cost), and the cost chip is a dollar amount', /\$\d/.test(cnt) && !HAN.test(cnt) && cnt.includes('chip'), cnt.slice(0, 200));
  const feed = P.html('#feedItems'), kinds = [...new Set(STATE.feed.map(e => e.kind))];
  check('card: the flow lists every event kind present (but notify, drawn by its title) by its kind.<code> word', kinds.filter(k => !['orch_say', 'user_say', 'notify'].includes(k)).every(k => shows(feed, P.T('kind.' + k))), kinds.filter(k => !['orch_say', 'user_say', 'notify'].includes(k) && !shows(feed, P.T('kind.' + k))));
  check('card: the flow filter tabs are the dictionary words', ['all', 'talk', 'peer', 'user', 'orch', 'status'].every(k => shows(P.html('#feedFilter'), P.T('board.feed.tab.' + k))), P.html('#feedFilter').slice(0, 200));
  const talk = P.html('#talkList');
  check('card: the talk card names the author "You" (common.you) and the orchestrator, with a day heading', shows(talk, P.T('common.you')) && shows(talk, P.T('common.orchestrator')) && /class="day"/.test(talk), talk.slice(0, 200));
  const at = P.html('#atalkList'), atabs = P.html('#atalkTabs');
  check('card: the agent talk card shows its tabs and message kinds in English, and the read line', ['all', 'orch', 'peer'].every(k => shows(atabs, P.T('board.atalk.tab.' + k))) && !HAN.test(at) && at.includes('class="msg'), at.slice(0, 200));
  clean('first screen', P);

  // ---------- 3) the agent drawer, all four tabs ----------
  const cx = STATE.agents.find(a => a.provider === 'codex');
  P.agent = id => ({ id, orch_msgs: [{ ts: STATE.now - 90, summary: 'Start T1', text: 'Please write r1/T1.md' }], handbacks: [{ ts: STATE.now - 20, text: 'Final report body' }],
    texts: [{ ts: STATE.now - 60, text: 'Thinking about the loader' }], writes: [{ ts: STATE.now - 30, path: '/w/r1/T1.md', short: 'r1/T1.md' }], reads: [{ ts: STATE.now - 40, path: '/w/r1/T2.md', short: 'r1/T2.md' }, { ts: STATE.now - 41, path: '/w/src/a.py', short: 'src/a.py' }],
    tool_counts: [['Read', 5], ['Bash', 2]], activity: [{ ts: STATE.now - 50, kind: 'tool', name: 'Read', text: 'a.py' }, { ts: STATE.now - 45, kind: 'msg', text: 'from orchestrator' }, { ts: STATE.now - 44, kind: 'text', text: 'ok' }],
    spawn_ts: STATE.now - 100, spawn_prompt: 'Review the loader', link: id === (cx && cx.id) ? { rule: 'proc', bash_ts: STATE.now - 99, dt: 1.25 } : null, partial: id === (cx && cx.id) ? { skipped_bytes: 52428800 } : null });
  const drawerTabs = ['overview', 'activity', 'messages', 'texts'], drawer = {};
  for (const id of [A.id, cx && cx.id].filter(Boolean)) {
    P.run(`openDrawer(${JSON.stringify(id)})`); await sleep(30);
    for (const tab of drawerTabs) { P.run(`ui.dTab = ${JSON.stringify(tab)}; renderDrawer()`); drawer[id + tab] = P.html('#dBody'); }
    P.run("ui.dTab = 'overview'; renderDrawer()");
    const tabsHtml = P.html('#dTabs');
    const tabWords = { overview: P.T('board.drawer.tab.overview'), activity: P.T('board.drawer.tab.activity'), messages: P.T('board.drawer.tab.messages', { count: 3 }), texts: P.T('board.drawer.tab.texts', { count: 1 }) };   // 1 + 1 + 1 messages, 1 text
    check(`drawer ${id === A.id ? 'Claude' : 'Codex'} agent: the status pill and the four tab labels (board.drawer.tab.*, with counts) are the dictionary's`, shows(P.html('#dTitle'), P.T('status.' + STATE.agents.find(a => a.id === id).status)) && drawerTabs.every(k => shows(tabsHtml, tabWords[k])), [tabWords, tabsHtml.slice(0, 250)]);
  }
  check('drawer overview: the box headings, token rows and tool counts are English words (board.drawer.*)', ['board.drawer.now', 'board.drawer.written', 'board.drawer.reads', 'board.drawer.tokensBox', 'board.drawer.toolsBox', 'board.drawer.recent'].every(k => shows(drawer[A.id + 'overview'], P.T(k))), drawer[A.id + 'overview'].slice(0, 200));
  check('drawer activity: a "message from the orchestrator" row uses board.drawer.gotMsg; tool and text rows keep the data', shows(drawer[A.id + 'activity'], P.T('board.drawer.gotMsg')) && drawer[A.id + 'activity'].includes('a.py'), drawer[A.id + 'activity'].slice(0, 200));
  check('drawer messages: the first instruction, the orchestrator message (summary as data) and the final report headings', shows(drawer[A.id + 'messages'], P.T('board.drawer.msg.first')) && shows(drawer[A.id + 'messages'], P.T('board.drawer.msg.handback')) && shows(drawer[A.id + 'messages'], P.T('board.drawer.msg.orch', { summary: 'Start T1' })), drawer[A.id + 'messages'].slice(0, 250));
  if (cx) check('drawer Codex agent: the link line (rule from board.link.<rule>, delay, partial size) is English', shows(P.html('#dSub'), P.T('board.link.line', { rule: P.T('board.link.proc') })) && shows(P.html('#dSub'), P.T('board.link.partial', { size: '52 MB' })), P.html('#dSub'));
  P.run("DETAIL.link = { rule: 'a_rule_the_dictionary_lacks' }; renderDrawer()");
  check('drawer: an unknown link rule is shown as it is (never a [key])', shows(P.html('#dSub'), 'a_rule_the_dictionary_lacks') && !KEY.test(P.html('#dSub')), P.html('#dSub'));
  P.run('closeDrawer()');
  clean('after the drawer', P);

  // ---------- 4) new server fields: titles, notify status, questions ----------
  P = makePage('index.html'); await P.load();
  const withKey = STATE.feed.filter(e => e.title_i18n && !['orch_say', 'user_say'].includes(e.kind) && e.kind !== 'spawn');
  const feed2 = P.html('#feedItems');
  check('event title (title_i18n): the old Korean title of a server-written title is never shown', withKey.length > 5 && withKey.every(e => HAN.test(e.title) && !feed2.includes(esc(e.title))), withKey.filter(e => feed2.includes(esc(e.title))).map(e => e.title));
  check('event title (title_i18n): each such event shows English words: its title (title_i18n) or, when only the kind is drawn, its kind word', withKey.every(e => shows(feed2, P.T(e.title_i18n.key, e.title_i18n.params)) || shows(feed2, P.T('kind.' + e.kind))), withKey.filter(e => !shows(feed2, P.T(e.title_i18n.key)) && !shows(feed2, P.T('kind.' + e.kind))).map(e => e.title_i18n.key));
  check('event title (notify): the status word comes from event.notify.<status> and the old "작업 끝" is not shown', STATE.feed.filter(e => e.kind === 'notify').every(e => shows(feed2, P.T(e.title_i18n.key)) && !feed2.includes(esc(e.title))), P.T('event.notify.done'));
  const L1x = (STATE.agents[1] || A).id;
  P.run(`S.feed = S.feed.concat([{ idx: 900, ts: ${STATE.now}, kind: 'notify', from: ${JSON.stringify(A.id)}, to: 'orch', title: '실패', text: 'it broke', agent: ${JSON.stringify(A.id)}, title_i18n: { key: 'event.notify.failed', params: {} }, title_is_default: false, status: 'failed' },
    { idx: 901, ts: ${STATE.now}, kind: 'notify', from: ${JSON.stringify(A.id)}, to: 'orch', title: '중지됨', text: '', agent: ${JSON.stringify(A.id)}, title_i18n: { key: 'event.notify.killed', params: {} }, title_is_default: false, status: 'killed' },
    { idx: 902, ts: ${STATE.now}, kind: 'spawn', from: 'orch', to: ${JSON.stringify(A.id)}, title: 'T9 Data title', text: '', agent: ${JSON.stringify(A.id)} },
    { idx: 903, ts: ${STATE.now}, kind: 'handback', from: ${JSON.stringify(A.id)}, to: 'orch', title: '최종 보고', text: 'x', agent: ${JSON.stringify(A.id)}, title_i18n: { key: 'event.not.in.the.dictionary', params: {} }, title_is_default: false },
    { idx: 904, ts: ${STATE.now}, kind: 'orch_msg', from: 'orch', to: ${JSON.stringify(A.id)}, title: 'DEFAULT-TITLE-MARK', text: 'y', agent: ${JSON.stringify(A.id)}, title_i18n: { key: 'event.message.title', params: {} }, title_is_default: true },
    { idx: 905, ts: ${STATE.now}, kind: 'spawn', from: 'orch', to: ${JSON.stringify(A.id)}, title: 'Claude Code 실행', text: '', agent: ${JSON.stringify(A.id)}, title_i18n: { key: 'event.spawn_cli.title', params: {} }, title_is_default: true },
    { idx: 906, ts: ${STATE.now}, kind: 'agent_msg', from: ${JSON.stringify(A.id)}, to: ${JSON.stringify(L1x)}, title: '메시지', text: 'z', agent: ${JSON.stringify(A.id)}, title_i18n: { key: 'event.message.title', params: {} }, title_is_default: true }]); renderFeed()`);
  const f3 = P.html('#feedItems');
  check('event title: failed and stopped notifications show the words of event.notify.failed / .killed', shows(f3, P.T('event.notify.failed')) && shows(f3, P.T('event.notify.killed')), f3.slice(-400));
  check('event title: a title that is data (a spawn description) is shown as it is, without a key', f3.includes('T9 Data title'), f3.slice(-300));
  check('event title: a title_i18n key that the dictionary lacks falls back to the old title (never a [key])', !KEY.test(f3) && f3.includes('최종 보고'), P.run('[...I18N.missing]'));
  check('event title: a title the server made up for the kind (title_is_default) shows the kind only, not "kind · kind"', !f3.includes('DEFAULT-TITLE-MARK') && !new RegExp('<span class="kind">[^<]* · ' + esc(P.T('event.message.title')) + '</span>').test(f3) && !/(<span class="kind">)([^<·]+?) · \2<\/span>/.test(f3), f3.slice(-300));
  check('event title: a made-up title that tells more than the kind word stays ("Assigned · Claude Code run"), one the kind word already contains goes ("Agent message · Message")',
    new RegExp('<span class="kind">' + esc(P.T('kind.spawn')) + ' · ' + esc(P.T('event.spawn_cli.title')) + '</span>').test(f3) && !new RegExp('<span class="kind">' + esc(P.T('kind.agent_msg')) + ' · ' + esc(P.T('event.message.title')) + '</span>').test(f3) && new RegExp('<span class="kind">' + esc(P.T('kind.agent_msg')) + '</span>').test(f3), [...f3.matchAll(/<span class="kind">[^<]*<\/span>/g)].map(m => m[0]).slice(0, 8));
  const talkQs = TALK.items.filter(e => Array.isArray(e.questions));
  check('questions: a choice question (questions field) is drawn from its structure in the talk card', talkQs.length > 0 && talkQs.every(e => shows(P.html('#talkList'), e.questions[0].question)), talkQs.map(e => e.idx));
  P.run(`ui.talk = ui.talk.concat([{ idx: 950, ts: ${STATE.now}, kind: 'orch_ask', from: 'orch', to: 'user', title: '선택지 질문', text: '**질문** 몇 번?\\n- 하나', title_i18n: { key: 'event.orch_ask.title', params: {} }, title_is_default: true,
    questions: [{ header: null, question: 'How many rounds?', options: [{ label: 'One', description: 'Fast' }, { label: 'Two', description: null }] }] }]); renderTalk()`);
  const msgOf = (html, idx) => { const i = html.indexOf(`data-idx="${idx}"`); const j = html.indexOf('data-idx="', i + 10); return i < 0 ? '' : html.slice(i, j < 0 ? undefined : j); };
  const tq = msgOf(P.html('#talkList'), 950);
  check('questions: a header-less question gets the default header (event.ask.header) in its own message, the options follow, no Korean from the old text', shows(tq, P.T('event.ask.header')) && tq.includes('How many rounds?') && tq.includes('One') && !tq.includes('몇 번') && !HAN.test(tq), tq.slice(0, 300));
  check('questions: the same event opens in the modal with the English text', (() => { P.run('openTalkMsg(950)'); return P.html('#mBody').includes('How many rounds?') && shows(P.html('#mBody'), P.T('event.ask.header')) && !HAN.test(P.html('#mBody')); })(), P.html('#mBody').slice(0, 200));
  P.run('closeModal()');

  // ---------- 4b) link reliability: the guess mark on an agent, and the "not linked" box ----------
  P = makePage('index.html'); await P.load();
  const [L0, L1, L2, L3] = STATE.agents.map(a => a.id);
  P.run(`S.agents[0].link = { rule: 'time', certain: false }; S.agents[1].link = { rule: 'session', certain: false }; S.agents[2].link = { rule: 'a_new_rule', certain: false }; S.agents[3].link = { rule: 'prompt', certain: true };
    ui.oldOpen = true; ui.agentFilter = 'all'; renderAgents()`);
  const card = id => (P.html('#agentList').split('<div class="card agent ').find(c => c.includes('data-agent="' + id + '"')) || '');
  const marks = h => [...h.matchAll(/<span class="guess" title="([^"]*)">([^<]*)<\/span>/g)].map(m => [m[1], m[2]]);
  check('link guess: an agent linked by a guess (certain false) gets the small mark (board.link.guess) in its card, the tooltip is the rule\'s sentence (board.link.tip.<rule>)',
    (m => m.length === 1 && m[0][1] === P.T('board.link.guess') && m[0][0] === esc(P.T('board.link.tip.time')))(marks(card(L0))) && P.T('board.link.tip.time').startsWith('Guess'), card(L0).slice(0, 400));
  check('link guess: the Codex-style rule session has its own sentence, an unknown rule gets the general one (never a [key])', (marks(card(L1))[0] || [])[0] === esc(P.T('board.link.tip.session')) && (marks(card(L2))[0] || [])[0] === esc(P.T('board.link.tip.other')) && !KEY.test(card(L1) + card(L2)), [marks(card(L1)), marks(card(L2))]);
  check('link guess: a certain link has no mark, and neither has an agent from a server that sends no link field', marks(card(L3)).length === 0 && marks(card(STATE.agents[4] ? STATE.agents[4].id : L3)).length === 0, card(L3).slice(0, 300));
  P.run(`delete S.agents[4].link; renderAgents()`);
  check('link guess: no link field at all (an older server) -> no mark', STATE.agents.length < 5 || marks(card(STATE.agents[4].id)).length === 0, '');
  P.run(`openDrawer(${JSON.stringify(L0)})`); await sleep(30);
  check('link guess: the drawer title carries the mark too', marks(P.html('#dTitle')).length === 1, P.html('#dTitle').slice(0, 300));
  P.run('closeDrawer()');
  P.run(`ui.atalk = [{ idx: 700, ts: ${STATE.now}, kind: 'orch_msg', from: 'orch', to: ${JSON.stringify(L0)}, title: 'm', text: 'hello', agent: ${JSON.stringify(L0)} }, { idx: 701, ts: ${STATE.now}, kind: 'orch_msg', from: 'orch', to: ${JSON.stringify(L3)}, title: 'm', text: 'hello', agent: ${JSON.stringify(L3)} }]; ui.atalkLoaded = true; renderAtalk({ force: true })`);
  check('link guess: the agent-talk card puts the mark next to the name of the guessed agent only', marks(P.html('#atalkList')).length === 1, P.html('#atalkList').slice(0, 300));
  P.run('S.unlinked = undefined; ui.unlinkedOpen = false; renderAgents()');
  check('not linked: a server without `unlinked` (older) or with an empty list shows nothing', !P.html('#agentList').includes('unlinkedToggle'), '');
  P.run('S.unlinked = []; renderAgents()');
  check('not linked: an empty list shows nothing', !P.html('#agentList').includes('unlinkedToggle'), '');
  P.run(`S.unlinked = [{ id: 'c11d0003-0000-4000-8000-000000000003', provider: 'claude', started: ${STATE.now - 300}, cwd: '~/work/<b>x</b>', reason: 'ambiguous' },
    { id: 'c11d0004-0000-4000-8000-000000000004', provider: 'codex', started: ${STATE.now - 200}, cwd: '~/work/y', reason: 'ended_before_seen' },
    { id: 'c11d0005-0000-4000-8000-000000000005', provider: 'claude', started: ${STATE.now - 100}, cwd: '~/work/z', reason: 'no_matching_call' },
    { id: 'c11d0006-0000-4000-8000-000000000006', provider: 'claude', started: ${STATE.now - 50}, cwd: '', reason: 'a_new_reason' }]; renderAgents()`);
  let ul = P.html('#agentList');
  check('not linked: a folded line with the count (board.unlinked.title, plural) is shown, no item yet', shows(ul, P.T('board.unlinked.title', { count: 4 })) && !ul.includes('card agent unlinked') && /id="unlinkedToggle"[^>]*>▸/.test(ul), ul.slice(-300));
  P.run('S.unlinked = S.unlinked.slice(0, 1); renderAgents()');
  check('not linked: one item reads in the singular', shows(P.html('#agentList'), P.T('board.unlinked.title', { count: 1 })) && P.T('board.unlinked.title', { count: 1 }).includes('1 child run not'), P.T('board.unlinked.title', { count: 1 }));
  P.run(`S.unlinked.push({ id: 'c11d0004-0000-4000-8000-000000000004', provider: 'codex', started: ${STATE.now - 200}, cwd: '~/work/y', reason: 'ended_before_seen' }, { id: 'c11d0005-0000-4000-8000-000000000005', provider: 'claude', started: ${STATE.now - 100}, cwd: '~/work/z', reason: 'no_matching_call' }, { id: 'c11d0006-0000-4000-8000-000000000006', provider: 'claude', started: ${STATE.now - 50}, cwd: '', reason: 'a_new_reason' }); ui.unlinkedOpen = true; renderAgents()`);
  ul = P.html('#agentList');
  const items = ul.split('card agent unlinked').slice(1);
  check('not linked: opened, every item shows provider, start time, folder, short id and the reason from board.unlinked.reason.<reason>',
    items.length === 4 && shows(items[0], 'Claude Code') && shows(items[1], 'Codex') && shows(items[0], P.T('board.drawer.started', { time: P.run(`hm(${STATE.now - 300})`) })) && shows(items[1], '~/work/y') && shows(items[1], 'c11d0004')
    && shows(items[0], P.T('board.unlinked.reason.ambiguous')) && shows(items[1], P.T('board.unlinked.reason.ended_before_seen')) && shows(items[2], P.T('board.unlinked.reason.no_matching_call')), items.map(x => x.slice(0, 200)));
  check('not linked: the folder is escaped, an unknown reason shows as it came (never a [key]), the explanation line is there', ul.includes('~/work/&lt;b&gt;x&lt;/b&gt;') && !ul.includes('<b>x</b>') && shows(items[3], 'a_new_reason') && !KEY.test(ul) && shows(ul, P.T('board.unlinked.note')), ul.slice(-500));
  clean('not linked / guess', P, ['<b>x</b>', 'a_new_reason']);

  // ---------- 5) alerts (title_i18n / text_i18n) ----------
  P = makePage('index.html'); await P.load();
  const alerts = [
    { id: 'ask:1', level: 'decide', title: '오케스트레이터가 선택지를 묻고 있습니다', text: 'Which? — A / B', ts: STATE.now - 5, agent: null, title_i18n: { key: 'alert.ask.title', params: {} } },
    { id: 'say:1', level: 'decide', title: '오케스트레이터가 답을 기다립니다', text: 'Let me know which option you prefer.', ts: STATE.now - 6, agent: null, title_i18n: { key: 'alert.say.title', params: {} } },
    { id: 'turn:1', level: 'info', title: '진행 중인 에이전트가 없고 오케스트레이터가 다음 지시를 기다립니다', text: 'All done.', ts: STATE.now - 7, agent: null, title_i18n: { key: 'alert.turn.title', params: {} } },
    { id: 'stall:1', level: 'check', title: 'T1-A — 7분째 활동 없음', text: 'running tests', ts: STATE.now - 8, agent: A.id, title_i18n: { key: 'alert.stall.title', params: { name: 'T1-A', minutes: 7 } } },
    { id: 'fail:1', level: 'check', title: 'T1-A — 실패', text: 'it broke', ts: STATE.now - 9, agent: A.id, title_i18n: { key: 'alert.fail.failed.title', params: { name: 'T1-A' } } },
    { id: 'hb:1', level: 'check', title: 'T1-A 보고에 사용자 결정·승인 항목', text: 'Needs your approval before merging', ts: STATE.now - 10, agent: A.id, title_i18n: { key: 'alert.hb.title', params: { name: 'T1-A' } } },
    { id: 'cxlimit:1', level: 'check', title: '◆ Codex 주간 한도 93% — 남은 Codex 작업이 멈출 수 있음', text: '계정 전체 값 · 기록 09/30 21:00 · 재설정 10/04 01:00', ts: STATE.now - 11, agent: null,
      title_i18n: { key: 'alert.cxlimit.percent.title', params: { percent: 93 } }, text_i18n: { key: 'alert.cxlimit.text', params: { as_of: STATE.now - 3600, resets_at: STATE.now + 86400 } } }];
  P.run(`S.alerts = ${JSON.stringify(alerts)}; ui.alertsOpen = true; renderAlerts()`);
  const al = P.html('#alerts');
  check('alerts: titles come from alert.<key>.title with their parameters (name, minutes, percent); the old Korean titles are not shown', alerts.every(a => shows(al, P.T(a.title_i18n.key, a.title_i18n.params))) && alerts.every(a => !al.includes(esc(a.title))), alerts.filter(a => !shows(al, P.T(a.title_i18n.key, a.title_i18n.params))).map(a => a.id));
  check('alerts: the level words (decide / check / info), the head and the "guessed from the last message" note are English', ['decide', 'check', 'info'].every(l => shows(al, P.T('board.alert.level.' + l))) && shows(al, P.T('board.alert.head')) && shows(al, P.T('board.alert.guess')), al.slice(0, 200));
  check('alerts: the Codex limit text is built from epoch seconds in this screen\'s language (24 h, no Korean)', !al.includes('계정 전체') && /Account-wide|recorded/.test(al) && /\d\d:\d\d/.test(al.slice(al.indexOf('Account-wide'))), al.slice(al.indexOf('Account-wide') - 5, al.indexOf('Account-wide') + 120));
  check('alerts: a text that is data (the orchestrator\'s own words) stays as it is', al.includes('Let me know which option you prefer.') && al.includes('Needs your approval before merging'), al.slice(0, 100));
  P.run("S.alerts = [{ id: 'old:1', level: 'check', title: '옛 서버의 제목', text: '옛 서버의 본문', ts: " + STATE.now + ", agent: null }]; ui.alertsOpen = true; renderAlerts()");
  check('alerts: an old server without title_i18n still shows its Korean title and text (the old fields are kept)', P.html('#alerts').includes('옛 서버의 제목') && P.html('#alerts').includes('옛 서버의 본문'), P.html('#alerts').slice(0, 200));
  P.run(`S.alerts = ${JSON.stringify(alerts)}; ui.alertsOpen = true; renderAlerts()`);
  P.run("S.alerts = []; renderAlerts()");
  clean('alerts (the old-server alert above is data from an older server: excluded)', P, ['옛 서버의 제목', '옛 서버의 본문']);

  // ---------- 6) the plan bar ----------
  P = makePage('index.html'); await P.load();
  const bar = c => { P.run('renderPlanBar(' + JSON.stringify({ now: STATE.now, claude: c, codex: null }) + ')'); return P.html('#planBar'); };
  const W = (pct, d) => ({ percent: pct, resets_at: STATE.now + d });
  const base = { source: 'cache', error: null, error_info: null, plan: 'Max 5x', scoped: [], extra: null, hits: {}, usage_api: true, as_of: STATE.now - 30, five_hour: W(12, 3600), seven_day: W(40, 86400 * 3) };
  let h = bar(base);
  check('plan bar: names, percentages and the "fetched" time are English words and numbers', shows(h, P.T('board.planbar.five')) && shows(h, P.T('board.planbar.week')) && h.includes('12%') && h.includes('40%') && /\d\d:\d\d/.test(h), h.slice(0, 250));
  for (const [code, params] of [['http_error', { status: 429 }], ['http_error', { status: 500 }], ['login_missing', {}], ['token_expired', {}], ['fetch_failed', { error: 'URLError' }]]) {
    h = bar(Object.assign({}, base, { error: '조회 실패(' + code + ')', error_info: { code, params } }));
    check(`plan bar: error_info ${code}${params.status ? ' ' + params.status : ''} shows plan.error.${code}, not the old Korean error`, shows(h, P.T('plan.error.' + code, params)) && !h.includes('조회 실패') && !HAN.test(h), h.slice(0, 300));
  }
  h = bar(Object.assign({}, base, { error: '조회 실패(HTTP 429)', error_info: { code: 'brand_new_code', params: {} } }));
  check('plan bar: an error code the dictionary lacks falls back to the old error text (never a [key])', h.includes('조회 실패(HTTP 429)') && !KEY.test(h), h.slice(0, 300));
  h = bar(Object.assign({}, base, { error: '조회 실패(HTTP 500)', error_info: null }));
  check('plan bar: an older server without error_info still shows its Korean error', h.includes('조회 실패(HTTP 500)'), h.slice(0, 300));
  h = bar(Object.assign({}, base, { usage_api: false, as_of: STATE.now - 3000, error: '조회 실패(HTTP 429)', error_info: { code: 'http_error', params: { status: 429 } } }));
  check('plan bar: usage API off + cache: the "recorded … refresh with /usage" line, and no error even if one was sent', shows(h, P.T('board.planbar.recordedRefresh', { time: P.run(`I18N.date(${STATE.now - 3000}, 'time')`) })) && !h.includes('class="warn"'), h.slice(0, 300));
  h = bar(Object.assign({}, base, { usage_api: false, as_of: null, five_hour: null, seven_day: null }));
  check('plan bar: usage API off + no cache: the one gray "open /usage" line', shows(h, P.T('board.planbar.noCache')), h);
  const cxBar = x => { P.run('renderPlanBar(' + JSON.stringify({ claude: null, codex: Object.assign({ plan_type: 'prolite', window_minutes: 10080, secondary: null, credits: null, as_of: STATE.now - 60 }, x) }) + ')'); return P.html('#planBar'); };
  const cxHits = [cxBar({ reached: true, stale: false, resets_at: STATE.now + 3600, used_percent: 100 }), cxBar({ reached: false, stale: false, resets_at: STATE.now + 3600, used_percent: 42 }),
    cxBar({ reached: false, stale: true, resets_at: STATE.now - 3600, used_percent: 42 }), cxBar({ reached: false, stale: false, resets_at: STATE.now + 3600, used_percent: 42, credits: { has_credits: true, unlimited: false, balance: '12.5' } })];
  check('plan bar: the Codex limit in four states (reached, percent, stale estimate, credits) has no Korean and no [key]; a percentage shows when fresh', cxHits.every(x => x && !HAN.test(x) && !KEY.test(x)) && cxHits[1].includes('42%') && !cxHits[2].includes('42%'), cxHits.map(x => x.slice(-160)));
  check('plan bar: a stale Codex value shows a dash, the estimate note and the "reset since the last record" tooltip (board.planbar.cxStaleTitle)', cxHits[2].includes('<b>—</b>') && shows(cxHits[2], P.T('board.planbar.cxStaleTitle')) && /\(est\.\)|est\./.test(P.T('board.planbar.estimate', { time: '0' })) === /\(est\.\)|est\./.test(cxHits[2]), cxHits[2].slice(-200));
  clean('plan bar (the two old-server Korean errors shown above are data from an older server: excluded)', P, ['조회 실패(HTTP 429)', '조회 실패(HTTP 500)']);

  // ---------- 7) the diagnosis card: every kind, in English ----------
  const diag = P_ => P_.html('#diag');
  const diagChecks = (name, P_, titleKey, labelKey, extra) => {
    check(`diagnosis ${name}: the card title is ${titleKey} and the header label is ${labelKey}`, shows(diag(P_), P_.T(titleKey, extra || {})) && shows(P_.html('#updated'), P_.T(labelKey)), [diag(P_).slice(0, 120), P_.html('#updated')]);
    clean(`diagnosis ${name}`, P_);
  };
  P = makePage('index.html', { search: '', sessions: EMPTY() }); P.status = { 'api/state': 404 }; await P.load();
  diagChecks('empty HOME (no folders)', P, 'diag.empty.title', 'diag.label.empty');
  check('diagnosis empty HOME: each provider row has its folder, its origin (default) and "Folder not found"; the hints name --claude-config-dir and --codex-home', ['~/.claude/projects', '~/.codex/sessions'].every(d => diag(P).includes(d)) && shows(diag(P), P.T('diag.count.nofolder')) && diag(P).includes('--claude-config-dir') && diag(P).includes('--codex-home'), diag(P).slice(0, 300));
  P = makePage('index.html', { search: '', sessions: EMPTY({ claude: { exists: true, from: 'flag', dir: '~/work/cl', sessions: 0, agent_sessions: 0 }, codex: { exists: true, from: 'env', dir: '~/cx' } }) }); await P.load();
  check('diagnosis empty (folders exist, 0 sessions): counts with plurals and the origin words (flag / env) are English', shows(diag(P), P.T('diag.count.sessions', { count: 0 })) && shows(diag(P), P.T('diag.count.recent', { count: 0 })) && !HAN.test(diag(P)), diag(P).slice(0, 300));
  P = makePage('index.html', { search: '', sessions: EMPTY({ claude: { exists: true, sessions: 1, agent_sessions: 1 }, codex: { exists: true, sessions: 3 } }) }); await P.load();
  check('diagnosis plurals: 1 session / 3 sessions (one and other forms)', shows(diag(P), P.T('diag.count.sessions.agents', { count: 1, agents: 1 })) && shows(diag(P), P.T('diag.count.recent', { count: 3 })) && !HAN.test(diag(P)), diag(P).slice(0, 300));
  clean('diagnosis plurals', P);
  P = makePage('index.html', { search: '', sessions: SESSIONS }); P.down = true; await P.load();
  diagChecks('server off', P, 'diag.down.title', 'diag.label.down');
  P = makePage('index.html', { search: '', sessions: SESSIONS }); P.status = { 'api/sessions': 403 }; await P.load();
  check('diagnosis 403 (text/plain body): the card says the server refused and shows the server\'s line of the page language only (the body has one line per language: the first is English)', shows(diag(P), P.T('diag.http.title', { status: 403 })) && diag(P).includes('ERR-403 plain') && !diag(P).includes('second line') && shows(P.html('#updated'), P.T('diag.label.http')) && !HAN.test(diag(P)), diag(P).slice(0, 300));
  P = makePage('index.html', { search: '', sessions: SESSIONS }); P.status = { 'api/sessions': 500 }; await P.load();
  check('diagnosis 500 (JSON body): the title uses the status, the server\'s error text is data', shows(diag(P), P.T('diag.http.title', { status: 500 })) && diag(P).includes('ERR-500') && !HAN.test(diag(P)), diag(P).slice(0, 300));
  P = makePage('index.html', { search: '', sessions: SESSIONS }); P.hang = /api\/sessions/; await P.load(); P.fire(10000, 10000); await sleep(30);
  diagChecks('no answer', P, 'diag.timeout.title', 'diag.label.down');
  check('diagnosis no answer: the body names the 10 s limit', shows(diag(P), P.T('diag.timeout.body', { sec: 10 })), diag(P).slice(0, 300));
  P = makePage('index.html', { search: '?session=deadbeef-0000-4000-8000-000000000000', sessions: SESSIONS }); P.status = { 'api/state': 404 }; await P.load();
  diagChecks('missing session', P, 'diag.missing.title', 'diag.label.missing');
  check('diagnosis missing session: the session id is data, the "open the default session" link stays', diag(P).includes('deadbeef-0000') && shows(diag(P), P.T('diag.missing.openDefault')) && diag(P).includes('href="?"'), diag(P).slice(0, 300));
  P = makePage('index.html'); await P.load(); P.down = true; await P.run('tick()'); await sleep(20);
  check('later outage (after a session was shown): the header says Disconnected (board.live.down), the board stays and no diagnosis card replaces it', shows(P.html('#updated'), P.T('board.live.down', { sec: P.run('Math.round((Date.now() - lastOk) / 1000)') })) && /dead/.test(P.html('#updated')) && P.html('#diag') === '', P.html('#updated'));
  clean('later outage', P);

  // ---------- 8) Korean data passes through unchanged and is the only Hangul ----------
  const KO = { session: '한글 세션 제목', agent: '한글 에이전트 설명', say: '한글로 쓴 사용자 지시입니다', topic: '한글 주제 제목' };
  const st = JSON.parse(JSON.stringify(STATE));
  st.session.title = KO.session;
  st.agents[0].title = KO.agent; st.agents[0].description = KO.agent;
  if (st.debates[0] && st.debates[0].topics[0]) st.debates[0].topics[0].title = KO.topic;
  const maxIdx = Math.max(...st.feed.map(e => e.idx));
  const talk2 = JSON.parse(JSON.stringify(TALK)); talk2.items.push({ idx: maxIdx + 1, ts: STATE.now, kind: 'user_say', from: 'user', to: 'orch', title: '사용자 지시', text: KO.say, agent: null, title_i18n: { key: 'event.user_say.title', params: {} }, title_is_default: true });
  st.feed.push(talk2.items[talk2.items.length - 1]);
  P = makePage('index.html', { state: { session: st.session, agents: st.agents, debates: st.debates, feed: st.feed }, talk: talk2 }); await P.load();
  P.run("ui.oldOpen = true; ui.agentFilter = 'all'; renderAll()");
  P.run(`openDrawer(${JSON.stringify(st.agents[0].id)})`); await sleep(30);
  const all = P.all().join('\n');
  check('Korean data: the session title, an agent\'s title and description, a topic title and the user\'s own words show unchanged', all.includes(KO.session) && all.includes(KO.agent) && all.includes(KO.say) && (!st.debates[0] || all.includes(KO.topic)), Object.entries(KO).filter(([, v]) => !all.includes(v)).map(([k]) => k));
  clean('Korean data present (data taken out first)', P, Object.values(KO));

  // ---------- 9) the office page ----------
  P = makePage('game.html'); await P.load();
  check('game.html in English: the page runs in English, the header and the cost chip are English words and a dollar amount', P.run('I18N.lang') === 'en' && /\$\d/.test(P.text('#cost')) && shows(P.ctx.document.title, P.T('office.page.title')) && !HAN.test(P.text('#cost') + P.text('#upd')), [P.text('#cost'), P.ctx.document.title]);
  check('game.html in English: the selector is wired (current language en)', P.els.get('#langSel').value === 'en', P.els.get('#langSel').value);
  clean('game.html', P);
  P = makePage('game.html', { search: '', sessions: EMPTY() }); P.status = { 'api/state': 404 }; await P.load();
  check('game.html first screen: the diagnosis card is the same English card (no sessions to open)', shows(diag(P), P.T('diag.empty.title')) && !HAN.test(diag(P)), diag(P).slice(0, 200));
  clean('game.html diagnosis', P);

  // ---------- 10) the same page in Korean still shows Korean (the language switch works both ways) ----------
  P = makePage('index.html', { lang: 'ko' }); await P.load();
  check('ko: the same fixture in Korean shows the Korean words (not the English ones) in the cards', P.run('I18N.lang') === 'ko' && shows(P.html('#orchCard'), P.T('common.orchestrator')) && HAN.test(P.T('common.orchestrator')) && !shows(P.html('#orchCard'), I18B.packs(dir).en.messages['common.orchestrator']), P.html('#orchCard').slice(0, 200));

  P.run(`S.agents[0].link = { rule: 'time', certain: false }; S.unlinked = [{ id: 'c11d0003-0000-4000-8000-000000000003', provider: 'claude', started: ${STATE.now - 300}, cwd: '~/work/x', reason: 'ambiguous' }]; ui.oldOpen = true; ui.agentFilter = 'all'; ui.unlinkedOpen = true; renderAgents()`);
  check('ko: the guess mark, its tooltip and the "not linked" box are Korean', HAN.test(P.T('board.link.guess')) && shows(P.html('#agentList'), P.T('board.link.guess')) && shows(P.html('#agentList'), P.T('board.link.tip.time')) && HAN.test(P.T('board.link.tip.time'))
    && shows(P.html('#agentList'), P.T('board.unlinked.title', { count: 1 })) && shows(P.html('#agentList'), P.T('board.unlinked.reason.ambiguous')) && HAN.test(P.T('board.unlinked.title', { count: 1 })), P.html('#agentList').slice(-400));

  console.log(fails ? 'FAILED ' + fails : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log('HARNESS ERROR', e); process.exit(2); });
