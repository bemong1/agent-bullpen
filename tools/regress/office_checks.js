// Synthetic checks of the office's texts (static/game.js, game-demo.js) in both languages. No browser, no npm: the fake DOM of game_snap.js with a virtual clock.
//  - the sentences the code builds: whiteboard stage, lounge sign, tags, legend, demo menu and banner, bubbles from event fields
//  - the new event fields are read when present (title_is_default, title_i18n, questions) and the old fields still work when they are not
//  - a whole demo run in each language draws no [key] and, in English, no Hangul
// usage: node office_checks.js <static dir> <fixture prefix>      (prints PASS/FAIL per check, exit code 1 if any FAIL)
const fs = require('fs'), vm = require('vm'), path = require('path');
const [dir, fx] = process.argv.slice(2);
const I18B = require('./i18n_boot');
const STATE = JSON.parse(fs.readFileSync(fx + '_state.json', 'utf8'));
const HAN = /[ㄱ-ㆎ가-힣]/;
let fails = 0;
const check = (name, ok, info) => { console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (info === undefined ? '' : typeof info === 'string' ? info : JSON.stringify(info)))); if (!ok) fails++; };

function world(lang) {
  let NOW = Math.round(STATE.now * 1000) + 5000, timers = [], tid = 0, raf = null, nid = 0;
  class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(NOW); } static now() { return NOW; } }
  const texts = [];                                           // every text / html / title assigned during the run
  const ctx2d = () => ({ set fillStyle(v) {}, get fillStyle() { return ''; }, globalAlpha: 1, imageSmoothingEnabled: false, fillRect() {}, clearRect() {}, drawImage() {} });
  function mk(tag) {
    const e = { tag, _id: tag + (nid++), children: [], parent: null, style: {}, dataset: {}, className: '', offsetWidth: 100, offsetHeight: 20, _q: {},
      append(...k) { k.forEach(c => this.appendChild(c)); }, appendChild(k) { if (k.parent) k.remove(); k.parent = this; this.children.push(k); return k; },
      remove() { if (this.parent) { this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; } },
      addEventListener() {}, querySelector(s) { return this._q[s] || (this._q[s] = mk('q')); }, querySelectorAll: () => [],
      getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0 }), clientWidth: 1600, getContext() { return this._c || (this._c = ctx2d()); } };
    for (const prop of ['textContent', 'innerHTML', 'title']) {
      let v = '';
      Object.defineProperty(e, prop, { get() { return v; }, set(x) { v = x; if (x) texts.push(String(x)); }, enumerable: true });
    }
    return e;
  }
  const ctx = { console, Date: FDate, Math, JSON, Map, Set, Object, Array, String, Number, Promise, innerWidth: 1600, innerHeight: 900, scrollX: 0, scrollY: 0,
    document: { createElement: mk, head: mk('head'), body: mk('body'), hidden: false, querySelectorAll: () => [], addEventListener() {} },
    requestAnimationFrame: f => { raf = f; }, addEventListener() {},
    setTimeout: (f, ms) => { const t = { id: ++tid, at: NOW + (ms || 0), f }; timers.push(t); return t.id; }, clearTimeout: id => { timers = timers.filter(t => t.id !== id); },
    IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  for (const f of I18B.scripts(dir, ['game-art.js', 'game.js', 'game-demo.js'])) {
    vm.runInContext(fs.readFileSync(path.join(dir, f), 'utf8'), ctx);
    if (f === 'i18n.js') I18B.boot(ctx, dir, lang);
  }
  const box = mk('div'), g = ctx.AgentGame.mount(box, { mode: 'strip' });
  const frame = ms => { NOW += ms; const due = timers.filter(t => t.at <= NOW).sort((a, b) => a.at - b.at || a.id - b.id); timers = timers.filter(t => t.at > NOW); due.forEach(t => t.f()); raf(NOW); };
  const state = o => Object.assign(JSON.parse(JSON.stringify(STATE)), o || {});
  return { ctx, g, box, texts, frame, state, I18N: vm.runInContext('I18N', ctx), now: () => NOW / 1000 };
}
const bubbles = W => W.g._g.bubbles.map(b => b.el.textContent);
const lastText = W => W.texts[W.texts.length - 1];

for (const lang of ['ko', 'en']) {
  const W = world(lang), T = (k, p) => W.I18N.t(k, p), ko = lang === 'ko';
  W.g.update(W.state(), null);
  W.frame(50);
  const base = W.state().feed || [], idx = (base.length ? base[base.length - 1].idx : 0);
  const names = W.state().agents.map(a => a.tag || a.title);
  const A = W.state().agents[0], nm = A.tag || A.title;
  let n = idx;                                                // event numbers keep growing, as the server's do (the office plays only events newer than the last seen)
  const feed = extra => W.g.update(Object.assign(W.state(), { feed: (W.state().feed || []).concat(extra.map(e => Object.assign({ idx: ++n, ts: W.now(), title: '', text: '' }, e))) }), null);
  const nthBubble = () => bubbles(W).slice(-1)[0];

  // ---- the sentences the code builds
  const leg = W.ctx.AgentGame.legendHtml(true);
  check(`${lang}: legend names both monitors, escaped`, leg.includes(ko ? '모니터 주황 &gt;_ = Claude Code' : 'Orange &gt;_ monitor = Claude Code') && leg.includes(ko ? '모니터 흰 ◆·안경 = Codex' : 'White ◆ monitor, glasses = Codex'), leg.slice(-200));
  check(`${lang}: legend without Codex has no Codex line`, !W.ctx.AgentGame.legendHtml(false).includes('Codex ') || W.ctx.AgentGame.legendHtml(false).split('Codex').length === 2, '');
  const sc = W.ctx.AgentGame.scenarios;
  check(`${lang}: scenarios export is looked up at read time, five of them, no [key]`, sc.length === 5 && sc.every(s => s.name && s.desc && !/^\[/.test(s.name) && !/^\[/.test(s.desc)), sc.map(s => s.name));
  check(`${lang}: scenario names are in the page language`, ko ? sc[0].name === '토론 한 바퀴' : sc[0].name === 'One debate cycle', sc[0].name);

  // ---- events: the new fields when present, the old ones when not
  feed([{ kind: 'agent_msg', from: A.id, to: 'orch', peer: null, agent: A.id, title: '메시지', text: 'Plain body. More text.' }]);   // i18n-ok: an old response carries the Korean default title
  check(`${lang}: old default title (no title_is_default): the bubble quotes the text`, nthBubble() === `@${T('common.orchestrator')} Plain body.`, nthBubble());
  feed([{ kind: 'agent_msg', from: A.id, to: 'orch', peer: null, agent: A.id, title: 'The made title', title_is_default: true, text: 'Body again. More.' }]);
  check(`${lang}: title_is_default true: the made title is not used`, nthBubble() === `@${T('common.orchestrator')} Body again.`, nthBubble());
  feed([{ kind: 'agent_msg', from: A.id, to: 'orch', peer: null, agent: A.id, title: 'Summary from the sender', title_is_default: false, text: 'x' }]);
  check(`${lang}: title_is_default false: the sender's title is used`, nthBubble() === `@${T('common.orchestrator')} Summary from the sender`, nthBubble());
  feed([{ kind: 'agent_msg', from: A.id, to: 'orch', peer: null, agent: A.id, title: 'Own title', text: 'x' }]);
  check(`${lang}: no title_is_default and a real title: used as before`, nthBubble() === `@${T('common.orchestrator')} Own title`, nthBubble());
  feed([{ kind: 'spawn', from: 'orch', to: A.id, agent: A.id, title: 'Run', title_i18n: { key: 'kind.spawn' }, title_is_default: true, text: '' }]);
  check(`${lang}: spawn with title_i18n: the dictionary text is shown`, nthBubble() === T('office.bubble.spawn', { name: nm, title: T('kind.spawn') }), nthBubble());
  feed([{ kind: 'spawn', from: 'orch', to: A.id, agent: A.id, title: 'Run', title_i18n: { key: 'event.not.in.the.dictionary' }, text: '' }]);
  check(`${lang}: spawn with an unknown title_i18n key: falls back to title`, nthBubble() === T('office.bubble.spawn', { name: nm, title: 'Run' }), nthBubble());
  feed([{ kind: 'spawn', from: 'orch', to: A.id, agent: null, title: 'No agent yet', text: '' }]);
  check(`${lang}: spawn before the agent is linked: the sentence without a name`, nthBubble() === T('office.bubble.spawnAnon', { title: 'No agent yet' }), nthBubble());
  const o = W.g._g.ents.get('orch');
  feed([{ kind: 'orch_ask', from: 'orch', to: 'user', title: '선택지 질문', text: '**질문** Old way?\n- a — b', questions: [{ header: null, question: 'New way?', options: [{ label: 'a', description: null }] }] }]);   // i18n-ok
  check(`${lang}: orch_ask with questions: the first question is quoted`, o.errands.some(e => e.text === T('office.errand.ask', { text: 'New way?' })), o.errands.map(e => e.text));
  feed([{ kind: 'orch_ask', from: 'orch', to: 'user', title: '선택지 질문', text: '**질문** Old way?\n- a — b' }]);   // i18n-ok
  check(`${lang}: orch_ask without questions (old response): the first line of the text, header stripped`, o.errands.some(e => e.text === T('office.errand.ask', { text: 'Old way?' })), o.errands.map(e => e.text));
  feed([{ kind: 'user_answer', from: 'user', to: 'orch', title: '선택', text: '**Q** Yes\n**R** No' }]);   // i18n-ok
  check(`${lang}: user_answer: the sentence with the answers`, o.errands.some(e => e.text === T('office.errand.answer', { text: 'Yes / No' })), o.errands.map(e => e.text));
  const B = W.state().agents[1] || A;                         // a reader who has not just spoken (the office skips a bubble within 15 s of the last one)
  feed([{ kind: 'xread', from: A.id, to: B.id, agent: B.id, title: 'r1/B.md', text: '' }]);
  check(`${lang}: xread: one sentence naming the writer and the source`, bubbles(W).includes(T('office.bubble.xread', { from: nm, doc: 'r1/B.md' })), bubbles(W));

  // ---- the signs and the whiteboard: states the saved state does not show (a Codex orchestrator, a waiting topic, people outside any debate, a crowded lounge)
  const draw = st => { const w = world(lang); w.g.update(st, null); w.frame(50); return w; };
  const has = (w, text, cls) => w.texts.some(x => x === text || (cls && x.includes(text)));
  let w = draw(Object.assign(W.state(), { orch: Object.assign({}, W.state().orch, { provider: 'codex', model: 'gpt-6-sol' }) }));
  check(`${lang}: Codex orchestrator: the sign carries the Codex form`, has(w, T('office.sign.orchCodex')) && !has(w, T('office.sign.orch')), w.texts.filter(x => /Codex|Control|지휘/.test(x)).slice(0, 4));
  w = draw(W.state());
  check(`${lang}: Claude orchestrator: the plain control-room sign, the tags of the two offices`, has(w, T('office.sign.orch')) && has(w, T('office.sign.user')) && has(w, T('office.tag.orch'), 1) && has(w, T('office.tag.user'), 1), '');
  const waiting = W.state(), tp = waiting.debates[0].topics[0];
  tp.final = { exists: false }; tp.deps = 'T7'; tp.rows.forEach(r => r.cells.forEach(c => { c.state = 'waiting'; c.agent = null; }));
  w = draw(waiting);
  check(`${lang}: waiting topic with a dependency`, has(w, T('office.board.after', { deps: 'T7' }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 3));
  tp.deps = '';
  w = draw(waiting);
  check(`${lang}: waiting topic without one`, w.texts.some(x => x.includes('<span class="st">' + T('office.board.pending') + '</span>')), '');
  const rnd = W.state(), tr = rnd.debates[0].topics[1];
  tr.final = { exists: false }; tr.rows.forEach((r, i) => r.cells.forEach(c => { if (c.round === 2) { c.state = i ? 'draft' : 'done'; c.agent = c.agent || 'x'; } }));
  w = draw(rnd);
  check(`${lang}: round under way: R/total from the cells`, has(w, T('office.board.round', { round: 2, done: 1, total: tr.rows.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  const crowd = W.state(); crowd.debates = []; crowd.agents.forEach(a => { a.status = 'running'; });
  w = draw(crowd);
  check(`${lang}: running people outside any debate: the other-work board and the head count`, has(w, T('office.board.other'), 1) && has(w, T('office.board.working', { count: crowd.agents.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 3));
  const empty = W.state(); empty.debates[0].topics[1].rows[0].agents = [];
  w = draw(empty);
  check(`${lang}: a seat nobody holds: the empty-seat tag`, w.texts.some(x => x.includes('<span class="nm">' + T('office.tag.empty') + '</span>')), '');
  const lounge = W.state(), many = [];
  for (let i = 0; i < 30; i++) many.push(Object.assign({}, lounge.agents[2], { id: 'rest' + i + 'x'.repeat(14), tag: 'R' + i, title: 'Resting ' + i, status: 'done', last_ts: lounge.now - 40 * i }));
  lounge.agents = lounge.agents.concat(many);
  w = draw(lounge);
  const sign = w.texts.find(x => x.startsWith(T('office.sign.lounge')));
  check(`${lang}: crowded lounge: one sign with the extra and the earlier counts`, sign && /\+\d+|\d+ ?earlier|지난 에이전트/.test(sign) && sign.split(' · ').length >= 3, sign);

  // ---- the orchestrator waits on a usage limit, a stopped agent rests, a paused cell, a system line, seats beside the launcher
  const Art = W.ctx.AgentGameArt, atTs = W.now() + 300;
  const clockOf = ts => { const d = new W.ctx.Date(ts * 1000), tm = W.I18N.date(ts, 'time'); return d.toDateString() === new W.ctx.Date().toDateString() ? tm : null; };
  const lw = extra => draw(Object.assign(W.state(), { orch: Object.assign({}, W.state().orch, { state: 'limit_wait', resets_at: atTs, auto: true }, extra) }));
  w = lw();
  check(`${lang}: limit_wait: the wall sign says when the orchestrator is back, not "control room"`, has(w, T('office.sign.orchLimit.auto', { time: clockOf(atTs) })) && !has(w, T('office.sign.orch')), w.texts.filter(x => /Control|지휘|Resumes|재개/.test(x)).slice(0, 4));
  check(`${lang}: limit_wait: the tag has the amber dot and the whole sentence on mouse-over`, w.texts.some(x => x.includes(T('office.tag.orch')) && x.includes(Art.C.amber)) && has(w, T('event.sys.limit.auto', { at: clockOf(atTs) }), 1), w.texts.filter(x => x.includes(T('office.tag.orch'))));
  check(`${lang}: limit_wait: the amber dot is not shown while working (the orchestrator works: green dot)`, !draw(W.state()).texts.some(x => x.includes(T('office.tag.orch')) && x.includes(Art.C.amber)), '');
  w = lw({ auto: false });
  check(`${lang}: limit_wait without "continues by itself": it resets, it does not resume`, has(w, T('office.sign.orchLimit', { time: clockOf(atTs) })) && has(w, T('event.sys.limit', { at: clockOf(atTs) }), 1), '');
  w = lw({ resets_at: null });
  check(`${lang}: limit_wait with no reset time on record: no time is made up`, has(w, T('office.sign.orchLimit.noTime')) && has(w, T('event.sys.limit.noTime.auto'), 1), '');
  check(`${lang}: the pause icon exists (two bars), the legend names it`, Array.isArray(Art.ICONS.pause) && Art.ICONS.pause.every(r => r.length === Art.ICONS.pause[0].length) && W.ctx.AgentGame.legendHtml(false).includes('‖'), '');
  const lim = W.state(), seated = st => st.debates[0].topics[1].rows.map(r => r.agents[r.agents.length - 1]).filter(Boolean).map(id => st.agents.find(a => a.id === id)).filter(Boolean);
  const [s0, s1] = seated(lim);                                  // only an agent that holds a seat in a room that is still open rests in the lounge; the others are counted in the sign
  Object.assign(s0, { status: 'interrupted', reason: 'limit', resets_at: atTs }); Object.assign(s1, { status: 'unknown', reason: null });
  w = draw(lim);
  check(`${lang}: a resting agent stopped by a limit says when it resets; an unknown one says it cannot be told`, has(w, T('event.sys.limit', { at: clockOf(atTs) }), 1) && has(w, T('status.why.unknown'), 1), w.texts.filter(x => /—/.test(x)).slice(0, 3));
  const stop = W.state(), run0 = seated(stop).find(a => a.status === 'running');
  if (run0) {
    check(`${lang}: an agent at a desk works; once interrupted it rests in the lounge`, draw(stop).g._g.ents.get(run0.id).where === 'sit' && (run0.status = 'interrupted', run0.reason = 'api_error', draw(stop).g._g.ents.get(run0.id).where === 'lounge'), '');
  }
  const pz = W.state(), pt = pz.debates[0].topics[1];
  pt.final = { exists: false }; pt.rows.forEach((r, i) => r.cells.forEach(c => { if (c.round === 2) { c.state = i ? 'paused' : 'done'; c.agent = c.agent || 'x'; } }));
  w = draw(pz);
  check(`${lang}: a paused cell keeps its round open on the whiteboard`, has(w, T('office.board.round', { round: 2, done: 1, total: pt.rows.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  const uz = W.state(), ut = uz.debates[0].topics[1];
  ut.final = { exists: false }; ut.rows.forEach((r, i) => r.cells.forEach(c => { if (c.round === 2) { c.state = i ? 'unknown' : 'done'; c.agent = c.agent || 'x'; } }));
  w = draw(uz);
  check(`${lang}: a cell in a state this page does not know keeps its round open on the whiteboard`, has(w, T('office.board.round', { round: 2, done: 1, total: ut.rows.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  const limEv = { kind: 'sys', from: 'sys', to: 'user', title: 'old Korean title', title_i18n: { key: 'event.sys.limit.auto', params: { at: atTs } }, text: '', sys: { code: 'limit', status: 429, resets_at: atTs, auto: true } };
  feed([limEv]);
  check(`${lang}: a usage-limit line makes the orchestrator say it from its desk, with the time of this page`, nthBubble() === T('event.sys.limit.auto', { at: clockOf(atTs) }), nthBubble());
  const nb = bubbles(W).length, errs = o.errands.length;
  feed([{ kind: 'sys', from: 'sys', to: 'user', title: 'x', title_i18n: { key: 'event.sys.api_error', params: { status: 529 } }, text: '', sys: { code: 'api_error', status: 529 } }]);
  check(`${lang}: any other system line is quiet in the office (no bubble, no errand)`, bubbles(W).length === nb && o.errands.length === errs, [bubbles(W).length, nb]);
  const kin = W.state(); kin.debates = [];
  kin.agents.forEach((a, i) => { a.status = 'running'; a.parent = null; });
  const [p0, p1, p2] = kin.agents; p2.parent = p0.id;                    // the third was launched by the first: it sits right after it, before the second
  const seatsOf = st => draw(st).g._g.blocks.find(b => b.other).seats.map(x => x.agentId);
  const order = seatsOf(kin);
  check(`${lang}: seats beside the launcher: an agent follows the one that launched it`, order.indexOf(p2.id) === order.indexOf(p0.id) + 1 && order.length === kin.agents.length, order);
  p2.parent = 'not-in-the-room'; const plain = seatsOf(kin), kin2 = W.state(); kin2.debates = []; kin2.agents.forEach(a => { a.status = 'running'; });
  check(`${lang}: a launcher that is not in the room changes nothing`, plain.join() === seatsOf(kin2).join(), [plain, seatsOf(kin2)]);

  // ---- a whole run of every demo scenario: no [key], no unfilled {name}, English without Hangul
  const missBefore = W.I18N.missing.size;
  for (const key of ['basic', 'new', 'round2', 'trouble', 'talk']) {
    const W2 = world(lang), alerts = [];
    W2.g.update(W2.state(), null);
    W2.g.demo(key, { onAlert: a => alerts.push(a) });
    for (let i = 0; i < 80 * 20; i++) W2.frame(50);
    const bad = W2.texts.filter(x => /\[(office|demo|common|status|kind)\.[^\]]*\]/.test(x) || /\{[A-Za-z_]\w*\}/.test(x));
    check(`${lang}: demo ${key}: no [key] and no unfilled {name} (${W2.texts.length} texts)`, bad.length === 0 && W2.I18N.missing.size === 0, [bad.slice(0, 3), [...W2.I18N.missing]]);
    if (!ko) {
      const han = W2.texts.filter(x => HAN.test(x) && !/^\s*\./.test(x) && !/\/\*/.test(x));
      check(`en: demo ${key}: no Hangul (the style sheet's own comments aside)`, han.length === 0, han.slice(0, 3));
      const al = alerts.filter(Boolean);
      check(`en: demo ${key}: the alerts it raises have no Hangul`, al.every(a => !HAN.test(a.title + a.text)), al);
    }
  }
  // ---- the demo menu
  W.ctx.document.body.appendChild = () => {};
  const anchor = { getBoundingClientRect: () => ({ left: 0, top: 0, right: 400, bottom: 20 }) };
  const before = W.texts.length;
  W.ctx.AgentGame.demoMenu(anchor, () => {});
  const menu = W.texts.slice(before).join('\n');
  check(`${lang}: demo menu: header, six items, durations`, menu.includes(T('demo.menu.header')) && menu.includes(T('demo.scenario.all.name')) && menu.includes(T('demo.menu.secs', { n: 80 })) && menu.includes(T('demo.menu.mins', { n: Math.round((80 + 55 + 65 + 56 + 57) / 60) })) && (menu.match(/<button /g) || []).length === 6, menu.slice(0, 300));
}
console.log(fails ? `FAILED ${fails}` : 'ALL PASS');
process.exit(fails ? 1 : 0);
