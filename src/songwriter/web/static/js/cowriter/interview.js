// Cowriter Step 3: producer interview questions and answers.

// ------------------------------------------------ Parameters
// Keep the writer's genre independent from the reference's catalog label.
function requestedGenre() {
  const style = val('gw-style-input');
  const meta = (state.activeTemplate && state.activeTemplate.metadata) || {};
  if (meta.genre && (style === meta.audio_prompt || style === meta.title)) {
    return [meta.genre, meta.subgenre].filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(' / ');
  }
  return style.split(',')[0].split('(')[0].trim();
}
function currentParams() {
  syncSongFromDOM(state.song);
  if (state.song) {
    state.song.answers = collectAnswers();
  }
  return state.song;
}
// ------------------------------------------------ Step 3: Producer Questions
// The questions, their defaults and the answers are written for one brief,
// story and reference song. This key records which; when any of them changes
// the questions are stale and must be regenerated, or the old reference's
// details (its sections, hook, scheme) leak into the draft through the answers.
function questionsContextKey() {
  const ref = (state.song && state.song.reference_song) || {};
  return [
    ref.id || ((ref.title || '') + '|' + (ref.artist || '')),
    state.referenceRevision,
    val('gw-prompt') || (state.song && state.song.brief) || '',
    val('gw-storyline-text') || (state.song && state.song.story) || '',
    requestedGenre(),
    val('gw-style-input'),
    val('gw-selected-template-id'),
    ref.genre || '',
  ].join('::');
}
function questionsAreCurrent() {
  return !!(state.song && state.song.questions && state.song.questions.length) && state.questionsKey === questionsContextKey();
}
// Drop questions and answers so Step 3 regenerates them for the new context.
function invalidateQuestions() {
  if (state.song) {
    state.song.questions = [];
    state.song.answers = {};
  }
  state.questionsKey = null;
  state.unlockedSteps = state.unlockedSteps.filter(function(s) { return s !== 4; });
  renderQuestions();
  setStatus('gw-interview-status', 'Reference or story changed: questions will be regenerated.', 'yellow');
}
async function loadInterviewQuestions() {
  const key = questionsContextKey();
  const host = el('gw-questions');
  if (!host) return;
  host.innerHTML = '<div class="empty-state">Generating brief-specific craft questions...</div>';
  const producerModel = (val('gw-model') || (state.song && state.song.model) || '').trim();
  setStatus('gw-interview-status', 'Consulting ' + (producerModel || 'the model') + '...', 'yellow');
  try {
    syncSongFromDOM(state.song);
    const returnedSong = await postJSON('/api/ai/interview?use_model=true', state.song);
    if (key !== questionsContextKey()) return;
    if (returnedSong) {
      Object.assign(state.song, returnedSong);
    }
    state.questionsKey = key;
    if (!state.song.answers || typeof state.song.answers !== 'object') {
      state.song.answers = {};
    }
    for (const q of (state.song.questions || [])) {
      if (state.song.answers[q.id] == null && q.default != null) {
        state.song.answers[q.id] = String(q.default);
      }
    }
    renderQuestions();
    setStatus('gw-interview-status', `${(state.song.questions || []).length} questions ready.`, 'green');
    if (!state.unlockedSteps.includes(4)) state.unlockedSteps.push(4);
    updateStepperUI();
  } catch (err) {
    host.innerHTML = '<div class="issue">Failed to load craft questions: ' + escapeHtml(err.message) + '</div>';
    setStatus('gw-interview-status', 'Could not load questions.', 'red');
  }
}
function renderQuestions() {
  const host = el('gw-questions');
  if (!host) return;
  const questions = (state.song && state.song.questions) || [];
  if (!questions.length) {
    host.innerHTML = '<div class="empty-state">No questions yet.</div>';
    return;
  }
  // Older saved interviews classified genre as production; keep it on the left too.
  const creative = questions.filter(function(q) { return q.id === 'genre' || q.category === 'creative' || !q.category; });
  const production = questions.filter(function(q) { return q.id !== 'genre' && q.category === 'production'; });
  function column(title, items, startIdx) {
    if (!items.length) return '';
    return '<div class="qcol"><h3 class="qgroup-title">' + title + '</h3>' +
      items.map(function(q, i) { return renderQuestion(q, startIdx + i); }).join('') + '</div>';
  }
  host.innerHTML =
    column('Creative direction', creative, 0) +
    column('Production', production, creative.length);
}
function renderQuestion(q, idx) {
  const current = String((state.song && state.song.answers && state.song.answers[q.id]) == null ? q.default : state.song.answers[q.id]);
  const name = 'q-' + q.id;

  let controlsHtml = '';
  if (q.id === 'bpm' || q.kind === 'number') {
    const minVal = q.min || 10;
    const maxVal = q.max || 350;
    const curNum = parseInt(current.match(/\d+/) ? current.match(/\d+/)[0] : '120', 10);
    const options = (q.options || []).map(function(opt) {
      const v = String(opt);
      const checked = (v === current || (v.match(/\d+/) && parseInt(v.match(/\d+/)[0], 10) === curNum)) ? ' checked' : '';
      return '<label class="qopt">' +
        '<input type="radio" name="' + escapeHtml(name) + '" value="' + escapeHtml(v) + '"' +
        ' data-qid="' + escapeHtml(q.id) + '"' + checked + '>' +
        '<span>' + escapeHtml(v) + ' BPM</span></label>';
    }).join('');

    controlsHtml = `
      <div class="flex items-center gap-12 mb-8 flex-wrap">
        <input type="range" id="q-bpm-range" min="${minVal}" max="${maxVal}" value="${curNum}" class="flex-1 min-w-160 cursor-pointer"
               oninput="onBpmSliderInput(this.value)">
        <div class="flex items-center gap-6">
          <input type="number" id="q-bpm-number" min="${minVal}" max="${maxVal}" value="${curNum}" class="w-80 fs-md fw-700 text-center"
                 oninput="onBpmNumberInput(this.value)">
          <span class="fs-xs fw-600 c-muted">BPM</span>
        </div>
      </div>
      <div class="qopts">${options}</div>
    `;
  } else {
    const options = (q.options || []).map(function(opt) {
      const v = String(opt);
      return '<label class="qopt">' +
        '<input type="radio" name="' + escapeHtml(name) + '" value="' + escapeHtml(v) + '"' +
        ' data-qid="' + escapeHtml(q.id) + '"' + (v === current ? ' checked' : '') + '>' +
        '<span>' + escapeHtml(v) + (q.option_labels && q.option_labels[v] ? ' <small>(' + escapeHtml(q.option_labels[v]) + ')</small>' : '') + '</span></label>';
    }).join('');
    controlsHtml = '<div class="qopts">' + options + '</div>';
  }

  return '<fieldset class="qitem" id="qcard-' + escapeHtml(q.id) + '">' +
    '<legend><span class="qnum">' + String(idx + 1).padStart(2, '0') + '</span>' + escapeHtml(q.question) + '</legend>' +
    (q.why ? '<p class="qhint">' + escapeHtml(q.why) + '</p>' : '') +
    controlsHtml +
    '</fieldset>';
}
function onBpmSliderInput(val) {
  const num = parseInt(val, 10);
  if (isNaN(num)) return;
  const numEl = el('q-bpm-number');
  if (numEl) numEl.value = num;
  selectQuestionOption('bpm', String(num));
}
function onBpmNumberInput(val) {
  const num = parseInt(val, 10);
  if (isNaN(num)) return;
  const rangeEl = el('q-bpm-range');
  if (rangeEl) rangeEl.value = num;
  selectQuestionOption('bpm', String(num));
}
// One delegated listener: native radios carry the value, so no text matching.
document.addEventListener('change', function(e) {
  const t = e.target;
  if (t && t.matches && t.matches('#gw-questions input[type=radio][data-qid]')) {
    selectQuestionOption(t.dataset.qid, t.value);
  }
});
function selectQuestionOption(qId, valStr) {
  if (!state.song) state.song = {};
  if (!state.song.answers) state.song.answers = {};
  state.song.answers[qId] = valStr;
  const isFollowRef = /^follow reference/i.test(valStr);
  if (qId === 'syllable_target' && state.song) {
    state.song.follow_reference_syllables = isFollowRef;
  } else if (qId === 'section_lines' && state.song) {
    state.song.follow_reference_lines = isFollowRef;
  } else if (qId === 'rhyme_scheme' && state.song) {
    state.song.follow_reference_rhyme = isFollowRef;
  }
  // "Follow reference ..." answers keep the reference's own shape; the backend
  // resolves them, so don't parse fragments like "Follow" into the Step 2 fields.
  if (isFollowRef) return;
  if (qId === 'bpm') {
    const nums = valStr.match(/\d+/);
    if (nums) {
      setFieldValue('gw-bpm', nums[0]);
      setFieldValue('gw-bpm-input', nums[0]);
      if (state.song) state.song.bpm = parseInt(nums[0], 10);
    }
  } else if (qId === 'musical_key') {
    const parts = valStr.split(' ');
    if (parts.length >= 2) {
      const rootKey = parts[0].trim();
      const scaleMode = parts[1].replace(/[\(\)]/g, '').toLowerCase().trim();
      setFieldValue('gw-key', rootKey);
      setFieldValue('gw-scale', scaleMode);
      setFieldValue('gw-key-select', rootKey);
      setFieldValue('gw-scale-select', scaleMode);
      if (state.song) {
        state.song.key = rootKey;
        state.song.scale = scaleMode;
      }
    }
  } else if (qId === 'syllable_target') {
    setFieldValue('gw-syllables', valStr.split(' ')[0]);
    if (state.song) {
      const nums = valStr.match(/\d+/g);
      if (nums && nums.length >= 2) {
        state.song.syllable_min = Math.min(parseInt(nums[0], 10), parseInt(nums[1], 10));
        state.song.syllable_max = Math.max(parseInt(nums[0], 10), parseInt(nums[1], 10));
      }
    }
  } else if (qId === 'rhyme_scheme') {
    const sch = valStr.split(' ')[0];
    setFieldValue('gw-scheme', sch);
    if (state.song) state.song.rhyme_scheme = sch;
  } else if (qId === 'section_plan') {
    const planKey = valStr.split(' ')[0].toLowerCase();
    setFieldValue('gw-plan', planKey);
  } else if (qId === 'vocal') {
    const vKey = valStr.split(' ')[0].toLowerCase();
    setFieldValue('gw-vocal', vKey);
    if (state.song) state.song.vocal = vKey;
    onVocalChanged();
  }
}
function collectAnswers() {
  const answers = {};
  const questions = (state.song && state.song.questions) || [];
  const songAnswers = (state.song && state.song.answers) || {};
  for (const q of questions) {
    const v = String(songAnswers[q.id] || '').trim();
    if (v) {
      answers[q.id] = v;
    }
  }
  return answers;
}
