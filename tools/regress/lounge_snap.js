// Check for the lounge feature: the office draw log with the LOUNGE block cut out, so a change confined to the lounge leaves the hash equal.
// Same fake browser as game_snap.js (game-art.js -> game.js -> game-demo.js, or the old single game.js), but every canvas call is a record:
//  - a sprite canvas (<= 60x60) is identified by the hash of its own draw calls, not by creation order (a new lounge sprite must not renumber the others)
//  - every fillRect / drawImage carries the fillStyle and globalAlpha in force
//  - the lounge block is found from its floor rect (#2f5a46 on the background canvas: x = b.x+2, w = b.w-4, y = top+2), region = block column of that row
//    (x in [b.x, b.x+b.w+3], y in [row top, row top+ROWH]); calls whose whole rect lies inside it, and DOM labels whose left/top lie inside it, are cut
// usage: node lounge_snap.js <static dir> <fixture prefix> [frames=40]   -> one JSON line
const fs = require('fs'), vm = require('vm'), path = require('path'), crypto = require('crypto');
const [dir, fx, frames = '40'] = process.argv.slice(2);
const WALL = 36, ROWH = 36 + 82 + 16;      // game.js layout constants (no placement numbers change)
const STATE = JSON.parse(fs.readFileSync(fx + '_state.json', 'utf8'));
let NOW = Math.round(STATE.now * 1000) + 5000;
class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(NOW); } static now() { return NOW; } }
const sha = x => crypto.createHash('sha1').update(x).digest('hex');
const recs = [];            // { el, op, a, st, al, img }
let nbig = 0;
function mk(tag) {
  const e = { tag, children: [], style: {}, dataset: {}, className: '', innerHTML: '', textContent: '', offsetWidth: 100, offsetHeight: 20,
    append(...k) { k.forEach(c => this.appendChild(c)); }, appendChild(k) { if (k.parent) k.remove(); k.parent = this; this.children.push(k); return k; },
    remove() { if (this.parent) { this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; } }, addEventListener() {}, querySelector: () => mk('q'), querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0 }), clientWidth: 1600,
    getContext() {
      if (this._c) return this._c;
      this.small = this.width > 0 && this.width <= 60 && this.height <= 60;
      if (!this.small) this.bigIdx = nbig++;
      let st = '', al = 1; const el = this, push = (op, a, img) => recs.push({ el, op, a, st, al, img });
      this._c = { set fillStyle(v) { st = v; }, get fillStyle() { return st; }, set globalAlpha(v) { al = v; }, get globalAlpha() { return al; }, imageSmoothingEnabled: false,
        fillRect: (...a) => push('r', a), clearRect: (...a) => push('c', a), drawImage: (img, ...a) => push('i', a, img) };
      return this._c;
    } };
  return e;
}
let raf = null;
const ctx = { console, Date: FDate, Math, JSON, Map, Set, Object, Array, String, Number, Promise, innerWidth: 1600, innerHeight: 900, scrollX: 0, scrollY: 0,
  document: { createElement: mk, head: mk('head'), body: mk('body'), hidden: false, querySelectorAll: () => [], addEventListener() {} },
  requestAnimationFrame: f => { raf = f; }, addEventListener() {}, setTimeout: () => 0, clearTimeout() {},
  IntersectionObserver: class { observe() {} }, ResizeObserver: class { observe() {} } };
ctx.window = ctx; vm.createContext(ctx);
// NO_LOGO=1: empty the monitor symbols after game-art.js (game.js keeps the same LOGO object), so two statics that differ only in the logos hash equal
const I18B = require('./i18n_boot');     // i18n.js first (only when this static dir has it), then its dictionaries from disk (AB_LANG, default ko)
for (const f of I18B.scripts(dir, ['game-art.js', 'game.js', 'game-demo.js'])) {
  vm.runInContext(fs.readFileSync(path.join(dir, f), 'utf8'), ctx);
  if (f === 'i18n.js') I18B.boot(ctx, dir);
  if (f === 'game-art.js' && process.env.NO_LOGO) vm.runInContext('Object.values(AgentGameArt.LOGO).forEach(l => { l.map = []; })', ctx);
}
const box = mk('div'); box.clientWidth = 1600;
const g = ctx.AgentGame.mount(box, { mode: 'strip' });
g.update(STATE, null);
for (let i = 1; i <= +frames; i++) { NOW += 50; raf(i * 50); }

// content id of a sprite canvas = hash of its own calls
const own = new Map();
recs.forEach(r => { if (r.el.small) { if (!own.has(r.el)) own.set(r.el, []); own.get(r.el).push([r.op, r.st, r.al, r.a.join(',')].join('|')); } });
const idOf = el => el.small ? 'S' + sha((own.get(el) || []).join('\n')).slice(0, 12) : 'B' + el.bigIdx;
// lounge floor rect
const fl = recs.find(r => !r.el.small && r.op === 'r' && r.st === '#2f5a46');
if (!fl) { console.log(JSON.stringify({ error: 'lounge floor rect (#2f5a46) not found' })); process.exit(2); }
const bx = fl.a[0] - 2, bw = fl.a[2] + 4, y0 = fl.a[1] - 2 - WALL;
const X0 = bx, X1 = bx + bw + 3, Y0 = y0, Y1 = y0 + ROWH;
const inside = (x, y, w, h) => x >= X0 && x + w <= X1 && y >= Y0 && y + h <= Y1;
const out = [], inn = [];
recs.forEach(r => {
  if (r.el.small) return;                                   // sprite internals are folded into the sprite id
  const id = idOf(r.el);
  let line, ok = false;
  if (r.op === 'r') { line = [id, 'r', r.st, r.al, r.a.join(',')].join('|'); ok = inside(...r.a); }
  else if (r.op === 'c') { line = [id, 'c', r.a.join(',')].join('|'); }
  else {
    const im = r.img, a = r.a, d = a.length >= 8 ? a.slice(4, 8) : [a[0], a[1], im.width, im.height];
    line = [id, 'i', idOf(im), r.al, a.join(',')].join('|'); ok = inside(...d);
  }
  (ok ? inn : out).push(line);
});
// DOM labels: (className, left, top, width, title, html), cut when the anchor lies in the lounge region
const cv = recs.find(r => r.el.bigIdx === 0).el, scale = parseFloat(cv.style.width) / cv.width;
const lab = [], labIn = [];
(function walk(e) { for (const k of e.children || []) { if (k.tag === 'div') {
  const l = parseFloat(k.style.left), t = parseFloat(k.style.top), row = [k.className, k.style.left, k.style.top, k.style.width || k.style.maxWidth || '', k.title || '', k.innerHTML || k.textContent].join('|');
  (l / scale >= X0 && l / scale <= X1 && t / scale >= Y0 && t / scale <= Y1 ? labIn : lab).push(row); } walk(k); } })(box);
console.log(JSON.stringify({ lounge: { x0: X0, x1: X1, y0: Y0, y1: Y1 }, scale, calls_outside: out.length, outside_sha1: sha(out.join('\n')), calls_lounge: inn.length, lounge_sha1: sha(inn.join('\n')),
  labels_outside: lab.length, labels_outside_sha1: sha(lab.join('\n')), labels_lounge: labIn.length, labels_lounge_sha1: sha(labIn.join('\n')) }));
