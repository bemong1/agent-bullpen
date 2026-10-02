// Checks of I18N.apply() (static/i18n.js): the English text written in the HTML survives when the dictionary cannot be loaded or lacks a key.
// Before the fix a failed dictionary request turned every static text into its key ([page.brand], [page.legend.read] ...). No browser, no npm:
// the real static/index.html and static/game.html are parsed with a regex into fake elements, and i18n.js runs in a bare vm context.
//  - no dictionary at all (every request fails, in two ways) -> every [data-i18n*] element keeps its HTML text and attributes, and nothing shows a [key]
//  - the dictionary is there but one key is missing in every language -> that element keeps its HTML text, the others are translated
//  - the chosen language lacks a key that English has -> the English text is used (as t() does)
//  - the dictionary is there (en and ko) -> every element of both pages is translated and none shows a [key]
// usage: node i18n_apply_checks.js [static dir]      (default: ../../static; exit code = number of failed checks)
const fs = require('fs'), vm = require('vm'), path = require('path');
const dir = process.argv[2] || path.join(__dirname, '..', '..', 'static');
const SRC = fs.readFileSync(path.join(dir, 'i18n.js'), 'utf8');
const pack = c => JSON.parse(fs.readFileSync(path.join(dir, 'locales', c + '.json'), 'utf8'));
const PACKS = { en: pack('en'), ko: pack('ko') };
const HAN = /[ㄱ-ㆎ가-힣]/, KEYISH = /\[[A-Za-z][\w.]*\]/;
let fails = 0;
const check = (name, ok, detail) => { if (!ok) fails++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)))); };
const eq = (name, got, want) => check(name, JSON.stringify(got) === JSON.stringify(want), 'got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));

// the elements of a page that carry data-i18n*, as fake nodes: { tag, attrs, textContent } (the text is what the HTML has up to the next tag)
const ATTRS = ['title', 'placeholder', 'aria-label'];
function parse(file) {
  const html = fs.readFileSync(path.join(dir, file), 'utf8'), nodes = [];
  for (const m of html.matchAll(/<([a-zA-Z][\w-]*)((?:\s+[^\s>=]+(?:="[^"]*")?)*)\s*>([^<]*)/g)) {
    const attrs = {};
    for (const a of m[2].matchAll(/\s+([^\s>=]+)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] === undefined ? '' : a[2];
    if (!Object.keys(attrs).some(k => k === 'data-i18n' || k.startsWith('data-i18n-'))) continue;
    nodes.push({ file, tag: m[1], attrs, textContent: m[3], html: m[3], initial: { text: m[3], attrs: Object.assign({}, attrs) },
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }, setAttribute(k, v) { this.attrs[k] = String(v); } });
  }
  return nodes;
}
const hasAttr = (n, k) => k in n.attrs;
// a page world: i18n.js plus a document whose querySelectorAll answers the selectors apply() uses; g = the fake browser's other globals (fetch, navigator, location ...)
function world(nodes, g = {}) {
  const cls = new Set(['i18n-wait']);
  const document = { documentElement: { lang: '', classList: { remove: c => cls.delete(c), contains: c => cls.has(c) } }, createElement: tag => ({ tag }),
    querySelectorAll: sel => { const m = /^\[(data-i18n(?:-[a-z-]+)?)\]$/.exec(sel); return m ? nodes.filter(n => hasAttr(n, m[1])) : []; } };
  const ctx = Object.assign({ console, document }, g);
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return { ctx, document, cls, run: code => vm.runInContext(code, ctx) };
}
const LANGS = [{ code: 'en', name: 'English' }, { code: 'ko', name: '한국어' }];
// what the server answers: api/i18n and locales/<code>.json; opt.status = the HTTP status of every answer that fails; opt.reject = fetch itself fails (server down)
const fetcher = (packs, opt = {}) => async u => {
  u = String(u);
  if (opt.reject) throw new TypeError('Failed to fetch');
  const ok = body => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) }), no = s => ({ ok: false, status: s, json: async () => ({}) });
  if (u === 'api/i18n') return opt.noList ? no(opt.status || 500) : ok({ default: 'en', languages: LANGS });
  const m = /^locales\/([^/]+)\.json$/.exec(u);
  return m && packs[m[1]] ? ok(packs[m[1]]) : no(opt.status || 404);
};
const texts = nodes => nodes.map(n => n.textContent);
const unchanged = nodes => nodes.filter(n => n.textContent !== n.initial.text || JSON.stringify(n.attrs) !== JSON.stringify(n.initial.attrs));
const keyish = nodes => nodes.filter(n => KEYISH.test(n.textContent) || Object.values(n.attrs).some(v => KEYISH.test(v)));

(async () => {
  const PAGES = ['index.html', 'game.html'], real = PAGES.flatMap(parse);
  check('the pages have static texts to translate (data-i18n elements and attributes)', real.length >= 20 && real.some(n => n.attrs['data-i18n']) && real.some(n => ATTRS.some(a => hasAttr(n, 'data-i18n-' + a))), real.length + ' elements');
  check('every HTML text written beside a data-i18n key is English: no Hangul, not empty, not a [key]', real.filter(n => n.attrs['data-i18n']).every(n => n.initial.text.trim() && !HAN.test(n.initial.text) && !KEYISH.test(n.initial.text)),
    real.filter(n => n.attrs['data-i18n'] && (!n.initial.text.trim() || HAN.test(n.initial.text))).map(n => n.file + ' ' + n.attrs['data-i18n']));
  check('every title / placeholder / aria-label written beside a data-i18n-* key is English and not a [key]', real.every(n => ATTRS.every(a => !hasAttr(n, 'data-i18n-' + a) || (n.initial.attrs[a] && !HAN.test(n.initial.attrs[a]) && !KEYISH.test(n.initial.attrs[a])))),
    real.filter(n => ATTRS.some(a => hasAttr(n, 'data-i18n-' + a) && (!n.initial.attrs[a] || HAN.test(n.initial.attrs[a])))).map(n => n.file + ' ' + JSON.stringify(n.attrs)));

  // ---------- the dictionary cannot be loaded: the HTML text stays ----------
  const failures = [['every request answers 404', { status: 404, noList: true }], ['every request answers 500', { status: 500, noList: true }], ['the server is down (fetch rejects)', { reject: true }],
    ['api/i18n works but the locale files are 404', {}], ['api/i18n is 500 and the locale files are 404', { noList: true }]];
  for (const [name, opt] of failures) {
    for (const file of PAGES) {
      const nodes = parse(file), w = world(nodes, { fetch: fetcher(opt.status === 404 || opt.noList || opt.reject || name.includes('locale files') ? {} : PACKS, opt), navigator: { languages: ['ko-KR'] } });
      const lang = await w.run('I18N.ready');
      check(`${file}, ${name}: init resolves, the wait class is removed, no dictionary is loaded`, lang === 'en' && w.run('I18N.ok') === false && !w.cls.has('i18n-wait'), [lang, w.run('I18N.ok')]);
      check(`${file}, ${name}: every element keeps its HTML text and attributes`, unchanged(nodes).length === 0, unchanged(nodes).map(n => n.attrs['data-i18n'] || JSON.stringify(n.attrs)));
      check(`${file}, ${name}: no element shows a [key]`, keyish(nodes).length === 0, keyish(nodes).map(n => n.textContent));
    }
  }
  // an explicit call after the failure changes nothing either (apply is called again by nobody today, but it must stay safe)
  { const nodes = parse('index.html'), w = world(nodes, { fetch: fetcher({}, { reject: true }) }); await w.run('I18N.ready'); w.run('I18N.apply()');
    check('apply() again after a failed load: still nothing overwritten', unchanged(nodes).length === 0 && w.run('[...I18N.missing].length') === 0, w.run('[...I18N.missing]')); }

  // ---------- the dictionary is there but lacks some keys ----------
  // en without two keys that the pages use: those two elements keep their HTML text, the rest is translated
  const dropped = ['page.brand', 'page.legend.read'], en2 = JSON.parse(JSON.stringify(PACKS.en)), ko2 = JSON.parse(JSON.stringify(PACKS.ko));
  dropped.forEach(k => { delete en2.messages[k]; delete ko2.messages[k]; });
  for (const [lang, packs] of [['en', { en: en2, ko: ko2 }], ['ko', { en: en2, ko: ko2 }]]) {
    const nodes = parse('index.html'), w = world(nodes, { fetch: fetcher(packs), location: { search: '?lang=' + lang } });
    await w.run('I18N.ready');
    const kept = nodes.filter(n => dropped.includes(n.attrs['data-i18n'])), rest = nodes.filter(n => n.attrs['data-i18n'] && !dropped.includes(n.attrs['data-i18n']));
    check(`${lang}: a key missing in every dictionary leaves that element's HTML text alone (${dropped.join(', ')})`, kept.length === dropped.length && kept.every(n => n.textContent === n.initial.text), kept.map(n => n.textContent));
    check(`${lang}: the other texts are translated from the dictionary (${lang})`, rest.length > 5 && rest.every(n => n.textContent === (packs[lang].messages[n.attrs['data-i18n']] ?? '')), rest.filter(n => n.textContent !== packs[lang].messages[n.attrs['data-i18n']]).map(n => n.attrs['data-i18n']));
    check(`${lang}: nothing shows a [key]`, keyish(nodes).length === 0, keyish(nodes).map(n => n.textContent));
  }
  // ko chosen, one key only in English: the English dictionary text (the same fallback as t())
  { const ko3 = JSON.parse(JSON.stringify(PACKS.ko)); delete ko3.messages['page.brand'];
    const nodes = parse('index.html'), w = world(nodes, { fetch: fetcher({ en: PACKS.en, ko: ko3 }), location: { search: '?lang=ko' } }); await w.run('I18N.ready');
    const n = nodes.find(x => x.attrs['data-i18n'] === 'page.brand');
    eq('ko chosen, the key only in English: the English dictionary text is used', [w.run('I18N.lang'), n.textContent], ['ko', PACKS.en.messages['page.brand']]); }
  // attributes follow the same rule (title / placeholder / aria-label)
  { const attrKeys = real.flatMap(n => ATTRS.filter(a => hasAttr(n, 'data-i18n-' + a)).map(a => n.attrs['data-i18n-' + a])), gone = attrKeys[0];
    const en3 = JSON.parse(JSON.stringify(PACKS.en)), ko3 = JSON.parse(JSON.stringify(PACKS.ko)); delete en3.messages[gone]; delete ko3.messages[gone];
    const nodes = parse('index.html').concat(parse('game.html')), w = world(nodes, { fetch: fetcher({ en: en3, ko: ko3 }), location: { search: '?lang=ko' } }); await w.run('I18N.ready');
    const hit = nodes.filter(n => ATTRS.some(a => n.attrs['data-i18n-' + a] === gone)), other = nodes.filter(n => ATTRS.some(a => hasAttr(n, 'data-i18n-' + a) && n.attrs['data-i18n-' + a] !== gone));
    check('an attribute whose key is missing keeps its HTML value; the others are translated', hit.length > 0 && hit.every(n => JSON.stringify(n.attrs) === JSON.stringify(n.initial.attrs)) && other.length > 3 && other.some(n => JSON.stringify(n.attrs) !== JSON.stringify(n.initial.attrs)), gone); }

  // ---------- the dictionary is there: both pages are translated in both languages ----------
  for (const lang of ['en', 'ko']) {
    for (const file of PAGES) {
      const nodes = parse(file), w = world(nodes, { fetch: fetcher(PACKS), location: { search: '?lang=' + lang } });
      await w.run('I18N.ready');
      const M = PACKS[lang].messages, wantText = n => n.attrs['data-i18n'] ? { textContent: M[n.attrs['data-i18n']] } : {};
      const bad = nodes.filter(n => (n.attrs['data-i18n'] && n.textContent !== M[n.attrs['data-i18n']]) || ATTRS.some(a => hasAttr(n, 'data-i18n-' + a) && n.attrs[a] !== M[n.attrs['data-i18n-' + a]]));
      check(`${file}, ${lang}: every text and attribute is the dictionary's (${nodes.length} elements)`, w.run('I18N.lang') === lang && bad.length === 0, bad.map(n => n.attrs['data-i18n'] || JSON.stringify(n.attrs)));
      check(`${file}, ${lang}: no [key]` + (lang === 'en' ? ', no Hangul' : '') + ', nothing recorded as missing', keyish(nodes).length === 0 && (lang !== 'en' || !nodes.some(n => HAN.test(n.textContent) || Object.values(n.attrs).some(v => HAN.test(v)))) && w.run('[...I18N.missing].length') === 0, w.run('[...I18N.missing]'));
    }
  }
  // the keys of the HTML exist in both dictionaries (the fallback never has to carry a shipped page)
  { const keys = new Set(real.flatMap(n => Object.entries(n.attrs).filter(([k]) => k === 'data-i18n' || k.startsWith('data-i18n-')).map(([, v]) => v)));
    const lack = c => [...keys].filter(k => !(k in PACKS[c].messages));
    eq('every key named in index.html and game.html is in en.json and in ko.json', [lack('en'), lack('ko')], [[], []]); }
  // information only: where the English text in the HTML differs from en.json (the page then changes wording once the dictionary arrives)
  const drift = real.filter(n => n.attrs['data-i18n'] && n.initial.text !== PACKS.en.messages[n.attrs['data-i18n']]).map(n => n.file + ' ' + n.attrs['data-i18n']);
  if (drift.length) console.log('NOTE the HTML fallback text differs from en.json in ' + drift.length + ' place(s): ' + drift.join(', '));

  console.log(fails ? 'FAILED ' + fails : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log('HARNESS ERROR', e); process.exit(2); });
