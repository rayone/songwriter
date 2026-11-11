// Cowriter Step 2: reference song search, recommendations and inspector.

// ------------------------------------------------ Step 2: Unified Song Card Renderer, Recommendations & Live Search
function renderSongCard(s, isRecommendation = false) {
  const sub = (s.year ? s.year + ' • ' : '') +
              (s.preview ? s.preview.replace(/\n+/g, ' / ').slice(0, 85) + '...' : 'Reference track');
  const bpmBadge = s.bpm ? `<span class="badge badge-warning badge-xs">${Math.round(s.bpm)} BPM</span>` : '';
  const keyBadge = s.key ? `<span class="badge badge-secondary badge-xs">${escapeHtml(s.key)}${s.scale ? ' ' + escapeHtml(s.scale) : ''}</span>` : '';
  const yearBadge = s.year ? `<span class="badge badge-dim badge-xs">${s.year}</span>` : '';
  const midiBadge = s.has_midi ? `<span class="badge badge-success badge-xs" title="Imported MIDI: YuE2 ABC score available">♪ MIDI</span>` : '';
  const importedBadge = s.song_id < 0 ? '<span class="badge badge-info badge-xs">Imported</span>' : '';
  const guidanceHtml = s.guidance
    ? `<div class="ref-choice-guidance mt-8">${escapeHtml(s.guidance)}</div>`
    : `<div class="ref-choice-guidance mt-8 c-muted fs-xs">${s.song_id < 0 ? 'Imported' : 'Corpus'} reference track</div>`;
  const selectAction = 'selectReferenceSong(' + JSON.stringify(s.song_id ?? null) + ', ' + JSON.stringify(s.title || '') + ', ' + JSON.stringify(s.artist || '') + ')';

  return `
    <div class="card">
      <div>
        <div class="ref-choice-head">
          <div>
            <div class="ref-choice-title">${escapeHtml(s.title)}</div>
            <div class="ref-choice-artist">${escapeHtml(s.artist)}</div>
          </div>
          <div class="ref-badges gap-4 justify-end">
            <span class="badge badge-info">${escapeHtml(s.genre || 'Song')}</span>
            ${bpmBadge}
            ${keyBadge}
            ${yearBadge}
            ${midiBadge}
            ${importedBadge}
          </div>
        </div>
        ${guidanceHtml}
        <div class="fs-xs c-muted mt-6 lh-14">${escapeHtml(sub)}</div>
      </div>
      <div class="flex gap-8 items-center mt-8">
        <button type="button" class="btn-primary btn-sm" onclick="${escapeHtml(selectAction)}">Select Reference</button>
        <a href="https://www.youtube.com/results?search_query=${encodeURIComponent(s.title + ' ' + s.artist)}" target="_blank" rel="noopener noreferrer" class="btn-secondary btn-sm">Listen on YouTube ↗</a>
      </div>
    </div>
  `;
}
async function loadReferenceRecommendations() {
  const grid = el('gw-recommendations-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="empty-state">Finding reference matches...</div>';
  try {
    const bpmVal = parseInt(val('gw-bpm'), 10) || (state.defaults && state.defaults.bpm) || null;
    const keyVal = val('gw-key') || (state.defaults && state.defaults.music_key) || null;
    const scaleVal = val('gw-scale') || (state.defaults && state.defaults.scale) || null;
    const tmplIdVal = parseInt(val('gw-selected-template-id'), 10) || null;
    const briefVal = val('gw-prompt') || '';
    const storyVal = val('gw-storyline-text') || state.storyline || '';
    const styleVal = val('gw-style-input') || '';

    const hasMidi = el('gw-ref-has-midi') && el('gw-ref-has-midi').checked;
    const recs = await postJSON('/api/ai/recommend_references', {
      brief: briefVal,
      story: storyVal,
      style: styleVal,
      genre: requestedGenre(),
      bpm: bpmVal,
      key: keyVal,
      scale: scaleVal,
      template_id: tmplIdVal,
      limit: 4,
      has_midi: hasMidi
    });
    state.recommendations = recs || [];
    renderRecommendations(state.recommendations);
  } catch (err) {
    grid.innerHTML = '<div class="empty-state">Could not load recommendations. Search manually below.</div>';
  }
}
function renderRecommendations(recs) {
  const grid = el('gw-recommendations-grid');
  if (!grid) return;
  if (!recs || !recs.length) {
    grid.innerHTML = '<div class="empty-state">No recommendations found. Use search below.</div>';
    return;
  }
  grid.innerHTML = recs.map(r => renderSongCard(r, true)).join('');
}

// The "Has MIDI score only" checkbox affects both the curated recommendations
// and the manual search, so refresh whichever is showing.
function onRefMidiToggle() {
  loadReferenceRecommendations();
  if (val('gw-ref-search-input').trim()) searchReferenceSongs();
}
async function searchReferenceSongs() {
  const q = val('gw-ref-search-input').trim();
  const statusEl = el('gw-ref-search-status');
  const resultsGrid = el('gw-search-results-grid');
  if (!q) {
    if (statusEl) statusEl.innerText = 'Please enter a song or artist name to search.';
    return;
  }
  if (statusEl) statusEl.innerText = 'Searching reference songs...';
  if (resultsGrid) resultsGrid.style.display = 'grid';
  try {
    const hasMidi = el('gw-ref-has-midi') && el('gw-ref-has-midi').checked;
    const songs = await getJSON('/api/songs/search?q=' + encodeURIComponent(q) + '&limit=12' + (hasMidi ? '&has_midi=true' : ''));
    state.searchResults = songs || [];
    renderSearchResults(state.searchResults);
    if (statusEl) {
      statusEl.innerText = songs.length
        ? `${songs.length} song(s) matched "${q}". Select one below:`
        : `No songs found matching "${q}". Try another artist or title.`;
    }
  } catch (err) {
    if (statusEl) statusEl.innerText = 'Search error: ' + err.message;
  }
}
let refSearchTimer = null;
function onRefSearchInput(val) {
  clearTimeout(refSearchTimer);
  const q = (val || '').trim();
  const recsSection = el('gw-recommendations-section');
  const resultsGrid = el('gw-search-results-grid');
  const statusEl = el('gw-ref-search-status');

  if (q.length < 2) {
    if (resultsGrid) resultsGrid.style.display = 'none';
    if (recsSection) recsSection.style.display = 'block';
    if (statusEl) statusEl.innerText = '';
    return;
  }
  if (recsSection) recsSection.style.display = 'none';
  if (resultsGrid) resultsGrid.style.display = 'grid';
  if (statusEl) statusEl.innerText = 'Searching 3.18M songs for "' + q + '"...';
  refSearchTimer = setTimeout(function() {
    searchReferenceSongs();
  }, 220);
}
function clearRefSearch() {
  const input = el('gw-ref-search-input');
  if (input) input.value = '';
  const recsSection = el('gw-recommendations-section');
  const resultsGrid = el('gw-search-results-grid');
  const statusEl = el('gw-ref-search-status');
  if (resultsGrid) resultsGrid.style.display = 'none';
  if (recsSection) recsSection.style.display = 'block';
  if (statusEl) statusEl.innerText = '';
}
function renderSearchResults(songs) {
  const grid = el('gw-search-results-grid');
  if (!grid) return;
  if (!songs || !songs.length) {
    grid.innerHTML = '<div class="empty-state">No matching songs found. Try a broader search query.</div>';
    return;
  }
  grid.innerHTML = songs.map(s => renderSongCard(s, false)).join('');
}
async function selectReferenceSong(songId, title, artist) {
  const statusEl = el('gw-ref-search-status');
  if (statusEl) statusEl.innerText = 'Loading blueprint for ' + title + '...';
  try {
    let url = '/api/ai/blueprint?';
    if (songId) url += 'song_id=' + encodeURIComponent(songId) + '&';
    url += 'title=' + encodeURIComponent(title) + '&artist=' + encodeURIComponent(artist || '');
    const bp = await getJSON(url);
    state.activeBlueprint = bp;
    const prevKey = questionsContextKey();
    state.referenceSong = bp.reference_song;
    if (prevKey !== questionsContextKey()) invalidateQuestions();
    renderSelectedReference(bp);
    applyBlueprintSpecs(bp);
    if (statusEl) statusEl.innerText = 'Selected: ' + title + ' by ' + (artist || bp.reference_song.artist);
    if (!state.unlockedSteps.includes(3)) state.unlockedSteps.push(3);
    updateStepperUI();
  } catch (err) {
    if (statusEl) statusEl.innerText = 'Failed to load blueprint: ' + err.message;
  }
}
function switchInspectorTab(tab) {
  for (const t of ['overview', 'abc', 'scansion']) {
    const btn = el('btn-insp-tab-' + t);
    const pane = el('insp-pane-' + t);
    if (btn) btn.classList.toggle('active', t === tab);
    if (pane) pane.classList.toggle('active', t === tab);
  }
}
function clearSelectedReference() {
  state.referenceSong = null;
  state.activeBlueprint = null;
  invalidateQuestions();
  const inspector = el('gw-reference-inspector');
  if (inspector) inspector.style.display = 'none';
  const recsSection = el('gw-recommendations-section');
  if (recsSection) recsSection.style.display = 'block';
  const resultsGrid = el('gw-search-results-grid');
  if (resultsGrid) resultsGrid.style.display = 'none';
  const statusEl = el('gw-ref-search-status');
  if (statusEl) statusEl.innerText = '';
  updateStepperUI();
}
function skipReferenceSong() {
  state.referenceSong = null;
  state.activeBlueprint = null;
  state.bypassedReference = true;
  invalidateQuestions();
  goToStep(3);
}
// ------------------------------------------------ Step 2: Selected Reference Inspector Panel Renderer
function renderSelectedReference(bp) {
  state.selectedBlueprint = bp;
  const ref = bp.reference_song || bp.reference_track || bp || {};

  const inspector = el('gw-reference-inspector');
  if (!inspector) return;
  inspector.style.display = 'block';

  // Hide search / recommendations to focus on full inspector
  const recsSection = el('gw-recommendations-section');
  if (recsSection) recsSection.style.display = 'none';
  const resultsGrid = el('gw-search-results-grid');
  if (resultsGrid) resultsGrid.style.display = 'none';

  // Header
  const titleEl = el('insp-ref-title');
  if (titleEl) titleEl.innerText = ref.title || 'Untitled Track';
  const metaEl = el('insp-ref-meta');
  if (metaEl) {
    const albumStr = ref.album ? ' • Album: ' + ref.album : '';
    const durStr = ref.duration_sec ? ' • Duration: ' + Math.round(ref.duration_sec) + 's' : '';
    metaEl.innerText = (ref.artist || 'Unknown') + albumStr + (ref.year ? ' • Year: ' + ref.year : ' • Year unspecified') + durStr;
  }

  const badgesEl = el('insp-ref-badges');
  if (badgesEl) {
    badgesEl.innerHTML = `
      <span class="badge badge-info">${escapeHtml(ref.genre || 'Music')}</span>
      <span class="badge badge-warning">${ref.tempo_bpm ? Math.round(ref.tempo_bpm) + ' BPM' : 'BPM unspecified'}</span>
      <span class="badge badge-secondary">${escapeHtml([ref.musical_key, ref.scale].filter(Boolean).join(' ') || 'Key unspecified')}</span>
      <span class="badge badge-dim">${escapeHtml(ref.time_signature || 'Meter unspecified')}</span>
      <span class="badge badge-info">${escapeHtml(ref.melodic_contour || 'Contour unavailable')}</span>
      ${ref.is_duet ? '<span class="badge badge-duet">Duet Arrangement</span>' : '<span class="badge badge-info">Solo Vocal</span>'}
      <span class="badge badge-success">${ref.id < 0 ? 'Imported Reference' : 'Corpus Track'}</span>
    `;
  }

  // Pane 1: Overview & Specs
  const bpmVal = ref.tempo_bpm ? Math.round(ref.tempo_bpm) : null;
  const bpmEl = el('spec-bpm');
  if (bpmEl) bpmEl.innerText = bpmVal == null ? 'Unspecified' : bpmVal + ' BPM';
  const bpmPct = bpmVal == null ? 0 : Math.min(100, Math.max(10, Math.round(((bpmVal - 40) / 160) * 100)));
  const bpmBar = el('spec-bpm-bar');
  if (bpmBar) bpmBar.style.width = bpmPct + '%';

  const keyEl = el('spec-key');
  if (keyEl) keyEl.innerText = [ref.musical_key, ref.scale].filter(Boolean).join(' ') || 'Unspecified';
  const timesigEl = el('spec-timesig');
  if (timesigEl) timesigEl.innerText = ref.time_signature ? ref.time_signature + ' meter' : 'Unspecified';
  const contourEl = el('spec-contour');
  if (contourEl) contourEl.innerText = (ref.melodic_contour || 'Unavailable').toUpperCase();

  const vLow = ref.vocal_range_low;
  const vHigh = ref.vocal_range_high;
  const vrEl = el('spec-vocal-range');
  if (vrEl) vrEl.innerText = (vLow && vHigh) ? `Vocal: ${vLow} - ${vHigh}` : 'Vocal range unavailable';

  const planEl = el('spec-plan');
  if (planEl) planEl.innerText = (bp.suggested_specs?.section_plan || 'standard').toUpperCase();
  const secCntEl = el('spec-sections-count');
  if (secCntEl) secCntEl.innerText = (ref.sections ? ref.sections.length : 0) + ' structured sections';

  // Audio Feature Meters
  for (const [field, id, multiplier] of [
    ['danceability', 'dance', 1], ['energy', 'energy', 1], ['valence', 'valence', 1],
    ['acousticness', 'acoustic', 1], ['speechiness', 'speech', 3],
  ]) {
    const value = ref[field];
    if (el('meter-' + id)) el('meter-' + id).innerText = value == null ? 'Unavailable' : Math.round(value * 100) + '%';
    if (el('bar-' + id)) el('bar-' + id).style.width = (value == null ? 0 : Math.min(100, Math.round(value * multiplier * 100))) + '%';
  }
  if (el('meter-loudness')) el('meter-loudness').innerText = ref.loudness == null ? 'Unavailable' : Number(ref.loudness).toFixed(1) + ' dB';
  if (el('bar-loudness')) el('bar-loudness').style.width = (ref.loudness == null ? 0 : Math.min(100, Math.max(0, ((ref.loudness + 24) / 24) * 100))) + '%';

  // Chords & Roman Numerals
  const chordsList = el('insp-chords-list');
  if (chordsList) {
    const chs = ref.chords || [];
    chordsList.innerHTML = chs.length ? chs.map(c => `<span class="badge badge-warning">${escapeHtml(c)}</span>`).join('') : 'Not supplied';
  }
  const romEl = el('insp-roman-numerals');
  if (romEl) {
    const roms = ref.roman_numerals || [];
    romEl.innerText = 'Harmonic Progression: ' + (roms.length ? roms.join(' - ') : 'Not supplied');
  }

  // Sensory & Mood
  const sensEl = el('insp-sensory-info');
  if (sensEl) {
    const cVal = ref.mean_concreteness == null ? null : Number(ref.mean_concreteness);
    const cPct = cVal == null ? 0 : Math.min(100, Math.max(10, Math.round((cVal / 5.0) * 100)));
    const cTier = cVal == null ? 'No rated lyric words' : (cVal >= 3.5 ? 'Highly Sensory / Tangible Objects' : (cVal >= 2.5 ? 'Balanced Pop & Narrative' : 'Abstract / Emotional Reflection'));
    sensEl.innerHTML = `
      <div class="flex justify-between items-center mb-4">
        <span><b>Dominant Emotion:</b> <span class="c-cyan fw-700">${escapeHtml(ref.dominant_emotion || 'Unavailable')}</span></span>
        <span class="fs-2xs c-dim uppercase">Modality: ${escapeHtml(ref.dominant_modality || 'Unavailable')}</span>
      </div>
      <div class="mt-6 mb-2">
        <div class="flex justify-between fs-xs">
          <span><b>Concreteness:</b> <span class="c-emerald fw-800">${cVal == null ? 'Unavailable' : cVal.toFixed(2)}</span> / 5.0 Max</span>
          <span class="c-muted fs-xs">${cTier}</span>
        </div>
        <div class="progress-bar mt-3">
          <div id="insp-concreteness-fill" class="progress-fill"></div>
        </div>
        <div class="fs-2xs c-dim mt-2">Sense Scale: 1.0 (Abstract thoughts) ➔ 5.0 (Physical sensory nouns)</div>
      </div>
    `;
    const cFill = el('insp-concreteness-fill');
    if (cFill) cFill.style.width = cPct + '%';
  }

  // Pane 2: ABC Score
  const abcBox = el('insp-ref-abc-box');
  if (abcBox) abcBox.value = ref.abc_score || 'No ABC score: this song has no imported MIDI.';

  // Pane 3: Scansion Table & Lyrics
  const scHost = el('insp-scansion-table-host');
  if (scHost && ref.sections) {
    scHost.innerHTML = ref.sections.map(function(sec) {
      const contourStr = sec.contour ? ' [' + sec.contour.join('-') + ' syl]' : '';
      const linesHtml = (sec.lines || []).map(function(l) {
        return `
          <tr>
            <td><span class="badge badge-info badge-xs">${l.syllables}</span></td>
            <td class="c-bright mono fs-xs">${escapeHtml(l.text)}</td>
            <td class="c-dim mono fs-2xs">${escapeHtml(l.stress_grid || '')}</td>
            <td class="c-cyan text-right fw-600 fs-xs">${escapeHtml(l.end_word || '')}</td>
          </tr>
        `;
      }).join('');
      return `
        <div class="subpanel mb-10">
          <div class="fs-md fw-700 c-cyan mb-6">
            [${escapeHtml(sec.name)}] <span class="fs-2xs c-muted">${contourStr}</span>
          </div>
          <table class="data-table mb-0">
            <thead><tr><th class="w-40">Syl</th><th>Line Text</th><th>Stress Grid</th><th class="text-right">End Word</th></tr></thead>
            <tbody>${linesHtml}</tbody>
          </table>
        </div>
      `;
    }).join('');
  }
  const lyrBox = el('insp-ref-lyrics-box');
  if (lyrBox) lyrBox.value = ref.lyrics || '';

  switchInspectorTab('overview');
}
function applyBlueprintSpecs(bp) {
  const s = bp.suggested_specs || {};
  if (s.bpm) {
    setFieldValue('gw-bpm', s.bpm);
    setFieldValue('gw-bpm-input', s.bpm);
  }
  if (s.music_key) {
    setFieldValue('gw-key', s.music_key);
    setFieldValue('gw-key-select', s.music_key);
  }
  if (s.scale) {
    setFieldValue('gw-scale', s.scale);
    setFieldValue('gw-scale-select', s.scale);
  }
  if (s.section_plan) setFieldValue('gw-plan', s.section_plan);
  if (s.rhyme_scheme) setFieldValue('gw-scheme', s.rhyme_scheme);
  if (s.vocal) setFieldValue('gw-vocal', s.vocal);
  // A reference supplies structural suggestions, not a replacement sound profile.
  if (s.template_id && !val('gw-selected-template-id')) el('gw-selected-template-id').value = s.template_id;
  onVocalChanged();
  if (s.vocal_roles) {
    if (s.vocal_roles.singer_a) el('gw-singer-a').value = s.vocal_roles.singer_a;
    if (s.vocal_roles.singer_b) el('gw-singer-b').value = s.vocal_roles.singer_b;
  }
}

// ------------------------------------------------ Reference library imports
let referenceImportBusy = false;
let referenceImportBatch = null;

function resetReferenceImportForm() {
  if (referenceImportBusy) return;
  for (const name of ['artist', 'title', 'lyrics', 'genre', 'year', 'bpm', 'key', 'scale']) {
    el('ref-import-' + name).value = '';
  }
  el('ref-import-file-input').value = '';
  el('ref-import-file-name').innerText = 'No file chosen';
  referenceImportBatch = null;
  el('ref-import-batch-btn').disabled = true;
  setStatus('ref-import-status', '', '');
}

async function submitSingleReferenceImport() {
  if (referenceImportBusy) return;
  for (const name of ['artist', 'title', 'lyrics', 'genre', 'year', 'bpm', 'key', 'scale']) {
    if (!el('ref-import-' + name).reportValidity()) return;
  }
  const song = {artist: val('ref-import-artist').trim(), title: val('ref-import-title').trim(), lyrics: val('ref-import-lyrics').trim()};
  for (const [field, id] of [['genre', 'genre'], ['year', 'year'], ['tempo_bpm', 'bpm'], ['musical_key', 'key'], ['scale', 'scale']]) {
    const value = val('ref-import-' + id).trim();
    if (value) song[field] = ['year', 'tempo_bpm'].includes(field) ? Number(value) : value;
  }
  await saveReferenceImport([song]);
}

async function onReferenceJsonFileSelected(input) {
  referenceImportBatch = null;
  el('ref-import-batch-btn').disabled = true;
  const file = input.files[0];
  el('ref-import-file-name').innerText = file ? file.name : 'No file chosen';
  if (!file) return;
  try {
    const records = JSON.parse(await file.text());
    if (input.files[0] !== file) return;
    if (!Array.isArray(records) || !records.length || records.length > 200) {
      throw new Error('JSON must be an array of 1–200 song objects.');
    }
    referenceImportBatch = records;
    el('ref-import-batch-btn').disabled = referenceImportBusy;
    setStatus('ref-import-status', records.length + ' songs loaded. Click Import JSON Batch to save.', '');
  } catch (err) {
    if (input.files[0] === file) setStatus('ref-import-status', 'Could not read JSON: ' + err.message, 'red');
  }
}

async function submitBulkReferenceImport() {
  if (referenceImportBatch) await saveReferenceImport(referenceImportBatch);
}

async function saveReferenceImport(songs) {
  if (referenceImportBusy) return;
  referenceImportBusy = true;
  const controls = ['save-btn', 'reset-btn', 'choose-file-btn', 'file-input', 'batch-btn'];
  controls.forEach(id => { el('ref-import-' + id).disabled = true; });
  setStatus('ref-import-status', 'Saving reference songs...', '');
  let result;
  try {
    const response = await fetch('/api/references/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({songs}),
    });
    const data = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(data.detail)
        ? data.detail.map(item => (item.loc || []).join('.') + ': ' + item.msg).join('; ')
        : data.detail;
      throw new Error(detail || 'HTTP ' + response.status);
    }
    result = data;
    const activeId = state.referenceSong?.id;
    if (activeId < 0 && result.song_ids.includes(activeId)) {
      state.referenceRevision++;
      invalidateQuestions();
      try {
        const bp = await getJSON('/api/ai/blueprint?song_id=' + encodeURIComponent(activeId));
        if (state.referenceSong?.id === activeId) {
          state.activeBlueprint = bp;
          state.referenceSong = bp.reference_song;
          renderSelectedReference(bp);
          applyBlueprintSpecs(bp);
          updateStepperUI();
        }
      } catch (err) {
        if (state.referenceSong?.id === activeId) clearSelectedReference();
        throw new Error('Saved, but the active blueprint could not refresh: ' + err.message + '. Select it again.');
      }
    }
    el('gw-ref-has-midi').checked = false;
    el('gw-ref-search-input').value = songs[0].artist;
    el('gw-recommendations-section').style.display = 'none';
    clearTimeout(refSearchTimer);
    await Promise.all([loadDatabaseStats(), loadReferenceRecommendations(), searchReferenceSongs()]);
    setStatus('ref-import-status', result.added + ' added, ' + result.updated + ' updated; ' + result.total +
      ' imported references saved. Showing matches for ' + songs[0].artist + '. No MIDI score is created.', 'green');
  } catch (err) {
    setStatus('ref-import-status', (result ? 'References saved. ' : 'Import failed: ') + err.message, 'red');
  } finally {
    referenceImportBusy = false;
    controls.forEach(id => { el('ref-import-' + id).disabled = id === 'batch-btn' && !referenceImportBatch; });
  }
}

function downloadSampleReferenceJson() {
  const songs = [{
    title: 'City Windows', artist: 'Example Artist',
    lyrics: `[Verse]
A lantern in the window glows
The last train takes the long way home

[Chorus]
Leave one light above the door
I will find the road once more`,
    genre: 'synth-pop', year: 2024, tempo_bpm: 120, musical_key: 'G', scale: 'minor',
  }];
  const url = URL.createObjectURL(new Blob([JSON.stringify(songs, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'reference-songs.example.json';
  link.click();
  URL.revokeObjectURL(url);
}
