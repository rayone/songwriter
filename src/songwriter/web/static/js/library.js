// Saved Songs tab: crafted-song table, inspector and batch actions.

// ------------------------------------------------ Crafted Songs Management
let activeCraftedSong = null;
let craftedSearchTimer = null;
let selectedCraftedSongIds = new Set();
async function loadCraftedSongsList() {
  const tbody = el('crafted-table-body');
  if (!tbody) return;
  const q = val('crafted-search-input').trim();
  const status = val('crafted-status-filter');
  const sortVal = val('crafted-sort-by') || 'updated_at:desc';
  const [sortBy, sortOrder] = sortVal.split(':');

  let url = `/api/crafted_songs?limit=200&sort_by=${encodeURIComponent(sortBy)}&sort_order=${encodeURIComponent(sortOrder)}`;
  if (q) url += '&q=' + encodeURIComponent(q);
  if (status) url += '&status=' + encodeURIComponent(status);

  try {
    const items = await getJSON(url);
    const countPill = el('crafted-count-pill');
    if (countPill) countPill.innerText = `${items ? items.length : 0} Songs`;
    selectedCraftedSongIds.clear();
    updateBatchDeleteUI();

    if (!items || !items.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="td-empty">No saved songs match your filter.</td></tr>';
      return;
    }

    tbody.innerHTML = items.map(function(s) {
      const isSelected = selectedCraftedSongIds.has(s.id);
      const activeCls = (activeCraftedSong && activeCraftedSong.id === s.id) ? ' active' : '';
      return `
        <tr class="library-row${activeCls}" onclick="onTableRowClick(event, '${s.id}')">
          <td onclick="event.stopPropagation()">
            <input type="checkbox" class="song-check cursor-pointer" value="${s.id}" onchange="toggleSelectSong('${s.id}', this.checked)">
          </td>
          <td>
            <div class="fw-700 c-bright fs-md">${escapeHtml(s.title)}</div>
            <div class="fs-xs c-muted mt-2 max-w-240 truncate">
              ${escapeHtml(s.lyrics_preview || 'No lyrics preview')}
            </div>
          </td>
          <td class="c-cyan fw-500">
            ${escapeHtml(s.genre || 'Pop')}
          </td>
          <td class="mono fs-sm c-main">
            ${s.tempo_bpm || 118} BPM <span class="c-muted">•</span> ${escapeHtml(s.musical_key || 'C')} ${escapeHtml(s.scale || 'maj')}
          </td>
          <td>
            <span class="badge badge-info ${s.status === 'finalized' ? 'duet' : ''} fs-2xs">${escapeHtml(s.status || 'draft')}</span>
          </td>
          <td onclick="event.stopPropagation()">
            <button type="button" class="btn-secondary btn-sm btn-danger-outline" onclick="deleteOneSong('${s.id}', '${escapeHtml(s.title)}')">
              Delete
            </button>
          </td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="td-empty c-rose">Error loading library: ${escapeHtml(e.message)}</td></tr>`;
  }
}
function onTableRowClick(event, songId) {
  selectCraftedSong(songId);
}
function toggleSelectSong(songId, checked) {
  if (checked) selectedCraftedSongIds.add(songId);
  else selectedCraftedSongIds.delete(songId);
  updateBatchDeleteUI();
}
function toggleSelectAllCrafted(checked) {
  const boxes = document.querySelectorAll('.song-check');
  selectedCraftedSongIds.clear();
  boxes.forEach(function(cb) {
    cb.checked = checked;
    if (checked) selectedCraftedSongIds.add(cb.value);
  });
  updateBatchDeleteUI();
}
function updateBatchDeleteUI() {
  const btn = el('btn-batch-delete');
  const cnt = el('batch-selected-count');
  if (!btn || !cnt) return;
  const count = selectedCraftedSongIds.size;
  cnt.innerText = count;
  btn.style.display = count > 0 ? 'inline-block' : 'none';
}
async function batchDeleteSelectedSongs() {
  const ids = Array.from(selectedCraftedSongIds);
  if (!ids.length) return;
  if (!confirm(`Are you sure you want to permanently delete ${ids.length} selected song(s)?`)) return;
  try {
    const res = await postJSON('/api/crafted_songs/batch_delete', { song_ids: ids });
    if (activeCraftedSong && ids.includes(activeCraftedSong.id)) {
      activeCraftedSong = null;
      if (el('crafted-inspector-empty')) el('crafted-inspector-empty').style.display = 'block';
      if (el('crafted-inspector-content')) el('crafted-inspector-content').style.display = 'none';
    }
    selectedCraftedSongIds.clear();
    loadCraftedSongsList();
  } catch (e) {
    alert('Batch delete failed: ' + e.message);
  }
}
async function deleteOneSong(songId, title) {
  if (!confirm(`Delete "${title}"?`)) return;
  try {
    const res = await fetch('/api/crafted_songs/' + encodeURIComponent(songId), { method: 'DELETE' });
    if (res.ok) {
      if (activeCraftedSong && activeCraftedSong.id === songId) {
        activeCraftedSong = null;
        if (el('crafted-inspector-empty')) el('crafted-inspector-empty').style.display = 'block';
        if (el('crafted-inspector-content')) el('crafted-inspector-content').style.display = 'none';
      }
      loadCraftedSongsList();
    } else {
      alert('Delete failed');
    }
  } catch (e) {
    alert('Delete error: ' + e.message);
  }
}
function changeCraftedSort(col) {
  const sel = el('crafted-sort-by');
  if (!sel) return;
  const cur = sel.value;
  if (cur.startsWith(col + ':desc')) sel.value = col + ':asc';
  else sel.value = col + ':desc';
  loadCraftedSongsList();
}
function onCraftedSearchInput(val) {
  clearTimeout(craftedSearchTimer);
  craftedSearchTimer = setTimeout(loadCraftedSongsList, 220);
}
// Inspector fields from one saved generation, so the latest and any older
// revision render identically.
function renderInspector(g, status) {
  const ep = g.engine_prompts || {};
  el('insp-title').innerText = g.title || 'Untitled Song';
  el('insp-meta').innerText = `${g.artist || 'AI Songwriter'} • ${g.genre || 'Pop'} ${g.subgenre ? '(' + g.subgenre + ')' : ''} • ${g.bpm} BPM in ${g.key} ${g.scale}`;
  el('insp-badges').innerHTML = `
    <span class="badge badge-info">${escapeHtml(g.genre || 'Pop')}</span>
    <span class="badge badge-info">${escapeHtml(g.bpm)} BPM</span>
    <span class="badge badge-info">${escapeHtml(g.key)} ${escapeHtml(g.scale)}</span>
    <span class="badge badge-info ${status === 'finalized' ? 'duet' : ''}">${escapeHtml(status || 'draft')}</span>
    ${g.abc_score ? '<span class="badge badge-success">ABC Score Ready</span>' : ''}
  `;
  el('insp-lyrics-sheet').innerHTML = formatLyricsWithBadges(g.lyrics || '');
  el('insp-abc-text').value = g.abc_score || 'No ABC score: this song has no imported MIDI.';
  el('insp-prompt-songbench').value = ep.songbench || '';
  el('insp-prompt-minimax').value = ep.minimax || '';
  el('insp-prompt-yue2').value = (ep.songbench || '') + (g.abc_score ? '\n\n--- ABC SCORE ---\n' + g.abc_score : '');
}
async function selectCraftedSong(songId) {
  try {
    const s = await getJSON('/api/crafted_songs/' + encodeURIComponent(songId));
    activeCraftedSong = s;
    activeCraftedSong.viewRevision = s.revision;
    const emptyState = el('crafted-inspector-empty');
    if (emptyState) emptyState.style.display = 'none';
    const content = el('crafted-inspector-content');
    if (content) content.style.display = 'flex';
    renderInspector(s.generation || {}, s.status);

    const revs = (await getJSON('/api/crafted_songs/' + encodeURIComponent(songId) + '/revisions')).revisions || [];
    const sel = el('insp-revision');
    sel.innerHTML = revs.map(function(r) {
      const when = r.created_at ? new Date(r.created_at).toLocaleString() : '';
      const label = 'Rev ' + r.revision + (r.revision === s.revision ? ' (latest)' : '') + (when ? ' · ' + when : '');
      return `<option value="${r.revision}">${escapeHtml(label)}</option>`;
    }).join('');
    sel.value = String(s.revision);
    el('insp-revision-note').innerText = '';
  } catch (e) {
    alert('Failed to load song details: ' + e.message);
  }
}
async function selectCraftedRevision(rev) {
  if (!activeCraftedSong) return;
  const revision = parseInt(rev, 10);
  try {
    const res = await getJSON('/api/crafted_songs/' + encodeURIComponent(activeCraftedSong.id) + '/revisions/' + revision);
    activeCraftedSong.viewRevision = revision;
    el('insp-revision').value = String(revision);
    renderInspector(res.generation || {}, res.status);
    el('insp-revision-note').innerText = revision === activeCraftedSong.revision ? ''
      : 'Viewing an older revision. Load into Cowriter and save to restore it as revision ' + (activeCraftedSong.revision + 1) + '.';
  } catch (e) {
    alert('Failed to load revision: ' + e.message);
  }
}
function showInspTab(tabName) {
  el('insp-view-lyrics').style.display = tabName === 'lyrics' ? 'block' : 'none';
  el('insp-view-abc').style.display = tabName === 'abc' ? 'flex' : 'none';
  el('insp-view-prompts').style.display = tabName === 'prompts' ? 'flex' : 'none';

  el('btn-insp-lyrics').classList.toggle('active', tabName === 'lyrics');
  el('btn-insp-abc').classList.toggle('active', tabName === 'abc');
  el('btn-insp-prompts').classList.toggle('active', tabName === 'prompts');
}
async function loadCraftedSongIntoCowriter() {
  if (!activeCraftedSong) return;
  let res;
  try {
    const rev = activeCraftedSong.viewRevision;
    const base = '/api/crafted_songs/' + encodeURIComponent(activeCraftedSong.id);
    res = await getJSON(rev && rev !== activeCraftedSong.revision ? base + '/revisions/' + rev : base + '/generation');
  } catch (e) {
    alert('Failed to load song generation: ' + e.message);
    return;
  }
  const g = res.generation || {};
  // Regenerating from here saves a new revision of this song, not a new song.
  state.editingSongId = res.song_id;

  // Step 1: model, title, brief, story, and the visible template card
  if (g.model) {
    const sel = el('gw-model');
    if (sel && !Array.from(sel.options).some(o => o.value === g.model)) {
      sel.add(new Option(g.model + ' (saved)', g.model));
    }
    setFieldValue('gw-model', g.model);
    onModelSelected(g.model);
  }
  setFieldValue('gw-title', g.title);
  setFieldValue('gw-prompt', g.brief);
  state.storyline = g.story || '';
  if (el('gw-storyline-text')) el('gw-storyline-text').value = state.storyline;
  if (el('gw-storyline-container')) el('gw-storyline-container').style.display = state.storyline ? 'flex' : 'none';
  if (g.template_id) {
    await showTemplateCard(g.template_id);
  } else {
    setFieldValue('gw-style-input', g.style || g.genre);
  }

  // Step 2: the saved reference as the selected card, plus the saved specs.
  // Saved values are applied last so a template or blueprint default can't overwrite them.
  state.referenceSong = g.reference_song || null;
  state.recommendations = [];
  if (state.referenceSong) {
    renderSelectedReference({
      reference_song: state.referenceSong,
      suggested_specs: { section_plan: (g.section_plan || []).join(', ') },
    });
  } else {
    clearSelectedReference();
  }
  setFieldValue('gw-bpm', g.bpm);
  setFieldValue('gw-key', g.key);
  setFieldValue('gw-scale', g.scale);
  setFieldValue('gw-vocal', g.vocal);
  if (g.vocal_roles) {
    setFieldValue('gw-singer-a', g.vocal_roles.singer_a);
    setFieldValue('gw-singer-b', g.vocal_roles.singer_b);
  }
  setFieldValue('gw-syllables', g.syllable_min + '-' + g.syllable_max);
  setFieldValue('gw-scheme', g.rhyme_scheme);
  setFieldValue('gw-plan', (g.section_plan || []).map(n => '[' + n + ']').join(', '));
  setFieldValue('gw-banned', (g.banned_pairs || []).join(', '));
  if (typeof onVocalChanged === 'function') onVocalChanged();

  // Step 3: the questions exactly as they were asked, with every option selectable
  state.answers = Object.assign({}, g.answers || {});
  state.questions = (g.questions || []).map(function(q) {
    const opts = (q.options || []).slice();
    const answer = state.answers[q.id];
    if (answer && !opts.includes(answer)) opts.unshift(answer);
    return Object.assign({}, q, { options: opts, default: answer || opts[0] || '' });
  });
  // The saved questions belong to the saved reference/story just restored above.
  state.questionsKey = questionsContextKey();
  if (typeof renderQuestions === 'function') renderQuestions();

  // Step 4: the saved take, carrying its generation so re-saving bumps the revision
  const lyrics = g.lyrics || '';
  const ep = g.engine_prompts || {};
  const takeId = state.nextTakeId++;
  state.takes[takeId] = {
    takeId: takeId,
    lyrics: lyrics,
    formatted_songbench: (ep.songbench ? ep.songbench + '\n\n' : '') + lyrics,
    formatted_yue2: (ep.yue2 ? ep.yue2 + '\n\n' : '') + lyrics + (g.abc_score ? '\n\n--- ABC SCORE ---\n' + g.abc_score : ''),
    formatted_minimax: (ep.minimax ? ep.minimax + '\n\n' : '') + lyrics,
    specPillsHtml: `<span class="m-pill">BPM<b>${escapeHtml(g.bpm)}</b></span><span class="m-pill">Key<b>${escapeHtml(g.key)} ${escapeHtml(g.scale)}</b></span><span class="m-pill">Syllables<b>${g.syllable_min}-${g.syllable_max}</b></span>`,
    scansionPillsHtml: '',
    trailHtml: '<div class="empty-state">Loaded from library: ' + escapeHtml(g.title) + ' (revision ' + res.revision + ')</div>',
    model_used: g.model || '',
    songId: res.song_id,
    generation: g
  };
  state.unlockedSteps = [1, 2, 3, 4];
  switchTab('ghostwriter');
  goToStep(4);
  if (typeof updateStepperUI === 'function') updateStepperUI();
  selectTake(takeId);
}
function loadCraftedSongIntoEditor() {
  if (!activeCraftedSong || !activeCraftedSong.lyrics) return;
  openInSongAnalytics(activeCraftedSong.lyrics, activeCraftedSong.title);
}
async function deleteActiveCraftedSong() {
  if (!activeCraftedSong) return;
  if (!confirm('Are you sure you want to delete "' + activeCraftedSong.title + '" from your library?')) return;
  try {
    const res = await fetch('/api/crafted_songs/' + encodeURIComponent(activeCraftedSong.id), { method: 'DELETE' });
    if (res.ok) {
      activeCraftedSong = null;
      el('crafted-inspector-empty').style.display = 'block';
      el('crafted-inspector-content').style.display = 'none';
      loadCraftedSongsList();
    } else {
      alert('Delete failed');
    }
  } catch (e) {
    alert('Delete error: ' + e.message);
  }
}
