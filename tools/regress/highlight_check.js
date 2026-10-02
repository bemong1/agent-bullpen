// Synthetic checks of the office highlight (game.js mount().highlight). No browser: the same fake DOM as game_snap.js.
//  - without a highlight the draw calls and labels are what the reference statics draw (pixel-level: every canvas call is hashed)
//  - highlight(ids) draws a ring only for people who are in the office, bolds only their name tags; highlight(null) restores frames and labels exactly
//  - never during a demo
// usage: node highlight_check.js <static dir> <fixture prefix> [reference static dir]      (exit code = number of failed checks)
const fs = require('fs'), vm = require('vm'), path = require('path'), crypto = require('crypto');
const I18B = require('./i18n_boot');
const [dir, fx, refDir] = process.argv.slice(2);
const STATE = JSON.parse(fs.readFileSync(fx + '_state.json', 'utf8'));
let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + detail)); };
const sha = x => crypto.createHash('sha1').update(x).digest('hex');

// runs the office for `frames` frames (50 ms apart); `script(frame, g)` may call g.highlight(...). Returns per-frame call lists and the label dump.
function run(staticDir, frames, script) {
  let NOW = Math.round(STATE.now * 1000) + 5000;
  class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(NOW); } static now() { return NOW; } }
  const log = []; let nid = 0, raf = null;
  const ctx2d = id => ({ set fillStyle(v) { log.push(id + 'f' + v); }, get fillStyle() { return ''; }, globalAlpha: 1, imageSmoothingEnabled: false,
    fillRect: (...a) => log.push(id + 'r' + a.join(',')), clearRect: (...a) => log.push(id + 'c' + a.join(',')), drawImage: (img, ...a) => log.push(id + 'i' + (img._id || '?') + ':' + a.join(',')) });
  const mk = tag => ({ tag, _id: tag + (nid++), children: [], style: {}, dataset: {}, className: '', innerHTML: '', textContent: '', offsetWidth: 100, offsetHeight: 20,
    append(...k) { this.children.push(...k); }, appendChild(k) { this.children.push(k); }, remove() {}, addEventListener() {}, querySelector: () => mk('q'), querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0 }), clientWidth: 1600, getContext() { return this._c || (this._c = ctx2d(this._id)); } });
  const ctx = { console, Date: FDate, Math, JSON, Map, Set, Object, Array, String, Number, Promise, RegExp, innerWidth: 1600, innerHeight: 900, scrollX: 0, scrollY: 0,
    document: { createElement: mk, head: mk('head'), body: mk('body'), hidden: false, querySelectorAll: () => [], addEventListener() {} },
    requestAnimationFrame: f => { raf = f; }, addEventListener() {}, setTimeout: () => 0, clearTimeout() {}, IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  for (const f of I18B.scripts(staticDir, ['game-art.js', 'game.js', 'game-demo.js'])) {
    vm.runInContext(fs.readFileSync(path.join(staticDir, f), 'utf8'), ctx);
    if (f === 'i18n.js') I18B.boot(ctx, staticDir);      // i18n.js (when present) and its dictionaries from disk, language AB_LANG (default ko)
    // NO_LOGO=1: empty the monitor symbols so a reference that differs only in the logos still compares equal (see game_snap.js)
    if (f === 'game-art.js' && process.env.NO_LOGO) vm.runInContext('Object.values(AgentGameArt.LOGO).forEach(l => { l.map = []; })', ctx);
  }
  const box = mk('div'); box.clientWidth = 1600;
  const g = ctx.AgentGame.mount(box, { mode: 'strip' });
  g.update(STATE, null);
  const perFrame = [];
  for (let i = 1; i <= frames; i++) {
    if (script) script(i, g, ctx);
    const from = log.length; NOW += 50; raf(i * 50); perFrame.push(log.slice(from));
  }
  const dom = []; (function walk(e) { for (const k of e.children || []) { if (k.tag === 'div') dom.push([k.className, k.style.left, k.style.top, k.style.width || k.style.maxWidth || '', k.title || '', k.innerHTML || k.textContent].join('|')); walk(k); } })(box);
  return { perFrame, dom, g, cv2: g._g ? g._g.cv2._id : null };
}
const N = 60, hash = fr => sha(fr.map(f => f.join('\n')).join('\n#\n'));
// Label rows are class|left|top|width|title|html. The reference statics (the frozen Korean screen) predate two changes that are allowed: the orchestrator's name tag
// is 72 canvas units wide instead of 64, and the three room signs got a width cap (the room's width, for the ellipsis). Every other difference stays one.
function labelDiff(ref, now, scale) {
  const cut = row => { let at = -1; const f = []; for (let i = 0; i < 4; i++) { const j = row.indexOf('|', at + 1); f.push(row.slice(at + 1, j)); at = j; } return [f, row.slice(at + 1)]; };
  if (ref.length !== now.length) return ['row count ' + ref.length + ' -> ' + now.length];
  const bad = [];
  ref.forEach((r, i) => {
    if (r === now[i]) return;
    const [a, ra] = cut(r), [b, rb] = cut(now[i]);
    const orchTag = a[0] === 'ag-tag' && b[0] === 'ag-tag' && a[3] === 64 * scale + 'px' && b[3] === 72 * scale + 'px';
    const sign = a[0] === 'ag-sign' && b[0] === 'ag-sign' && a[3] === '' && /^[\d.]+px$/.test(b[3]);
    if (ra !== rb || a[0] !== b[0] || a[1] !== b[1] || a[2] !== b[2] || !(orchTag || sign)) bad.push(r + '  =>  ' + now[i]);
  });
  return bad;
}

const ctrl = run(dir, N);
if (refDir) {
  const ref = run(refDir, N);
  check('no highlight: every canvas call of every frame equals the reference statics', hash(ctrl.perFrame) === hash(ref.perFrame), 'draw differs');
  const bad = labelDiff(ref.dom, ctrl.dom, ctrl.g._g.scale);
  check('no highlight: labels (class, position, text) equal the reference statics, apart from the orchestrator tag width and the sign widths', bad.length === 0, bad.length + ' rows differ, first: ' + bad[0]);
}
const ents = ctrl.g._g.ents;
const agent = [...ents.keys()].find(k => k !== 'orch' && k !== 'user' && ents.get(k).where === 'sit') || [...ents.keys()].find(k => k !== 'orch' && k !== 'user');
const lounger = [...ents.keys()].find(k => k !== 'orch' && k !== 'user' && ents.get(k).where === 'lounge');
const RING = c => c.endsWith('f#fff4c2');
const ringCalls = (frame, cv2) => frame.filter((x, i) => x === cv2 + 'f#fff4c2' && /r\d/.test(frame[i + 1] || '')).length;
if (!agent) console.log('NOTE no agent is in the office in this fixture: only the orchestrator is pointed at');
const who = agent ? [agent, 'orch'] : ['orch'];

// highlight frames 20..40 with the agent, the orchestrator, and someone who is not in the office
const A = 20, B = 40;
const hv = run(dir, N, (i, g) => { if (i === A) g.highlight(who.concat('not-in-the-office')); if (i === B + 1) g.highlight(null); });
const cv2 = hv.cv2;
check('highlight: rings are drawn only while highlighted', hv.perFrame.slice(0, A - 1).every(f => ringCalls(f, cv2) === 0) && hv.perFrame.slice(A, B).every(f => ringCalls(f, cv2) >= 30 * who.length) && hv.perFrame.slice(B + 1).every(f => ringCalls(f, cv2) === 0),
  hv.perFrame.map(f => ringCalls(f, cv2)).join(','));
if (agent) check('highlight: ring size = two people (orchestrator + agent) x (bright + dark edge), the absent id adds none', (() => { const n = ringCalls(hv.perFrame[A + 2], cv2); const one = ringCalls(run(dir, A + 3, (i, g) => { if (i === A) g.highlight([agent]); }).perFrame[A + 2], cv2); return n === 2 * one && one > 30; })(), 'n/a');
check('un-highlighted frames before and after are identical to the control run', hash(hv.perFrame.slice(0, A - 1)) === hash(ctrl.perFrame.slice(0, A - 1)) && hash(hv.perFrame.slice(B + 1)) === hash(ctrl.perFrame.slice(B + 1)), 'frames differ after clearing');
check('highlight cleared: labels are exactly the control run labels', sha(hv.dom.join('\n')) === sha(ctrl.dom.join('\n')), 'labels differ');
// bold tags while highlighted (check at frame B, before clearing)
const mid = run(dir, B, (i, g) => { if (i === A) g.highlight(who.concat('not-in-the-office')); });
const hl = mid.dom.filter(d => / hl\|/.test(d) || d.startsWith('ag-tag') && /\bhl\b/.test(d.split('|')[0]));
check('highlight: only the tags of the agent and the orchestrator get the hl class', hl.length >= 1 && hl.length <= who.length && hl.some(d => d.includes('오케스트레이터')), JSON.stringify(hl.map(d => d.split('|')[0] + '|' + d.slice(-30))));
check('highlight of people not in the office draws nothing and marks nothing', (() => { const r = run(dir, 30, (i, g) => { if (i === 5) g.highlight(['not-in-the-office', 'user-x']); }); return r.perFrame.every(f => ringCalls(f, r.cv2) === 0) && r.dom.every(d => !/\bhl\b/.test(d.split('|')[0])) && sha(r.perFrame.map(f => f.join('\n')).join('\n')) === sha(run(dir, 30).perFrame.map(f => f.join('\n')).join('\n')); })(), 'drew something');
if (lounger) check('highlight: someone resting in the lounge gets a ring too', (() => { const r = run(dir, 12, (i, g) => { if (i === 3) g.highlight([lounger]); }); return ringCalls(r.perFrame[8], r.cv2) > 30; })(), 'no ring');
// the demo never highlights
let demoOn = false;
const demo = run(dir, 30, (i, g) => { if (i === 2) { try { g.demo('basic', {}); } catch (e) {} } if (i === 5) { demoOn = g.demoOn; g.highlight(['orch']); } });
check('highlight during the demo is ignored (the demo really runs)', demoOn && demo.perFrame.slice(6).every(f => ringCalls(f, demo.cv2) === 0), demoOn ? 'ring drawn in demo' : 'demo did not start');
console.log(fails ? 'FAILED ' + fails : 'ALL PASS');
process.exit(fails ? 1 : 0);
