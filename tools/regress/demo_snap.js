// Deterministic replay of the office demo scenarios (the part game_snap.js cannot reach: it stubs setTimeout).
// A virtual clock advances 50 ms per frame; due timers fire, then one animation frame is drawn. Canvas calls are hashed
// frame by frame, DOM labels/bubbles/banner every 10 s and at the end. No npm.
// usage: node demo_snap.js <static dir> <fixture prefix> [scenario: all|basic|new|round2|trouble|talk] [seconds]
// Loads i18n.js (if present; its dictionaries come from <static dir>/locales, language AB_LANG, default ko), game-art.js (if present), game.js, game-demo.js (if present) in order, so it works on the single-file game.js too.
const fs = require('fs'), vm = require('vm'), path = require('path'), crypto = require('crypto');
const [dir, fx, key = 'all', secs = '300'] = process.argv.slice(2);
const STATE = JSON.parse(fs.readFileSync(fx + '_state.json', 'utf8'));
let NOW = Math.round(STATE.now * 1000) + 5000;
class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(NOW); } static now() { return NOW; } }
const hash = crypto.createHash('sha1'), domHash = crypto.createHash('sha1');
let log = [], calls = 0;
function ctx2d(id) { return { set fillStyle(v) { log.push(id + 'f' + v); }, get fillStyle() { return ''; }, globalAlpha: 1, imageSmoothingEnabled: false,
  fillRect: (...a) => log.push(id + 'r' + a.join(',')), clearRect: (...a) => log.push(id + 'c' + a.join(',')), drawImage: (img, ...a) => log.push(id + 'i' + (img._id || '?') + ':' + a.join(',')) }; }
let nid = 0;
function mk(tag) {
  const e = { tag, _id: tag + (nid++), children: [], parent: null, style: {}, dataset: {}, className: '', innerHTML: '', textContent: '', offsetWidth: 100, offsetHeight: 20, _q: {},
    append(...k) { k.forEach(c => this.appendChild(c)); }, appendChild(k) { if (k.parent) k.remove(); k.parent = this; this.children.push(k); return k; },
    remove() { if (this.parent) { this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; } },
    addEventListener() {}, querySelector(s) { return this._q[s] || (this._q[s] = mk('q')); }, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0 }), clientWidth: 1600, getContext() { return this._c || (this._c = ctx2d(this._id)); } };
  return e;
}
let raf = null, timers = [], tid = 0;
const ctx = { console, Date: FDate, Math, JSON, Map, Set, Object, Array, String, Number, Promise, innerWidth: 1600, innerHeight: 900, scrollX: 0, scrollY: 0,
  document: { createElement: mk, head: mk('head'), body: mk('body'), hidden: false, querySelectorAll: () => [], addEventListener() {} },
  requestAnimationFrame: f => { raf = f; }, addEventListener() {},
  setTimeout: (f, ms) => { const t = { id: ++tid, at: NOW + (ms || 0), f }; timers.push(t); return t.id; }, clearTimeout: id => { timers = timers.filter(t => t.id !== id); },
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
const alerts = [], notes = [];
let ends = 0;
g.demo(key, { onAlert: a => alerts.push(a ? a.id : null), onEnd: () => ends++ });
const walk = e => { const out = []; (function w(n) { for (const k of n.children || []) { if (k.tag === 'div') out.push([k.className, k.style.left, k.style.top, k.style.width || k.style.maxWidth || '', k.style.opacity || '', k.title || '', k.innerHTML || k.textContent].join('|')); w(k); } })(e); return out.join('\n'); };
const bannerText = () => { const b = box.children[0] && box.children[0].children.find(c => c.className === 'ag-demo'); return b ? (b._q['.sn'] ? b._q['.sn'].textContent : '') + ' / ' + (b._q['.dt'] ? b._q['.dt'].textContent : '') + ' / nx=' + (b._q['.nx'] ? b._q['.nx'].style.display : '') : '(no banner)'; };
const N = Math.round(+secs * 20);
for (let i = 1; i <= N; i++) {
  NOW += 50;
  const due = timers.filter(t => t.at <= NOW).sort((a, b) => a.at - b.at || a.id - b.id);
  timers = timers.filter(t => t.at > NOW);
  due.forEach(t => t.f());
  raf(i * 50);
  calls += log.length; hash.update(log.join('\n')); log = [];
  if (i % 200 === 0 || i === N) { domHash.update(walk(box)); const bt = bannerText(); notes.push(bt); domHash.update(bt); }
}
console.log(JSON.stringify({ frames: N, calls, draw_sha1: hash.digest('hex'), dom_sha1: domHash.digest('hex'), alerts: alerts.length + ':' + alerts.join(','), ends, demoOnAtEnd: g.demoOn, timersLeft: timers.length, lastBanner: notes[notes.length - 1] }));
