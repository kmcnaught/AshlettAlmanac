// Headless smoke test for index.html.
// Invoked by scripts/smoke_test_html.py with HTML path + tides.json path + verses.json path as argv.
const fs = require('fs');

const html = fs.readFileSync(process.argv[2], 'utf8');
const tidesPath = process.argv[3];
const versesPath = process.argv[4];

const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length < 2) { console.error('expected 2 <script> tags, got', scripts.length); process.exit(3); }

// Fake DOM — only what the app touches.
const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, _text: '', _html: '', style: {},
      get textContent() { return this._text; },
      set textContent(v) { this._text = v; },
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; },
    };
  }
  return els[id];
}
globalThis.document = {
  getElementById: el,
  querySelectorAll: () => [],
};
globalThis.window = globalThis;

const tides = JSON.parse(fs.readFileSync(tidesPath, 'utf8'));
const verses = versesPath ? JSON.parse(fs.readFileSync(versesPath, 'utf8')) : null;
// localStorage stub — the picker uses it for least-recently-shown rotation.
globalThis.localStorage = (() => {
  const m = {};
  return {
    getItem: k => k in m ? m[k] : null,
    setItem: (k, v) => { m[k] = String(v); },
    removeItem: k => { delete m[k]; },
  };
})();
let fetchCalls = [];
globalThis.fetch = async (url) => {
  fetchCalls.push(url);
  if (url.includes('tides.json')) {
    return { ok: true, json: async () => tides };
  }
  if (verses && url.includes('verses.json')) {
    return { ok: true, json: async () => verses };
  }
  throw new Error('weather offline (stubbed)');
};

// Load SunCalc (first <script>) into globalThis.SunCalc.
// The UMD wrapper checks `typeof module !== "undefined"` to pick CommonJS;
// we shadow `module`/`exports` to force the global-assignment branch.
(function () {
  var module, exports;  // eslint-disable-line no-unused-vars
  eval(scripts[0]);
})();
if (!globalThis.SunCalc) { console.error('SunCalc failed to load'); process.exit(5); }

try {
  eval(scripts[1]);
} catch (e) {
  console.error('runtime error:', e.message);
  process.exit(4);
}

setTimeout(() => {
  console.log('fetch calls:', fetchCalls);
  const checks = [
    ['date',        t => t.length > 0],
    ['tide-window', t => /float|fills|low/i.test(t)],
    ['extremes',    t => /High|Low/.test(t)],
    ['light',       t => t.length > 0],
    ['verse-line',  t => t.length > 0],
    ['marsh',       t => t.length > 0],
    ['moon-name',   t => t.length > 0],
    ['curve',       t => t.length > 0],
  ];
  let failed = 0;
  for (const [id, ok] of checks) {
    const e = els[id];
    const v = e ? (e._html || e._text) : '';
    const pass = e && ok(v);
    console.log((pass ? 'PASS' : 'FAIL'), id + ':', JSON.stringify(v.slice(0, 80)));
    if (!pass) failed++;
  }
  process.exit(failed ? 1 : 0);
}, 100);
