// Runs the page's script (dmm_downloader/web/app.js) in Node with a small stand-in for the browser and prints what
// its queue shows after each step (a JSON list of HTML). The steps (a JSON file) are the tool's answers to
// /api/state, {"state": {...}}, one after the other, and clicks on a row, {"open": <job id>}; the first is a state.
// Used by tests/test_web.py: node tests/page.cjs <app.js> <steps.json>
"use strict";
const fs = require("fs");
const vm = require("vm");

const [script, stepsFile] = process.argv.slice(2);
const steps = JSON.parse(fs.readFileSync(stepsFile, "utf8"));

const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) {
    elements.set(selector, {
      innerHTML: "", textContent: "", value: "", hidden: false, disabled: false, checked: false, className: "",
      dataset: {}, scrollTop: 0, clientHeight: 0, scrollHeight: 0, listeners: {},
      classList: { toggle() {}, add() {}, remove() {} },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      setAttribute() {}, focus() {}, blur() {}, click() {}, insertAdjacentHTML() {},
      contains: () => false, closest: () => null,
    });
  }
  return elements.get(selector);
}

let answer = steps[0].state;
let poll = null; // the page's next refresh() and its delay
const context = {
  document: {
    querySelector: element, querySelectorAll: () => [], documentElement: { dataset: {} }, activeElement: null,
    addEventListener() {}, body: {},
  },
  matchMedia: () => ({ matches: false }),
  localStorage: { getItem: () => null, setItem() {} },
  location: { host: "127.0.0.1" },
  setTimeout: (fn, ms) => { if (fn.name === "refresh") poll = [fn, ms]; return 1; },
  clearTimeout() {},
  fetch: async () => ({ status: 200, ok: true, json: async () => answer }),
};

async function drawn(number) {
  await new Promise(resolve => setImmediate(resolve)); // the answer is read and the page drawn
  if (!poll || poll[1] === 3000) throw new Error(`the page couldn't draw step ${number}`); // refresh() failed
}

(async () => {
  vm.runInNewContext(fs.readFileSync(script, "utf8"), context, { filename: script }); // ends with refresh()
  await drawn(1);
  const queues = [element("#queue").innerHTML];
  for (const [index, step] of steps.entries()) {
    if (index === 0) continue;
    if (step.state) {
      answer = step.state;
      const [refresh] = poll;
      poll = null;
      refresh();
      await drawn(index + 1);
    } else {
      const row = { dataset: { open: String(step.open) } };
      const target = { closest: selector => (selector === "[data-open]" ? row : null) };
      for (const fn of element("#queue").listeners.click || []) fn({ target });
    }
    queues.push(element("#queue").innerHTML);
  }
  process.stdout.write(JSON.stringify(queues));
})().catch(err => {
  process.stderr.write(`${err.stack || err}\n`);
  process.exit(1);
});
