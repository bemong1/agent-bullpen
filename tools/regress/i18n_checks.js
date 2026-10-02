// Checks of static/i18n.js (language order, lookup, plurals, dates, links, the picker) and of the shipped dictionaries' shape. No browser, no npm:
// the script runs in bare vm contexts, so every test chooses which of navigator / location / localStorage / fetch / document exist.
// usage: node i18n_checks.js [static dir]      (default: ../../static; exit code = number of failed checks)
const fs = require('fs'), vm = require('vm'), path = require('path');
const dir = process.argv[2] || path.join(__dirname, '..', '..', 'static');
const SRC = fs.readFileSync(path.join(dir, 'i18n.js'), 'utf8');
const pack = c => JSON.parse(fs.readFileSync(path.join(dir, 'locales', c + '.json'), 'utf8'));
const PACKS = { en: pack('en'), ko: pack('ko') }, LANGS = [{ code: 'en', name: 'English' }, { code: 'ko', name: '한국어' }];
const sleep = ms => new Promise(r => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)))); };
const eq = (name, got, want) => check(name, JSON.stringify(got) === JSON.stringify(want), 'got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));

// a fake browser with only what the test names; `g` = extra globals (location, navigator, localStorage, fetch, document ...). Returns { run, ctx }
function world(g = {}) {
  const ctx = { console };
  Object.defineProperties(ctx, Object.getOwnPropertyDescriptors(g));      // keeps getters lazy (a localStorage that throws when touched)
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return { ctx, run: code => vm.runInContext(code, ctx) };
}
const store = (init = {}) => ({ _m: Object.assign({}, init), getItem(k) { return this._m[k] ?? null; }, setItem(k, v) { this._m[k] = String(v); } });
const diskFetch = (log = [], opt = {}) => async u => {       // what the server answers: api/i18n and locales/<code>.json
  u = String(u); log.push(u);
  const m = /^locales\/([^/]+)\.json$/.exec(u);
  const ok = body => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) }), no = s => ({ ok: false, status: s, json: async () => ({}) });
  if (u === 'api/i18n') return opt.noList ? no(500) : ok({ default: 'en', languages: LANGS });
  if (m && PACKS[m[1]] && !(opt.fail || []).includes(m[1])) return ok(PACKS[m[1]]);
  return no(404);
};
const doc = () => { const cls = new Set(['i18n-wait']); return { documentElement: { lang: '', classList: { remove: c => cls.delete(c), contains: c => cls.has(c) } }, createElement: tag => ({ tag }), querySelectorAll: () => [] }; };

(async () => {
  const SUP = ['en', 'ko'];
  // ---------- language order ----------
  const W0 = world();
  const P = o => { W0.ctx.__o = o; return W0.run('I18N.pick(__o)'); };
  eq('order: ?lang= beats the stored choice, the browser and the default', P({ search: '?lang=en', stored: 'ko', navLangs: ['ko-KR'], supported: SUP }), 'en');
  eq('order: the stored choice beats the browser', P({ search: '', stored: 'ko', navLangs: ['en-US'], supported: SUP }), 'ko');
  eq('order: no address, no stored choice: the first supported browser language', P({ search: '', stored: null, navLangs: ['fr-FR', 'ko-KR', 'en'], supported: SUP }), 'ko');
  eq('order: a regional tag is reduced to its base language (en-GB -> en, ko-KR -> ko)', [P({ navLangs: ['en-GB'], supported: SUP }), P({ navLangs: ['ko-KR'], supported: SUP })], ['en', 'ko']);
  eq('order: case and underscores do not matter (KO_kr, EN)', [P({ navLangs: ['KO_kr'], supported: SUP }), P({ search: '?lang=EN', supported: SUP })], ['ko', 'en']);
  eq('order: nothing supported anywhere -> English', P({ search: '?lang=fr', stored: 'de', navLangs: ['ja-JP', 'zh'], supported: SUP }), 'en');
  eq('order: an unsupported ?lang= falls through to the stored choice', P({ search: '?lang=xx', stored: 'ko', navLangs: ['en'], supported: SUP }), 'ko');
  eq('order: an unsupported stored choice falls through to the browser', P({ search: '', stored: 'xx', navLangs: ['ko'], supported: SUP }), 'ko');
  eq('order: empty or odd ?lang= is ignored; other query keys do not matter', [P({ search: '?lang=&session=a&demo=1', stored: 'ko', supported: SUP }), P({ search: '?session=a&lang=ko#agent=1', supported: SUP }), P({ search: '?lang=%E0%A4%A', stored: 'ko', supported: SUP })], ['ko', 'ko', 'ko']);
  eq('order: a language that the server registered later is matched exactly before its base (zh-TW over zh)', P({ navLangs: ['zh-TW'], supported: ['en', 'zh', 'zh-TW'] }), 'zh-TW');
  eq('order: without English in the list the first registered language is the default', P({ supported: ['ko', 'ja'] }), 'ko');

  // ---------- the environment may be missing or hostile ----------
  let w = world();
  eq('nothing but the script (no navigator, location, localStorage, fetch, document): init({packs}) works', w.run(`I18N.init({ lang: 'ko', packs: ${JSON.stringify(PACKS)} }); [I18N.lang, t('status.running'), I18N.ok]`), ['ko', '작업 중', true]);
  w = world({ localStorage: { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('QuotaExceededError'); } }, location: { search: '', pathname: '/', hash: '' }, navigator: { languages: ['ko-KR'] }, fetch: diskFetch(), document: doc() });
  eq('storage that throws on read is ignored (the browser language decides)', await w.run('I18N.init()'), 'ko');
  w = world({ get localStorage() { throw new Error('access denied'); }, navigator: { languages: ['ko'] }, fetch: diskFetch() });
  eq('a localStorage property that throws on access, no location: still no exception', await w.run('I18N.init()'), 'ko');
  w = world({ fetch: diskFetch() });
  eq('no navigator, no location, no localStorage: the default language over the fetch path', await w.run('I18N.init()'), 'en');
  const log = []; w = world({ location: { search: '?lang=ko' }, navigator: { languages: ['en'] }, localStorage: store({ 'ab.lang': '"en"' }), fetch: diskFetch(log), document: doc() });
  eq('address wins over the stored choice and the browser, end to end (fetch path)', await w.run('I18N.init()'), 'ko');
  eq('fetch path: api/i18n first, then the chosen language and English', log, ['api/i18n', 'locales/ko.json', 'locales/en.json']);
  eq('after init: html lang set, the wait class removed', [w.ctx.document.documentElement.lang, w.ctx.document.documentElement.classList.contains('i18n-wait')], ['ko', false]);
  w = world({ localStorage: store({ 'ab.lang': '"ko"' }), navigator: { languages: ['en'] }, fetch: diskFetch() });
  eq('the stored choice (JSON string, as board.js store writes it) wins over the browser', await w.run('I18N.init()'), 'ko');
  w = world({ localStorage: store({ 'ab.lang': 'ko' }), fetch: diskFetch() });
  eq('a stored choice written as a bare string is accepted too', await w.run('I18N.init()'), 'ko');

  // ---------- init: lazy start, failures, idempotence ----------
  const l2 = []; w = world({ fetch: diskFetch(l2), navigator: { languages: ['ko'] } });
  eq('ready starts init by itself, once', [await w.run('I18N.ready'), await w.run('I18N.ready'), await w.run('I18N.init()'), l2.length], ['ko', 'ko', 'ko', 3]);
  w = world({ fetch: diskFetch([], { noList: true }), navigator: { languages: ['ko'] } });
  eq('api/i18n down: English (the list is needed to know which languages exist)', [await w.run('I18N.ready'), w.run("t('status.done')")], ['en', 'Done']);
  w = world({ fetch: diskFetch([], { fail: ['ko'] }), navigator: { languages: ['ko'] } });
  eq('the chosen language file fails: English, not [key]', [await w.run('I18N.ready'), w.run("t('status.done')")], ['en', 'Done']);
  w = world({ fetch: diskFetch([], { fail: ['ko', 'en'] }), navigator: { languages: ['ko'] }, document: doc() });
  eq('every file fails: ready still resolves (never rejects), ok is false, t gives [key], the page is revealed', [await w.run('I18N.ready'), w.run('I18N.ok'), w.run("t('status.done')"), w.ctx.document.documentElement.classList.contains('i18n-wait')], ['en', false, '[status.done]', false]);
  w = world();
  eq('no fetch at all and no packs: ready resolves, t gives [key]', [await w.run('I18N.ready'), w.run("t('common.you')")], ['en', '[common.you]']);
  w = world({ fetch: () => new Promise(() => {}), setTimeout: (f, ms) => { w.timer = { f, ms }; return 1; }, clearTimeout() {}, AbortController: class { constructor() { this.signal = {}; } abort() { w.aborted = (w.aborted || 0) + 1; } } });
  const slow = w.run('I18N.init()'); await sleep(5);
  check('a server that never answers: a time limit is set on each request (8 s)', w.timer && w.timer.ms === 8000, JSON.stringify(w.timer && w.timer.ms));

  // ---------- t(): parameters, plurals, fallback, formatters ----------
  const T = (lang, packs, code) => { const x = world(); x.ctx.__p = { lang, packs }; x.run('I18N.init(__p)'); return x.run(code); };
  eq('t: {name} is replaced; a name without a value stays visible', T('en', PACKS, "[t('time.ago.hourMin', { h: 3, m: 12 }), t('time.ago.hourMin', { h: 3 })]"), ['3h 12m ago', '3h {m}m ago']);
  eq('t: English plural one/other (0, 1, 2, 1.5, no count)', T('en', PACKS, "[0, 1, 2, 1.5, undefined].map(n => t('unit.agent', { count: n }))"), ['0 agents', '1 agent', '2 agents', '1.5 agents', '{count} agents']);
  eq('t: Korean has the other form only (the same count gives the one form)', T('ko', PACKS, "[1, 2].map(n => t('unit.agent', { count: n }))"), ['1명', '2명']);
  const ko = JSON.parse(JSON.stringify(PACKS.ko)); delete ko.messages['unit.agent'];
  eq('t: a key missing in the chosen language uses the English text and English plural rules', T('ko', { en: PACKS.en, ko }, "[1, 3].map(n => t('unit.agent', { count: n }))"), ['1 agent', '3 agents']);
  eq('t: a key in no language gives [key] and is recorded in I18N.missing', T('ko', PACKS, "[t('no.such.key'), [...I18N.missing]]"), ['[no.such.key]', ['no.such.key']]);
  eq('t: has() tells a real key from a missing one', T('ko', PACKS, "[I18N.has('status.done'), I18N.has('nope')]"), [true, false]);
  const sub = { en: { meta: { code: 'en', locale: 'en-US' }, messages: { x: '{name:subject} read', n: '{v:number} calls', p: { one: '{count} x', few: '{count} y', other: '{count} z' }, tail: 'a {name} b' } } };
  eq('subject formatter: 이/가 by the last letter (받침, digit 0136 78, l m n r)', T('en', sub, "['서버', '토큰', 'T1-A', 'T1', 'T3', 'T8', 'orch-l', 'x-r', 'x-q', '', '값'].map(v => t('x', { name: v }))"),
    ['서버가 read', '토큰이 read', 'T1-A가 read', 'T1이 read', 'T3이 read', 'T8이 read', 'orch-l이 read', 'x-r이 read', 'x-q가 read', '가 read', '값이 read']);
  eq('number formatter groups digits by the language', T('en', sub, "[t('n', { v: 1234567 }), t('n', { v: 12 })]"), ['1,234,567 calls', '12 calls']);
  const ru = { meta: { code: 'ru', locale: 'ru-RU' }, messages: sub.en.messages };
  eq('plural categories come from Intl.PluralRules of the pack\'s language (ru: one/few/many); a category the pack lacks falls to other', T('ru', { ru, en: sub.en }, "[1, 2, 5, 21, 22].map(n => t('p', { count: n }))"), ['1 x', '2 y', '5 z', '21 x', '22 y']);
  eq('values with $ or braces are put in as they are', T('en', sub, "t('tail', { name: '$& {name} $1' })"), 'a $& {name} $1 b');

  // ---------- dates and numbers ----------
  const stamps = [1790800000, 1790803200 - 60, 1790803200 + 5 * 60 + 9, 1790836800, 1790886399, 0, 1767225600];     // incl. a midnight and 23:59:59 in Seoul
  const DATE = { time: ['toLocaleTimeString', { hour: '2-digit', minute: '2-digit', hour12: false }], timeSec: ['toLocaleTimeString', { hour12: false }], dateTime: ['toLocaleString', undefined] };
  const oldTZ = process.env.TZ;
  for (const tz of ['Asia/Seoul', 'America/New_York', 'UTC']) {
    process.env.TZ = tz;
    const got = T('ko', PACKS, `[${JSON.stringify(stamps)}.map(s => ['time', 'timeSec', 'dateTime'].map(p => I18N.date(s, p)))]`)[0];
    const want = stamps.map(s => Object.keys(DATE).map(p => new Date(s * 1000)[DATE[p][0]]('ko-KR', DATE[p][1])));
    eq('date presets (ko) equal the calls the pages make today: toLocaleTimeString/toLocaleString(\'ko-KR\'), TZ ' + tz, got, want);
  }
  process.env.TZ = 'Asia/Seoul';
  const dd = new Date(1790800000 * 1000), EN = PACKS.en.formats, fmtEn = f => new Intl.DateTimeFormat('en-US', f).format(dd);
  eq('date accepts a Date as well as seconds; an unknown preset gives the language\'s plain date', T('en', PACKS, "[I18N.date(new Date(1790800000 * 1000), 'time'), I18N.date(1790800000, 'nope')]"), [fmtEn(EN.time), fmtEn(undefined)]);
  eq('English presets are the en.json formats (en-US)', T('en', PACKS, "[I18N.date(1790800000, 'dateTime'), I18N.date(1790800000, 'monthShort')]"), [fmtEn(EN.dateTime), 'Oct']);
  eq('English presets use a 24-hour cycle that starts at 00 (not 24:05 at midnight), seconds and date-time too', T('en', PACKS, "[1790953509, 1790953509 + 43200].map(s => [I18N.date(s, 'time'), I18N.date(s, 'timeSec'), I18N.date(s, 'dateTime')])"), [['00:05', '00:05:09', 'Oct 3, 2026, 00:05:09'], ['12:05', '12:05:09', 'Oct 3, 2026, 12:05:09']]);
  eq('num: grouping by language', [T('en', PACKS, 'I18N.num(1234567.5)'), T('ko', PACKS, 'I18N.num(1234567)')], ['1,234,567.5', '1,234,567']);
  process.env.TZ = oldTZ === undefined ? '' : oldTZ; if (oldTZ === undefined) delete process.env.TZ;

  // ---------- links and the picker ----------
  const U = (h, c) => { W0.ctx.__h = [h, c]; return W0.run('I18N.url(...__h)'); };
  eq('url: ?lang= is set; session, demo and the hash are kept; no duplicate', [U('game?session=a#h', 'ko'), U('./?lang=en&demo=basic', 'ko'), U('?lang=en', 'ko'), U('./', 'en'), U('x?a=1&lang=ko&b=2#agent=z', 'en')],
    ['game?session=a&lang=ko#h', './?demo=basic&lang=ko', '?lang=ko', './?lang=en', 'x?a=1&b=2&lang=en#agent=z']);
  w = world({ location: { search: '?session=s&lang=en', pathname: '/', hash: '' } });
  eq('link: follows an explicit ?lang= of this page', w.run("I18N.link('game?session=s')"), 'game?session=s&lang=en');
  w = world({ location: { search: '?session=s', pathname: '/', hash: '' } });
  eq('link: no ?lang= on this page, the href is left alone (the stored choice carries the language)', w.run("I18N.link('game?session=s')"), 'game?session=s');
  w = world();
  eq('link: no location at all', w.run("I18N.link('game')"), 'game');

  const go = []; const ls = store();
  w = world({ location: { search: '?session=s&demo=basic&lang=en', pathname: '/game', hash: '#agent=a1' }, localStorage: ls });
  w.ctx.__p = { lang: 'en', packs: PACKS, langs: LANGS }; w.ctx.__go = go; w.run('I18N.init(__p); I18N._go = u => __go.push(u)');
  eq('choose: stores only the user\'s choice (JSON string), keeps session, demo and the hash, replaces ?lang=', [w.run("I18N.choose('ko')"), ls._m['ab.lang'], go], ['/game?session=s&demo=basic&lang=ko#agent=a1', '"ko"', ['/game?session=s&demo=basic&lang=ko#agent=a1']]);
  go.length = 0;
  eq('choose: an unsupported language is refused: nothing stored, nowhere to go', [w.run("I18N.choose('xx')"), go.length, Object.keys(ls._m).length], [null, 0, 1]);
  const ls2 = { getItem() { return null; }, setItem() { throw new Error('QuotaExceededError'); } };
  w = world({ location: { search: '', pathname: '/', hash: '' }, localStorage: ls2 }); w.ctx.__p = { lang: 'en', packs: PACKS, langs: LANGS }; w.ctx.__go = []; w.run('I18N.init(__p); I18N._go = u => __go.push(u)');
  eq('choose: storage that throws on write still switches (the address carries ?lang=)', [w.run("I18N.choose('ko')"), w.ctx.__go], ['/?lang=ko', ['/?lang=ko']]);
  const asg = []; w = world({ location: { search: '', pathname: '/', hash: '', assign: u => asg.push(u) } }); w.ctx.__p = { lang: 'en', packs: PACKS, langs: LANGS }; w.run('I18N.init(__p)'); w.run("I18N.choose('ko')");
  eq('choose: the default navigation is location.assign', asg, ['/?lang=ko']);

  const mk = (hidden = true) => ({ tag: 'select', hidden, children: [], set innerHTML(v) { this.children = []; }, appendChild(o) { this.children.push(o); }, setAttribute(k, v) { this[k] = v; } });   // innerHTML = '' empties it, like the DOM
  const d2 = { createElement: tag => ({ tag }), querySelectorAll: () => [], documentElement: { classList: { remove() {} } } };
  w = world({ document: d2 }); w.ctx.__p = { lang: 'ko', packs: PACKS, langs: LANGS }; w.run('I18N.init(__p)');
  const sel = mk(); w.ctx.__s = sel; w.run('I18N.mountPicker(__s)');
  eq('picker: one option per language, the languages\' own names, the current one selected, named by common.language; the HTML\'s hidden attribute is left alone (it stays hidden)', [sel.children.map(o => o.value + ':' + o.textContent), sel.value, sel.hidden, sel.title, sel['aria-label'], typeof sel.onchange], [['en:English', 'ko:한국어'], 'ko', true, '언어', '언어', 'function']);
  const sel1 = mk(false); w.ctx.__s = sel1; w.run('I18N.mountPicker(__s)');
  eq('picker: a selector the HTML shows stays shown (the page decides, not the script)', sel1.hidden, false);
  const sel2 = mk(); w.ctx.__s = sel2; w.run('I18N.mountPicker(__s); I18N.mountPicker(__s)');
  eq('picker: mounting twice does not duplicate the options', sel2.children.length, 2);
  w = world({ document: d2 }); w.ctx.__p = { lang: 'en', packs: { en: PACKS.en }, langs: [LANGS[0]] }; w.run('I18N.init(__p)'); const sel3 = mk(false); w.ctx.__s = sel3; w.run('I18N.mountPicker(__s)');
  eq('picker: a single language hides the selector', [sel3.hidden, sel3.children.length], [true, 0]);
  w = world(); w.ctx.__p = { lang: 'en', packs: PACKS }; w.run('I18N.init(__p)');
  eq('picker: no document or no element: nothing happens', [w.run('I18N.mountPicker(null)'), w.run('I18N.mountPicker({})')], [undefined, undefined]);

  const nodes = [{ a: { 'data-i18n': 'common.close' } }, { a: { 'data-i18n-title': 'common.language' } }].map(o => Object.assign({ textContent: '', attrs: {}, getAttribute(k) { return this.a[k]; }, setAttribute(k, v) { this.attrs[k] = v; } }, o));
  const root = { querySelectorAll: s => s === '[data-i18n]' ? [nodes[0]] : s === '[data-i18n-title]' ? [nodes[1]] : [] };
  w = world(); w.ctx.__p = { lang: 'ko', packs: PACKS }; w.ctx.__r = root; w.run('I18N.init(__p); I18N.apply(__r)');
  eq('apply: [data-i18n] sets the text, [data-i18n-title] the attribute', [nodes[0].textContent, nodes[1].attrs.title], ['닫기', '언어']);

  // ---------- the shipped dictionaries ----------
  for (const c of ['en', 'ko']) {
    const p = PACKS[c], ok = (() => { try { new Intl.Locale(p.meta.locale); Object.values(p.formats).forEach(f => new Intl.DateTimeFormat(p.meta.locale, f)); return true; } catch (e) { return e.message; } })();
    check(c + '.json: meta (code = file name, name, locale), the four parts, locale and formats usable by Intl', p.meta.code === c && p.meta.name && ok === true && ['meta', 'messages', 'formats', 'limits'].every(k => k in p) && Object.keys(p).join() === 'meta,messages,formats,limits', String(ok));
    check(c + '.json: every plural value has "other"', Object.entries(p.messages).every(([k, v]) => typeof v === 'string' || (v && typeof v.other === 'string')), 'no other');
  }
  const p0 = p => Object.keys(p.messages).filter(k => /^(common|status|kind|time|unit)\./.test(k) || k.endsWith('._'));     // the common sections only (the full comparison is i18n_check.py)
  eq('en.json and ko.json have the same keys in the same order in the common sections', p0(PACKS.ko), p0(PACKS.en));

  console.log(fails ? 'FAILED ' + fails : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log('HARNESS ERROR', e); process.exit(2); });
