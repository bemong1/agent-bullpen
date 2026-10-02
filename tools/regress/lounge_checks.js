// Synthetic checks for the lounge (static/game.js + game-art.js). No browser: the same fake DOM as game_snap.js, with a virtual clock.
// usage: node lounge_checks.js <static dir> <fixture prefix (e.g. from tools/make_synth_fixture.py)> [baseline static dir for the block-width comparison]
// Prints PASS/FAIL per check; exit code 1 if any FAIL. Needs game.js's mount() to return `_g` (the internal state, for tests only) and to accept opts.tagWidth.
// Tag widths are simulated (css px = 10 + 7 * characters: 'T1-A' 38, 'sol6.1-38' 73, 'opus5.5-13' 80 — real Galmuri measurements: 38, 68, 80).
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, fx, oldDir] = process.argv.slice(2);
const BASE = JSON.parse(fs.readFileSync(fx + '_state.json', 'utf8'));
// The virtual clock starts at a fixed time, not at the fixture's `now`. Slots (150 s, shifted per person by an id hash) and the weighted picks hash the absolute
// time, so what the statistical checks see depends on the start time: with a real clock the same code would pass or fail from run to run.
// LOUNGE_T0=<ms since epoch> tries another start time.
const T0 = +process.env.LOUNGE_T0 || 1790800005000;
let NOW = T0;
class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(NOW); } static now() { return NOW; } }
let nid = 0, draws = 0;
const TAGW = n => 10 + 7 * String(n).length;                 // simulated css width of a name tag
function mk(tag) { const e = { tag, _id: tag + (nid++), children: [], style: {}, dataset: {}, className: '', innerHTML: '', textContent: '', offsetWidth: 100, offsetHeight: 20,
  append(...k) { this.children.push(...k); }, appendChild(k) { this.children.push(k); }, remove() { }, addEventListener() {}, querySelector: () => mk('q'), querySelectorAll: () => [],
  getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0 }), clientWidth: 1600,
  getContext() { return this._c || (this._c = { set fillStyle(v) {}, get fillStyle() { return ''; }, globalAlpha: 1, imageSmoothingEnabled: false, fillRect() { draws++; }, clearRect() {}, drawImage() { draws++; } }); } }; return e; }
// each world = its own vm context (own raf + timers)
function world(sdir, opts = {}) {
  let raf = null, timers = [], tid = 0;
  const ctx = { console, Date: FDate, Math, JSON, Map, Set, Object, Array, String, Number, Promise, innerWidth: opts.iw || 1600, innerHeight: opts.ih || 900, scrollX: 0, scrollY: 0,
    document: { createElement: mk, head: mk('head'), body: mk('body'), hidden: false, querySelectorAll: () => [], addEventListener() {} },
    requestAnimationFrame: f => { raf = f; }, addEventListener() {},
    setTimeout: (f, ms) => { const t = { id: ++tid, at: NOW + (ms || 0), f }; timers.push(t); return t.id; }, clearTimeout: id => { timers = timers.filter(t => t.id !== id); },
    IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  for (const f of I18B.scripts(sdir, ['game-art.js', 'game.js', 'game-demo.js'])) {
    let code = fs.readFileSync(path.join(sdir, f), 'utf8');
    if (f === 'game.js' && !code.includes('_g: G')) code = code.replace('const G = {', 'const G = window.__G = {');   // an older game.js has no _g
    vm.runInContext(code, ctx);
    if (f === 'i18n.js') I18B.boot(ctx, sdir);          // i18n.js (when present) and its dictionaries from disk, language AB_LANG (default ko)
  }
  const box = mk('div'); box.clientWidth = opts.cw || 1600;
  const game = ctx.AgentGame.mount(box, { mode: opts.mode || 'strip', onAgent() {}, tagWidth: TAGW });
  let i = 0;
  const w = { ctx, game, G: game._g || ctx.__G, box,
    frames(n) { for (let k = 0; k < n; k++) { NOW += 50; const due = timers.filter(t => t.at <= NOW).sort((a, b) => a.at - b.at); timers = timers.filter(t => t.at > NOW); due.forEach(t => t.f()); raf(++i * 50); } },
    secs(s) { w.frames(Math.round(s * 20)); } };
  return w;
}
// synthetic state: agents in debate seats (3 per topic) with the given statuses; names: 'short' (T1-A), 'mixed' (every 3rd/5th long), 'long' (all long)
function nameOf(names, t, p, i) { if (names === 'short') return 'T' + (t + 1) + '-' + p; if (names === 'long') return i % 2 ? 'opus5.5-' + (10 + i) : 'sol6.1-' + (30 + i); return i % 3 === 0 ? 'sol6.1-' + (30 + i) : i % 5 === 1 ? 'opus5.5-' + (10 + i) : 'T' + (t + 1) + '-' + p; }
function synth(statuses, lastAgo = 60, names = 'short') {
  const st = JSON.parse(JSON.stringify(BASE)), now = NOW / 1000;
  st.agents = []; st.feed = []; st.alerts = [];
  const topics = [];
  statuses.forEach((s, i) => {
    const t = Math.floor(i / 3), p = 'ABC'[i % 3], id = 'a' + String(1000 + i * 37) + '-bee7-4c1e-9d3a-' + (100000 + i * 7919);
    const status = typeof s === 'string' ? s : s.status, ago = typeof s === 'string' ? lastAgo : (s.ago ?? lastAgo), prov = (typeof s === 'object' && s.provider) || 'claude';
    st.agents.push({ id, tag: nameOf(names, t, p, i), title: 'role ' + p, description: '', type: 'general-purpose', model: prov === 'codex' ? 'gpt-6-astra' : 'claude-opus-5-5', provider: prov, effort: 'high', status,
      spawn_ts: now - 5000, first_ts: now - 5000, last_ts: now - (i % 4 === 3 ? 4000 : ago), tool_count: 0, errors: 0, top_tools: [], current: null, last_tool: null, ctx: 0, tokens: null, spark: [], handbacks: 0, received: 0, units: [], notification: null });
    if (!topics[t]) topics[t] = { dir: 'x/t' + t, key: 't' + t, title: 'T' + (t + 1) + ' topic', name: '', deps: '', brief: true, docs: [], rounds: [1], final: { path: null, rel: null, exists: false, mtime: null, lines: 0 }, rows: [] };
    topics[t].rows.push({ p, role: 'role ' + p, agents: [id], cells: [{ round: 1, state: 'done', path: '', agent: id, writer: id, readers: [], lines: 0, mtime: null }] });
  });
  st.debates = statuses.length ? [{ root: 'x', name: 'x', title: 'x', topics, finals: [], current: true, last_ts: now }] : [];
  st.now = now;
  return st;
}
const results = [];
const ok = (name, cond, detail) => { results.push([name, !!cond, detail]); console.log((cond ? 'PASS ' : 'FAIL ') + name + (detail !== undefined && !cond ? '  -> ' + JSON.stringify(detail).slice(0, 700) : '')); };
const lounge = G => G.blocks.find(b => b.type === 'lounge');
const restingEnts = G => [...G.ents.values()].filter(e => e.where === 'lounge' && e.agent);
const layoutOf = w => ({ scale: w.G.scale, W: w.G.W, rows: w.G.rows, blocks: w.G.blocks.map(b => b.type + ':' + b.w + '@' + b.x + ',' + b.row + (b.sofas ? ':s' + b.sofas : '')).join(' ') });
const uid = (pre, i) => pre + i.toString(36) + 'x-1111-4c1e-9d3a-' + i;
function single(status, ago, i) {     // one resting agent with a fresh id (for statistics)
  const st = synth([status], ago), a = st.agents[0]; a.id = uid('b', i);
  st.debates[0].topics[0].rows[0].agents = [a.id]; st.debates[0].topics[0].rows[0].cells[0].agent = a.id; a.last_ts = NOW / 1000 - ago;
  return st;
}
const ov = (p, q) => p.l < q.r && q.l < p.r && p.t < q.b && q.t < p.b;
const rectOf = f => ({ l: f.x, r: f.x + f.w, t: f.y, b: f.y + f.h });

// ---- 1. width rule unchanged: strip rows, scale and every block (x, width, row) equal the baseline game.js ----
if (oldDir) {
  const bad = []; let n = 0;
  for (const idle of [0, 1, 5, 6, 7, 12, 13, 18, 19, 24, 25, 30]) for (const mode of ['strip', 'full']) for (const cw of [420, 900, 1214, 1300, 1654, 1720, 2200]) {
    const st = synth(Array(idle).fill('done')), c = { idle, mode, cw };
    const a = world(dir, { cw, mode, iw: cw, ih: 1000 }), b = world(oldDir, { cw, mode, iw: cw, ih: 1000 }); a.game.update(st, null); b.game.update(st, null); n++;
    const la = layoutOf(a), lb = layoutOf(b); if (JSON.stringify(la) !== JSON.stringify(lb)) bad.push([c, la, lb]);
  }
  ok('lounge block width, strip rows and scale equal the baseline in ' + n + ' cases (idle 0..30 x strip/full x 7 widths)', !bad.length, bad.slice(0, 2));
} else console.log('SKIP block-width comparison (no baseline static dir given)');

// ---- 2. no overlaps: furniture, people and name tags, for the requested widths x resting counts x name sets ----
// widths: strip in a 1280 window (container 1214), strip in a 1720 window (1654), /game in a 1600 window (full mode). counts 0, 6, 12, 25, 40.
function overlaps(w) {
  const G = w.G, L = lounge(G), s = G.scale, top = L.y + 36, out = [];
  const furn = L.furn.map(f => Object.assign({ n: f.n }, rectOf(f)));
  const people = restingEnts(G).filter(e => e.act).map(e => { const g = e.goal, sp = e.act.spot, name = e.agent.tag || e.agent.title;
    const lt = G.ltags.get(e.id), cap = lt && lt.el.style.maxWidth ? parseFloat(lt.el.style.maxWidth) / s : Infinity;      // the width the tag is really shown with (max-width of the element), not only the width used for the assignment
    const full = TAGW(name) / s, tw = Math.min(full, cap), ax = g.pt.x + g.tag.dx, tl = g.tag.al === 'l' ? ax : g.tag.al === 'r' ? ax - tw : ax - tw / 2, ty = g.pt.y + g.tag.dy;
    if (tw < full - 0.01) out.push('tag ' + name + ' is cut: shown ' + tw.toFixed(1) + ' of ' + full.toFixed(1));
    return { e, name, node: sp.node, key: sp.key, body: { l: g.pt.x, r: g.pt.x + 12, t: g.pt.y, b: g.pt.y + (sp.stand ? 16 : 12) }, tag: { l: tl, r: tl + tw, t: ty, b: ty + 16 / s } }; });
  for (let i = 0; i < furn.length; i++) for (let j = i + 1; j < furn.length; j++) if (furn[i].n !== furn[j].n && ov(furn[i], furn[j])) out.push('furniture ' + furn[i].n + ' x ' + furn[j].n);
  furn.forEach(f => { if (f.l < L.x + 2 || f.r > L.x + L.w - 2 || f.t < top || f.b > top + 82) out.push('furniture outside room ' + f.n); });
  people.forEach(p => {
    furn.forEach(f => { if (f.n !== p.node && ov(p.body, f)) out.push('person ' + p.key + ' x furniture ' + f.n); if (f.n !== p.node && ov(p.tag, f)) out.push('tag ' + p.name + '@' + p.key + ' x furniture ' + f.n); });
    if (p.tag.l < L.x + 2 - 0.01 || p.tag.r > L.x + L.w - 2 + 0.01 || p.tag.b > top + 84 + 0.01 || p.tag.t < top + 0.99) out.push('tag ' + p.name + ' outside room');
  });
  for (let i = 0; i < people.length; i++) for (let j = 0; j < people.length; j++) { const a = people[i], b = people[j];
    if (i < j && ov(a.body, b.body)) out.push('person x person ' + a.key + ' ' + b.key);
    if (i < j && ov(a.tag, b.tag)) out.push('tag x tag ' + a.name + '@' + a.key + ' ' + b.name + '@' + b.key);
    if (i !== j && ov(a.tag, b.body)) out.push('tag ' + a.name + '@' + a.key + ' x person ' + b.key); }
  if (new Set(people.map(p => p.key)).size !== people.length) out.push('shared spot');
  return { out, people };
}
{
  const cases = [], bad = [], info = [];
  for (const [label, mode, cw, iw] of [['strip@1280', 'strip', 1214, 1280], ['strip@1720', 'strip', 1654, 1720], ['game@1600', 'full', 1600, 1600]])
    for (const n of [0, 6, 12, 25, 40]) for (const names of ['short', 'mixed', 'long']) cases.push({ label, mode, cw, iw, n, names });
  for (const c of cases) {
    const w = world(dir, { cw: c.cw, mode: c.mode, iw: c.iw, ih: 1000 }); w.game.update(synth(Array(c.n).fill('done'), 60, c.names), null);
    for (let step = 0; step < 4; step++) {          // now, and after slot changes (each person changes at its own time within 150 s)
      if (step) w.secs(45);
      const r = overlaps(w), L = lounge(w.G), shown = restingEnts(w.G).length;
      if (r.out.length) bad.push([c, 'step ' + step, r.out.slice(0, 3)]);
      if (shown + L.extra !== c.n) bad.push([c, 'step ' + step, 'shown ' + shown + ' + extra ' + L.extra + ' != ' + c.n]);
      if (!step) info.push(c.label + ' n=' + c.n + ' ' + c.names + ': ' + shown + ' shown' + (L.extra ? ' +' + L.extra : '') + ' cfg ' + JSON.stringify(L.cfg));
    }
  }
  ok('no overlap of furniture / people / name tags (' + cases.length + ' cases x 4 times: 3 widths x resting 0, 6, 12, 25, 40 x short, mixed, long names); shown + "+N" = resting', !bad.length, bad.slice(0, 3));
  if (process.env.VERBOSE) info.forEach(l => console.log('  ' + l));
}

// ---- 2b. a long name is shown in full: the tag is displayed with the width the assignment used, and the overlap check uses the shown width ----
{
  const bad = [], long31 = 'sol6.1-' + 'x'.repeat(24);                       // 31 characters
  for (const [label, mode, cw, iw] of [['strip@1280', 'strip', 1214, 1280], ['strip@1720', 'strip', 1654, 1720], ['game@1600', 'full', 1600, 1600]]) for (const n of [1, 3, 12]) for (const nm of [long31, 'opus5.5-13']) {
    const st = synth(Array(n).fill('done')); st.agents[0].tag = nm;
    const w = world(dir, { cw, mode, iw, ih: 1000 }); w.game.update(st, null);
    const L = lounge(w.G), e = w.G.ents.get(st.agents[0].id), lt = e && w.G.ltags.get(e.id);
    if (!e || !e.act) { if (n === 1) bad.push([label, n, nm.length, 'the only resting person got no spot']); continue; }
    if (!lt) { bad.push([label, n, nm.length, 'no tag element']); continue; }
    if (lt.el.style.maxWidth) bad.push([label, n, nm.length, 'the tag still has a max-width', lt.el.style.maxWidth]);
    if (Math.abs(lt.w * w.G.scale - TAGW(nm)) > 0.01) bad.push([label, n, nm.length, 'tag width used for placing != measured width', lt.w * w.G.scale, TAGW(nm)]);
    const r = overlaps(w); if (r.out.length) bad.push([label, n, nm.length, r.out.slice(0, 2)]);
  }
  ok('long names are not cut: one resting person with a 31-character (and a 10-character) name at 1280/1720/1600 x 1, 3, 12 resting: the tag has no max-width, its width equals the width used for the assignment, and nothing overlaps with that shown width', !bad.length, bad.slice(0, 3));
}
// ---- 2c. while somebody walks the tag follows the person (position only, no rebuild); on arrival it sits at the spot's tag position ----
{
  const st = synth(Array(12).fill('done')), w = world(dir, { cw: 1654, iw: 1654 }); w.game.update(st, null);
  const G = w.G, s = G.scale, bad = [], clampXY = (lt, x, y) => [lt.w * 2 >= G.W ? G.W / 2 : Math.max(lt.w / 2, Math.min(G.W - lt.w / 2, x)), Math.max(0, Math.min(G.H - 16 / s, y))];
  const want = e => { const tg = e.goal.tag, lt = G.ltags.get(e.id), mv = e.path && e.path.length, dx = mv ? 6 : tg.dx, dy = mv ? 19 : tg.dy, al = mv ? null : tg.al, shift = al === 'l' ? lt.w / 2 : al === 'r' ? -lt.w / 2 : 0, xy = clampXY(lt, e.x + dx + shift, e.y + dy); return [xy[0] * s, xy[1] * s]; };   // walking: right below the person; arrived: the spot's tag position
  let walk = null, frames = 0, checked = 0, maxDist = 0, rebuilt = 0;
  { const before = new Map([...G.ltags].map(([id, lt]) => [id, lt.el])); w.game.update(JSON.parse(JSON.stringify(st)), null); w.game.update(JSON.parse(JSON.stringify(st)), null);
    const same = [...G.ltags].filter(([id, lt]) => before.get(id) === lt.el).length;
    ok('a state update (labels rebuilt) keeps the lounge tag elements: ' + same + ' of ' + before.size + ' are the same elements, only their positions are set again', before.size > 0 && same === before.size && G.ltags.size === before.size, [same, before.size, G.ltags.size]); }
  for (let i = 0; i < 12000 && !walk; i++) { w.frames(1); walk = restingEnts(G).find(e => e.moving); }
  ok('somebody starts to walk to a new spot', !!walk);
  if (walk) {
    const el0 = G.ltags.get(walk.id).el, startTag = [parseFloat(el0.style.left), parseFloat(el0.style.top)], goalTag = (() => { const tg = walk.goal.tag, lt = G.ltags.get(walk.id), shift = tg.al === 'l' ? lt.w / 2 : tg.al === 'r' ? -lt.w / 2 : 0, xy = clampXY(lt, walk.goal.pt.x + tg.dx + shift, walk.goal.pt.y + tg.dy); return [xy[0] * s, xy[1] * s]; })();
    const dStart = Math.hypot(startTag[0] - goalTag[0], startTag[1] - goalTag[1]);
    if (dStart < 1) bad.push('the tag already sits at the destination when the walk starts');
    const labels0 = G.labels.length;
    while (walk.moving && frames < 600) {
      const lt = G.ltags.get(walk.id); if (!lt || lt.el !== el0) { rebuilt++; break; }          // same element all the time = positions are updated, the labels are not rebuilt
      const w0 = want(walk), dist = Math.hypot(parseFloat(lt.el.style.left) - w0[0], parseFloat(lt.el.style.top) - w0[1]); maxDist = Math.max(maxDist, dist); checked++;
      if (dist > 0.6) bad.push(['tag is ' + dist.toFixed(2) + ' px (screen) from where the person is', walk.x, walk.y]);
      w.frames(1); frames++;
    }
    const lt = G.ltags.get(walk.id), fin = want(walk), dEnd = Math.hypot(parseFloat(lt.el.style.left) - goalTag[0], parseFloat(lt.el.style.top) - goalTag[1]);
    ok('the tag follows the walking person (' + checked + ' frames, largest gap ' + maxDist.toFixed(2) + ' px on screen), stays the same element (no rebuild during the walk), and is at the spot\'s tag position on arrival (' + dEnd.toFixed(2) + ' px off)', !bad.length && !rebuilt && checked >= 3 && dEnd < 0.6 && lt.el === el0, [bad.slice(0, 2), rebuilt, checked, dEnd]);
    ok('the walk started with the tag near the person, not at the destination (gap start->destination ' + dStart.toFixed(1) + ' px on screen)', dStart >= 1);
  }
}

// ---- 2d. tags stay inside the visible area (strip / /game canvas) while people walk to the lounge: through the corridor of the last row, the hall and the entrance; long names too ----
// running -> done: people leave their desks, walk out of the room, along the corridor (and the vertical hall when the lounge is on another row) and into the lounge.
{
  const bad = [], seen = { corrLast: 0, hall: 0, moving: 0, cases: 0 }, rows = new Set();
  const LONG = 'sol6.1-' + 'x'.repeat(24);
  for (const [label, mode, cw, iw] of [['strip@1280', 'strip', 1214, 1280], ['strip@1720', 'strip', 1654, 1720], ['game@1600', 'full', 1600, 1600]]) for (const n of [3, 12, 24]) for (const names of ['short', 'long']) {
    const st = synth(Array(n).fill('running'), 60, names), w = world(dir, { cw, mode, iw, ih: 1000 }); w.game.update(st, null); w.secs(10);
    if (names === 'long') st.agents.forEach((a, i) => { if (i % 2 === 0) a.tag = LONG.slice(0, 20 + (i % 12)); });         // names 20-31 characters (the desk labels ignore this; the lounge tags show them in full)
    const st2 = JSON.parse(JSON.stringify(st)); st2.agents.forEach(a => { a.status = 'done'; a.last_ts = NOW / 1000; }); w.game.update(st2, null);
    const G = w.G, s = G.scale, L = lounge(G), lastRow = G.rows - 1, corrTop = lastRow * 134 + 36 + 82 - 2; seen.cases++; rows.add(G.rows);
    for (let f = 0; f < 700; f++) {
      w.frames(1);
      for (const [id, lt] of G.ltags) {
        const e = lt.e, left = parseFloat(lt.el.style.left), top = parseFloat(lt.el.style.top), name = e.agent.tag || e.agent.title, wc = TAGW(name);
        const l = left - wc / 2, r = left + wc / 2, b = top + 16;
        if (l < -0.01 || r > G.W * s + 0.01 || top < -0.01 || b > G.H * s + 0.01) bad.push([label, n, names, 'tag outside the visible area', name.length, [l, r, top, b].map(v => +v.toFixed(1)), 'canvas', G.W * s, G.H * s, 'person', +e.x.toFixed(1), +e.y.toFixed(1)]);
        if (e.path && e.path.length) { seen.moving++; if (e.y + 15 >= corrTop) seen.corrLast++; if (e.x < 14) seen.hall++;
          const own = [(e.x + 6) * s, (e.y + 19) * s]; if (b < G.H * s - 0.01 && Math.hypot(left - Math.max(wc / 2, Math.min(G.W * s - wc / 2, own[0])), top - own[1]) > 0.6) bad.push([label, n, names, 'a walking tag is not right below its person', name.length]); }
      }
      if (bad.length > 400) break;
    }
  }
  const kinds = {}; bad.forEach(b => { kinds[b[3]] = (kinds[b[3]] || 0) + 1; });
  ok('tags stay inside the visible area while people walk running -> done (' + seen.cases + ' cases: 3 widths x 3, 12, 24 people x short/long names, ' + seen.moving + ' walking tag samples, of them ' + seen.corrLast + ' in the corridor of the last row and ' + seen.hall + ' in the hall/entrance column, rows ' + [...rows].join('/') + '); a walking tag sits right below the person (not at the destination offset)', !bad.length && seen.corrLast > 0 && seen.hall > 0, { kinds, first: bad.slice(0, 2) });
}

// ---- 2e. a lounge tag stays above the desk tags, nameplates and signs of the other rooms, also when its element is reused (then it sits earlier in the DOM) ----
// Stacking order inside .ag-ov (its own stacking context, z-index 1, below the people canvas): higher z-index wins, the same z-index -> the later element in the DOM wins.
{
  const w = world(dir, { cw: 1654, iw: 1654 }), st = synth(Array(6).fill('running').concat(Array(6).fill('done')));
  w.game.update(st, null); w.game.update(JSON.parse(JSON.stringify(st)), null); w.game.update(JSON.parse(JSON.stringify(st)), null);          // updates: the other labels are recreated, the lounge tags are reused
  const G = w.G, css = w.ctx.document.head.children.map(c => c.textContent).join('\n').replace(/\/\*[\s\S]*?\*\//g, ''), rules = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].map(m => ({ sel: m[1].trim(), body: m[2] }));
  const zOf = cls => { let z = 0, spec = -1; rules.forEach(r => r.sel.split(',').forEach(sel => { sel = sel.trim(); if (!/^(\.[\w-]+)+$/.test(sel)) return; const cs = sel.split('.').filter(Boolean), zz = /z-index:\s*(\d+)/.exec(r.body); if (zz && cs.every(c => cls.split(/\s+/).includes(c)) && cs.length >= spec) { z = +zz[1]; spec = cs.length; } })); return z; };
  const ovZ = zOf('ag-ov'), peopleZ = +(/canvas\.ag-top[^}]*z-index:\s*(\d+)/.exec(css) || [0, 0])[1], kids = G.ov.children, lounge = [...G.ltags.values()].map(x => x.el), others = kids.filter(k => !lounge.includes(k));
  const idx = el => kids.indexOf(el), above = (a, b) => zOf(a.className) > zOf(b.className) || (zOf(a.className) === zOf(b.className) && idx(a) > idx(b));
  const bad = []; lounge.forEach(l => others.forEach(o => { if (!above(l, o)) bad.push([l.className + '@' + idx(l), 'under', o.className + '@' + idx(o)]); }));
  const earlier = lounge.filter(l => others.some(o => idx(l) < idx(o))).length;
  ok('the lounge tags are above the other rooms\' labels: ' + lounge.length + ' lounge tags (' + earlier + ' of them earlier in the DOM than some other label, because they are reused) vs ' + others.length + ' other labels (desk tags, nameplates, signs, boards): none is below one; and the tags stay under the people layer (.ag-ov z-index ' + ovZ + ' < canvas.ag-top ' + peopleZ + ')', lounge.length >= 3 && earlier > 0 && !bad.length && ovZ < peopleZ, [bad.slice(0, 3), lounge.length, earlier, ovZ, peopleZ]);
}

// ---- 3. layout order and the priority list ----
// Left: small 2-seat sofas (one row, at the far left), then the tea tables (small round tables). Right end: coffee, games, treadmills, dumbbell (right-aligned, constant column pitch). Middle empty.
// Priority when narrow (kept in this order, the rest is cut from the back): sofa1, table1, coffee, game1, treadmill1, dumbbell, 4 chairs, table2, game2, treadmill2, sofa2, table3, game3, game4, treadmill3, sofa3
const STEPS = [s => { s.sofa = 1; }, s => { s.tables = 1; }, s => { s.coffee = 1; }, s => { s.game = 1; }, s => { s.tread = 1; }, s => { s.dumb = 1; }, s => { s.chairs4 = true; },
  s => { s.tables = 2; }, s => { s.game = 2; }, s => { s.tread = 2; }, s => { s.sofa = 2; }, s => { s.tables = 3; }, s => { s.game = 3; }, s => { s.game = 4; }, s => { s.tread = 3; }, s => { s.sofa = 3; }];
const prefix = k => { const s = { sofa: 0, tables: 0, coffee: 0, game: 0, tread: 0, dumb: 0, chairs4: false }; STEPS.slice(0, k).forEach(f => f(s)); return JSON.stringify(s); };
const PREFIXES = STEPS.map((_, k) => prefix(k + 1));
{
  const bad = [], cfgs = [];
  for (let cw = 520; cw <= 3000; cw += 20) for (const idle of [0, 12, 24]) {
    const w = world(dir, { cw, mode: 'strip', iw: cw, ih: 1000 }); w.game.update(synth(Array(idle).fill('done')), null); const L = lounge(w.G), c = L.cfg, tag = [cw, idle, L.w];
    const xs = k => L.byKind[k].map(s => s.x), teaX = xs('tea'), coffeeX = xs('coffee')[0], gameX = xs('game'), trX = xs('tread'), dumbX = xs('dumb');
    // sofas: small, far left, two per column front/back (second column only for the third), all facing down
    if (L.units.length < 1 || L.units.length > 3) bad.push([tag, 'sofa count', L.units.length]);
    if (Math.min(...L.units.map(u => u.x)) !== L.x + 6) bad.push([tag, 'sofas not at the far left (x + 6)']);
    if (L.units[0].w < 30 || L.units[0].w > 40) bad.push([tag, 'sofa width', L.units[0].w]);
    { const cols = {}; L.units.forEach(u => { (cols[u.x] = cols[u.x] || []).push(u); });
      if (Object.values(cols).some(c => c.length > 2)) bad.push([tag, 'more than two sofas in a column']);
      const lowerOk = 63 + 2 * L.th <= 84;
      if (lowerOk && L.units.length >= 2 && !(cols[L.x + 6] && cols[L.x + 6].length === 2)) bad.push([tag, 'sofas not front/back in the first column']);
      if (lowerOk && L.units.length === 3 && Object.keys(cols).length !== 2) bad.push([tag, 'third sofa not in a second column']);
      if (!lowerOk && Object.values(cols).some(c => c.length > 1)) bad.push([tag, 'front/back sofas although the tags do not fit']);
      Object.values(cols).forEach(c => { if (c.length === 2) { const [u0, u1] = c.sort((a, b) => a.y - b.y); if (!(u1.y - 12 >= u0.y + 12 + L.th + 2 - 0.01)) bad.push([tag, 'front/back sofas too close: sofa + tag height', u0.y, u1.y, L.th]); } });
      const xs = Object.keys(cols).map(Number).sort((a, b) => a - b); xs.slice(1).forEach((x, i) => { if (x - (xs[i] + L.units[0].w) < 8) bad.push([tag, 'sofa column gap < 8']); });
      if (L.byKind.sofa.some(sp => sp.dir !== 'down')) bad.push([tag, 'a sofa seat does not face down']); }
    // tea: right beside the sofas, small round tables (diameter 12-14) with 2 or 4 chairs each
    const sofaEnd = Math.max(...L.units.map(u => u.x + u.w)), tables = [...new Set(L.byKind.tea.map(s => s.node))];
    if (Math.min(...teaX) < sofaEnd || Math.min(...teaX) - sofaEnd > 30) bad.push([tag, 'tea not right beside the sofas', sofaEnd, Math.min(...teaX)]);
    if (tables.length < 1 || tables.length > 3) bad.push([tag, 'tea table count', tables.length]);
    tables.forEach(n => {
      const seatsT = L.byKind.tea.filter(s => s.node === n), f = L.furn.find(q => q.n === n && q.w > 12 && q.h > 12);
      if (seatsT.length !== 2 && seatsT.length !== 4) bad.push([tag, 'chairs per table', seatsT.length]);
      if (!f || f.w < 12 || f.w > 14 || f.h !== f.w) { bad.push([tag, 'table not a small round table', f]); return; }
      // cross: everybody faces the table and sits right against it: top (front view, faces down), bottom (back view, faces up), left (faces right), right (faces left)
      const want = { N: ['down', 0, -1], S: ['up', 0, 1], W: ['right', -1, 0], E: ['left', 1, 0] }, tcx = f.x + f.w / 2 - 0.5;
      seatsT.forEach(sp => { const k = sp.key.split('.').pop(), [dir, dx, dy] = want[k]; if (sp.dir !== dir) bad.push([tag, 'tea seat ' + k + ' faces ' + sp.dir + ', not ' + dir]);
        const body = { l: sp.x, r: sp.x + 12, t: sp.y, b: sp.y + 12 };
        const gapX = dx < 0 ? f.x - body.r : dx > 0 ? body.l - (f.x + f.w) : 0, gapY = dy < 0 ? f.y - body.b : dy > 0 ? body.t - (f.y + f.h) : 0;
        if (dx !== 0 && (Math.abs(gapX) > 1 || Math.abs((sp.y + 6) - (f.y + f.h / 2)) > 1.5)) bad.push([tag, 'tea seat ' + k + ' not beside the table', gapX]);
        if (dy !== 0 && (Math.abs(gapY) > 1 || Math.abs((sp.x + 6) - (f.x + f.w / 2)) > 1.5)) bad.push([tag, 'tea seat ' + k + ' not above/below the table', gapY]); });
      if (seatsT.length === 4 && ![...'NSWE'].every(k => seatsT.some(sp => sp.key.endsWith('.' + k)))) bad.push([tag, 'cross needs N, S, W, E']);
      if (seatsT.length === 2 && !(seatsT.some(sp => sp.key.endsWith('.N')) && seatsT.some(sp => sp.key.endsWith('.S')))) bad.push([tag, 'two chairs must be top and bottom']);
    });
    // right group: coffee, games, treadmills, dumbbell in that order, constant pitch 22, right-aligned; middle stays empty
    const cols = [coffeeX].concat(gameX, trX, dumbX).map(x => x + 6);
    cols.slice(1).forEach((x, i) => { if (x - cols[i] !== 22) bad.push([tag, 'column pitch', x - cols[i]]); });
    if (Math.abs(cols[cols.length - 1] + 11 - (L.x + L.w - 3)) > 0.01) bad.push([tag, 'right group not aligned to the right wall', cols[cols.length - 1] + 11, L.x + L.w - 3]);
    if (!(Math.max(...teaX) + 12 < coffeeX)) bad.push([tag, 'tea not left of coffee']);
    // machines are always there (also in the narrowest lounge): >= 1 game, >= 1 treadmill, the dumbbell; and they are really drawn
    const fills = new Set(); L.decor.forEach(f => f((col) => fills.add(col)));
    if (L.arcs.length < 1 || L.treads.length < 1 || dumbX.length !== 1) bad.push([tag, 'machine missing', L.arcs.length, L.treads.length, dumbX.length]);
    if (!fills.has('#e0503c') || !fills.has('#0e2a1c') || !fills.has('#5b6072') || !fills.has('#a86b3a')) bad.push([tag, 'a machine is not drawn', [...fills].filter(c => ['#e0503c', '#0e2a1c', '#5b6072', '#a86b3a'].includes(c))]);
    if (![1, 2, 3, 4].includes(c.game) || ![1, 2, 3].includes(c.tread) || c.dumb !== 1 || c.coffee !== 1) bad.push([tag, 'counts out of range', c]);
    cfgs.push({ cw, idle, th: L.th, L: L.w, c, k: PREFIXES.indexOf(JSON.stringify(c)) });
  }
  ok('layout in ' + cfgs.length + ' widths: 1-3 small 2-seat sofas in one row at the far left, round tea tables (2 or 4 chairs) right beside them, coffee + games + treadmills + dumbbell right-aligned in a constant column pitch; game, treadmill and dumbbell are drawn at every width', !bad.length, bad.slice(0, 3));
  const viol = [];
  cfgs.forEach(x => { if (x.k < 0) viol.push([x.cw, x.idle, 'config is not a prefix of the priority list', x.c]); });
  const byIdle = {}; cfgs.forEach(x => { const key = x.idle + '/th' + x.th; (byIdle[key] = byIdle[key] || []).push(x); });   // same tag height (= same scale class): a wider lounge never has less
  Object.values(byIdle).forEach(list => { list.sort((a, b) => a.L - b.L); list.slice(1).forEach((y, i) => { const x = list[i]; if (y.L > x.L && y.k < x.k) viol.push([x.idle, x.L + '->' + y.L, 'a wider lounge has fewer things', x.k, y.k]); }); });
  const kmin = Math.min(...cfgs.map(x => x.k)), kmax = Math.max(...cfgs.map(x => x.k));
  ok('priority list: at every width the config is the first N entries of sofa1, table1, coffee, game1, treadmill1, dumbbell, 4 chairs, table2, game2, treadmill2, sofa2, table3, game3, game4, treadmill3, sofa3 (N from ' + (kmin + 1) + ' to ' + (kmax + 1) + ' of 16); wider never has less', !viol.length && kmin >= 5, viol.slice(0, 3));
  console.log('  narrowest lounge ' + cfgs.reduce((m, x) => Math.min(m, x.L), 9999) + ': ' + JSON.stringify(cfgs.filter(x => x.k === kmin)[0].c) + '; widest ' + cfgs.reduce((m, x) => Math.max(m, x.L), 0) + ': ' + JSON.stringify(cfgs.filter(x => x.k === kmax)[0].c));
  // deterministic: the same width gives the same layout
  const A = world(dir, { cw: 1654, mode: 'strip', iw: 1654 }), B = world(dir, { cw: 1654, mode: 'strip', iw: 1654 }); A.game.update(synth(Array(12).fill('done')), null); B.game.update(synth(Array(12).fill('done')), null);
  ok('same width, same layout (spots, furniture)', JSON.stringify(lounge(A.G).spots.map(s => [s.key, s.x, s.y])) === JSON.stringify(lounge(B.G).spots.map(s => [s.key, s.x, s.y])) && JSON.stringify(lounge(A.G).furn) === JSON.stringify(lounge(B.G).furn));
}

// ---- 4. sofas: drawn = in use (min 1, max 3), never more sitters than seats, all facing the front ----
{
  const bad = [];
  for (const [n, names] of [[6, 'short'], [12, 'short'], [25, 'short'], [25, 'long'], [40, 'mixed']]) for (const [cw, mode] of [[1654, 'strip'], [1214, 'strip'], [1600, 'full'], [2200, 'strip']]) {
    const w = world(dir, { cw, mode, iw: cw, ih: 1000 }); w.game.update(synth(Array(n).fill('done'), 60, names), null);
    const L = lounge(w.G), es = restingEnts(w.G).filter(e => e.act.kind === 'sofa'), used = new Set(es.map(e => e.act.spot.site)), maxU = Math.max(-1, ...used);
    if (L.sofaN !== Math.max(1, maxU + 1)) bad.push([cw, n, 'drawn sofas != used', L.sofaN, maxU + 1]);
    if (L.sofaN > 3 || es.length > L.units.length * 2) bad.push([cw, n, 'more sofa sitters than seats', es.length, L.units.length]);
    if (es.some(e => e.act.dir !== 'down' && e.act.mode !== 'chat')) bad.push([cw, n, 'a sofa sitter does not face the front']);
  }
  ok('sofas: drawn sofas = sofas in use (min 1, max 3); never more sitters than seats; they face the front (side view only when chatting)', !bad.length, bad.slice(0, 3));
}

// ---- 5. assignment ----
{
  const st = synth(Array(12).fill('done'));
  const w = world(dir, { cw: 1654, iw: 1654 }); w.game.update(st, null);
  const snap = () => restingEnts(w.G).map(e => e.id + '>' + e.act.spot.key + '/' + e.act.mode).sort().join(';');
  const a1 = snap(); w.game.update(JSON.parse(JSON.stringify(st)), null); w.game.update(JSON.parse(JSON.stringify(st)), null);
  ok('same input, same result: 3 updates in a row give the same spot and mode for everyone (no pop when redrawn)', a1 === snap() && a1.length > 0, [a1, snap()]);
}
{
  // distribution over many (id, slot) with everything free: one person at a time
  const N = 4000, cnt = {}, W = { sofa: 35, tea: 20, game: 15, tread: 8, dumb: 7, coffee: 10 };
  const w = world(dir, { cw: 2200, iw: 2200 }); let k = 0;
  for (let i = 0; i < N; i++) {
    NOW = T0 + (i * 77) * 1000;    // different time each iteration -> different slot
    w.game.update(single('done', 60, i), null);
    const e = restingEnts(w.G)[0]; if (e) { cnt[e.act.kind] = (cnt[e.act.kind] || 0) + 1; k++; }
  }
  const present = Object.keys(W).filter(x => lounge(w.G).byKind[x].length), tot = present.reduce((t, x) => t + W[x], 0);
  const dev = present.map(x => [x, +(100 * (cnt[x] || 0) / k).toFixed(1), +(100 * W[x] / tot).toFixed(1)]);
  ok('weights (sofa 35, tea 20, game 15, treadmill 8, dumbbell 7, coffee 10, over the kinds that exist) reproduced within 2 points over ' + k + ' samples', dev.every(d => Math.abs(d[1] - d[2]) < 2), dev);
  NOW = T0;
}
{
  const N = 3000; let sofa = 0, doze = 0; const w = world(dir, { cw: 2200, iw: 2200 });
  for (let i = 0; i < N; i++) {
    NOW = T0 + (i * 91) * 1000;
    w.game.update(single('done', 3600, i), null);
    const e = restingEnts(w.G)[0]; if (e.act.kind === 'sofa') { sofa++; if (e.act.mode === 'doze') doze++; }
  }
  const L = lounge(w.G), others = { tea: 20, game: 15, tread: 8, dumb: 7, coffee: 10 }, o = Object.keys(others).filter(x => L.byKind[x].length).reduce((t, x) => t + others[x], 0), expS = 100 * 70 / (70 + o);
  ok('rested over 30 min: sofa share ~ 70/(70+' + o + ') = ' + expS.toFixed(1) + '% (got ' + (100 * sofa / N).toFixed(1) + '%) and dozing on the sofa ~ 65% (got ' + (100 * doze / sofa).toFixed(1) + '%)', Math.abs(100 * sofa / N - expS) < 3 && Math.abs(100 * doze / sofa - 65) < 5);
  let sofaS = 0, dozeS = 0; NOW = T0;
  for (let i = 0; i < N; i++) { NOW = T0 + (i * 91) * 1000; w.game.update(single('done', 60, i), null); const e = restingEnts(w.G)[0]; if (e.act.kind === 'sofa') { sofaS++; if (e.act.mode === 'doze') dozeS++; } }
  ok('recently finished: dozing on the sofa ~ 30% (got ' + (100 * dozeS / sofaS).toFixed(1) + '%)', Math.abs(100 * dozeS / sofaS - 30) < 5);
  NOW = T0;
  const bad2 = [];
  for (const status of ['failed', 'killed', 'ended']) for (const [cw, mode, iw] of [[1600, 'full', 1600], [1654, 'strip', 1720], [1214, 'strip', 1280]]) {
    const w2 = world(dir, { cw, mode, iw, ih: 1000 }); w2.game.update(synth([status, 'done', 'done', 'done']), null);
    const e = restingEnts(w2.G).find(x => x.agent.status === status);
    if (!e || e.act.kind !== 'sofa' || e.act.mode !== 'sit') bad2.push([status, cw, mode, e && e.act.kind + '/' + e.act.mode]);
  }
  ok('failed / killed / ended take the sofa first and sit awake (as before), at every width', !bad2.length, bad2);
  const st3 = synth(Array(3).fill('failed').concat(Array(20).fill('done'))); const w3 = world(dir, { cw: 1654, iw: 1654 }); w3.game.update(st3, null);
  ok('dozing only on the sofa', !restingEnts(w3.G).some(e => e.act.mode === 'doze' && e.act.kind !== 'sofa'));
}
{
  // capacity: 40 resting: everyone who fits is placed, the rest is "+N", no spot shared
  const st = synth(Array(40).fill('done')); const w = world(dir, { cw: 1654, iw: 1654 }); w.game.update(st, null);
  const L = lounge(w.G), es = restingEnts(w.G);
  ok('40 resting: placed ' + es.length + ' <= spots ' + L.spots.length + ', "+' + L.extra + '" = the rest, no shared spot', es.length <= L.spots.length && es.length + L.extra === 40 && new Set(es.map(e => e.act.spot.key)).size === es.length);
  const one = synth(Array(12).fill('done'), 60, 'short'); const w2 = world(dir, { cw: 1214, mode: 'strip', iw: 1280 }); w2.game.update(one, null);
  const L2 = lounge(w2.G);
  ok('long names only sit where the neighbours leave room: 12 resting with long names never overlap tags (checked above) and none is dropped when spots are free (shown ' + restingEnts(w2.G).length + '/12 at 1280)', restingEnts(w2.G).length + L2.extra === 12);
}

// ---- 6. time slots: staggered, walk to the new spot, props vanish ----
// How many people move in the same second depends on the start time (the picks hash the absolute time, and one flip can push a neighbour to another spot).
// So it is measured from six start times 53 s apart (six phases of the 150 s slot) and the bounds are ones that held for every start time tried
// (16 start times 25 s apart: 4-5 at once, 4-5 walking at once, 26-42 changes in 320 s). "Staggered" = never more than half of the 12 move in the same second.
{
  const T = NOW, runs = [];
  for (let k = 0; k < 6; k++) {
    NOW = T + k * 53000;
    const st = synth(Array(12).fill('done')); const w = world(dir, { cw: 1654, iw: 1654 }); w.game.update(st, null);
    const first = () => new Map(restingEnts(w.G).map(e => [e.id, e.act.spot.key]));
    const changes = []; let maxWalkers = 0, prev = first();
    for (let s = 0; s < 320; s++) {           // 320 s, sampled every second
      w.secs(1);
      const cur = first(); let n = 0;
      cur.forEach((v, id) => { if (v !== prev.get(id)) n++; });
      changes.push(n); prev = cur;
      maxWalkers = Math.max(maxWalkers, restingEnts(w.G).filter(e => e.moving).length);
    }
    runs.push({ tot: changes.reduce((a, b) => a + b, 0), maxAtOnce: Math.max(...changes), maxWalkers });
  }
  NOW = T + 320000;                           // later sections go on from where a single run used to leave the clock
  const lo = f => Math.min(...runs.map(r => r[f])), hi = f => Math.max(...runs.map(r => r[f]));
  ok('12 resting, 320 s, 6 start times: ' + lo('tot') + '-' + hi('tot') + ' spot changes (need >= 6 each), at most ' + hi('maxAtOnce') + ' in one second (need <= 6), at most ' + hi('maxWalkers') + ' walking at once (need <= 7)',
    runs.every(r => r.tot >= 6 && r.maxAtOnce <= 6 && r.maxWalkers <= 7), runs);
}
{
  const st = synth(Array(12).fill('done')); const w = world(dir, { cw: 1654, iw: 1654 }); w.game.update(st, null);
  let seen = null;
  for (let s = 0; s < 8000 && !seen; s++) {
    const before = new Map(restingEnts(w.G).map(e => [e.id, e.act.spot.key]));
    w.frames(1);
    for (const e of restingEnts(w.G)) if (before.get(e.id) !== e.act.spot.key) { seen = { from: before.get(e.id), to: e.act.spot.key, e }; break; }
  }
  ok('a slot flip sends the person walking to the new spot (goal = new spot, path non-empty)', seen && seen.e.path.length > 0 && seen.e.goal.pt.x === seen.e.act.spot.x && seen.e.goal.pt.y === seen.e.act.spot.y, seen && [seen.from, seen.to]);
  if (seen) {
    let frames = 0; while (seen.e.moving && frames < 400) { w.frames(1); frames++; }
    ok('... and arrives at exactly the spot after ' + frames + ' frames', !seen.e.moving && seen.e.x === seen.e.goal.pt.x && seen.e.y === seen.e.goal.pt.y, [seen.e.x, seen.e.y, seen.e.goal.pt]);
  }
}
{
  const st = synth(Array(12).fill('done')); const w = world(dir, { cw: 1654, iw: 1654 }); w.game.update(st, null);
  const G = w.G, rest = () => G.loungeNext - NOW / 1000;
  w.G.demo = { real: null, timers: [] };   // what game-demo.js sets while a demo runs
  w.game.update(st, null);
  ok('demo: next slot change within 20 s (' + rest().toFixed(1) + ' s)', rest() > 0 && rest() <= 20);
  w.G.demo = null; w.game.update(st, null);
  ok('not demo: next slot change within 150 s (' + rest().toFixed(1) + ' s)', rest() > 0 && rest() <= 150);
}
{
  const st = synth(Array(6).fill('done')); const w = world(dir, { cw: 1654, iw: 1654 }); w.game.update(st, null);
  const id = st.agents[0].id; const e = w.G.ents.get(id);
  ok('resting person has an activity', e && e.act);
  st.agents[0].status = 'running'; w.game.update(JSON.parse(JSON.stringify(st)), null);
  ok('given work again: act is cleared and the person leaves for the desk (where=sit, path non-empty)', e.act === null && e.where === 'sit' && e.path.length > 0, [e.act, e.where, e.path.length]);
  w.secs(15);
  ok('... and arrives at the desk seat', !e.moving && e.x === e.seat.pos.x && e.y === e.seat.pos.y, [e.x, e.y, e.seat.pos]);
}
// ---- 7. chat pairs ----
{
  const bad = []; let pairs = 0;
  for (let t = 0; t < 60; t++) {
    NOW = T0 + t * 1000 * 37;
    const st = synth(Array(12).fill('done')); const w = world(dir, { cw: 2200, iw: 2200 }); w.game.update(st, null);
    const by = new Map(restingEnts(w.G).map(e => [e.act.spot.key, e]));
    for (const e of by.values()) if (e.act.mode === 'chat') {
      const sp = e.act.spot, nbK = e.act.dir === 'right' ? sp.k + 1 : sp.k - 1, nb = by.get('sofa' + sp.site + '.' + nbK);
      if (!nb || nb.act.mode !== 'chat' || nb.act.dir === e.act.dir) bad.push([sp.key, e.act.dir]); else pairs++;
    }
  }
  NOW = T0;
  ok('chatting people always sit next to a chatting partner they face (' + pairs + ' facings checked)', !bad.length && pairs > 0, bad.slice(0, 3));
}
// ---- 8. speed: 30 in the lounge, 20 fps budget ----
{
  const st = synth(Array(30).fill('done')); const w = world(dir, { cw: 2200, iw: 2200 }); w.game.update(st, null);
  w.secs(2);
  const t0 = process.hrtime.bigint(), N = 400; draws = 0; w.frames(N); const ms = Number(process.hrtime.bigint() - t0) / 1e6 / N;
  ok('30 resting: ' + restingEnts(w.G).length + ' shown; step+draw in the fake browser ' + ms.toFixed(2) + ' ms/frame (budget 50 ms), ' + Math.round(draws / N) + ' canvas calls/frame', ms < 25);
}
const failed = results.filter(r => !r[1]);
console.log(failed.length ? failed.length + ' FAILED of ' + results.length : 'ALL ' + results.length + ' PASSED');
process.exit(failed.length ? 1 : 0);
