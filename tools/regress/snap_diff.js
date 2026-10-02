// Compare two render_snap.js outputs (or two game_snap.js outputs) and say which keys differ.
// usage: node snap_diff.js a.json b.json
// Exit code 0 when identical, 1 otherwise. Each differing panel prints the first differing spot with some context.
const fs = require('fs');
const [fa, fb] = process.argv.slice(2);
const A = JSON.parse(fs.readFileSync(fa, 'utf8')), B = JSON.parse(fs.readFileSync(fb, 'utf8'));
let bad = 0;
for (const k of [...new Set([...Object.keys(A), ...Object.keys(B)])].sort()) {
  const x = A[k], y = B[k];
  if (x === y) continue;
  bad++;
  if (typeof x !== 'string' || typeof y !== 'string') { console.log('DIFF', k, ':', JSON.stringify(x), '->', JSON.stringify(y)); continue; }
  let i = 0; while (i < x.length && i < y.length && x[i] === y[i]) i++;
  const cut = s => JSON.stringify(s.slice(Math.max(0, i - 50), i + 90));
  console.log('DIFF', k, '(len', x.length, '->', y.length + ') at', i);
  console.log('   a:', cut(x)); console.log('   b:', cut(y));
}
console.log(bad ? bad + ' key(s) differ' : 'identical (' + Object.keys(A).length + ' keys)');
process.exit(bad ? 1 : 0);
