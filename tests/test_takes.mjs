// Dependency-free state/stream regression tests. Run: node --test tests/test_takes.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Minimal DOM adapter: keeps real take panes separate, without a browser dependency.
function harness(mode = '') {
  const nodes = new Map();
  let document;
  class Node {
    constructor() {
      this.children = [];
      this.style = {};
      this.classes = new Set();
      this.classList = {
        add: (...cs) => cs.forEach(c => this.classes.add(c)),
        remove: (...cs) => cs.forEach(c => this.classes.delete(c)),
        contains: c => this.classes.has(c),
        toggle: (c, on = !this.classes.has(c)) => on ? this.classes.add(c) : this.classes.delete(c)
      };
    }
    set id(id) { this._id = id; nodes.set(id, this); }
    get id() { return this._id; }
    set className(s) { this.classes = new Set(s.split(' ')); }
    set innerHTML(s) { this.html = s; this.children = []; }
    get innerHTML() { return this.html || ''; }
    appendChild(n) { this.children.push(n); n.parent = this; }
    remove() { this.parent.children = this.parent.children.filter(n => n !== this); }
    querySelector(selector) {
      return this.children.find(n => selector.startsWith('.') && n.classes.has(selector.slice(1))) || null;
    }
    setAttribute() {}
    focus() { document.activeElement = this; }
  }
  document = { createElement: () => new Node(), getElementById: id => nodes.get(id), addEventListener() {} };
  for (const id of ['ws-take-switcher', 'gw-chat-turns-window', 'lyrics-sheet-content', 'gw-status',
    'gw-chat-turn-indicator', 'btn-copy-chat-transcript', 'btn-start-interactive-cowrite',
    'take-action-modal', 'take-modal-body', 'btn-modal-parallel']) {
    const node = new Node(); node.id = id;
  }
  nodes.get('take-action-modal').classList.add('hidden');
  const requests = [];
  const turns = [];
  const context = vm.createContext({
    document, window: {}, AbortController, TextDecoder, console,
    currentParams: () => ({ prompt: 'test brief', answers: { validation_mode: mode } }),
    confirm: () => true, goToStep() {},
    fetch: async (url, options) => {
      const stream = new ReadableStream({ start(controller) { requests.push({ controller, options }); } });
      return { ok: true, body: stream };
    }
  });
  for (const name of ['core.js', 'cowriter/takes.js']) {
    vm.runInContext(readFileSync(new URL('../src/songwriter/web/static/js/' + name, import.meta.url), 'utf8'), context);
  }
  context.postJSON = (url, body) => new Promise(resolve => turns.push({ url, body, resolve }));
  context.state.currentStep = 4;
  context.state.selectedModel = 'glm-4.6';
  const emit = (i, event) => requests[i].controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(event) + '\n\n'));
  const finish = (i, lyrics) => { emit(i, { type: 'complete', lyrics }); requests[i].controller.close(); };
  return { c: context, requests, turns, nodes, emit, finish };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('first take reserves its ID immediately; completed take creates a visible new tab immediately', async () => {
  const { c, nodes, finish } = harness();
  const first = c.runGenerateTake(false);
  assert.equal(c.state.activeTakeId, 1);
  assert.equal(c.state.nextTakeId, 2);
  assert.equal(c.state.takes[1].status, 'running');
  assert.match(nodes.get('btn-take-1').innerHTML, /take-spinner/);
  finish(0, 'first song');
  await first;
  const second = c.onNewTakeClicked();
  assert.equal(c.state.activeTakeId, 2);
  assert.equal(c.state.takes[2].status, 'running');
  assert.ok(nodes.get('btn-take-2'));
  assert.equal(c.state.takes[1].lyrics, 'first song');
  finish(1, 'second song');
  await second;
  assert.equal(c.state.nextTakeId, 3);
});

test('new take while running asks; cancel leaves it untouched; parallel streams stay isolated', async () => {
  const { c, nodes, requests, emit, finish } = harness();
  const first = c.runGenerateTake(false);
  c.onNewTakeClicked();
  assert.equal(requests.length, 1);
  assert.equal(nodes.get('take-action-modal').classList.contains('hidden'), false);
  c.closeTakeModal();
  assert.equal(c.state.nextTakeId, 2);
  c.onNewTakeClicked();
  const second = c.onTakeModalParallel();
  assert.equal(requests[0].options.signal.aborted, false);
  assert.equal(c.state.activeTakeId, 2);
  emit(0, { type: 'line_accepted', section: 'Verse', line: 'first line' });
  emit(1, { type: 'line_accepted', section: 'Chorus', line: 'other line' });
  await tick();
  assert.equal(c.state.takes[1].lyricsPane.children.length, 2);
  assert.equal(c.state.takes[2].lyricsPane.children.length, 2);
  assert.match(c.state.takes[1].lyricsPane.children[1].innerHTML, /first line/);
  assert.match(c.state.takes[2].lyricsPane.children[1].innerHTML, /other line/);
  finish(0, 'first finished in background');
  await first;
  assert.equal(c.state.activeTakeId, 2);
  assert.equal(c.state.takes[2].status, 'running');
  c.selectTake(1);
  assert.equal(c.state.takes[1].lyricsPane.classList.contains('hidden'), false);
  assert.equal(c.state.takes[2].lyricsPane.classList.contains('hidden'), true);
  finish(1, 'second finished in background');
  await second;
  assert.equal(c.state.activeTakeId, 1);
  assert.equal(c.state.takes[2].lyrics, 'second finished in background');
});

test('reset reuses the tab, aborts only that run and ignores its late completion', async () => {
  const { c, requests, finish } = harness();
  const oldRun = c.runGenerateTake(false);
  c.onNewTakeClicked();
  const resetRun = c.onTakeModalReset();
  assert.equal(requests[0].options.signal.aborted, true);
  assert.equal(c.state.nextTakeId, 2);
  assert.equal(Object.keys(c.state.takes).length, 1);
  finish(0, 'stale song');
  await oldRun;
  assert.equal(c.state.takes[1].status, 'running');
  finish(1, 'replacement song');
  await resetRun;
  assert.equal(c.state.takes[1].lyrics, 'replacement song');
});

test('failed streams stop showing running and can create a new take', async () => {
  const { c, requests, finish } = harness();
  const first = c.runGenerateTake(false);
  requests[0].controller.close();
  await first;
  assert.equal(c.state.takes[1].status, 'failed');
  const second = c.onNewTakeClicked();
  assert.equal(c.state.activeTakeId, 2);
  finish(1, 'recovered');
  await second;
});

test('discard invalidates all pending runs, even when IDs are reused', async () => {
  const { c, requests, finish } = harness();
  const first = c.runGenerateTake(false);
  c.onNewTakeClicked();
  const second = c.onTakeModalParallel();
  c.discardAndStartOver();
  assert.equal(requests.every(r => r.options.signal.aborted), true);
  const fresh = c.runGenerateTake(false);
  finish(0, 'discarded one'); finish(1, 'discarded two');
  await Promise.all([first, second]);
  assert.equal(c.state.takes[1].status, 'running');
  finish(2, 'fresh'); await fresh;
  assert.equal(c.state.takes[1].lyrics, 'fresh');
});

test('human review responses remain attached to their originating take after switching', async () => {
  const { c, turns } = harness('Human review');
  const first = c.runGenerateTake(false);
  c.onNewTakeClicked();
  const second = c.onTakeModalParallel();
  const result = section => ({ section, lines: ['test line'], turn_index: 0, total_turns: 2 });
  turns[0].resolve(result('Verse')); await first;
  assert.equal(c.state.activeTakeId, 2);
  assert.equal(c.state.takes[1].humanReview.slots[0].section, 'Verse');
  assert.equal(c.state.takes[2].humanReview.slots.length, 0);
  turns[1].resolve(result('Chorus')); await second;
  c.selectTake(1);
  assert.equal(c.state.humanReview, c.state.takes[1].humanReview);
  assert.notEqual(c.turnCardId(1, 1, 1), c.turnCardId(1, 1, 2));
  const finalizing = c.finalizeHumanReview();
  c.selectTake(2);
  turns[2].resolve({ lyrics: 'approved first take' });
  await finalizing;
  assert.equal(c.state.activeTakeId, 2);
  assert.equal(c.state.takes[1].lyrics, 'approved first take');
  assert.equal(c.state.takes[1].status, 'completed');
});

test('resetting human review ignores stale turn responses and preserves the parallel take', async () => {
  const { c, turns } = harness('Human review');
  const oldRun = c.runGenerateTake(false);
  const oldTake = c.state.takes[1];
  c.onNewTakeClicked();
  const parallel = c.onTakeModalParallel();
  c.selectTake(1);
  c.onNewTakeClicked();
  const reset = c.onTakeModalReset();
  assert.equal(oldTake.abort.signal.aborted, true);
  assert.equal(c.state.takes[2].abort.signal.aborted, false);
  turns[0].resolve({ section: 'Stale', lines: ['discarded'] });
  await oldRun;
  assert.equal(c.state.takes[1].humanReview.slots.length, 0);
  const result = section => ({ section, lines: ['new line'], turn_index: 0, total_turns: 2 });
  turns[1].resolve(result('Parallel'));
  turns[2].resolve(result('Replacement'));
  await Promise.all([parallel, reset]);
  assert.equal(c.state.takes[1].humanReview.slots[0].section, 'Replacement');
  assert.equal(c.state.takes[2].humanReview.slots[0].section, 'Parallel');
});

test('turn panels name the model that ran the take, not a generic LLM label', async () => {
  const { c, emit } = harness();
  c.runGenerateTake(false);
  emit(0, { type: 'prompt', turn_id: 1, attempt: 1, section: 'Verse', turn_type: 'verse', max_attempts: 3, prompt: 'write a line' });
  await tick();
  const chat = c.state.takes[1].chatPane;
  const card = chat.children[chat.children.length - 1];
  assert.match(card.innerHTML, /Prompt sent to glm-4\.6/);
  assert.match(card.innerHTML, /glm-4\.6 response/);
  assert.doesNotMatch(card.innerHTML, /sent to the LLM|LLM response|LLM Critique/);
});

test('a take keeps its own model label after the dropdown selection changes', () => {
  const { c } = harness();
  c.runGenerateTake(false);
  c.state.selectedModel = 'some-other-model';
  assert.equal(c.state.takes[1].model, 'glm-4.6');
});

test('model label falls back cleanly when no model is known', () => {
  const { c } = harness();
  c.state.selectedModel = '';
  assert.equal(c.modelLabel(''), 'the model');
  assert.equal(c.modelLabel('claude-x'), 'claude-x');
});

test('critique panel never shows an unreadable critic reply as a pass, and shows line fixes', () => {
  const { c } = harness();
  const unreadable = c.critiquePanelHtml({ passed: true, parsed: false, summary: 'Critic reply was not valid JSON.' }, 'glm-4.6');
  assert.match(unreadable, /Critique unavailable/);
  assert.doesNotMatch(unreadable, /Passed critique/);
  const failed = c.critiquePanelHtml({
    passed: false, parsed: true,
    sense: { score: 1, verdict: 'fail', feedback: 'spliced idiom' },
    lines: [{ line: 2, meaning: 'rip me to pieces', makes_sense: false, forced_word: 'up', fix: 'Break me apart' }]
  }, 'glm-4.6');
  assert.match(failed, /Failed critique/);
  assert.match(failed, /Sense \[1\/5\]/);
  assert.match(failed, /forced: 'up'/);
  assert.match(failed, /fix: <span class="c-bright">Break me apart<\/span>/);
});
