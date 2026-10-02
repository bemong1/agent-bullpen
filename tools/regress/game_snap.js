// Deterministic draw log of the office (static/game.js, or game-art.js + game.js + game-demo.js once split; i18n.js first when present, language AB_LANG, default ko): canvas calls recorded over N frames for a saved state. No npm.
// Demo scenarios are not played here (setTimeout is stubbed); demo_snap.js replays them.
// usage: node game_snap.js <static dir> <fixture prefix> [frames]
const fs = require('fs'), vm = require('vm'), path = require('path'), crypto = require('crypto');
const [dir, fx, frames = '60'] = process.argv.slice(2);
const STATE = JSON.parse(fs.readFileSync(fx + '_state.json', 'utf8'));
let NOW = Math.round(STATE.now * 1000) + 5000;
class FDate extends Date { constructor(...a) { a.length ? super(...a) : super(NOW); } static now() { return NOW; } }
const log = [];
function ctx2d(id) { const c = { set fillStyle(v) { log.push(id + 'f' + v); }, get fillStyle() { return ''; }, globalAlpha: 1, imageSmoothingEnabled: false,
  fillRect: (...a) => log.push(id + 'r' + a.join(',')), clearRect: (...a) => log.push(id + 'c' + a.join(',')), drawImage: (img, ...a) => log.push(id + 'i' + (img._id || '?') + ':' + a.join(',')) }; return c; }
let nid = 0;
function mk(tag) { const e = { tag, _id: tag + (nid++), children: [], style: {}, dataset: {}, className: '', innerHTML: '', textContent: '', offsetWidth: 100, offsetHeight: 20,
  append(...k) { this.children.push(...k); }, appendChild(k) { this.children.push(k); }, remove() {}, addEventListener() {}, querySelector: () => mk('q'), querySelectorAll: () => [],
  getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0 }), clientWidth: 1600, getContext() { return this._c || (this._c = ctx2d(this._id)); } }; return e; }
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
}   // works both before the split (one game.js) and after (art -> game -> demo)
const box = mk('div'); box.clientWidth = 1600;
const g = ctx.AgentGame.mount(box, { mode: 'strip' });
g.update(STATE, null);
for (let i = 1; i <= +frames; i++) { NOW += 50; raf(i * 50); }
const dom = [];
(function walk(e) { for (const k of e.children || []) { if (k.tag === 'div') dom.push([k.className, k.style.left, k.style.top, k.style.width || k.style.maxWidth || '', k.title || '', k.innerHTML || k.textContent].join('|')); walk(k); } })(box);
const hash = x => crypto.createHash('sha1').update(x).digest('hex');
console.log(JSON.stringify({ calls: log.length, draw_sha1: hash(log.join('\n')), labels: dom.length, labels_sha1: hash(dom.join('\n')) }));
