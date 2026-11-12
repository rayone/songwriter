// Run: node --test tests/test_genre_selection.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

function harness() {
  const nodes = new Map();
  for (const [id, value] of Object.entries({
    'gw-style-input': 'Progressive House / Vocal Trance', 'gw-prompt': 'a song',
    'gw-questions': '', 'gw-selected-template-id': '7', 'gw-storyline-text': 'a story',
    'gw-interview-status': '', 'gw-genre-input': ''
  })) nodes.set(id, { value, style: {} });
  const c = vm.createContext({ document: { getElementById: id => nodes.get(id), addEventListener() {} }, window: {}, updateStepperUI() {}, onVocalChanged() {}, structuredClone: typeof structuredClone !== 'undefined' ? structuredClone : undefined });
  for (const file of ['core.js', 'cowriter/interview.js', 'cowriter/reference.js']) {
    vm.runInContext(readFileSync(new URL('../src/songwriter/web/static/js/' + file, import.meta.url), 'utf8'), c);
  }
  return { c, nodes };
}

test('draft requests carry original genre separately from reference and Step 3 answer', () => {
  const { c } = harness();
  if (!c.state.song) c.state.song = {};
  c.state.song.reference_song = { genre: 'Pop' };
  c.state.song.questions = [{ id: 'genre', question: 'Which genre?', category: 'production' }];
  if (!c.state.song.answers) c.state.song.answers = {};
  c.state.song.answers.genre = 'Melodic House';
  const params = c.currentParams();
  assert.equal(params.genre, 'Progressive House / Vocal Trance');
  assert.equal(params.reference_song.genre, 'Pop');
  assert.equal(params.answers.genre, 'Melodic House');
});

test('template genre uses metadata instead of the entire production caption', () => {
  const { c, nodes } = harness();
  nodes.get('gw-style-input').value = 'Dreamy pads and a long breakdown';
  c.state.activeTemplate = { metadata: { genre: 'Progressive House', subgenre: 'Vocal Trance', audio_prompt: 'Dreamy pads and a long breakdown' } };
  assert.equal(c.requestedGenre(), 'Progressive House / Vocal Trance');
  nodes.get('gw-style-input').value = 'Indie Folk';
  assert.equal(c.requestedGenre(), 'Indie Folk');
});

test('editing gw-genre-input carries forward as writer genre and invalidates questions when changed', () => {
  const { c, nodes } = harness();
  if (!c.state.song) c.state.song = {};
  nodes.get('gw-genre-input').value = 'Darkwave Synth';
  assert.equal(c.requestedGenre(), 'Darkwave Synth');
  const params = c.currentParams();
  assert.equal(params.genre, 'Darkwave Synth');
  c.state.song.questions = [{ id: 'genre' }];
  c.state.questionsKey = c.questionsContextKey();
  assert.equal(c.questionsAreCurrent(), true);
  nodes.get('gw-genre-input').value = 'EBM Industrial';
  assert.equal(c.questionsAreCurrent(), false);
});

test('reference selection does not replace the writer sound profile', () => {
  const { c, nodes } = harness();
  c.applyBlueprintSpecs({ suggested_specs: { template_id: 99 } });
  assert.equal(nodes.get('gw-selected-template-id').value, '7');
  assert.equal(c.requestedGenre(), 'Progressive House / Vocal Trance');
});

test('changing original genre invalidates producer answers', () => {
  const { c, nodes } = harness();
  if (!c.state.song) c.state.song = {};
  c.state.song.questions = [{ id: 'genre' }];
  c.state.questionsKey = c.questionsContextKey();
  assert.equal(c.questionsAreCurrent(), true);
  nodes.get('gw-style-input').value = 'Indie';
  assert.equal(c.questionsAreCurrent(), false);
});

test('interview receives genre and template; stale genre replies are ignored', async () => {
  const { c, nodes } = harness();
  let resolve;
  let sent;
  c.postJSON = (url, body) => { sent = body; return new Promise(r => resolve = r); };
  const loading = c.loadInterviewQuestions();
  assert.equal(sent.genre, 'Progressive House / Vocal Trance');
  assert.equal(sent.template_id, 7);
  nodes.get('gw-style-input').value = 'Indie';
  resolve({ questions: [{ id: 'genre', default: 'Pop' }] });
  await loading;
  assert.equal((c.state.song.questions || []).length, 0);
});

test('updating an imported reference discards its in-flight producer questions', async () => {
  const { c } = harness();
  if (!c.state.song) c.state.song = {};
  c.state.song.reference_song = { id: -1, title: 'City Windows', genre: 'Darkwave' };
  let resolve;
  c.postJSON = () => new Promise(r => { resolve = r; });
  const loading = c.loadInterviewQuestions();
  c.state.referenceRevision++;
  c.invalidateQuestions();
  resolve({ questions: [{ id: 'stale-hook', default: 'the old chorus' }] });
  await loading;
  assert.equal((c.state.song.questions || []).find(q => q.id === 'stale-hook'), undefined);
  assert.equal(c.state.questionsKey, null);
});

test('genre belongs in the left creative column, including older saved interviews', () => {
  const { c, nodes } = harness();
  if (!c.state.song) c.state.song = {};
  c.state.song.questions = [
    { id: 'bpm', category: 'production', question: 'Tempo', default: '120', options: ['120'] },
    { id: 'genre', category: 'production', question: 'Choose a genre', default: 'Indie', options: ['Indie', 'Pop'] }
  ];
  c.renderQuestions();
  const html = nodes.get('gw-questions').innerHTML;
  const genrePosition = html.indexOf('id="qcard-genre"');
  assert.ok(genrePosition > html.indexOf('Creative direction'));
  assert.ok(genrePosition < html.indexOf('>Production</h3>'));
  assert.equal(html.split('id="qcard-genre"').length - 1, 1);
});

test('AI labels are visible but do not contaminate saved genre values', () => {
  const { c } = harness();
  const q = { id: 'genre', question: 'Choose a genre', default: 'Indie', options: ['Indie', 'Pop', 'Indie Folk'], option_labels: { 'Indie Folk': 'AI recommendation' } };
  if (!c.state.song) c.state.song = {};
  c.state.song.questions = [q];
  if (!c.state.song.answers) c.state.song.answers = {};
  c.state.song.answers.genre = 'Indie Folk';
  assert.match(c.renderQuestion(q, 0), /Indie Folk <small>\(AI recommendation\)<\/small>/);
  assert.equal(c.collectAnswers().genre, 'Indie Folk');
});
