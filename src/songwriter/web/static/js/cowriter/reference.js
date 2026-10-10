// Cowriter Step 2: reference song search, recommendations and inspector.

// ------------------------------------------------ Step 2: Unified Song Card Renderer, Recommendations & Live Search
function renderSongCard(s, isRecommendation = false) {
  const sub = (s.year ? s.year + ' • ' : '') +
              (s.preview ? s.preview.replace(/\n+/g, ' / ').slice(0, 85) + '...' : 'Available in 3.18M corpus');
  const bpmBadge = s.bpm ? `<span class="badge badge-warning badge-xs">${Math.round(s.bpm)} BPM</span>` : '';
  const keyBadge = s.key ? `<span class="badge badge-secondary badge-xs">${escapeHtml(s.key)}${s.scale ? ' ' + escapeHtml(s.scale) : ''}</span>` : '';
  const yearBadge = s.year ? `<span class="badge badge-dim badge-xs">${s.year}</span>` : '';
  const midiBadge = s.has_midi ? `<span class="badge badge-success badge-xs" title="Imported MIDI: YuE2 ABC score available">♪ MIDI</span>` : '';
  const guidanceHtml = s.guidance
    ? `<div class="ref-choice-guidance mt-8">${escapeHtml(s.guidance)}</div>`
    : `<div class="ref-choice-guidance mt-8 c-muted fs-xs">Live corpus reference track</div>`;

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
          </div>
        </div>
        ${guidanceHtml}
        <div class="fs-xs c-muted mt-6 lh-14">${escapeHtml(sub)}</div>
      </div>
      <div class="flex gap-8 items-center mt-8">
        <button type="button" class="btn-primary btn-sm" onclick="selectReferenceSong(${s.song_id}, '${escapeHtml(s.title)}', '${escapeHtml(s.artist)}')">Select Reference</button>
        <a href="https://www.youtube.com/results?search_query=${encodeURIComponent(s.title + ' ' + s.artist)}" target="_blank" rel="noopener noreferrer" class="btn-secondary btn-sm">Listen on YouTube ↗</a>
      </div>
    </div>
  `;
}
async function loadReferenceRecommendations() {
  const grid = el('gw-recommendations-grid');
  if (!grid) return;
  grid.innerHTML = '<div class="empty-state">Finding best reference matches from 3.18M corpus...</div>';
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
      genre: styleVal,
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
  if (statusEl) statusEl.innerText = 'Searching 3.18M songs in corpus...';
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
    metaEl.innerText = (ref.artist || 'Unknown') + albumStr + ' • Year: ' + (ref.year || '2020') + durStr;
  }

  const badgesEl = el('insp-ref-badges');
  if (badgesEl) {
    badgesEl.innerHTML = `
      <span class="badge badge-info">${escapeHtml(ref.genre || 'Music')}</span>
      <span class="badge badge-warning">${Math.round(ref.tempo_bpm || 118)} BPM</span>
      <span class="badge badge-secondary">${escapeHtml(ref.musical_key || 'C')} ${escapeHtml(ref.scale || 'major')}</span>
      <span class="badge badge-dim">${escapeHtml(ref.time_signature || '4/4')}</span>
      <span class="badge badge-info">${escapeHtml(ref.melodic_contour || 'wave')}</span>
      ${ref.is_duet ? '<span class="badge badge-duet">Duet Arrangement</span>' : '<span class="badge badge-info">Solo Vocal</span>'}
      <span class="badge badge-success">Verified Corpus Track</span>
    `;
  }

  // Pane 1: Overview & Specs
  const bpmVal = Math.round(ref.tempo_bpm || 118);
  const bpmEl = el('spec-bpm');
  if (bpmEl) bpmEl.innerText = bpmVal + ' BPM';
  const bpmPct = Math.min(100, Math.max(10, Math.round(((bpmVal - 40) / 160) * 100)));
  const bpmBar = el('spec-bpm-bar');
  if (bpmBar) bpmBar.style.width = bpmPct + '%';

  const keyEl = el('spec-key');
  if (keyEl) keyEl.innerText = (ref.musical_key || 'C') + ' ' + (ref.scale || 'major');
  const timesigEl = el('spec-timesig');
  if (timesigEl) timesigEl.innerText = (ref.time_signature || '4/4') + ' meter';
  const contourEl = el('spec-contour');
  if (contourEl) contourEl.innerText = (ref.melodic_contour || 'wave').toUpperCase();

  const vLow = ref.vocal_range_low;
  const vHigh = ref.vocal_range_high;
  const vrEl = el('spec-vocal-range');
  if (vrEl) vrEl.innerText = (vLow && vHigh) ? `Vocal: ${vLow} - ${vHigh}` : 'Vocal: ~2 Octaves';

  const planEl = el('spec-plan');
  if (planEl) planEl.innerText = (bp.suggested_specs?.section_plan || 'standard').toUpperCase();
  const secCntEl = el('spec-sections-count');
  if (secCntEl) secCntEl.innerText = (ref.sections ? ref.sections.length : 0) + ' structured sections';

  // Audio Feature Meters
  const dnc = ref.danceability != null ? ref.danceability : 0.55;
  const nrg = ref.energy != null ? ref.energy : 0.65;
  const val_ = ref.valence != null ? ref.valence : 0.50;
  const aco = ref.acousticness != null ? ref.acousticness : 0.20;
  const loud = ref.loudness != null ? ref.loudness : -7.5;
  const spch = ref.speechiness != null ? ref.speechiness : 0.05;

  if (el('meter-dance')) el('meter-dance').innerText = Math.round(dnc * 100) + '%';
  if (el('bar-dance')) el('bar-dance').style.width = Math.round(dnc * 100) + '%';

  if (el('meter-energy')) el('meter-energy').innerText = Math.round(nrg * 100) + '%';
  if (el('bar-energy')) el('bar-energy').style.width = Math.round(nrg * 100) + '%';

  if (el('meter-valence')) el('meter-valence').innerText = Math.round(val_ * 100) + '%';
  if (el('bar-valence')) el('bar-valence').style.width = Math.round(val_ * 100) + '%';

  if (el('meter-acoustic')) el('meter-acoustic').innerText = Math.round(aco * 100) + '%';
  if (el('bar-acoustic')) el('bar-acoustic').style.width = Math.round(aco * 100) + '%';

  if (el('meter-loudness')) el('meter-loudness').innerText = (typeof loud === 'number' ? loud.toFixed(1) : loud) + ' dB';
  const loudPct = Math.min(100, Math.max(10, Math.round(((Number(loud) + 24) / 24) * 100)));
  if (el('bar-loudness')) el('bar-loudness').style.width = loudPct + '%';

  if (el('meter-speech')) el('meter-speech').innerText = Math.round(spch * 100) + '%';
  if (el('bar-speech')) el('bar-speech').style.width = Math.min(100, Math.round(spch * 300)) + '%';

  // Chords & Roman Numerals
  const chordsList = el('insp-chords-list');
  if (chordsList) {
    const chs = (ref.chords && ref.chords.length) ? ref.chords : ['C', 'G', 'Am', 'F'];
    chordsList.innerHTML = chs.map(c => `<span class="badge badge-warning">${escapeHtml(c)}</span>`).join('');
  }
  const romEl = el('insp-roman-numerals');
  if (romEl) {
    const roms = (ref.roman_numerals && ref.roman_numerals.length) ? ref.roman_numerals : (ref.scale === 'minor' ? ['i', 'VI', 'III', 'VII'] : ['I', 'V', 'vi', 'IV']);
    romEl.innerText = 'Harmonic Progression: ' + roms.join(' - ');
  }

  // Sensory & Mood
  const sensEl = el('insp-sensory-info');
  if (sensEl) {
    const cVal = ref.mean_concreteness ? Number(ref.mean_concreteness) : 2.85;
    const cPct = Math.min(100, Math.max(10, Math.round((cVal / 5.0) * 100)));
    const cTier = cVal >= 3.5 ? 'Highly Sensory / Tangible Objects' : (cVal >= 2.5 ? 'Balanced Pop & Narrative' : 'Abstract / Emotional Reflection');
    sensEl.innerHTML = `
      <div class="flex justify-between items-center mb-4">
        <span><b>Dominant Emotion:</b> <span class="c-cyan fw-700">${escapeHtml(ref.dominant_emotion || 'Introspective')}</span></span>
        <span class="fs-2xs c-dim uppercase">Modality: ${escapeHtml(ref.dominant_modality || 'action')}</span>
      </div>
      <div class="mt-6 mb-2">
        <div class="flex justify-between fs-xs">
          <span><b>Concreteness:</b> <span class="c-emerald fw-800">${cVal.toFixed(2)}</span> / 5.0 Max</span>
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
  if (s.template_id) el('gw-selected-template-id').value = s.template_id;
  onVocalChanged();
  if (s.vocal_roles) {
    if (s.vocal_roles.singer_a) el('gw-singer-a').value = s.vocal_roles.singer_a;
    if (s.vocal_roles.singer_b) el('gw-singer-b').value = s.vocal_roles.singer_b;
  }
}
