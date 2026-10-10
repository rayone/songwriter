// Corpus Explorer tab.

async function searchSongs() {
  const q = val('song-query');
  const by = val('song-by') || 'all';
  const host = el('song-results');
  if (!q) return;
  host.innerHTML = '<div class="status-line">Searching 3.18M songs...</div>';
  try {
    const rows = await getJSON('/api/songs/search?q=' + encodeURIComponent(q) + '&by=' + encodeURIComponent(by) + '&limit=100');
    state.songs = rows;
    renderSongTable();
  } catch (e) {
    host.innerHTML = '<div class="issue">Corpus query failed: ' + escapeHtml(e.message) + '</div>';
  }
}
function renderSongTable() {
  const host = el('song-results');
  if (!state.songs.length) {
    host.innerHTML = '<div class="empty-state">No songs matched query.</div>';
    return;
  }
  host.innerHTML = '<table class="data-table"><thead><tr>' +
    '<th onclick="sortSongTable(\'title\')" class="th-sort">Title</th>' +
    '<th onclick="sortSongTable(\'artist\')" class="th-sort">Artist</th>' +
    '<th>Genre</th><th>Year</th><th>Action</th>' +
    '</tr></thead><tbody>' +
    state.songs.map(function row(s) {
      return '<tr class="cursor-pointer" onclick="loadSong(' + s.song_id + ')" title="Open in Song Analytics">' +
        '<td class="fw-600">' + escapeHtml(s.title) + '</td>' +
        '<td>' + escapeHtml(s.artist) + '</td>' +
        '<td>' + escapeHtml(s.genre) + '</td>' +
        '<td>' + escapeHtml(s.year || '-') + '</td>' +
        '<td>' +
          '<button class="btn-secondary btn-sm" onclick="event.stopPropagation(); useAsReference(' + s.song_id + ', \'' + escapeHtml(s.title.replace(/'/g, "\\'")) + '\', \'' + escapeHtml(s.artist.replace(/'/g, "\\'")) + '\')">Use as Blueprint</button>' +
        '</td>' +
      '</tr>';
    }).join('') +
    '</tbody></table>';
}
function useAsReference(songId, title, artist) {
  switchTab('ghostwriter');
  applyRef(title, artist);
}
function sortSongTable(key) {
  const dir = state.songSort.key === key ? -state.songSort.dir : 1;
  state.songSort = { key: key, dir: dir };
  state.songs.sort(function cmp(a, b) {
    const x = a[key];
    const y = b[key];
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * dir;
    return String(x).localeCompare(String(y)) * dir;
  });
  renderSongTable();
}
async function loadSong(songId) {
  setStatus('analysis-status', 'Loading song...', 'yellow');
  try {
    const song = await getJSON('/api/songs/' + encodeURIComponent(songId));
    const parts = [];
    for (const sec of song.sections || []) {
      if (sec.name) parts.push('[' + sec.name + ']');
      for (const line of sec.lines || []) parts.push(line.text);
      parts.push('');
    }
    const text = parts.join('\n').trim();
    openInSongAnalytics(text, (song.title || 'Untitled') + (song.artist ? ' — ' + song.artist : ''));
  } catch (e) {
    setStatus('analysis-status', 'Song load failed: ' + e.message, 'red');
  }
}
