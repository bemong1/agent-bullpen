/* Runtime for the UI dictionary (static/locales/<code>.json): picks the language, loads the dictionary, answers t(). Every page loads it first (before common.js).
 *   t('status.running')                      -> the dictionary text
 *   t('time.ago.min', { n: 5 })               -> {name} is replaced by params.name; a name without a value stays as {name}
 *   t('unit.agent', { count: 3 })             -> a dictionary value { one, other, ... } is chosen by count with Intl.PluralRules (other is required)
 *   '{name:subject}' in a dictionary text    -> the value followed by the Korean subject particle (i/ga); '{n:number}' -> the number grouped for the language
 * A key missing in the chosen language falls back to the English dictionary (English plural rules), then to [key]. t() does not escape HTML:
 * when the result goes into innerHTML, wrap data values in esc() before passing them as params.
 * Language order: a valid ?lang= -> the stored choice (localStorage ab.lang, only what the user picked) -> a supported navigator.languages entry -> English.
 * Start: t() answers once I18N.ready (a promise) has resolved; pages draw their first screen after it. Reading ready starts init() by itself, so no page needs to call it.
 *   init()                          browser: fetch api/i18n (the languages), then locales/<code>.json (and English). Later calls return the same promise
 *   init({ lang, packs, langs? })   tests and regression tools: the dictionary objects are passed in; nothing is fetched and no timer is set, so it is done at once
 * Nothing here throws when navigator, location, localStorage, Intl or fetch are missing (the fake browsers of the regression tools). */
'use strict';
const I18N = (() => {
  const DEFAULT = 'en', STORE_KEY = 'ab.lang', FETCH_MS = 8000;
  const S = { lang: DEFAULT, packs: {}, langs: [], ok: false, ready: null };
  const missing = new Set();                      // keys that no dictionary had (tests look for a drawn [key])
  const rules = {}, dtf = {}, nf = {};            // Intl object caches (emptied when the language changes)

  // ---------- language choice ----------
  // Matches a tag to a supported code: case-insensitive, the exact tag first, then with trailing parts dropped (zh-Hant-TW -> zh-Hant -> zh). null when nothing matches
  function match(tag, supported) {
    if (typeof tag !== 'string') return null;
    const parts = tag.trim().toLowerCase().replace(/_/g, '-').split('-').filter(Boolean);
    for (let n = parts.length; n > 0; n--) {
      const hit = (supported || []).find(c => c.toLowerCase() === parts.slice(0, n).join('-'));
      if (hit) return hit;
    }
    return null;
  }
  const queryLang = search => { const m = /[?&]lang=([^&#]*)/.exec(search || ''); try { return m ? decodeURIComponent(m[1]) : null; } catch { return null; } };
  // The order alone, as a pure function: { search, stored, navLangs, supported } -> language code
  function pick(o) {
    const sup = o.supported && o.supported.length ? o.supported : [DEFAULT];
    for (const tag of [queryLang(o.search), o.stored].concat(o.navLangs || [])) { const m = match(tag, sup); if (m) return m; }
    return sup.includes(DEFAULT) ? DEFAULT : sup[0];
  }
  function stored() {                              // no storage, or a blocked one (private mode ...): treated as nothing stored
    try {
      const v = localStorage.getItem(STORE_KEY);
      if (v == null) return null;
      try { const j = JSON.parse(v); return typeof j === 'string' ? j : null; } catch { return v; }   // board.js's store writes JSON; a bare string is accepted too
    } catch { return null; }
  }
  function detect(supported) {
    let search = '', navLangs = [];
    try { search = location.search || ''; } catch {}
    try { navLangs = (navigator.languages && navigator.languages.length ? Array.from(navigator.languages) : [navigator.language]).filter(Boolean); } catch {}
    return pick({ search, stored: stored(), navLangs, supported });
  }

  // ---------- dictionary ----------
  const pack = code => S.packs[code];
  const localeOf = code => (pack(code) && pack(code).meta && pack(code).meta.locale) || code;
  function setup(o) {
    S.packs = o.packs || {};
    const codes = Object.keys(S.packs);
    S.langs = o.langs || codes.map(c => ({ code: c, name: (S.packs[c].meta && S.packs[c].meta.name) || c }));
    S.lang = o.lang && S.packs[o.lang] ? o.lang : codes.length ? detect(codes) : DEFAULT;
    S.ok = !!(S.packs[S.lang] || S.packs[DEFAULT]);
    for (const m of [rules, dtf, nf]) for (const k of Object.keys(m)) delete m[k];
    missing.clear();
    try { document.documentElement.lang = S.lang; } catch {}
    return S.lang;
  }
  async function getJson(url) {
    const ctl = typeof AbortController === 'function' ? new AbortController() : null;
    const guard = ctl && typeof setTimeout === 'function' ? setTimeout(() => ctl.abort(), FETCH_MS) : 0;   // a server that never answers must not hold the page start for ever
    try {
      const r = await fetch(url, ctl ? { signal: ctl.signal } : undefined);
      if (!r.ok) throw new Error(r.status + ' ' + url);
      return await r.json();
    } finally { if (guard) clearTimeout(guard); }
  }
  async function load() {
    let langs = [];
    try { langs = ((await getJson('api/i18n')).languages || []).filter(l => l && typeof l.code === 'string'); } catch {}
    const supported = langs.map(l => l.code), want = detect(supported.length ? supported : [DEFAULT]);
    const get = code => getJson('locales/' + encodeURIComponent(code) + '.json').then(j => [code, j], () => null);
    const got = (await Promise.all([get(want)].concat(want === DEFAULT ? [] : [get(DEFAULT)]))).filter(Boolean);
    const packs = Object.fromEntries(got);
    setup({ lang: packs[want] ? want : DEFAULT, packs, langs });   // English when the chosen language cannot be fetched
    return S.lang;
  }
  // Shows the screen that the HTML hid before the first draw (html.i18n-wait), whether or not the dictionary arrived
  function reveal() {
    try { document.documentElement.classList.remove('i18n-wait'); } catch {}
    try { apply(); } catch {}
  }
  function init(o) {
    if (o && o.packs) { setup(o); reveal(); return (S.ready = Promise.resolve(S.lang)); }
    if (!S.ready) S.ready = load().catch(() => { S.ok = false; return S.lang; }).then(lang => { reveal(); return lang; });
    return S.ready;
  }

  // ---------- lookup and substitution ----------
  function find(key) {                             // [value, dictionary language]: the chosen language, then English
    for (const code of S.lang === DEFAULT ? [DEFAULT] : [S.lang, DEFAULT]) {
      const m = pack(code) && pack(code).messages, v = m && Object.prototype.hasOwnProperty.call(m, key) ? m[key] : undefined;
      if (typeof v === 'string' || (v && typeof v === 'object')) return [v, code];
    }
    return null;
  }
  function category(code, n) {
    try { return (rules[code] || (rules[code] = new Intl.PluralRules(localeOf(code)))).select(n); } catch { return 'other'; }
  }
  const gaI = w => { const c = String(w).slice(-1), k = c.charCodeAt(0);        // Korean subject particle i/ga: i after a Hangul final consonant, a digit 0 1 3 6 7 8 or a letter l m n r, else ga
    return (k >= 0xAC00 && k <= 0xD7A3 ? (k - 0xAC00) % 28 : /[013678lmnr]/i.test(c) ? 1 : 0) ? '이' : '가'; };   // i18n-ok: the particle itself
  const FORMATTERS = { subject: v => v + gaI(v), number: v => num(v) };
  function t(key, params) {
    const hit = find(key);
    if (!hit) { missing.add(key); return '[' + key + ']'; }
    let [text, code] = hit;
    const p = params || {};
    if (typeof text === 'object') {                // plural: the category comes from count; a category the text lacks falls to other
      const n = Number(p.count), cat = p.count == null || !isFinite(n) ? 'other' : category(code, n);
      text = text[cat] !== undefined ? text[cat] : text.other !== undefined ? text.other : '';
    }
    return text.replace(/\{([A-Za-z_]\w*)(?::([A-Za-z]+))?\}/g, (all, name, fmt) => {
      const v = p[name];
      if (v === undefined || v === null) return all;
      return fmt && FORMATTERS[fmt] ? FORMATTERS[fmt](v) : String(v);
    });
  }
  const has = key => !!find(key);
  // ---------- dates and numbers ----------
  // ts = a server time (seconds) or a Date. preset = a name in the dictionary's formats (time, timeSec, dateTime, monthShort ...); an unknown name gives the language's plain date
  function date(ts, preset) {
    const d = ts && typeof ts.getTime === 'function' ? ts : new Date(ts * 1000), loc = localeOf(S.lang);
    const f = (pack(S.lang) && pack(S.lang).formats && pack(S.lang).formats[preset]) || (pack(DEFAULT) && pack(DEFAULT).formats && pack(DEFAULT).formats[preset]);
    try { return (dtf[preset] || (dtf[preset] = new Intl.DateTimeFormat(loc, f))).format(d); } catch { return String(d); }
  }
  function num(n, opts) {
    try { return (opts ? new Intl.NumberFormat(localeOf(S.lang), opts) : nf.d || (nf.d = new Intl.NumberFormat(localeOf(S.lang)))).format(n); } catch { return String(n); }
  }

  // ---------- addresses and the selector ----------
  // Sets ?lang= of href to code (adds it when missing). The hash and the other query keys (session, demo ...) are kept

  function url(href, code) {
    const h = href.indexOf('#'), base = h < 0 ? href : href.slice(0, h), hash = h < 0 ? '' : href.slice(h), q = base.indexOf('?');
    const path = q < 0 ? base : base.slice(0, q), kept = (q < 0 ? '' : base.slice(q + 1)).split('&').filter(x => x && !/^lang=/.test(x));
    return path + '?' + kept.concat('lang=' + encodeURIComponent(code)).join('&') + hash;
  }
  // Link between pages: when this address has ?lang=, the link carries it (a shared link keeps its language between the dashboard and the office)
  function link(href) {
    let cur = null; try { cur = queryLang(location.search); } catch {}
    return cur ? url(href, cur) : href;
  }
  function go(to) { try { location.assign(to); } catch { try { location.href = to; } catch {} } }
  // The language the user picked: stored (only what the user picked is stored), then the page reloads with session, demo and the hash kept. Even where storage is blocked the address carries ?lang=
  function choose(code) {
    const m = match(code, S.langs.map(l => l.code));
    if (!m) return null;
    try { localStorage.setItem(STORE_KEY, JSON.stringify(m)); } catch {}
    let cur = ''; try { cur = (location.pathname || '') + (location.search || '') + (location.hash || ''); } catch {}
    const to = url(cur || './', m);
    api._go(to);
    return to;
  }
  // Fills and wires the header <select>. The options are the languages' own names (as /api/i18n gives them), never translated. One language only: hidden.
  // With two or more it shows (the header keeps room for it).
  function mountPicker(sel) {
    if (!sel || typeof document === 'undefined' || !document.createElement) return;
    if (S.langs.length < 2) { sel.hidden = true; return; }
    sel.innerHTML = '';
    S.langs.forEach(l => { const o = document.createElement('option'); o.value = l.code; o.textContent = l.name; sel.appendChild(o); });
    sel.value = S.lang;
    sel.title = t('common.language');
    if (sel.setAttribute) sel.setAttribute('aria-label', sel.title);
    sel.onchange = () => choose(sel.value);
  }
  // Translates static HTML: [data-i18n] (text) and [data-i18n-title] / [data-i18n-placeholder] / [data-i18n-aria-label] (attributes). Called once when init is done.
  // An element is overwritten only when a dictionary has its key: the English text written in the HTML stays when the dictionary could not be loaded (or lacks the key),
  // so a failed request leaves English words on the page, never a [key]
  function apply(root) {
    root = root || (typeof document !== 'undefined' ? document : null);
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('[data-i18n]').forEach(e => { const k = e.getAttribute('data-i18n'); if (has(k)) e.textContent = t(k); });
    ['title', 'placeholder', 'aria-label'].forEach(a => root.querySelectorAll('[data-i18n-' + a + ']').forEach(e => { const k = e.getAttribute('data-i18n-' + a); if (has(k)) e.setAttribute(a, t(k)); }));
  }

  const api = {
    DEFAULT, STORE_KEY, init, t, has, date, num, match, pick, url, link, choose, mountPicker, apply, missing, _go: go,
    get ready() { return S.ready || init(); },
    get lang() { return S.lang; }, get langs() { return S.langs; }, get ok() { return S.ok; },
  };
  return api;
})();
// The short name. In a function that shadows t with a local (.forEach(t => ...), const t = ...) call I18N.t instead
const t = (key, params) => I18N.t(key, params);
