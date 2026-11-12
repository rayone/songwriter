// Audio formatter: packages lyrics for YuE2 / MiniMax.

// ------------------------------------------------ Audio Formatter Tab
async function runAudioFormat() {
  const lyrics = val('fmt-input').trim();
  if (!lyrics) {
    setStatus('fmt-status', 'Paste lyrics first', 'yellow');
    return;
  }
  const tmplRaw = val('fmt-template');
  const tmplId = isNaN(tmplRaw) || !tmplRaw ? null : parseInt(tmplRaw, 10);
  setStatus('fmt-status', 'Formatting...', 'yellow');
  try {
    let song = (state.activeTakeId && state.takes[state.activeTakeId] && state.takes[state.activeTakeId].song)
      ? Object.assign({}, state.takes[state.activeTakeId].song)
      : Object.assign({}, state.song || defaultSongTemplate);
    if (tmplId != null) {
      song.template_id = tmplId;
    }
    const data = await postJSON('/api/music/format', {
      song: song,
      text: lyrics,
      engine: 'all'
    });
    setStatus('fmt-status', 'Done', 'green');
    el('fmt-results').innerHTML =
      '<div class="flex flex-col gap-16">' +
        '<div class="card">' +
          '<div class="pkg-head"><div class="pkg-title">YuE2 Package</div><button class="btn-secondary" onclick="copyBox(\'fmt-yue2\', this)">Copy</button></div>' +
          '<textarea id="fmt-yue2" class="pkg-box" rows="12" readonly>' + escapeHtml(data.yue2 || '') + '</textarea>' +
        '</div>' +
        '<div class="card">' +
          '<div class="pkg-head"><div class="pkg-title">MiniMax 3 Package</div><button class="btn-secondary" onclick="copyBox(\'fmt-minimax\', this)">Copy</button></div>' +
          '<textarea id="fmt-minimax" class="pkg-box" rows="12" readonly>' + escapeHtml(data.minimax || '') + '</textarea>' +
        '</div>' +
      '</div>';
  } catch (e) {
    setStatus('fmt-status', 'Packaging error: ' + e.message, 'red');
  }
}
async function loadFormatterTemplates() {
  try {
    const rows = await getJSON('/api/templates/search?limit=30');
    const sel = el('fmt-template');
    if (sel && rows.length) {
      sel.innerHTML = '<option value="">Generic defaults</option>' +
        rows.map(function opt(t) {
          return '<option value="' + t.id + '">' + escapeHtml(t.title + ' (' + t.genre + ', ' + t.bpm + ' BPM)') + '</option>';
        }).join('');
    }
  } catch (e) {}
}
