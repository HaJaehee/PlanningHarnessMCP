// Runs the approval page's scripts outside a browser, for tests/pagejs.py.
//
//   node tests/web/runner.js planning/web/page.js [more scripts, in page order]
//
// The scripts are evaluated in one context, the way a page runs its <script> tags, with
// just enough of a browser around them that their start-up code runs. Then each line on
// stdin is one request and each line on stdout its answer:
//
//   {"fn": "tookText", "args": [125]}     -> {"ok": "2분 5초"}
//   {"get": "DONE_LABEL"}                  -> {"ok": {...}}
//   {"set": "RUNS", "value": [...]}        -> {"ok": null}
//
// Nothing here draws or clicks: what the page builds is checked as the text it builds.
'use strict';
process.env.TZ = 'UTC';
const fs = require('fs');
const vm = require('vm');
const readline = require('readline');

const VERSION_TOKEN = /__PLANNING_MCP_VERSION__/g;
const version = process.env.PAGE_VERSION || '9.9.9';

function element() {
  return {
    addEventListener() {}, removeEventListener() {}, appendChild() {}, insertBefore() {},
    remove() {}, setAttribute() {}, getAttribute() { return null; }, hasAttribute() { return false; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, contains() { return false; }, focus() {},
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    style: {}, dataset: {}, children: [], textContent: '', innerHTML: '', value: '',
  };
}
const store = new Map();
const sandbox = {
  console,
  document: {
    title: '', activeElement: null,
    getElementById: element, createElement: element,
    querySelector() { return null; }, querySelectorAll() { return []; },
  },
  localStorage: {
    get length() { return store.size; },
    key(i) { return Array.from(store.keys())[i] ?? null; },
    getItem(k) { return store.has(k) ? store.get(k) : null; },
    setItem(k, v) { store.set(k, String(v)); },
    removeItem(k) { store.delete(k); },
  },
  // The first poll is started at load; it never answers.
  fetch() { return new Promise(() => {}); },
  setInterval() { return 0; }, clearInterval() {},
  setTimeout() { return 0; }, clearTimeout() {},
  confirm() { return false; }, alert() {},
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
const context = vm.createContext(sandbox);
for (const file of process.argv.slice(2)) {
  const code = fs.readFileSync(file, 'utf8').replace(VERSION_TOKEN, version);
  vm.runInContext(code, context, { filename: file });
}

function answer(request) {
  if (request.fn) {
    const fn = vm.runInContext(request.fn, context);
    if (typeof fn !== 'function') throw new Error(request.fn + ' is not a function');
    return fn(...(request.args || []));
  }
  if (request.get) return vm.runInContext(request.get, context);
  if (request.set) {
    context.__value = request.value;
    vm.runInContext(request.set + '=__value', context);
    return null;
  }
  throw new Error('unknown request: ' + JSON.stringify(request));
}

readline.createInterface({ input: process.stdin }).on('line', line => {
  let reply;
  try {
    const result = answer(JSON.parse(line));
    reply = { ok: result === undefined ? null : result };
  } catch (e) {
    reply = { error: String(e && e.stack || e) };
  }
  process.stdout.write(JSON.stringify(reply) + '\n');
});
