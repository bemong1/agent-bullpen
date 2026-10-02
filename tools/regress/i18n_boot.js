// Shared by the regression loaders: puts static/i18n.js and its dictionaries into a fake browser (a vm context).
//   const I18B = require('./i18n_boot');
//   I18B.lang()                      language the loaders run in: AB_LANG, default 'ko' (the Korean snapshots are compared against the reference statics)
//   I18B.packs(dir)                  { code: parsed <dir>/locales/<code>.json } read from disk (nothing when the static dir has no locales: the reference copy)
//   I18B.boot(ctx, dir, code)        after i18n.js ran in ctx: I18N.init({ lang, packs, langs }) with the packs from disk (no fetch, no timers)
//   I18B.route(dir, url)             what the server answers for 'api/i18n' and 'locales/<code>.json' (a plain object), else null: the page-level fake fetch uses it
//   I18B.scripts(dir, names)         the names that exist in dir, with i18n.js first when the static dir has it (the reference statics have none)
// The statics under test may predate i18n.js: every function does nothing when <dir>/i18n.js is missing, so one loader runs both copies.
const fs = require('fs'), path = require('path');

const lang = () => process.env.AB_LANG || 'ko';
const has = dir => fs.existsSync(path.join(dir, 'i18n.js'));
const cache = {};
function packs(dir) {                  // parsed once per static dir (lounge_checks boots a new fake browser per scenario)
  if (cache[dir]) return cache[dir];
  const d = path.join(dir, 'locales'), out = {};
  if (fs.existsSync(d)) for (const f of fs.readdirSync(d).sort()) if (/^[a-z]{2,3}(-[A-Za-z0-9]+)*\.json$/.test(f)) out[f.slice(0, -5)] = JSON.parse(fs.readFileSync(path.join(d, f), 'utf8'));
  return (cache[dir] = out);
}
const langs = p => Object.keys(p).sort((a, b) => (a === 'en' ? -1 : b === 'en' ? 1 : a < b ? -1 : 1)).map(code => ({ code, name: p[code].meta.name }));
function boot(ctx, dir, code) {
  if (!has(dir)) return false;
  const p = packs(dir);
  ctx.__boot = { lang: code || lang(), packs: p, langs: langs(p) };
  require('vm').runInContext('I18N.init(__boot)', ctx);
  delete ctx.__boot;
  return true;
}
function route(dir, url) {
  const m = /(?:^|\/)(api\/i18n|locales\/([A-Za-z0-9-]+)\.json)(?:\?|$)/.exec(String(url));
  if (!m || !has(dir)) return null;
  const p = packs(dir);
  if (m[1] === 'api/i18n') return { default: 'en', languages: langs(p) };
  return p[m[2]] || null;
}
function scripts(dir, names) {
  return (has(dir) ? ['i18n.js'] : []).concat(names).filter(n => fs.existsSync(path.join(dir, n)));
}
module.exports = { lang, has, packs, boot, route, scripts };
