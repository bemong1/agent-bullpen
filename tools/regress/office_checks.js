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
const unfinal = tp => { tp.final = { confirmed: false, exists: false, why: ['none'], candidates: [] }; tp.closable = false; return tp; };     // a topic whose end is not confirmed: no final, not closable

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
  const zones = st => draw(st).g._g.blocks.filter(b => b.type === 'zone' && b.topic).length;
  const has = (w, text, cls) => w.texts.some(x => x === text || (cls && x.includes(text)));
  const stages = w => w.texts.map(x => (x.match(/<span class="st">([^<]*)<\/span>/) || [])[1]).filter(Boolean);     // the stage lines of the whiteboards
  let w = draw(Object.assign(W.state(), { orch: Object.assign({}, W.state().orch, { provider: 'codex', model: 'gpt-6-sol' }) }));
  check(`${lang}: Codex orchestrator: the sign carries the Codex form`, has(w, T('office.sign.orchCodex')) && !has(w, T('office.sign.orch')), w.texts.filter(x => /Codex|Control|지휘/.test(x)).slice(0, 4));
  w = draw(W.state());
  check(`${lang}: Claude orchestrator: the plain control-room sign, the tags of the two offices`, has(w, T('office.sign.orch')) && has(w, T('office.sign.user')) && has(w, T('office.tag.orch'), 1) && has(w, T('office.tag.user'), 1), '');
  const waiting = W.state(), tp = waiting.debates[0].topics[0];
  unfinal(tp); tp.deps = 'T7'; tp.rows.forEach(r => r.cells.forEach(c => { c.state = 'waiting'; c.agent = null; }));
  w = draw(waiting);
  check(`${lang}: waiting topic with a dependency`, has(w, T('office.board.after', { deps: 'T7' }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 3));
  tp.deps = '';
  w = draw(waiting);
  check(`${lang}: waiting topic without one`, w.texts.some(x => x.includes('<span class="st">' + T('office.board.pending') + '</span>')), '');
  const rnd = W.state(), tr = rnd.debates[0].topics[1];
  unfinal(tr); tr.rows.forEach((r, i) => r.cells.forEach(c => { if (c.round === 2) { c.state = i ? 'draft' : 'done'; c.agent = c.agent || 'x'; } }));
  w = draw(rnd);
  check(`${lang}: round under way: R/total from the cells`, has(w, T('office.board.round', { round: 2, done: 1, total: tr.rows.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  const crowd = W.state(); crowd.debates = []; crowd.agents.forEach(a => { a.status = 'running'; });
  w = draw(crowd);
  check(`${lang}: running people outside any debate: the other-work board and the head count`, has(w, T('office.board.other'), 1) && has(w, T('office.board.working', { count: crowd.agents.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 3));
  // the "other work" room: a newcomer takes the empty end seat, whatever place the server lists it in, and nobody else is moved a seat over; when somebody leaves the rest close up in the same order
  const otherSeats = Wd => Wd.g._g.seats.filter(sq => sq.block.other).map(sq => sq.agentId);
  const wk = world(lang), room0 = W.state(); room0.debates = []; room0.agents.forEach(a => { a.status = 'running'; });
  wk.g.update(room0, null); wk.frame(50);
  const seatsBefore = otherSeats(wk), mkLate = (n, tag) => Object.assign({}, room0.agents[0], { id: 'late' + n + 'x'.repeat(12), tag, title: 'Newcomer ' + n, status: 'running', spawn_ts: room0.now, parent: null });
  const late1 = mkLate(1, 'N1'), late2 = mkLate(2, 'N2'), st1 = JSON.parse(JSON.stringify(room0));
  st1.agents.unshift(late1);                                                    // listed seatsBefore everybody else
  wk.g.update(st1, null); wk.frame(50);
  const after1 = otherSeats(wk);
  check(`${lang}: other-work room, a newcomer listed first: it takes the end seat and everybody keeps theirs`, seatsBefore.length === room0.agents.length && JSON.stringify(after1) === JSON.stringify(seatsBefore.concat([late1.id])), [seatsBefore, after1]);
  const st2 = JSON.parse(JSON.stringify(st1));
  st2.agents.splice(2, 0, late2);                                               // and another one listed in the middle
  wk.g.update(st2, null); wk.frame(50);
  const after2 = otherSeats(wk);
  check(`${lang}: other-work room, a second newcomer listed in the middle: the end seat again`, JSON.stringify(after2) === JSON.stringify(seatsBefore.concat([late1.id, late2.id])), [seatsBefore, after2]);
  const st3 = JSON.parse(JSON.stringify(st2));
  st3.agents = st3.agents.filter(a => a.id !== seatsBefore[1]);
  wk.g.update(st3, null); wk.frame(50);
  check(`${lang}: other-work room, somebody leaves: the rest close up in the same order`, JSON.stringify(otherSeats(wk)) === JSON.stringify(after2.filter(id => id !== seatsBefore[1])), [after2, otherSeats(wk)]);
  check(`${lang}: other-work room, a first drawing still seats a launched run beside its launcher`, (() => { const k = JSON.parse(JSON.stringify(room0)); const [pa, ch] = [k.agents[0], k.agents[1]]; ch.parent = pa.id; k.agents.splice(1, 1); k.agents.push(ch);
    const ww = world(lang); ww.g.update(k, null); ww.frame(50); const o = otherSeats(ww); return o.indexOf(ch.id) === o.indexOf(pa.id) + 1; })(), '');
  check(`${lang}: other-work room, a first drawing seats the agents launched together side by side (the same \`launch\`), the rest in their order`, (() => { const k = JSON.parse(JSON.stringify(room0)); const ag = k.agents.map(a => { a.parent = null; a.launch = null; return a; });
    ag[0].launch = ag[3].launch = 'claude:aaaa:-:grp1'; const ww = world(lang); ww.g.update(k, null); ww.frame(50); const o = otherSeats(ww); return o.indexOf(ag[3].id) === o.indexOf(ag[0].id) + 1 && o.length === ag.length; })(), '');
  // the agents thought to work in a topic (no cell there) sit in its room; the bundle's own line seats in the first room; "N est." when nobody holds a cell
  { const st = W.state(), tp = st.debates[0].topics[0]; const extra = Object.assign({}, st.agents[0], { id: 'placed1' + 'x'.repeat(12), tag: 'PL1', title: 'Placed one', status: 'running', parent: null, launch: null, spawn_ts: st.now });
    st.agents.push(extra); unfinal(tp); tp.placed = [{ agent: extra.id, why: 'guide_read', whys: ['guide_read'], sure: false, live: true }];
    tp.rows.forEach(r => { r.agents = []; r.cells.forEach(c => { c.state = 'waiting'; c.agent = null; c.owner = null; }); });
    w = draw(st);
    const seat = w.g._g.seats.find(sq => sq.agentId === extra.id);
    check(`${lang}: an agent thought to work in a topic sits in that topic's room, not in the other-work room`, !!seat && !seat.block.other && seat.block.topic.key === tp.key, seat ? seat.block.topic && seat.block.topic.key : 'no seat');
    check(`${lang}: a topic nobody holds a cell in but somebody is thought to work in says "${T('office.board.estimated', { count: 1 })}", not "pending"`, has(w, T('office.board.estimated', { count: 1 }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 3));
    tp.rows = [];                                             // no row at all: a topic with no participants has no room, unless somebody is thought to work in it
    w = draw(st);
    const seat2 = w.g._g.seats.find(sq => sq.agentId === extra.id);
    check(`${lang}: a topic with no rows keeps its room while somebody is thought to work in it, and loses it when nobody is`, !!seat2 && !seat2.block.other && seat2.block.topic.key === tp.key && (tp.placed[0].live = false, !draw(st).g._g.blocks.some(b => b.type === 'zone' && b.topic && b.topic.key === tp.key)), seat2 ? seat2.block.topic && seat2.block.topic.key : 'no seat'); }
  { const st = W.state(), tp = st.debates[0].topics[1]; tp.final = { confirmed: false, exists: false, why: ['none'], candidates: [] }; tp.closable = true;      // closable with no final of its own: the bundle's final closes it
    st.agents.forEach(a => { a.last_ts = st.now - 5; }); tp.rows.forEach(r => r.cells.forEach(c => { c.mtime = st.now - 5; }));                                     // (a room stays for twenty minutes after its last activity)
    w = draw(st);
    check(`${lang}: a topic closed by the final of its bundle says "${T('office.board.bundleFinal')}" on its whiteboard`, has(w, T('office.board.bundleFinal'), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 3)); }
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
  unfinal(pt); pt.rows.forEach((r, i) => r.cells.forEach(c => { if (c.round === 2) { c.state = i ? 'paused' : 'done'; c.agent = c.agent || 'x'; } }));
  w = draw(pz);
  check(`${lang}: a paused cell keeps its round open on the whiteboard`, has(w, T('office.board.round', { round: 2, done: 1, total: pt.rows.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  const uz = W.state(), ut = uz.debates[0].topics[1];
  unfinal(ut); ut.rows.forEach((r, i) => r.cells.forEach(c => { if (c.round === 2) { c.state = i ? 'unknown' : 'done'; c.agent = c.agent || 'x'; } }));
  w = draw(uz);
  check(`${lang}: a cell in a state this page does not know keeps its round open on the whiteboard`, has(w, T('office.board.round', { round: 2, done: 1, total: ut.rows.length }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  // rooms: people who work together in a folder. One round and no round folder; or no cell at all (participants only). A room closes only when the judgment says it can be closed (`closable`: a confirmed
  // end, nobody tied to it working, not an estimate), and then 20 minutes after its last participant stops; a room that is all in but whose end is not confirmed stays
  const roomState = (kind, cellState, status, quiet, closable, sure) => {
    const st = W.state(), ag = st.agents.slice(0, 3);
    ag.forEach(a => { a.status = status; a.last_ts = st.now - (quiet || 5); });
    st.debates = [{ root: '/r/m', short: 'm', name: 'm', title: 'Sync', finals: [], last_ts: st.now, current: true, sure: sure !== false, final: null, placed: [], topics: [{ dir: '/r/m', key: 'm', title: 'Sync', name: '', deps: '', kind: 'rounds', room: kind,
      room_sure: sure !== false, room_why: sure === false ? 'launch' : 'tag', final: { confirmed: false, exists: false, why: closable ? [] : ['none'], candidates: [] }, closable: !!closable, placed: [],
      rounds: kind === 'cells' ? [1] : [], rows: ag.map((a, i) => ({ p: 'ABC'[i], role: '', agents: [a.id], cells: kind === 'cells' ? [{ round: 1, state: cellState(i), path: '/r/m/' + i, agent: a.id, owner: a.id, editors: [], evidence: 'tool', hint: null, previous: false, readers: [], lines: 1, mtime: st.now - (quiet || 5) }] : [] })) }] }];
    return st;
  };
  w = draw(roomState('cells', i => (i ? 'draft' : 'done'), 'running'));
  check(`${lang}: a room of cells: its own stage line (no round number), in/total from the cells`, has(w, T('office.board.room', { done: 1, total: 3 }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  w = draw(roomState('cells', () => 'done', 'running'));
  check(`${lang}: a room whose every file is in but whose people still work: all in`, has(w, T('office.board.roomDone', { total: 3 }), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  w = draw(roomState('cells', () => 'done', 'done', 5, true));
  check(`${lang}: a room whose every file is in, nobody works and the judgment can close it: complete (no round to wait for)`, stages(w).join('|') === T('office.board.complete'), stages(w));
  w = draw(roomState('cells', () => 'done', 'done'));
  check(`${lang}: the same room whose end is not confirmed is not "complete": all in`, stages(w).join('|') === T('office.board.roomDone', { total: 3 }), stages(w));
  w = draw(roomState('cells', i => (i ? 'missing' : 'done'), 'done'));
  check(`${lang}: a room with a seat that never handed in is stopped, in/total, not complete`, stages(w).join('|') === T('office.board.room', { done: 1, total: 3 }), stages(w));
  const unstarted = quiet => { const st = roomState('cells', () => 'waiting', 'done', quiet); st.debates[0].topics[0].rows.forEach(r => { r.agents = []; r.cells.forEach(c => { c.agent = null; c.owner = null; c.mtime = null; }); }); return st; };
  check(`${lang}: a room that has not started (every cell waiting, nobody on it) keeps its room, however long it has been`, zones(unstarted()) === 1 && zones(unstarted(3600)) === 1, [zones(unstarted()), zones(unstarted(3600))]);
  check(`${lang}: a room whose people are all unknown but whose results are not in keeps its room after an hour`, zones(roomState('cells', i => (i ? 'writing' : 'done'), 'unknown', 3600)) === 1, zones(roomState('cells', i => (i ? 'writing' : 'done'), 'unknown', 3600)));
  check(`${lang}: nor does one that was cut off with a paused cell`, zones(roomState('cells', i => (i ? 'paused' : 'done'), 'interrupted', 3600)) === 1, zones(roomState('cells', i => (i ? 'paused' : 'done'), 'interrupted', 3600)));
  check(`${lang}: a room that is all in and nobody works but whose end is not confirmed stays after an hour; one the judgment can close is closed; an estimated room never is`,
    zones(roomState('cells', () => 'done', 'done', 3600)) === 1 && zones(roomState('cells', () => 'done', 'done', 3600, true)) === 0 && zones(roomState('cells', () => 'done', 'done', 3600, false, false)) === 1, '');
  check(`${lang}: a room with a confirmed end is closed whatever its cells say (the judgment is the one that says it can be closed)`, zones(roomState('cells', i => (i ? 'missing' : 'done'), 'done', 3600, true)) === 0, '');
  w = draw(roomState('members', () => 'done', 'running'));
  check(`${lang}: a room of participants only: how many talk, and they sit in the room (no other-work board)`, has(w, T('office.board.members', { count: 3 }), 1) && !has(w, T('office.board.other'), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  w = draw(roomState('members', () => 'done', 'done', 60));
  check(`${lang}: the same room once they have all stopped says it ended`, has(w, T('office.board.membersEnded'), 1), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
  w = draw(roomState('members', () => 'done', 'done', 3600, true));
  check(`${lang}: and an hour later the room is closed (nobody working, the judgment can close it)`, !has(w, T('office.board.membersEnded'), 1) && !w.texts.some(x => x.includes('Sync')), w.texts.filter(x => x.includes('class="st"')).slice(0, 4));
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

  // ---- people who come in walk in from the entrance, also when their arrival changes the layout of the rooms (a seat more in "other work", a new room, the lounge);
  // those who were already there are put at their (moved) desks at once, and those still walking on their way carry on. One world is updated twice: a fresh world has no past, so it puts everybody in place.
  const clone = o => JSON.parse(JSON.stringify(o));
  const newcomer = (st, i, status) => Object.assign(clone(st.agents[0]), { id: 'newcomer' + i + 'x'.repeat(10), tag: 'N' + i, title: 'New ' + i, parent: null, status: status || 'running', last_ts: st.now, current: null });
  const at = (e, p, d) => Math.hypot(e.x - p.x, e.y - p.y) <= d;
  const home = e => !e.path.length && e.x === e.goal.pt.x && e.y === e.goal.pt.y;
  const walkIn = (w, e, frames) => { let n = 0; while (n < frames && !home(e)) { w.frame(50); n++; } return home(e) ? n : -1; };
  const others0 = W.state(); others0.debates = []; others0.agents.forEach(a => { a.status = 'running'; a.parent = null; });
  {
    const w = world(lang), G = w.g._g;
    w.g.update(others0, null); w.frame(50);
    const before = G.sig, firstSeats = G.blocks.find(b => b.other).seats.length, was = others0.agents.map(a => G.ents.get(a.id));
    const st = clone(others0), nc = newcomer(st, 1); st.agents.push(nc);
    w.g.update(st, null);
    const e = G.ents.get(nc.id);
    check(`${lang}: one more person in Other work changes the layout (a seat more, the desks move)`, G.sig !== before && G.blocks.find(b => b.other).seats.length === firstSeats + 1, [before, G.sig]);
    check(`${lang}: that person starts at the entrance and has a way to walk, not at the desk`, e && at(e, G.entrance, 0.01) && e.path.length > 0 && !at(e, e.goal.pt, 20), e && [e.x, e.y, e.path.length]);
    check(`${lang}: the people who were already there are at their (moved) desks at once, not walking`, was.every(x => x && G.ents.get(x.id) === x && home(x) && x.where === 'sit'), was.map(x => x && [x.x, x.y, x.path.length]));
    w.frame(50);
    check(`${lang}: a tick later the new person has moved on from the entrance along the way`, at(e, G.entrance, 40) && !at(e, G.entrance, 0.01), [e.x, e.y]);
    const n = walkIn(w, e, 200);
    check(`${lang}: and gets to the desk after a few ticks (${n})`, n > 0 && n < 120, n);
    check(`${lang}: the ones who were there did not move meanwhile`, was.every(x => home(x)), was.map(x => [x.x, x.y]));
  }
  {
    const full = W.state(), room = full.debates[0].topics[1], who = room.rows[0].agents[room.rows[0].agents.length - 1];
    const w = world(lang), G = w.g._g;
    const first = clone(full); first.debates = []; first.agents = first.agents.filter(a => a.id !== who);
    first.agents.forEach(a => { a.status = 'running'; });
    w.g.update(first, null); w.frame(50);
    const before = G.sig, was = first.agents.map(a => G.ents.get(a.id));
    const again = clone(full); again.agents.forEach(a => { a.status = 'running'; });                 // everybody works: the new room is open and its people sit at its desks
    w.g.update(again, null);
    const e = G.ents.get(who);
    check(`${lang}: a debate room that appears for the first time changes the layout`, G.sig !== before && G.blocks.some(b => b.topic && b.topic.key === room.key), [before, G.sig]);
    check(`${lang}: the first participant of that new room starts at the entrance and walks in`, e && at(e, G.entrance, 0.01) && e.path.length > 0 && e.goal.block.topic && e.goal.block.topic.key === room.key, e && [e.x, e.y, e.path.length]);
    check(`${lang}: the people who were already in the office are at their new desks at once`, was.every(x => x && G.ents.get(x.id) === x && home(x)), was.map(x => x && [x.x, x.y, x.path.length]));
    check(`${lang}: and the new participant sits down at the room's desk a few ticks later`, walkIn(w, e, 200) > 0 && e.where === 'sit' && e.goal.block.topic.key === room.key, e && [e.x, e.y]);
  }
  {
    const w = world(lang), G = w.g._g;
    const rest = W.state(), room = rest.debates[0].topics[1], holders = room.rows.map(r => r.agents[r.agents.length - 1]).filter(Boolean);
    w.g.update(rest, null); w.frame(50);                                                       // two people rest in the lounge (their room is still open)
    const lounge = rest.agents.filter(a => !['running', 'working'].includes(a.status) && holders.includes(a.id));
    const before = G.sig, was = rest.agents.map(a => G.ents.get(a.id)).filter(Boolean);
    const st = clone(rest), nc = newcomer(st, 2); st.agents.push(nc);                          // and a runner arrives in Other work: the layout changes while the lounge people are in place
    w.g.update(st, null);
    check(`${lang}: with people in the lounge, a new runner changes the layout and the lounge people stay in place`, G.sig !== before && lounge.length > 0 && lounge.every(a => G.ents.get(a.id).where === 'lounge' && home(G.ents.get(a.id))), [before, G.sig, lounge.length]);
    check(`${lang}: the runner comes in at the entrance`, at(G.ents.get(nc.id), G.entrance, 0.01) && G.ents.get(nc.id).path.length > 0, '');
  }
  {
    const w = world(lang), G = w.g._g;
    w.g.update(others0, null); w.frame(50);
    const a = clone(others0), n1 = newcomer(a, 3); a.agents.push(n1);
    w.g.update(a, null);
    const e1 = G.ents.get(n1.id);
    for (let i = 0; i < 4; i++) w.frame(50);
    const mid = [e1.x, e1.y], moving = e1.path.length > 0;
    const b = clone(a), n2 = newcomer(b, 4); b.agents.push(n2);                                // another one comes in while the first is still walking: the layout changes again
    const sig = G.sig;
    w.g.update(b, null);
    const e2 = G.ents.get(n2.id);
    check(`${lang}: a second arrival lays the rooms out again while the first still walks`, moving && G.sig !== sig, [moving, sig, G.sig]);
    check(`${lang}: the one still walking is not put at the desk: it carries on from where it is`, e1.path.length > 0 && !at(e1, e1.goal.pt, 5) && at(e1, { x: mid[0], y: mid[1] }, 1), [e1.x, e1.y, mid]);
    check(`${lang}: the second one starts at the entrance`, at(e2, G.entrance, 0.01) && e2.path.length > 0, [e2.x, e2.y]);
    check(`${lang}: both get to their desks`, walkIn(w, e1, 300) >= 0 && walkIn(w, e2, 300) >= 0 && home(e1) && home(e2), [e1.x, e1.y, e2.x, e2.y]);
  }
  {
    const w = world(lang), G = w.g._g;                                                          // the first picture: nobody is replayed
    w.g.update(others0, null);
    check(`${lang}: the first picture puts everybody in place (nobody walks in from the entrance)`, others0.agents.every(a => home(G.ents.get(a.id))), '');
    const restart = clone(others0); restart.boot = (others0.boot || 0) + '-restarted';
    const nc = newcomer(restart, 5); restart.agents.push(nc);
    w.g.update(Object.assign(clone(others0), { boot: 'first' }), null);
    w.g.update(restart, null);
    check(`${lang}: the first picture after a server restart puts everybody in place too`, home(G.ents.get(nc.id)), '');
  }

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
