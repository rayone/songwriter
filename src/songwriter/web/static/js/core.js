// Shared state, DOM/HTTP helpers and top-level tab switching. Loaded first.

var state = {
  currentStep: 1,
  unlockedSteps: [1],
  storyline: '',
  recommendations: [],
  searchResults: [],
  questions: [],
  answers: {},
  defaults: {},
  takes: {},
  nextTakeId: 1,
  activeTakeId: null,
  activeBlueprint: null,
  referenceSong: null,
  editingSongId: null,
  songs: [],
  songSort: { key: null, dir: 1 },
  vocab: { genre: [], subgenre: [], vocal: [] }
};
window.state = state;
function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function el(id) {
  return document.getElementById(id);
}
function val(id) {
  const node = el(id);
  return node ? String(node.value || '').trim() : '';
}
function setStatus(id, text, color) {
  const node = el(id);
  if (!node) return;
  node.innerText = text;
  node.style.color = color === 'green' ? 'var(--emerald)' : (color === 'red' ? 'var(--rose)' : (color === 'yellow' ? 'var(--amber)' : 'var(--text-dim)'));
}
async function getJSON(url) {
  // no-store: fetch() uses the HTTP cache by default, so a GET response cached
  // before an API shape change gets replayed and the new code misreads it. That
  // is what made a known word report "not in the dictionary" -- the cached body
  // was the old bare array, which has no `known` field.
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try {
      const err = await res.json();
      if (err && err.detail) detail = err.detail;
    } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}
async function putJSON(url, body) {
  const res = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try {
      const err = await res.json();
      if (err && err.detail) detail = err.detail;
    } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}
function debounce(fn, ms) {
  let timer = null;
  return function debounced() {
    const args = arguments;
    clearTimeout(timer);
    timer = setTimeout(function run() {
      fn.apply(null, args);
    }, ms);
  };
}
// ------------------------------------------------ Main Tabs
function switchTab(tabId) {
  const panels = document.querySelectorAll('.tab-content');
  for (const panel of panels) panel.classList.remove('active');
  const target = el('tab-' + tabId);
  if (target) target.classList.add('active');

  const buttons = document.querySelectorAll('nav button');
  for (const btn of buttons) {
    if (btn.dataset.tab === tabId) btn.classList.add('active');
    else btn.classList.remove('active');
  }
  if (tabId === 'rhymes') loadRhymeDbStats();
  if (tabId === 'crafted') loadCraftedSongsList();
  if (tabId === 'settings' && typeof loadSettings === 'function') loadSettings();
}
function setFieldValue(id, value) {
  const node = el(id);
  if (!node || value == null || value === '') return;
  const v = String(value);
  if (node.tagName === 'SELECT') {
    const present = Array.from(node.options).some(function has(o) { return o.value === v; });
    if (!present) {
      const opt = document.createElement('option');
      opt.value = v;
      opt.innerText = v;
      node.insertBefore(opt, node.firstChild);
    }
  }
  node.value = v;
}
function formatLyricsWithBadges(lyrics) {
  const lines = (lyrics || '').split('\n');
  const out = [];
  let lineNum = 1;
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith('[') && line.endsWith(']')) {
      const inner = line.slice(1, -1).trim();
      const low = inner.toLowerCase();
      let cls = 'sec-default';
      if (low.includes('singer a') || low.includes('male')) cls = 'sec-singer-a';
      else if (low.includes('singer b') || low.includes('female')) cls = 'sec-singer-b';
      else if (low.includes('duet') || low.includes('both') || low.includes('harmony')) cls = 'sec-harmony';
      out.push('<span class="sheet-section-header ' + cls +'">[' + escapeHtml(inner) + ']</span>');
    } else if (line) {
      let styledLine = escapeHtml(line);
      styledLine = styledLine.replace(/\((Both)\)/gi, '<b class="c-harmony">(Both)</b>');
      styledLine = styledLine.replace(/\((Singer A[^\)]*)\)/gi, '<b class="c-singer-a">($1)</b>');
      styledLine = styledLine.replace(/\((Singer B[^\)]*)\)/gi, '<b class="c-singer-b">($1)</b>');
      const numPrefix = `<span class="mono fs-xs c-dim line-num">${String(lineNum).padStart(2, '0')}</span>`;
      out.push(numPrefix + styledLine);
      lineNum++;
    } else {
      out.push('');
    }
  }
  return out.join('\n');
}
function copyBox(boxId, btn) {
  const box = el(boxId);
  if (!box) return;
  copyText(box.value || box.innerText, btn);
}
async function copyText(text, btn) {
  const label = btn ? btn.innerText : '';
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {}
  if (btn) {
    btn.innerText = 'Copied!';
    setTimeout(function restore() { btn.innerText = label; }, 1200);
  }
}
