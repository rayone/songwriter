// Shared state, DOM/HTTP helpers and top-level tab switching. Loaded first.

function deepClone(obj) {
  if (obj === null || typeof obj !== 'object') return obj;
  if (typeof structuredClone === 'function') {
    try {
      return structuredClone(obj);
    } catch (e) {}
  }
  try {
    return JSON.parse(JSON.stringify(obj));
  } catch (e) {
    return Object.assign({}, obj);
  }
}
if (typeof window !== 'undefined') window.deepClone = deepClone;

let defaultSongTemplate = {};

async function loadSongDefaults() {
  try {
    const data = await getJSON('/api/ai/song_defaults');
    if (data && typeof data === 'object') {
      defaultSongTemplate = deepClone(data);
      if (!state.song || !Object.keys(state.song).length) {
        state.song = deepClone(defaultSongTemplate);
      } else {
        for (const [k, v] of Object.entries(defaultSongTemplate)) {
          if (state.song[k] === undefined) {
            state.song[k] = deepClone(v);
          }
        }
      }
    }
  } catch (e) {
    console.error('Failed to load song defaults from /api/ai/song_defaults:', e);
    if (typeof setStatus === 'function') {
      setStatus('gw-status', 'Warning: could not load song defaults (' + (e.message || e) + ')', 'red');
    }
  }
}

function getEnginePromptDisplay(song, engine) {
  if (!song) return '';
  const ep = song.engine_prompts || {};
  const prompt = ep[engine] || '';
  const lyrics = song.lyrics || '';
  if (engine === 'yue2') {
    let out = prompt;
    if (lyrics) out = (out ? out + '\n\n' : '') + lyrics;
    if (song.abc_score) out = (out ? out + '\n\n' : '') + '--- ABC SCORE ---\n' + song.abc_score;
    return out;
  }
  if (!prompt) return lyrics;
  return lyrics ? prompt + '\n\n' + lyrics : prompt;
}

function syncSongFromDOM(song) {
  if (!song) song = state.song;
  if (!song) return song;

  const titleEl = el('gw-title');
  if (titleEl) song.title = titleEl.value.trim();

  const briefEl = el('gw-prompt');
  if (briefEl) song.brief = briefEl.value.trim();

  const storyEl = el('gw-storyline-text');
  if (storyEl) song.story = storyEl.value.trim();

  const modelEl = el('gw-model');
  if (modelEl) {
    const m = modelEl.value.trim();
    if (m) song.model = m;
  }

  const styleEl = el('gw-style-input');
  if (styleEl) {
    const style = styleEl.value.trim();
    if (style !== song.style && typeof requestedGenre === 'function') {
      song.genre = requestedGenre();
    }
    song.style = style;
  }

  const tmplEl = el('gw-selected-template-id');
  if (tmplEl) {
    const tmplVal = tmplEl.value.trim();
    if (tmplVal) {
      const tmplId = parseInt(tmplVal, 10);
      song.template_id = isNaN(tmplId) ? null : tmplId;
    } else {
      song.template_id = null;
    }
  }

  const bpmEl = el('gw-bpm');
  if (bpmEl) {
    const bpmVal = bpmEl.value.trim();
    if (bpmVal) {
      const bpmNum = parseInt(bpmVal, 10);
      if (!isNaN(bpmNum)) song.bpm = bpmNum;
    }
  }

  const keyEl = el('gw-key');
  if (keyEl && keyEl.value.trim()) song.key = keyEl.value.trim();

  const scaleEl = el('gw-scale');
  if (scaleEl && scaleEl.value.trim()) song.scale = scaleEl.value.trim();

  const vocalEl = el('gw-vocal');
  if (vocalEl && vocalEl.value.trim()) {
    song.vocal = vocalEl.value.trim();
    if (song.vocal === 'duet') {
      song.vocal_roles = {
        singer_a: val('gw-singer-a') || 'Singer A (Male Vocal)',
        singer_b: val('gw-singer-b') || 'Singer B (Female Vocal)'
      };
    }
  }

  const sylEl = el('gw-syllables');
  if (sylEl && sylEl.value.trim()) {
    const nums = sylEl.value.trim().match(/\d+/g);
    if (nums && nums.length >= 2) {
      song.syllable_min = Math.min(parseInt(nums[0], 10), parseInt(nums[1], 10));
      song.syllable_max = Math.max(parseInt(nums[0], 10), parseInt(nums[1], 10));
    } else if (nums && nums.length === 1) {
      const n = parseInt(nums[0], 10);
      song.syllable_min = Math.max(1, n - 1);
      song.syllable_max = n + 1;
    }
  }

  const schemeEl = el('gw-scheme');
  if (schemeEl && schemeEl.value.trim()) song.rhyme_scheme = schemeEl.value.trim();

  const planEl = el('gw-plan');
  if (planEl) {
    const planVal = planEl.value.trim();
    if (planVal) {
      if (planVal.includes('[')) {
        const matches = planVal.match(/\[([^\]]+)\]/g);
        if (matches) {
          song.section_plan = matches.map(s => s.slice(1, -1).trim());
        }
      } else {
        song.section_plan = planVal.split(',').map(s => s.trim()).filter(Boolean);
      }
    } else {
      song.section_plan = [];
    }
  }

  const bannedEl = el('gw-banned');
  if (bannedEl) {
    const bannedVal = bannedEl.value.trim();
    song.banned_pairs = bannedVal ? bannedVal.split(',').map(s => s.trim()).filter(Boolean) : [];
  }

  const engineEl = el('gw-engine');
  if (engineEl && engineEl.value.trim()) song.engine_target = engineEl.value.trim();

  return song;
}

function syncDOMFromSong(song) {
  if (!song) song = state.song;
  if (!song) return;

  setFieldValue('gw-title', song.title || '');
  setFieldValue('gw-prompt', song.brief || '');
  if (el('gw-storyline-text')) el('gw-storyline-text').value = song.story || '';
  if (el('gw-storyline-container')) el('gw-storyline-container').style.display = song.story ? 'flex' : 'none';

  if (song.model) {
    const sel = el('gw-model');
    if (sel && !Array.from(sel.options).some(o => o.value === song.model)) {
      sel.add(new Option(song.model + ' (saved)', song.model));
    }
    setFieldValue('gw-model', song.model);
    if (typeof onModelSelected === 'function') onModelSelected(song.model);
  }

  if (song.template_id && typeof showTemplateCard === 'function') {
    showTemplateCard(song.template_id, song.style !== undefined && song.style !== null);
  } else if (typeof clearActiveTemplate === 'function') {
    clearActiveTemplate(true);
  } else {
    setFieldValue('gw-selected-template-id', '');
    const card = el('gw-selected-template-card');
    if (card) card.style.display = 'none';
    const clearBtn = el('gw-template-clear-btn');
    if (clearBtn) clearBtn.style.display = 'none';
  }
  setFieldValue('gw-style-input', song.style ?? song.genre ?? '');

  setFieldValue('gw-bpm', song.bpm != null ? song.bpm : '');
  setFieldValue('gw-bpm-input', song.bpm != null ? song.bpm : '');
  setFieldValue('gw-key', song.key || '');
  setFieldValue('gw-key-select', song.key || '');
  setFieldValue('gw-scale', song.scale || '');
  setFieldValue('gw-scale-select', song.scale || '');
  setFieldValue('gw-vocal', song.vocal || '');
  if (song.vocal_roles) {
    if (song.vocal_roles.singer_a) setFieldValue('gw-singer-a', song.vocal_roles.singer_a);
    if (song.vocal_roles.singer_b) setFieldValue('gw-singer-b', song.vocal_roles.singer_b);
  }
  if (typeof onVocalChanged === 'function') onVocalChanged();

  if (song.syllable_min != null && song.syllable_max != null) {
    setFieldValue('gw-syllables', `${song.syllable_min}-${song.syllable_max}`);
  } else {
    setFieldValue('gw-syllables', '');
  }
  setFieldValue('gw-scheme', song.rhyme_scheme || '');

  setFieldValue('gw-plan', Array.isArray(song.section_plan) && song.section_plan.length
    ? song.section_plan.map(n => n.startsWith('[') ? n : `[${n}]`).join(', ')
    : '');

  setFieldValue('gw-banned', Array.isArray(song.banned_pairs) ? song.banned_pairs.join(', ') : '');

  if (song.reference_song && typeof renderSelectedReference === 'function') {
    renderSelectedReference({
      reference_song: song.reference_song,
      suggested_specs: { section_plan: (song.section_plan || []).join(', ') }
    });
  } else if (typeof clearSelectedReference === 'function') {
    clearSelectedReference();
  }

  if (Array.isArray(song.questions) && song.questions.length) {
    if (typeof questionsContextKey === 'function') {
      state.questionsKey = questionsContextKey();
    }
    if (typeof renderQuestions === 'function') renderQuestions();
  }
}

var state = {
  currentStep: 1,
  unlockedSteps: [1],
  recommendations: [],
  searchResults: [],
  takes: {},
  nextTakeId: 1,
  activeTakeId: null,
  activeBlueprint: null,
  referenceRevision: 0,
  songs: [],
  songSort: { key: null, dir: 1 },
  vocab: { genre: [], subgenre: [], vocal: [] },
  song: {}
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
async function postJSON(url, body, signal) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal
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
  if (!node) return;
  if (value == null || value === '') {
    node.value = '';
    return;
  }
  const v = String(value);
  if (node.tagName === 'SELECT' && node.options) {
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
