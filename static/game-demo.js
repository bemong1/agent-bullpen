/* Agent office demo: replays fake events on the screen only (the server and the records are untouched).
 * Loaded after game.js. Uses AgentGame._core (update, RUN, esc, ensureStyle) that game.js exports,
 * and adds demoMenu · scenarios · _demo({ run, stop }) to AgentGame. game.demo() and stopDemo(), returned by mount, call _demo.
 */
(function () {
  'use strict';
  const { update, RUN, esc, ensureStyle } = window.AgentGame._core;

  const clone = o => JSON.parse(JSON.stringify(o));
  const PQ = ['A', 'B', 'C'];
  // Tools the scenarios use: change the copy of the state (apply) and add fake events (push). Also creates demo-only topics and agents.
  function demoCtx(G, D, base) {
    const st = clone(base);
    let d0 = (st.debates || []).find(x => x.current);
    if (!d0) { d0 = { root: 'demo', name: 'demo', title: t('demo.topic.debate'), topics: [], finals: [], current: true, last_ts: 0 }; st.debates = [d0].concat(st.debates || []); }
    const now = () => Date.now() / 1000;
    const c = {
      st, d0, now,
      push: ev => { st.feed = (st.feed || []).concat([Object.assign({ idx: ++D.idx, ts: now(), text: '', title: '' }, ev)]); },
      apply: () => update(G, clone(st), null),
      byId: id => st.agents.find(a => a.id === id),
      nm: id => { const a = c.byId(id); return a ? (a.tag || a.title) : '?'; },
      seatIds: () => d0.topics.flatMap(t => t.rows.map(r => r.agents[r.agents.length - 1]).filter(Boolean)),
      zoneOf: id => { const t = d0.topics.find(t => t.rows.some(r => r.agents.includes(id))); return t ? t.key : ''; },
      status: (id, status, more) => { const a = c.byId(id); if (a) Object.assign(a, { status, last_ts: now() }, more || {}); },
      tool: (id, name) => c.status(id, 'running', { last_tool: { name, ts: now(), text: '' }, current: { kind: 'tool', name, ts: now(), text: '' } }),
      talk: (id, text) => c.status(id, 'running', { current: { kind: 'text', ts: now(), text } }),
      topic: (key, title, roles) => {       // demo-only room: seats A·B·C, round 1 and 2 cells
        const t = { dir: 'demo/' + key, key, title, name: title, deps: '', brief: true, docs: [], rounds: [1, 2],
          final: { path: null, rel: null, exists: false, mtime: null, lines: 0 },
          rows: roles.map((role, i) => ({ p: PQ[i], role, agents: [],
            cells: [1, 2].map(r => ({ round: r, state: 'waiting', path: '', agent: null, writer: null, readers: [], lines: 0, mtime: null })) })) };
        d0.topics.push(t);
        return t;
      },
      agent: (t, p, status) => {            // put a demo-only agent in that seat
        const row = t.rows.find(r => r.p === p), tag = (t.title.match(/^(T\d+)/) || ['', 'D'])[1] + '-' + p;
        const id = 'demo-' + t.key + '-' + p + '-' + (++D.idx);
        const cx = p === 'C';                  // the C seat of a demo room is Codex (a mixed room)
        st.agents.push({ id, tag, title: row.role, description: tag + ' ' + row.role, type: 'general-purpose', model: cx ? 'gpt-6-astra' : 'claude-opus-5-5',
          provider: cx ? 'codex' : 'claude', effort: cx ? 'high' : 'xhigh', status, spawn_ts: now(), first_ts: now(), last_ts: now(), tool_count: 0, errors: 0, top_tools: [],
          current: null, last_tool: null, ctx: 0, tokens: null, spark: [], handbacks: 0, received: 0, units: [], notification: null });
        row.agents.push(id);
        return id;
      },
      cell: (t, p, round, state) => { const row = t.rows.find(r => r.p === p), cl = row && row.cells.find(x => x.round === round); if (cl) { cl.state = state; cl.agent = row.agents[row.agents.length - 1]; } },
      submit: (tp, p, id, round) => { c.cell(tp, p, round, 'done'); c.status(id, 'done');
        c.push({ kind: 'handback', from: id, to: 'orch', agent: id, text: t('demo.report.text', { round, p }) }); },
      alert: a => D.hooks.onAlert && D.hooks.onAlert(a),
    };
    return c;
  }
  // Texts are looked up when a scenario starts, not when the script loads (the dictionary arrives after the scripts)
  const logRoles = () => ['a', 'b', 'c'].map(k => t('demo.logging.role.' + k));
  const errRoles = () => ['a', 'b', 'c'].map(k => t('demo.errors.role.' + k));
  function scBasic(c) {
    let ids = c.seatIds();
    if (ids.length < 4) { const tp = c.topic('dbase', t('demo.logging.topic'), logRoles()); PQ.forEach(p => c.agent(tp, p, 'done')); ids = c.seatIds(); }   // (tp, not t: t is the translation function)
    const run = ids.filter(id => RUN(c.byId(id).status)), rest = ids.filter(id => !RUN(c.byId(id).status));
    const A = run[0] || ids[0], B = run.find(id => c.zoneOf(id) !== c.zoneOf(A)) || rest.find(id => c.zoneOf(id) !== c.zoneOf(A)) || ids[1];
    const Cm = ids.find(id => id !== A && id !== B && c.zoneOf(id) === c.zoneOf(A)) || ids.find(id => id !== A && id !== B);
    const L = rest.find(id => ![A, B, Cm].includes(id)) || ids.find(id => ![A, B, Cm].includes(id));
    c.status(A, 'running'); c.status(B, 'running');
    return [
      [0, t('demo.basic.cap.start'), () => c.push({ kind: 'orch_say', from: 'orch', to: 'user', text: t('demo.basic.say.hello') })],
      [2500, t('demo.basic.cap.xread', { a: c.nm(A), b: c.nm(Cm) }), () => c.push({ kind: 'xread', from: Cm, to: A, title: 'r1/' + c.nm(Cm).slice(-1) + '.md', agent: A })],
      [6000, t('demo.basic.cap.msg', { a: c.nm(A), b: c.nm(B) }), () => c.push({ kind: 'agent_msg', from: A, to: B, peer: B, title: t('demo.basic.msg.ranges'), agent: A })],
      [19500, t('demo.basic.cap.reply', { a: c.nm(A), b: c.nm(B) }), () => c.push({ kind: 'agent_msg', from: B, to: A, peer: A, title: t('demo.basic.msg.reply'), agent: B })],
      [33000, t('demo.basic.cap.user'), () => c.push({ kind: 'user_say', from: 'user', to: 'orch', text: t('demo.basic.say.user') })],
      [36500, t('demo.basic.cap.report'), () => c.push({ kind: 'orch_say', from: 'orch', to: 'user', text: t('demo.basic.say.ack') })],
      [38000, t('demo.basic.cap.back', { a: c.nm(L) }), () => c.tool(L, 'Read')],
      [48000, t('demo.basic.cap.allDone'), () => ids.forEach(id => c.status(id, 'done'))],
    ];
  }
  function scNew(c) {
    const tp = c.topic('dnew', t('demo.logging.topic'), logRoles()), ids = {}, roles = logRoles();
    const spawn = p => () => { ids[p] = c.agent(tp, p, 'running'); c.cell(tp, p, 1, 'writing'); c.push({ kind: 'spawn', from: 'orch', to: ids[p], agent: ids[p], title: roles[PQ.indexOf(p)] }); };
    return [
      [0, t('demo.new.cap.user'), () => c.push({ kind: 'user_say', from: 'user', to: 'orch', text: t('demo.new.say.user') })],
      [10500, t('demo.new.cap.assignA'), spawn('A')],
      [12000, t('demo.new.cap.assign', { p: 'B' }), spawn('B')],
      [13500, t('demo.new.cap.assign', { p: 'C' }), spawn('C')],
      [19000, t('demo.new.cap.read'), () => PQ.forEach(p => c.tool(ids[p], 'Read'))],
      [23000, t('demo.new.cap.draft'), () => PQ.forEach(p => { c.tool(ids[p], 'Write'); c.cell(tp, p, 1, 'draft'); })],
      [25500, t('demo.new.cap.thought'), () => c.talk(ids.B, t('demo.new.say.thought'))],
      [30000, t('demo.new.cap.submitA'), () => c.submit(tp, 'A', ids.A, 1)],
      [33000, t('demo.cap.submit', { p: 'B' }), () => c.submit(tp, 'B', ids.B, 1)],
      [36000, t('demo.new.cap.submitC'), () => c.submit(tp, 'C', ids.C, 1)],
      [40000, t('demo.cap.reportToUser'), () => c.push({ kind: 'orch_say', from: 'orch', to: 'user', text: t('demo.new.say.round1Done') })],
    ];
  }
  function scRound2(c) {
    const tp = c.topic('dr2', t('demo.logging.topic'), logRoles()), P = {};
    PQ.forEach(p => { P[p] = c.agent(tp, p, 'done'); c.cell(tp, p, 1, 'done'); });
    const reads = [['A', 'B'], ['A', 'C'], ['B', 'A'], ['B', 'C'], ['C', 'A'], ['C', 'B']];
    return [
      [0, t('demo.round2.cap.rest'), null],
      [2500, t('demo.round2.cap.letter'), () => PQ.forEach(p => c.push({ kind: 'orch_msg', from: 'orch', to: P[p], agent: P[p], title: t('demo.round2.msg.start', { p }) }))],
      [3800, t('demo.round2.cap.back'), () => PQ.forEach(p => { c.tool(P[p], 'Read'); c.cell(tp, p, 2, 'writing'); })],
      ...reads.map(([r, w], i) => [9500 + i * 900, t('demo.round2.cap.xread', { r, w }), () => c.push({ kind: 'xread', from: P[w], to: P[r], title: `r1/${w}.md`, agent: P[r] })]),
      [16500, t('demo.round2.cap.rebut'), () => c.push({ kind: 'agent_msg', from: P.B, to: P.A, peer: P.A, title: t('demo.round2.msg.rebut'), agent: P.B })],
      [29500, t('demo.round2.cap.counter'), () => c.push({ kind: 'agent_msg', from: P.A, to: P.B, peer: P.B, title: t('demo.round2.msg.counter'), agent: P.A })],
      [42000, t('demo.round2.cap.draft'), () => PQ.forEach(p => { c.tool(P[p], 'Write'); c.cell(tp, p, 2, 'draft'); })],
      [46000, t('demo.cap.submit', { p: 'C' }), () => c.submit(tp, 'C', P.C, 2)],
      [48000, t('demo.cap.submit', { p: 'A' }), () => c.submit(tp, 'A', P.A, 2)],
      [50000, t('demo.round2.cap.submitLast'), () => c.submit(tp, 'B', P.B, 2)],
      [53000, t('demo.cap.reportToUser'), () => c.push({ kind: 'orch_say', from: 'orch', to: 'user', text: t('demo.round2.say.round2Done') })],
    ];
  }
  function scTrouble(c) {
    const tp = c.topic('dtr', t('demo.logging.topic'), logRoles()), P = {};
    PQ.forEach(p => { P[p] = c.agent(tp, p, 'running'); c.cell(tp, p, 1, 'writing'); });
    c.tool(P.A, 'Bash'); c.tool(P.B, 'Read'); c.tool(P.C, 'Write');
    return [
      [0, t('demo.trouble.cap.writing'), null],
      [3000, t('demo.trouble.cap.stalled'), () => c.status(P.A, 'stalled')],
      [8000, t('demo.trouble.cap.failed'), () => { c.status(P.B, 'failed'); c.cell(tp, 'B', 1, 'missing'); }],
      [11000, t('demo.trouble.cap.ask'), () => {
        c.push({ kind: 'orch_say', from: 'orch', to: 'user', text: t('demo.trouble.say.failed') });
        c.alert({ id: 'demo:trouble', level: 'decide', title: t('demo.trouble.alertTitle'), text: t('demo.trouble.say.failed'), ts: c.now(), agent: null });
      }],
      [21000, t('demo.trouble.cap.answer'), () => { c.push({ kind: 'user_say', from: 'user', to: 'orch', text: t('demo.trouble.say.user') }); c.alert(null); }],
      [31500, t('demo.trouble.cap.respawn'), () => { P.B2 = c.agent(tp, 'B', 'running'); c.cell(tp, 'B', 1, 'writing'); c.push({ kind: 'spawn', from: 'orch', to: P.B2, agent: P.B2, title: t('demo.trouble.msg.respawn') }); }],
      [36000, t('demo.trouble.cap.nudge'), () => c.push({ kind: 'orch_msg', from: 'orch', to: P.A, agent: P.A, title: t('demo.trouble.msg.nudge') })],
      [38000, t('demo.trouble.cap.moving'), () => c.tool(P.A, 'Bash')],
      [42000, t('demo.trouble.cap.report'), () => c.push({ kind: 'orch_say', from: 'orch', to: 'user', text: t('demo.trouble.say.fixed') })],
    ];
  }
  function scTalk(c) {
    const tp = c.topic('dtk', t('demo.errors.topic'), errRoles()), P = {};
    PQ.forEach(p => { P[p] = c.agent(tp, p, 'running'); c.tool(P[p], 'Read'); });
    const X = c.seatIds().find(id => c.zoneOf(id) !== tp.key) || P.C;     // a counterpart in another room (the C of the same room if there is none)
    c.status(X, 'running');
    return [
      [0, t('demo.talk.cap.start'), null],
      [2000, t('demo.talk.cap.ask'), () => c.push({ kind: 'agent_msg', from: P.A, to: P.B, peer: P.B, title: t('demo.talk.msg.ask'), agent: P.A })],
      [15000, t('demo.talk.cap.reply'), () => c.push({ kind: 'agent_msg', from: P.B, to: P.A, peer: P.A, title: t('demo.talk.msg.reply'), agent: P.B })],
      [28000, t('demo.talk.cap.other', { x: c.nm(X) }), () => c.push({ kind: 'agent_msg', from: X, to: P.A, peer: P.A, title: t('demo.talk.msg.other'), agent: X })],
      [41000, t('demo.talk.cap.agree'), () => c.push({ kind: 'agent_msg', from: P.A, to: 'orch', peer: null, title: t('demo.talk.msg.agree'), agent: P.A })],
      [44000, t('demo.cap.reportToUser'), () => c.push({ kind: 'orch_say', from: 'orch', to: 'user', text: t('demo.talk.say.report') })],
    ];
  }
  // name and desc are dictionary keys demo.scenario.<key>.name / .desc, looked up when the menu or the banner is built
  const SCENARIOS = [
    { key: 'basic', secs: 80, build: scBasic },
    { key: 'new', secs: 55, build: scNew },
    { key: 'round2', secs: 65, build: scRound2 },
    { key: 'trouble', secs: 56, build: scTrouble },
    { key: 'talk', secs: 57, build: scTalk },
  ];
  const scName = key => t('demo.scenario.' + key + '.name'), scDesc = key => t('demo.scenario.' + key + '.desc');
  function runDemo(G, key, hooks) {
    if (!G.state) return;
    if (G.demo) stopDemo(G, true);
    const keys = key === 'all' ? SCENARIOS.map(s => s.key) : [SCENARIOS.some(s => s.key === key) ? key : 'basic'];
    const D = { real: null, saved: [G.state, G.debate], savedIdx: G.lastIdx, idx: (G.lastIdx || 0) + 100000, timers: [], hooks: hooks || {}, queue: keys.slice(1), total: keys.length };
    G.demo = D;
    const banner = document.createElement('div');
    banner.className = 'ag-demo';
    banner.innerHTML = `${esc(t('common.demo'))} · <b class="sn"></b> · <span class="dt"></span><button class="nx">${esc(t('demo.banner.next'))}</button><button class="stp">${esc(t('demo.banner.stop'))}</button>`;
    banner.querySelector('.stp').onclick = () => stopDemo(G);
    banner.querySelector('.nx').onclick = () => nextScenario(G);
    G.wrap.appendChild(banner); D.banner = banner;
    playScenario(G, keys[0]);
  }
  function nextScenario(G) { const D = G.demo; if (!D) return; if (D.queue.length) playScenario(G, D.queue.shift()); else stopDemo(G); }
  function playScenario(G, key) {
    const D = G.demo, sc = SCENARIOS.find(s => s.key === key);
    D.timers.forEach(clearTimeout); D.timers = [];
    if (D.hooks.onAlert) D.hooks.onAlert(null);
    const o = G.ents.get('orch'); if (o) { o.errands = []; o.errand = null; }
    G.userCalling = false;
    const c = demoCtx(G, D, D.real ? D.real[0] : D.saved[0]);   // every scenario starts from a copy of the real state at that moment
    const steps = sc.build(c);
    D.banner.querySelector('.sn').textContent = D.total > 1 ? t('demo.banner.progress', { name: scName(sc.key), i: D.total - D.queue.length, n: D.total }) : scName(sc.key);
    D.banner.querySelector('.nx').style.display = D.queue.length ? '' : 'none';
    const say = text => { D.banner.querySelector('.dt').textContent = text; };   // (not t: t is the translation function)
    steps.forEach(([ms, label, fn]) => D.timers.push(setTimeout(() => { if (G.demo !== D) return; say(label); if (fn) fn(); c.apply(); }, ms)));
    D.timers.push(setTimeout(() => { if (G.demo === D) nextScenario(G); }, sc.secs * 1000));
    c.apply();
  }
  function stopDemo(G, quiet) {
    const D = G.demo;
    if (!D) return;
    D.timers.forEach(clearTimeout);
    D.banner.remove();
    G.demo = null;
    const o = G.ents.get('orch'); if (o) { o.errands = []; o.errand = null; }   // do not leave demo errands behind
    G.userCalling = false;
    if (D.hooks.onAlert) D.hooks.onAlert(null);
    G.lastIdx = D.savedIdx;
    const [s, d] = D.real || D.saved;
    update(G, s, d);
    if (!quiet && D.hooks.onEnd) D.hooks.onEnd();
  }
  // the menu for picking a demo (shared by the dashboard and the full screen)
  function demoMenu(anchor, onPick) {
    document.querySelectorAll('.ag-menu').forEach(m => m.remove());
    ensureStyle();
    const m = document.createElement('div'); m.className = 'ag-menu';
    const items = SCENARIOS.concat([{ key: 'all', secs: SCENARIOS.reduce((a, x) => a + x.secs, 0) }]);
    m.innerHTML = `<div class="hd">${esc(t('demo.menu.header'))}</div>` +
      items.map(i => `<button data-k="${i.key}"><b>${esc(scName(i.key))}</b><span>${esc(i.secs >= 120 ? t('demo.menu.mins', { n: Math.round(i.secs / 60) }) : t('demo.menu.secs', { n: i.secs }))}</span><small>${esc(scDesc(i.key))}</small></button>`).join('');
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    m.style.top = (r.bottom + 6 + scrollY) + 'px';
    m.style.left = Math.max(8, Math.min(innerWidth - 358, r.right - 350)) + scrollX + 'px';
    m.querySelectorAll('button').forEach(b => b.onclick = ev => { ev.stopPropagation(); m.remove(); onPick(b.dataset.k); });
    setTimeout(() => document.addEventListener('click', function off(ev) { if (!m.contains(ev.target)) { m.remove(); document.removeEventListener('click', off); } }), 0);
  }

  Object.assign(window.AgentGame, {
    demoMenu, _demo: { run: runDemo, stop: stopDemo },
  });
  // The scenario list for callers (names in the language of the page, looked up on every read)
  Object.defineProperty(window.AgentGame, 'scenarios', { enumerable: true, get: () => SCENARIOS.map(({ key, secs }) => ({ key, name: scName(key), desc: scDesc(key), secs })) });
})();
