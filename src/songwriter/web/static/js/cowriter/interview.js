// Cowriter Step 3: producer interview questions and answers.

// ------------------------------------------------ Parameters
function currentParams() {
  const bannedRaw = val('gw-banned');
  const banned = bannedRaw
    ? bannedRaw.split(',').map(function trim(s) { return s.trim(); }).filter(Boolean)
    : null;
  const bpmRaw = parseInt(val('gw-bpm'), 10);
  const tmplRaw = parseInt(val('gw-selected-template-id'), 10);
  const scheme = val('gw-scheme');
  const vocal = val('gw-vocal');
  let vocalRoles = null;
  if (vocal === 'duet') {
    vocalRoles = {
      singer_a: val('gw-singer-a') || 'Singer A (Male Vocal)',
      singer_b: val('gw-singer-b') || 'Singer B (Female Vocal)'
    };
  }

  return {
    title: val('gw-title') || 'Untitled Song',
    prompt: val('gw-prompt'),
    style: val('gw-style-input') || null,
    template_id: isNaN(tmplRaw) ? null : tmplRaw,
    reference_song: state.referenceSong || null,
    bpm: isNaN(bpmRaw) ? null : bpmRaw,
    music_key: val('gw-key') || null,
    scale: val('gw-scale') || null,
    vocal: vocal || null,
    vocal_roles: vocalRoles,
    banned_words: banned,
    rhyme_scheme: scheme === 'free' ? null : scheme,
    section_plan: val('gw-plan') || null,
    syllable_target: val('gw-syllables') || null,
    engine_target: val('gw-engine') || 'standard',
    answers: collectAnswers(),
    questions: state.questions.map(function(q) {
      return { id: q.id, question: q.question, why: q.why || '', options: q.options || [], category: q.category || 'creative' };
    }),
    song_id: state.editingSongId || null,
    model: val('gw-model') || state.selectedModel || ''
  };
}
// ------------------------------------------------ Step 3: Producer Questions
// The questions, their defaults and the answers are written for one brief,
// story and reference song. This key records which; when any of them changes
// the questions are stale and must be regenerated, or the old reference's
// details (its sections, hook, scheme) leak into the draft through the answers.
function questionsContextKey() {
  const ref = state.referenceSong || {};
  return [
    ref.id || ((ref.title || '') + '|' + (ref.artist || '')),
    val('gw-prompt') || '',
    val('gw-storyline-text') || state.storyline || '',
  ].join('::');
}
function questionsAreCurrent() {
  return !!(state.questions && state.questions.length) && state.questionsKey === questionsContextKey();
}
// Drop questions and answers so Step 3 regenerates them for the new context.
function invalidateQuestions() {
  state.questions = [];
  state.answers = {};
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
  setStatus('gw-interview-status', 'Consulting AI producer...', 'yellow');
  try {
    const data = await postJSON('/api/ai/interview', {
      brief: val('gw-prompt') || 'An original song',
      style: val('gw-style-input') || '',
      reference_song: state.referenceSong || null,
      story: val('gw-storyline-text') || state.storyline || '',
      model: val('gw-model') || state.selectedModel || '',
      use_model: true
    });
    // A newer request (context changed again mid-flight) wins; drop this reply.
    if (key !== questionsContextKey()) return;
    state.questions = data.questions || [];
    state.questionsKey = key;
    state.defaults = data.defaults || {};
    state.answers = {};
    for (const q of state.questions) {
      state.answers[q.id] = String(q.default || '');
    }
    renderQuestions();
    setStatus('gw-interview-status', `${state.questions.length} questions ready.`, 'green');
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
  if (!state.questions.length) {
    host.innerHTML = '<div class="empty-state">No questions yet.</div>';
    return;
  }
  const creative = state.questions.filter(function(q) { return q.category === 'creative' || !q.category; });
  const production = state.questions.filter(function(q) { return q.category === 'production'; });
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
  const current = String(state.answers[q.id] == null ? q.default : state.answers[q.id]);
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
        '<span>' + escapeHtml(v) + '</span></label>';
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
  state.answers[qId] = valStr;
  // "Follow reference ..." answers keep the reference's own shape; the backend
  // resolves them, so don't parse fragments like "Follow" into the Step 2 fields.
  if (/^follow reference/i.test(valStr)) return;
  if (qId === 'bpm') {
    const nums = valStr.match(/\d+/);
    if (nums) {
      setFieldValue('gw-bpm', nums[0]);
      setFieldValue('gw-bpm-input', nums[0]);
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
    }
  } else if (qId === 'syllable_target') {
    setFieldValue('gw-syllables', valStr.split(' ')[0]);
  } else if (qId === 'rhyme_scheme') {
    setFieldValue('gw-scheme', valStr.split(' ')[0]);
  } else if (qId === 'section_plan') {
    const planKey = valStr.split(' ')[0].toLowerCase();
    setFieldValue('gw-plan', planKey);
  } else if (qId === 'vocal') {
    const vKey = valStr.split(' ')[0].toLowerCase();
    setFieldValue('gw-vocal', vKey);
    onVocalChanged();
  }
}
function collectAnswers() {
  const answers = {};
  for (const q of state.questions) {
    const v = String(state.answers[q.id] || '').trim();
    if (v) {
      answers[q.id] = {
        question: q.question || q.id,
        answer: v,
        category: q.category || 'creative'
      };
    }
  }
  return answers;
}
