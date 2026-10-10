// Settings tab: LLM backend + generation defaults, persisted to the DB.

async function loadSettings() {
  try {
    const resp = await getJSON('/api/settings');
    const s = (resp && resp.settings) || {};
    const put = function (id, key) {
      const field = s[key];
      if (!field) return;
      const inp = el(id);
      if (inp && !field.is_secret) inp.value = field.value == null ? '' : field.value;
      const src = el(id + '-src');
      if (src) src.innerText = sourceLabel(field);
    };
    put('set-llm-endpoint', 'llm_endpoint');
    put('set-default-model', 'default_model');
    put('set-default-bpm', 'default_bpm');
    put('set-default-music-key', 'default_music_key');
    put('set-default-scale', 'default_scale');
    put('set-default-vocal', 'default_vocal');
    put('set-cliche-min-freq', 'cliche_min_freq');
    put('set-idiom-allowlist', 'idiom_allowlist');
    // Checkboxes: value is a truthy string.
    const truthy = function (v) { return String(v).toLowerCase() === 'true' || v === true || String(v) === '1'; };
    if (s['cliche_check_enabled'] && el('set-cliche-check-enabled')) el('set-cliche-check-enabled').checked = truthy(s['cliche_check_enabled'].value);
    if (s['melodic_delta_enabled'] && el('set-melodic-delta-enabled')) el('set-melodic-delta-enabled').checked = truthy(s['melodic_delta_enabled'].value);
    const vstat = el('validation-status');
    if (vstat) { vstat.innerText = 'threshold ' + ((s['cliche_min_freq'] && s['cliche_min_freq'].value) || '—'); vstat.className = 'badge badge-info'; }

    // API key: never populate the field; show whether one is configured + source.
    const keyField = s['llm_api_key'] || {};
    const keySrc = el('set-llm-api-key-src');
    if (keySrc) {
      keySrc.innerText = (keyField.configured ? 'Current key: ' + keyField.value + ' (' + keyField.source + '). ' : 'No key set. ')
        + 'Blank keeps it; type a new key to replace, "-" to clear.';
    }
    // Populate the default-model dropdown from the live endpoint.
    await populateSettingsModels(s['default_model'] && s['default_model'].value);
    await loadPrompts();
  } catch (e) {
    console.warn('Could not load settings:', e);
  }
}

function sourceLabel(field) {
  if (!field || !field.source) return '';
  if (field.source === 'db') return 'Saved in database.';
  if (field.source === 'env') return 'From environment variable (not yet overridden).';
  return 'Using built-in default.';
}

async function populateSettingsModels(selected) {
  const sel = el('set-default-model');
  if (!sel) return;
  try {
    const resp = await getJSON('/api/ai/models');
    const models = (resp && resp.models) || [];
    sel.innerHTML = '<option value="">(use first available)</option>' +
      models.map(function (m) { return '<option value="' + escapeHtml(m) + '">' + escapeHtml(m) + '</option>'; }).join('');
    if (selected) sel.value = selected;
    const status = el('settings-llm-status');
    if (status) {
      if (models.length) { status.innerText = models.length + ' models available'; status.className = 'badge badge-success'; }
      else { status.innerText = 'unreachable'; status.className = 'badge badge-danger'; status.title = (resp && resp.error) || ''; }
    }
  } catch (e) {
    const status = el('settings-llm-status');
    if (status) { status.innerText = 'error'; status.className = 'badge badge-danger'; }
  }
}

async function testLlmConnection() {
  const resEl = el('settings-test-result');
  if (resEl) { resEl.innerText = 'Testing...'; resEl.className = 'status-line'; }
  // Save first so the test uses the values currently in the form.
  await saveSettings(true);
  try {
    const resp = await getJSON('/api/ai/models');
    const models = (resp && resp.models) || [];
    if (resEl) {
      if (models.length) {
        resEl.innerText = 'OK — ' + models.length + ' models at ' + (resp.endpoint || '');
        resEl.className = 'status-line c-success';
      } else {
        resEl.innerText = 'Failed — ' + ((resp && resp.error) || 'no models returned');
        resEl.className = 'status-line c-danger';
      }
    }
    await populateSettingsModels(val('set-default-model'));
    if (typeof loadModels === 'function') loadModels();
  } catch (e) {
    if (resEl) { resEl.innerText = 'Failed — could not reach the server.'; resEl.className = 'status-line c-danger'; }
  }
}

async function saveSettings(silent) {
  const body = {
    llm_endpoint: val('set-llm-endpoint'),
    default_model: val('set-default-model'),
    default_bpm: parseInt(val('set-default-bpm'), 10) || undefined,
    default_music_key: val('set-default-music-key'),
    default_scale: val('set-default-scale'),
    default_vocal: val('set-default-vocal'),
    cliche_min_freq: parseInt(val('set-cliche-min-freq'), 10) || undefined,
    cliche_check_enabled: (el('set-cliche-check-enabled') && el('set-cliche-check-enabled').checked) ? 'true' : 'false',
    melodic_delta_enabled: (el('set-melodic-delta-enabled') && el('set-melodic-delta-enabled').checked) ? 'true' : 'false',
    idiom_allowlist: (el('set-idiom-allowlist') && el('set-idiom-allowlist').value) || '',
  };
  const keyVal = val('set-llm-api-key');
  if (keyVal) body.llm_api_key = keyVal; // blank keeps existing

  try {
    await putJSON('/api/settings', body);
    const kinp = el('set-llm-api-key');
    if (kinp) kinp.value = ''; // never retain the typed key in the field
    if (!silent) {
      const r = el('settings-save-result');
      if (r) { r.innerText = 'Saved.'; r.className = 'status-line c-success'; }
      const r2 = el('settings-test-result');
      if (r2) { r2.innerText = ''; }
      await loadSettings();
      if (typeof loadModels === 'function') loadModels();
    }
  } catch (e) {
    if (!silent) {
      const r = el('settings-save-result');
      if (r) { r.innerText = 'Save failed: ' + e; r.className = 'status-line c-danger'; }
    }
  }
}

// ------------------------------------------------ LLM Prompt editor
let prompts = {};        // key -> prompt object
let currentPromptKey = null;

async function loadPrompts() {
  try {
    const resp = await getJSON('/api/prompts');
    const list = (resp && resp.prompts) || [];
    prompts = {};
    const sel = el('prompt-select');
    if (sel) sel.innerHTML = '';
    list.forEach(function (p) {
      prompts[p.key] = p;
      if (sel) {
        const o = document.createElement('option');
        o.value = p.key;
        o.innerText = p.name;
        sel.appendChild(o);
      }
    });
    const status = el('prompt-status');
    if (status) { status.innerText = list.length + ' prompts'; status.className = 'badge badge-info'; }
    if (list.length) {
      const key = currentPromptKey && prompts[currentPromptKey] ? currentPromptKey : list[0].key;
      if (sel) sel.value = key;
      selectPrompt(key);
    }
  } catch (e) {
    console.warn('Could not load prompts:', e);
  }
}

function selectPrompt(key) {
  const p = prompts[key];
  if (!p) return;
  currentPromptKey = key;
  if (el('prompt-text')) el('prompt-text').value = p.template;
  if (el('prompt-desc')) el('prompt-desc').innerText = p.description || '';
  if (el('prompt-params')) {
    el('prompt-params').innerText = (p.params || []).map(function (x) { return '{' + x + '}'; }).join('   ');
  }
  const vsel = el('prompt-version-select');
  if (vsel) {
    vsel.innerHTML = (p.versions || []).map(function (v) {
      const label = v === 0 ? 'v0 (original)' : 'v' + v;
      return '<option value="' + v + '"' + (v === p.active ? ' selected' : '') + '>' + label + (v === p.active ? ' · active' : '') + '</option>';
    }).join('');
  }
}

async function savePrompt() {
  const key = currentPromptKey;
  const template = el('prompt-text') ? el('prompt-text').value : '';
  const res = el('prompt-result');
  try {
    const r = await putJSON('/api/prompts', { key: key, template: template });
    if (res) { res.innerText = 'Saved as v' + r.version + ' (active).'; res.className = 'status-line c-success'; }
    await loadPrompts();
  } catch (e) {
    if (res) { res.innerText = 'Save failed: ' + e; res.className = 'status-line c-danger'; }
  }
}

async function activatePromptVersion(version) {
  const key = currentPromptKey;
  const res = el('prompt-result');
  try {
    await fetch('/api/prompts/activate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: key, version: parseInt(version, 10) }),
    });
    if (res) { res.innerText = 'Activated v' + version + '.'; res.className = 'status-line c-success'; }
    await loadPrompts();
  } catch (e) {
    if (res) { res.innerText = 'Activate failed: ' + e; res.className = 'status-line c-danger'; }
  }
}

async function resetPrompt() {
  const key = currentPromptKey;
  if (!key) return;
  if (!confirm('Reset "' + (prompts[key] ? prompts[key].name : key) + '" to its original default?')) return;
  const res = el('prompt-result');
  try {
    await fetch('/api/prompts/' + encodeURIComponent(key) + '/reset', { method: 'POST' });
    if (res) { res.innerText = 'Reset to original.'; res.className = 'status-line c-success'; }
    await loadPrompts();
  } catch (e) {
    if (res) { res.innerText = 'Reset failed: ' + e; res.className = 'status-line c-danger'; }
  }
}
