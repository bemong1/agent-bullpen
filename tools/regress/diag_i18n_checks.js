// Checks of the first-screen diagnostic card (static/common.js): the card's HTML in Korean is byte-identical to the reference statics' card,
// and in every language it has no [key] placeholders; in English no Hangul at all. No npm.
//   usage: node diag_i18n_checks.js <static dir> [<reference static dir>]        (exit code = number of failed checks)
// The reference dir (default /tmp/ab-i18n-base/static) has the pre-i18n common.js with the Korean texts written in the code.
// Without it (a CI checkout has no reference copy) the Korean-identical checks are skipped with a note; the checks of the new statics themselves still run.
const fs = require('fs'), vm = require('vm'), path = require('path');
const I18B = require('./i18n_boot');
const [dir, ref = '/tmp/ab-i18n-base/static'] = process.argv.slice(2);
if (!dir) { console.error('usage: node diag_i18n_checks.js <static dir> [<reference static dir>]'); process.exit(2); }
let fails = 0, passes = 0;
const haveRef = fs.existsSync(path.join(ref, 'common.js'));
if (!haveRef) console.log('note: no common.js in ' + ref + ' - the checks against the reference statics are skipped');
const check = (name, ok, detail) => { if (ok) passes++; else { fails++; console.log('FAIL ' + name + '  ' + (detail || '')); } };
const HANGUL = /[ㄱ-ㆎ가-힣]/;                     // i18n-ok: what the check looks for

// one fake browser per (dir, language, location.host): the page's own scripts in the order the pages load them
function load(d, lang, host) {
  const ctx = { console, Date, Math, JSON, Promise, Set, Map, Object, Array, String, Number, Intl, Error, AbortController, encodeURIComponent, decodeURIComponent,
    location: host === undefined ? { search: '' } : { search: '', host }, localStorage: { getItem: () => null, setItem() {} } };
  ctx.window = ctx; vm.createContext(ctx);
  for (const n of I18B.scripts(d, ['common.js'])) { vm.runInContext(fs.readFileSync(path.join(d, n), 'utf8'), ctx, { filename: n }); if (n === 'i18n.js') I18B.boot(ctx, d, lang); }
  return ctx;
}
const call = (ctx, expr) => { ctx.__a = expr; return vm.runInContext('diagHtml(...__a)', ctx); };

// the answers the card is drawn from: /api/sessions' sources, the failed request, the session the address asked for
const src = (provider, o) => Object.assign({ provider, dir: '/home/u/.' + (provider === 'claude' ? 'claude' : 'codex'), from: 'default', exists: true, sessions: 3 }, o);
const SOURCES = [
  [],
  [src('claude', { exists: false, sessions: 0 }), src('codex', { exists: false, sessions: 0 })],
  [src('claude', { sessions: 0 }), src('codex', { sessions: 0, from: 'flag' })],
  [src('claude', { sessions: 7, agent_sessions: 3, from: 'env' }), src('codex', { sessions: 1, agent_sessions: 0, from: 'flag' })],
  [src('claude', { dir: '/x/projects', sessions: 2 }), src('codex', { dir: '/x/sessions/', sessions: 12, agent_sessions: 4 })],
  [src('claude', { dir: '<img src=x>&"', sessions: 1 }), src('other', { dir: '/o', sessions: 0 })],
];
const CASES = [];
for (const sources of SOURCES) {
  CASES.push(['empty', { sess: { sources } }]);
  CASES.push(['missing', { sess: { sources, default: true }, id: 'deadbeef-0000' }]);
  CASES.push(['missing', { sess: { sources }, id: '<b>x</b>' }]);
  CASES.push(['missing', { sess: { sources, default: true } }]);
}
CASES.push(['empty', {}], ['missing', { id: 'abc' }]);
CASES.push(['down', {}], ['down', { timeout: true }], ['http', { status: 403 }], ['http', { status: 403, detail: 'ERR-403 plain\nsecond' }], ['http', { status: 500 }], ['http', { status: 500, detail: '<i>boom</i>' }]);

const HOSTS = ['127.0.0.1:8790', undefined, ''];
let compared = 0;
for (const host of HOSTS) {
  const base = haveRef ? load(ref, 'ko', host) : null, ko = load(dir, 'ko', host), en = load(dir, 'en', host);
  for (const [kind, d] of CASES) {
    const name = `${kind} ${JSON.stringify(d).slice(0, 90)} host=${host}`;
    const got = call(ko, [kind, d]), eng = call(en, [kind, d]);
    compared++;
    // a 403 body with a line per language (English first, then Korean): the reference showed both lines, the screen now shows the line of its own language (ko: the last, en: the first)
    const lines = kind === 'http' && d.status === 403 && /\n/.test(d.detail || '') ? d.detail.split('\n') : null;
    if (haveRef) { const want = call(base, [kind, lines ? Object.assign({}, d, { detail: lines[lines.length - 1] }) : d]); check('ko identical to the reference: ' + name, got === want, '\n   ref ' + want + '\n   new ' + got); }
    if (lines) { check('403 with a line per language: ko shows only the last line: ' + name, got.includes(lines[1]) && !got.includes(lines[0]), got); check('403 with a line per language: en shows only the first line: ' + name, eng.includes(lines[0]) && !eng.includes(lines[1]), eng); }
    check('ko has no [key]: ' + name, !/\[[a-z]+\.[A-Za-z.]+\]/.test(got), got);
    check('en has no Hangul: ' + name, !HANGUL.test(eng), eng);
    check('en has no [key]: ' + name, !/\[[a-z]+\.[A-Za-z.]+\]/.test(eng), eng);
    check('en differs from ko (it is translated): ' + name, eng !== got);
    check('en has no unfilled {name}: ' + name, !/\{[A-Za-z_]\w*\}/.test(eng), eng);
  }
}
// the header labels and the "every 5 s" note that makeDiag passes to status(kind, label, note)
{
  const ref0 = haveRef ? load(ref, 'ko', 'h') : null, ko = load(dir, 'ko', 'h'), en = load(dir, 'en', 'h');
  const REF = { empty: '세션 기다리는 중', missing: '세션을 열지 못함', down: '서버 응답 없음', http: '서버 오류' };    // i18n-ok: the reference texts
  const refLabel = k => vm.runInContext('DIAG_LABEL', ref0)[k];
  for (const k of Object.keys(REF)) {
    if (haveRef) check('status label ' + k + ' in the reference statics is what this check expects', refLabel(k) === REF[k], refLabel(k));
    check('status label ' + k + ' ko identical', vm.runInContext(`t('diag.label.${k}')`, ko) === REF[k], vm.runInContext(`t('diag.label.${k}')`, ko));
    const e = vm.runInContext(`t('diag.label.${k}')`, en);
    check('status label ' + k + ' en has no Hangul / [key]', !HANGUL.test(e) && !/^\[/.test(e), e);
  }
  check('status note ko identical to board.js text', vm.runInContext("t('diag.recheck.short')", ko) === '5초마다 확인');   // i18n-ok: the reference text
  check('status note en has no Hangul / [key]', !HANGUL.test(vm.runInContext("t('diag.recheck.short')", en)) && !/^\[/.test(vm.runInContext("t('diag.recheck.short')", en)));
}
// makeDiag draws the card and tells the page's header: status(kind, label, note). A failed first /api/state with a status draws the "server refused" card
for (const [lang, label, note] of [['ko', '서버 오류', '5초마다 확인'], ['en', 'Server error', 'checking every 5 s']]) {      // i18n-ok: the Korean texts of the old header
  const c = load(dir, lang, 'h'), box = { hidden: true, innerHTML: '', classList: { toggle() {} }, timeEl: { textContent: '' }, querySelector(sel) { return sel === '.dg-time' ? this.timeEl : null; } };
  let got = null;
  Object.assign(c, { document: { createElement: () => ({}), head: { appendChild() {} }, body: { classList: { add() {}, remove() {} } } }, setTimeout: () => 1, clearTimeout() {}, __box: box, __cb: (...a) => { got = a; } });
  vm.runInContext("makeDiag(__box, { session: () => '', retry() {}, status: __cb }).fail({ status: 500, detail: 'x' })", c);
  check('makeDiag ' + lang + ': status(kind, label, note)', got && got[0] === 'http' && got[1] === label && got[2] === note, JSON.stringify(got));
  check('makeDiag ' + lang + ': the card is shown with a clock in the footer', box.hidden === false && /\d.*\d/.test(box.timeEl.textContent) && box.innerHTML.includes('dg-time'), box.timeEl.textContent);
}
// the time in the card's footer: the same text as the old toLocaleTimeString('ko-KR', { hour12: false }) at every minute of a day (and the seconds that differ in width)
{
  const ko = load(dir, 'ko', 'h'); let bad = 0;
  for (let m = 0; m < 1440; m += 7) for (const s of [0, 9, 59]) {
    const t0 = new Date(2026, 9, 1, Math.floor(m / 60), m % 60, s); ko.__d = t0;
    const got = vm.runInContext("I18N.date(__d, 'timeSec')", ko), want = t0.toLocaleTimeString('ko-KR', { hour12: false });
    if (got !== want) { bad++; if (bad < 4) console.log('  time differs', t0.toISOString(), JSON.stringify(got), JSON.stringify(want)); }
  }
  check('footer time: I18N.date(..., "timeSec") = toLocaleTimeString("ko-KR", {hour12:false})', bad === 0, bad + ' differ');
}
console.log(`diag_i18n_checks: ${compared} cards x 3 languages-or-hosts compared, ${passes} passed, ${fails} failed`);
process.exit(fails);
