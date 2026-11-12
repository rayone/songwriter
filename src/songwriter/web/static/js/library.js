// Saved Songs tab: crafted-song table, inspector and batch actions.

// ------------------------------------------------ Crafted Songs Management
let activeCraftedSong = null;
let activeCraftedRevision = null;
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
      const songId = s.song_id;
      const isSelected = selectedCraftedSongIds.has(songId);
      const activeCls = (activeCraftedSong && activeCraftedSong.song_id === songId) ? ' active' : '';
      const previewText = s.lyrics ? s.lyrics.split('\n').filter(l => l.trim() && !l.trim().startsWith('[')).slice(0, 2).join(' / ') : (s.brief || 'No lyrics preview');
      return `
        <tr class="library-row${activeCls}" onclick="onTableRowClick(event, '${escapeHtml(songId)}')">
          <td onclick="event.stopPropagation()">
            <input type="checkbox" class="song-check cursor-pointer" value="${escapeHtml(songId)}" onchange="toggleSelectSong('${escapeHtml(songId)}', this.checked)">
          </td>
          <td>
            <div class="fw-700 c-bright fs-md">${escapeHtml(s.title || 'Untitled Song')}</div>
            <div class="fs-xs c-muted mt-2 max-w-240 truncate">
              ${escapeHtml(previewText)}
            </div>
          </td>
          <td class="c-cyan fw-500">
            ${escapeHtml(s.genre || '—')}
          </td>
          <td class="mono fs-sm c-main">
            ${s.bpm == null ? '—' : `${s.bpm} BPM`} <span class="c-muted">•</span> ${escapeHtml(s.key || '—')} ${escapeHtml(s.scale || '')}
          </td>
          <td>
            <span class="badge badge-info ${s.status === 'finalized' ? 'duet' : ''} fs-2xs">${escapeHtml(s.status || 'unknown')}</span>
          </td>
          <td onclick="event.stopPropagation()">
            <button type="button" class="btn-secondary btn-sm btn-danger-outline" onclick="deleteOneSong('${escapeHtml(songId)}', '${escapeHtml(s.title || 'Untitled Song')}')">
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
    if (activeCraftedSong && ids.includes(activeCraftedSong.song_id)) {
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
      if (activeCraftedSong && activeCraftedSong.song_id === songId) {
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
  el('insp-title').innerText = g.title || 'Untitled Song';
  el('insp-meta').innerText = `${g.artist || 'AI Songwriter'} • ${g.genre || 'Pop'}${g.subgenre ? ' (' + g.subgenre + ')' : ''} • ${g.bpm || 92} BPM in ${g.key || 'C'} ${g.scale || 'major'}`;
  el('insp-badges').innerHTML = `
    <span class="badge badge-info">${escapeHtml(g.genre || 'Pop')}</span>
    <span class="badge badge-info">${escapeHtml(g.bpm || 92)} BPM</span>
    <span class="badge badge-info">${escapeHtml(g.key || 'C')} ${escapeHtml(g.scale || 'major')}</span>
    <span class="badge badge-info ${status === 'finalized' ? 'duet' : ''}">${escapeHtml(status || g.status || 'draft')}</span>
    ${g.abc_score ? '<span class="badge badge-success">ABC Score Ready</span>' : ''}
  `;
  el('insp-lyrics-sheet').innerHTML = formatLyricsWithBadges(g.lyrics || '');
  el('insp-abc-text').value = g.abc_score || 'No ABC score: this song has no imported MIDI.';
  el('insp-prompt-songbench').value = getEnginePromptDisplay(g, 'songbench');
  el('insp-prompt-minimax').value = getEnginePromptDisplay(g, 'minimax');
  el('insp-prompt-yue2').value = getEnginePromptDisplay(g, 'yue2');
}
async function selectCraftedSong(songId) {
  try {
    const s = await getJSON('/api/crafted_songs/' + encodeURIComponent(songId));
    activeCraftedSong = s;
    activeCraftedRevision = s.revision;
    const emptyState = el('crafted-inspector-empty');
    if (emptyState) emptyState.style.display = 'none';
    const content = el('crafted-inspector-content');
    if (content) content.style.display = 'flex';
    renderInspector(s, s.status);

    const revs = await getJSON('/api/crafted_songs/' + encodeURIComponent(songId) + '/revisions');
    const revList = Array.isArray(revs) ? revs : (revs.revisions || []);
    const sel = el('insp-revision');
    sel.innerHTML = revList.map(function(r) {
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
  const songId = activeCraftedSong.song_id;
  try {
    const res = await getJSON('/api/crafted_songs/' + encodeURIComponent(songId) + '/revisions/' + revision);
    activeCraftedRevision = revision;
    el('insp-revision').value = String(revision);
    renderInspector(res, res.status);
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
  const songId = activeCraftedSong.song_id;
  try {
    const rev = activeCraftedRevision;
    const base = '/api/crafted_songs/' + encodeURIComponent(songId);
    res = await getJSON(rev && rev !== activeCraftedSong.revision ? base + '/revisions/' + rev : base);
  } catch (e) {
    alert('Failed to load song generation: ' + e.message);
    return;
  }
  const g = res;
  state.song = typeof deepClone === 'function' ? deepClone(g) : JSON.parse(JSON.stringify(g));
  syncDOMFromSong(state.song);

  const takeSong = typeof deepClone === 'function' ? deepClone(g) : JSON.parse(JSON.stringify(g));
  const takeId = state.nextTakeId++;
  state.takes[takeId] = {
    takeId: takeId,
    status: 'completed',
    statusText: 'Loaded from library: ' + (g.title || 'Untitled Song') + ' (revision ' + g.revision + ')',
    statusColor: 'green',
    specPillsHtml: `<span class="m-pill">BPM<b>${escapeHtml(g.bpm)}</b></span><span class="m-pill">Key<b>${escapeHtml(g.key)} ${escapeHtml(g.scale)}</b></span><span class="m-pill">Syllables<b>${g.syllable_min}-${g.syllable_max}</b></span>`,
    scansionPillsHtml: '',
    trailHtml: '<div class="empty-state">Loaded from library: ' + escapeHtml(g.title) + ' (revision ' + g.revision + ')</div>',
    song: takeSong
  };
  switchTab('ghostwriter');
  goToStep(4);
  if (typeof updateStepperUI === 'function') updateStepperUI();
  selectTake(takeId);
}
function loadCraftedSongIntoEditor() {
  if (!activeCraftedSong || !activeCraftedSong.lyrics) return;
  openInSongAnalytics(activeCraftedSong.lyrics, activeCraftedSong.title || 'Untitled Song');
}
async function deleteActiveCraftedSong() {
  if (!activeCraftedSong) return;
  const songId = activeCraftedSong.song_id;
  if (!confirm('Are you sure you want to delete "' + (activeCraftedSong.title || 'this song') + '" from your library?')) return;
  try {
    const res = await fetch('/api/crafted_songs/' + encodeURIComponent(songId), { method: 'DELETE' });
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
