// Cowriter 4-step wizard: step navigation, model selection, Step 1 storyline.

// ------------------------------------------------ Duet Toggle
function onVocalChanged() {
  const v = val('gw-vocal');
  const drawer = el('gw-duet-drawer');
  if (drawer) {
    drawer.style.display = (v === 'duet') ? 'block' : 'none';
  }
}
// ------------------------------------------------ Sequential 4-Step Wizard Navigation
function goToStep(stepNum) {
  // Step 4 drafts from the Step 3 answers; stale ones belong to another reference.
  if (stepNum === 4 && !questionsAreCurrent()) {
    stepNum = 3;
  }
  state.currentStep = stepNum;
  updateStepperUI();

  // Show only active step pane
  for (let i = 1; i <= 4; i++) {
    const pane = el('step-pane-' + i);
    if (pane) {
      if (i === stepNum) pane.classList.add('active');
      else pane.classList.remove('active');
    }
  }

  // Hook for step-specific data loading
  if (stepNum === 2) {
    const currentRecKey = [val('gw-prompt'), val('gw-style-input'), val('gw-bpm'), val('gw-key'), val('gw-scale'), val('gw-selected-template-id')].join('::');
    if (!state.recommendations.length || state.lastRecKey !== currentRecKey) {
      state.lastRecKey = currentRecKey;
      loadReferenceRecommendations();
    }
  } else if (stepNum === 3) {
    if (!questionsAreCurrent()) {
      loadInterviewQuestions();
    }
  }
}
function advanceToStep(stepNum) {
  // Enforce step completion prerequisites before advancing
  if (stepNum === 2) {
    const hasBrief = !!val('gw-prompt');
    const hasStory = !!val('gw-storyline-text');
    if (!hasBrief || !hasStory) {
      setStatus('gw-story-status', 'Please enter a song brief and generate a 3-act story before proceeding.', 'yellow');
      return;
    }
  } else if (stepNum === 3) {
    // Reference track is optional: if none is selected, advance cleanly using template/genre specs
    // and let Step 3 producer questions calibrate the song's intent.
    if (!state.referenceSong) {
      state.bypassedReference = true;
    }
  } else if (stepNum === 4) {
    if (!questionsAreCurrent()) {
      alert('Producer questions are loading for the current reference; answer them first.');
      goToStep(3);
      return;
    }
  }
  goToStep(stepNum);
}
function updateStepNavState() {
  // Step 1 Next Button: disabled until Brief and Storyline are present
  const s1Btn = el('btn-next-step-1');
  const hasBrief = !!val('gw-prompt');
  const hasStory = !!(val('gw-storyline-text') || state.storyline);
  const s1Ready = hasBrief && hasStory;
  if (s1Btn) {
    s1Btn.disabled = !s1Ready;
    s1Btn.style.opacity = s1Ready ? '1' : '0.45';
    s1Btn.style.cursor = s1Ready ? 'pointer' : 'not-allowed';
    s1Btn.title = s1Ready ? '' : 'Enter a song brief and generate a 3-act story to unlock Step 2';
  }

  // Step 2 Next Button: always accessible once Step 2 is unlocked (reference is optional)
  const s2Btn = el('btn-next-step-2');
  if (s2Btn) {
    s2Btn.disabled = false;
    s2Btn.style.opacity = '1';
    s2Btn.style.cursor = 'pointer';
    s2Btn.innerText = state.referenceSong ? 'Next: Producer Questions ➔' : 'Next: Producer Questions (Use Defaults) ➔';
  }

  // Step 3 Next Button: disabled until questions are loaded
  const s3Btn = el('btn-next-step-3');
  const s3Ready = !!(state.questions && state.questions.length > 0);
  if (s3Btn) {
    s3Btn.disabled = !s3Ready;
    s3Btn.style.opacity = s3Ready ? '1' : '0.45';
    s3Btn.style.cursor = s3Ready ? 'pointer' : 'not-allowed';
    s3Btn.title = s3Ready ? '' : 'Waiting for craft questions to load';
  }

  // Story Button Label Toggle: "Generate Story" -> "Regenerate Story"
  const genBtn = el('btn-gen-storyline');
  if (genBtn) {
    const hasStoryAlready = !!(val('gw-storyline-text') || state.storyline);
    const label = hasStoryAlready ? 'Regenerate Story' : 'Generate Story';
    genBtn.innerHTML = `<svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> ${label}`;
  }
}
function updateStepperUI() {
  for (let i = 1; i <= 4; i++) {
    const tabBtn = el('tab-step-' + i);
    const numEl = el('num-step-' + i);
    if (!tabBtn || !numEl) continue;

    const isActive = state.currentStep === i;
    const isCompleted = (i === 1 && state.storyline) ||
                        (i === 2 && (state.referenceSong || state.bypassedReference)) ||
                        (i === 3 && Object.keys(state.answers).length > 0) ||
                        (i === 4 && Object.keys(state.takes).length > 0);
    if (isActive) {
      tabBtn.className='step-tab active';
      numEl.innerText = i;
    } else if (isCompleted) {
      tabBtn.className='step-tab completed';
      numEl.innerHTML = '✓';
    } else {
      tabBtn.className='step-tab';
      numEl.innerText = i;
    }
  }
  updateStepNavState();
}
// ------------------------------------------------ Step 1: Generate Storyline
async function generateStoryline() {
  const brief = val('gw-prompt');
  if (!brief) {
    setStatus('gw-story-status', 'Please enter a song brief first.', 'yellow');
    return;
  }
  const tmplIdVal = val('gw-selected-template-id');
  const tmplId = tmplIdVal ? parseInt(tmplIdVal, 10) : null;
  const tmplTitle = el('gw-card-tmpl-title')?.innerText || '';
  const tmplGenre = el('gw-card-tmpl-badges')?.innerText || '';
  setStatus('gw-story-status', 'Generating 3-act narrative treatment...', 'yellow');
  try {
    const data = await postJSON('/api/ai/storyline', {
      title: val('gw-title') || '',
      brief: brief,
      style: val('gw-style-input') || '',
      genre: tmplGenre || val('gw-style-input') || '',
      bpm: parseFloat(val('gw-bpm')) || null,
      key: val('gw-key') || null,
      scale: val('gw-scale') || null,
      template_title: tmplTitle,
      template_id: tmplId,
      model: val('gw-model') || state.selectedModel || ''
    });
    el('gw-storyline-text').value = data.storyline;
    el('gw-storyline-container').style.display = 'flex';
    setStatus('gw-story-status', '3-Act storyline ready!', 'green');
    if (!state.unlockedSteps.includes(2)) state.unlockedSteps.push(2);
    updateStepperUI();
    updateStepNavState();
  } catch (err) {
    setStatus('gw-story-status', 'Storyline generation failed: ' + err.message, 'red');
  }
}
// ------------------------------------------------ Boot & Models
async function loadModels() {
  const sel = el('gw-model');
  const statusBadge = el('gw-model-status');
  try {
    const resp = await getJSON('/api/ai/models');
    const models = (resp && resp.models) || [];
    if (sel && models.length) {
      const currentVal = sel.value;
      const configuredDefault = (resp && resp.default_model) || '';
      sel.innerHTML = '<option value="" disabled' + (!currentVal ? ' selected' : '') + '>-- Select AI Model (Required) --</option>' +
        models.map(function opt(m) {
          return '<option value="' + escapeHtml(m) + '">' + escapeHtml(m) + '</option>';
        }).join('');
      // Priority: keep an explicit current selection, else the saved default
      // model, else the first available model.
      if (currentVal && models.includes(currentVal)) {
        sel.value = currentVal;
      } else if (configuredDefault && models.includes(configuredDefault)) {
        sel.value = configuredDefault;
      } else {
        sel.value = models[0];
      }
      state.selectedModel = sel.value;
      if (statusBadge) { statusBadge.innerText = 'Active: ' + sel.value; statusBadge.className = 'badge badge-success c-bright'; }
    } else {
      const reason = (resp && resp.error) || 'No models returned by the endpoint.';
      if (sel) sel.innerHTML = '<option value="" disabled selected>No models — ' + escapeHtml(reason) + '</option>';
      if (statusBadge) { statusBadge.innerText = 'LLM error'; statusBadge.className = 'badge badge-danger c-bright'; statusBadge.title = reason; }
      console.warn('Model list unavailable:', resp && resp.error, 'endpoint:', resp && resp.endpoint);
    }
  } catch (e) {
    if (sel) sel.innerHTML = '<option value="" disabled selected>Could not reach the server to load models.</option>';
    if (statusBadge) { statusBadge.innerText = 'LLM error'; statusBadge.className = 'badge badge-danger c-bright'; }
    console.warn('Could not fetch models:', e);
  }
}
function onModelSelected(modelName) {
  state.selectedModel = modelName;
  const statusBadge = el('gw-model-status');
  if (statusBadge) statusBadge.innerText = 'Active: ' + modelName;
}
