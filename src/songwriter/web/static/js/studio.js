// Song Analytics tab: editable scansion table, analysis and rhyme palette.

function togglePasteLyricsBox(forceOpen) {
  const drawer = el('paste-lyrics-drawer');
  if (!drawer) return;
  const isHidden = drawer.style.display === 'none';
  const open = forceOpen !== undefined ? forceOpen : isHidden;
  drawer.style.display = open ? 'block' : 'none';
  if (open) {
    const area = el('studio-text');
    if (area) area.focus();
  }
}
function loadPastedLyricsIntoTable() {
  const text = val('studio-text').trim();
  if (!text) {
    alert('Please paste or write some lyrics first.');
    return;
  }
  togglePasteLyricsBox(false);
  const tbody = el('unified-lyrics-tbody');
  if (tbody) tbody.innerHTML = '';
  runStudioAnalysis(text);
}
// Single entry point for opening lyrics in Song Analytics (corpus, library, takes).
// Clears the previous song's table first: runStudioAnalysis() prefers existing table
// rows over the text box, so a stale table used to win over the newly loaded song.
function openInSongAnalytics(text, label) {
  const area = el('studio-text');
  if (area) area.value = text || '';
  const tbody = el('unified-lyrics-tbody');
  if (tbody) tbody.innerHTML = '';
  const title = el('studio-song-title');
  if (title) {
    title.textContent = label || '';
    title.classList.toggle('hidden', !label);
  }
  switchTab('studio');
  runStudioAnalysis(text || '');
}
let activeRhymeTargetInput = null;
async function runStudioAnalysis(forceRawText = null) {
  const linesFromTable = forceRawText ? [] : getLinesFromUnifiedTable();
  const rawText = forceRawText ? forceRawText.trim() : (linesFromTable.length ? linesFromTable.join('\n') : val('studio-text').trim());
  if (!rawText) {
    const tbody = el('unified-lyrics-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="td-empty">Paste or write lyrics below to initialize real-time table.</td></tr>';
    if (el('issues-container')) el('issues-container').innerHTML = '';
    if (el('similarity-matrix-container')) el('similarity-matrix-container').innerHTML = '<div class="empty-state">Matrix generates automatically when lyrics are analyzed.</div>';
    if (el('section-symmetry-card')) el('section-symmetry-card').style.display = 'none';
    setStatus('analysis-status', 'Ready', 'green');
    return;
  }

  setStatus('analysis-status', 'Evaluating real-time...', 'yellow');
  try {
    const curKey = val('gw-key') || 'C';
    const curScale = val('gw-scale') || 'major';
    const data = await postJSON('/api/analyze_song', { lyrics: rawText, key: curKey, scale: curScale });

    // 1. Cadence & Pulse Pillar
    if (el('m-cadence-var')) el('m-cadence-var').innerText = data.variance.toFixed(2);
    if (el('m-cadence-label')) el('m-cadence-label').innerText = data.cadence_consistency;

    // 2. Vocabulary & TTR Pillar
    if (el('m-ttr-val')) el('m-ttr-val').innerText = `${data.ttr}%`;
    if (el('m-ttr-label')) el('m-ttr-label').innerText = `${data.ttr_diagnostic} (${data.unique_words}/${data.word_count} words)`;

    // 3. Rhyme Architecture Pillar
    if (el('m-density')) el('m-density').innerText = data.rhyme_density != null ? Math.round(data.rhyme_density * 100) + '%' : '-';
    if (el('m-scheme')) el('m-scheme').innerText = `Scheme: ${data.rhyme_scheme || '-'}`;

    // 4. Syllable & Line Averages
    if (el('m-avg-syl')) el('m-avg-syl').innerText = data.avg_syllables.toFixed(1);
    if (el('m-lines-label')) el('m-lines-label').innerText = `${data.line_count} total lines (±${data.std_dev} SD)`;

    // 5. Harmony Progression & Roman Numerals
    const harm = data.harmony || {};
    if (el('studio-harmony-prog-name')) el('studio-harmony-prog-name').innerText = harm.progression_name || '';
    if (el('studio-chords-list') && harm.chords) {
      el('studio-chords-list').innerHTML = harm.chords.map(c => `<span class="badge badge-warning">${escapeHtml(c)}</span>`).join(' ');
    }
    if (el('studio-roman-numerals') && harm.roman_numerals) {
      el('studio-roman-numerals').innerText = `Harmonic Progression: ${harm.roman_numerals.join(' - ')} (${harm.key} ${harm.scale})`;
    }

    // 6. Sensory Concreteness & Mood Architecture
    const sens = data.sensory || {};
    const mood = data.mood || {};
    const sensInfoEl = el('studio-sensory-info');
    if (sensInfoEl) {
      const concScore = sens.mean_concreteness != null ? Number(sens.mean_concreteness).toFixed(2) : (data.mean_concreteness != null ? Number(data.mean_concreteness).toFixed(2) : '3.00');
      const emoStr = mood.dominant_emotion || data.dominant_emotion || 'Introspective';
      const modStr = sens.dominant_modality || data.dominant_modality || 'Visual & Auditory';
      const concPct = Math.round((sens.concrete_ratio || 0) * 100);
      const absPct = Math.round((sens.abstract_ratio || 0) * 100);
      const anchorsHtml = sens.sensory_words && sens.sensory_words.length
        ? `<div class="fs-xs c-dim mt-3">Sensory anchors: ${sens.sensory_words.map(w => escapeHtml(w.word)).join(', ')}</div>`
        : '';

      const cVal = sens.mean_concreteness != null ? Number(sens.mean_concreteness) : (data.mean_concreteness != null ? Number(data.mean_concreteness) : 3.0);
      const cPct = Math.min(100, Math.max(10, Math.round((cVal / 5.0) * 100)));
      const cTier = cVal >= 3.5 ? 'Highly Sensory / Tangible Objects' : (cVal >= 2.5 ? 'Balanced Pop & Narrative' : 'Abstract / Emotional Reflection');
      sensInfoEl.innerHTML = `
        <div class="flex justify-between mb-3">
          <span><b>Dominant Emotion:</b> <span class="c-cyan fw-700">${escapeHtml(emoStr)}</span></span>
          <span class="fs-xs c-dim">Valence: ${(mood.valence || 0.5).toFixed(2)} · Arousal: ${(mood.arousal || 0.5).toFixed(2)}</span>
        </div>
        <div class="mt-4 mb-2">
          <div class="flex justify-between fs-xs">
            <span><b>Concreteness:</b> <span class="c-emerald fw-800">${cVal.toFixed(2)}</span> / 5.0 Max</span>
            <span class="c-muted fs-xs">${cTier}</span>
          </div>
          <div class="progress-bar mt-3">
            <div id="studio-concreteness-fill" class="progress-fill"></div>
          </div>
        </div>
        <div class="flex justify-between fs-xs c-muted mt-4">
          <span><b>Sensory Modality:</b> <span class="c-bright">${escapeHtml(modStr)}</span></span>
          <span>${concPct}% sensory · ${absPct}% abstract</span>
        </div>
        ${anchorsHtml}
        <div class="fs-2xs c-dim mt-2">Sense Scale: 1.0 (Abstract thoughts) ➔ 5.0 (Tangible sensory nouns)</div>
      `;
      const sFill = el('studio-concreteness-fill');
      if (sFill) sFill.style.width = cPct + '%';
    }
    // Global Song-Level Diagnostics (Abstract ratio, overall lyric texture)
    const guidanceBox = el('global-guidance-container');
    if (guidanceBox) {
      const globalIssues = (data.issues || []).filter(i => !i.toLowerCase().startsWith('line'));
      if (globalIssues.length) {
        guidanceBox.innerHTML = globalIssues.map(i => `<div class="status-line alert-warning">ℹ ${escapeHtml(i)}</div>`).join('');
      } else {
        guidanceBox.innerHTML = '<div class="status-line alert-success">✓ Complete song cadence & structure validated.</div>';
      }
    }
    // Render In-Place Editable Scansion Table Rows
    renderUnifiedTableRows(data.lines || [], true);
    // Self-Similarity Matrix Heatmap
    const matrixHost = el('similarity-matrix-container');
    const matrixSize = el('matrix-size-label');
    if (matrixHost && data.similarity_matrix && data.similarity_matrix.length) {
      const n = data.similarity_matrix.length;
      if (matrixSize) matrixSize.innerText = `${n} x ${n}`;
      let mHtml = '<div class="matrix-grid">';
      for (let r = 0; r < n; r++) {
        mHtml += '<div class="flex">';
        for (let c = 0; c < n; c++) {
          const score = data.similarity_matrix[r][c];
          let cCls = 'matrix-c0';
          if (score >= 0.99) cCls = 'matrix-c4';
          else if (score >= 0.5) cCls = 'matrix-c3';
          else if (score >= 0.25) cCls = 'matrix-c2';
          else if (score > 0) cCls = 'matrix-c1';
          const title = `L${r+1} vs L${c+1}: ${Math.round(score * 100)}% similarity`;
          mHtml += `<div title="${escapeHtml(title)}" class="matrix-cell ${cCls}"></div>`;
        }
        mHtml += '</div>';
      }
      mHtml += '</div>';
      matrixHost.innerHTML = mHtml;
    }

    setStatus('analysis-status', 'Real-Time Synchronized', 'green');
  } catch (e) {
    setStatus('analysis-status', 'Analysis failed: ' + e.message, 'red');
  }
}
function renderUnifiedTableRows(lines, force = false) {
  const tbody = el('unified-lyrics-tbody');
  if (!tbody) return;

  if (!lines || !lines.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="td-empty">No lyric lines analyzed yet.</td></tr>';
    return;
  }

  tbody.innerHTML = lines.map(function(l, idx) {
    const isOptimal = (l.syllables >= 8 && l.syllables <= 10);
    const badgeCls = isOptimal ? 'badge-optimal' : 'badge-suboptimal';
    const endWord = l.end_word || '';

    // Inline Cliches & Symmetry Badges directly beneath the line text
    const clichesHtml = (l.cliches && l.cliches.length)
      ? l.cliches.map(c => `<span class="badge-cliche">⚠ Cliche: ${escapeHtml(c)}</span>`).join(' ')
      : '';

    let symHtml = '';
    if (l.symmetry) {
      const symCls = l.symmetry.flag ? 'badge-cliche' : 'badge-clean';
      symHtml = `<span class="${symCls}">${escapeHtml(l.symmetry.msg)}</span>`;
    }

    const diagnosticsHtml = (clichesHtml || symHtml)
      ? `<div class="flex flex-wrap gap-6 mt-4">${clichesHtml} ${symHtml}</div>`
      : '';

    // Clickable interactive word chips for full-line word substitution
    const words = l.text.split(/(\s+)/);
    const chipsHtml = words.map(function(token) {
      const cleanW = token.replace(/[^a-zA-Z']/g, '');
      if (!cleanW) return escapeHtml(token);
      return `<span class="intellisense-word" title="Click for RhymeDB word substitution" onclick="openWordIntellisense(event, '${escapeHtml(cleanW)}', ${idx})">${escapeHtml(token)}</span>`;
    }).join('');

    return `
      <tr class="canvas-row" id="row-line-${idx}">
        <td class="canvas-cell-num">${idx + 1}</td>
        <td class="canvas-cell-sec">
          <input type="text" class="canvas-sec-input" value="${escapeHtml(l.section)}" oninput="debounceAnalysis()">
        </td>
        <td class="canvas-cell-line">
          <div class="flex flex-col gap-3">
            <input type="text" class="canvas-line-input" id="line-input-${idx}" value="${escapeHtml(l.text)}" oninput="debounceAnalysis()">
            <div class="canvas-palette">
              <span class="canvas-palette-label">Word Palette:</span>
              ${chipsHtml}
            </div>
            ${diagnosticsHtml}
          </div>
        </td>
        <td class="canvas-cell-syl">
          <span class="badge ${badgeCls} line-badge" id="badge badge-warning-${idx}">${l.syllables}</span>
        </td>
        <td class="canvas-cell-stress" id="stress-grid-${idx}">
          ${escapeHtml(l.stress_grid || '')}
        </td>
        <td class="canvas-cell-rhyme">
          <button type="button" class="btn-secondary rhyme-btn" onclick="openRhymeDropdown(event, '${escapeHtml(endWord)}', ${idx})">
            ${escapeHtml(endWord || '(Rhyme)')} ▾
          </button>
        </td>
        <td class="canvas-cell-action">
          <button type="button" class="btn-icon c-rose" title="Delete line" onclick="deleteLyricLine(${idx})">✕</button>
        </td>
      </tr>
    `;
  }).join('');
}
function getLinesFromUnifiedTable() {
  const rows = document.querySelectorAll('#unified-lyrics-tbody .canvas-row');
  if (!rows || !rows.length) return [];
  const out = [];
  let lastSec = '';
  rows.forEach(function(r) {
    const sec = (r.querySelector('.canvas-sec-input')?.value || '').trim();
    const line = (r.querySelector('.canvas-line-input')?.value || '').trim();
    if (sec && sec !== lastSec) {
      out.push(`[${sec}]`);
      lastSec = sec;
    }
    if (line) out.push(line);
  });
  return out;
}
function addNewLyricLine() {
  const tbody = el('unified-lyrics-tbody');
  if (!tbody) return;
  const idx = tbody.querySelectorAll('tr').length;
  const tr = document.createElement('tr');
  tr.className = 'canvas-row';
  tr.id = `row-line-${idx}`;
  tr.innerHTML = `
    <td class="canvas-cell-num">${idx + 1}</td>
    <td class="canvas-cell-sec">
      <input type="text" class="canvas-sec-input" value="Verse" oninput="debounceAnalysis()">
    </td>
    <td class="canvas-cell-line">
      <input type="text" class="canvas-line-input" id="line-input-${idx}" value="" placeholder="Type new lyric line..." oninput="debounceAnalysis()">
    </td>
    <td class="canvas-cell-syl">
      <span class="badge badge-suboptimal line-badge" id="badge badge-warning-${idx}">0</span>
    </td>
    <td class="canvas-cell-stress" id="stress-grid-${idx}"></td>
    <td class="canvas-cell-rhyme">
      <button type="button" class="btn-secondary rhyme-btn" onclick="openRhymeDropdown(event, '', ${idx})">(Rhyme) ▾</button>
    </td>
    <td class="canvas-cell-action">
      <button type="button" class="btn-icon c-rose" onclick="deleteLyricLine(${idx})">✕</button>
    </td>
  `;
  tbody.appendChild(tr);
  tr.querySelector('.canvas-line-input')?.focus();
}
function deleteLyricLine(idx) {
  const row = el(`row-line-${idx}`);
  if (row) row.remove();
  runStudioAnalysis();
}
async function openWordIntellisense(event, word, lineIdx) {
  event.stopPropagation();
  activeRhymeTargetInput = lineIdx;
  const dropdown = el('rhyme-smart-dropdown');
  if (!dropdown) return;

  const rect = event.target.getBoundingClientRect();
  dropdown.style.display = 'block';
  dropdown.style.top = `${Math.min(window.innerHeight - 340, rect.bottom + 6)}px`;
  dropdown.style.left = `${Math.min(window.innerWidth - 280, rect.left)}px`;

  el('rhyme-dropdown-word').innerText = `Substitutes for '${word}'`;
  const container = el('rhyme-dropdown-items');
  container.innerHTML = '<div class="empty-state p-10">Querying RhymeDB...</div>';

  try {
    const res = await getJSON(`/api/rhymes?word=${encodeURIComponent(word)}&limit=30`);
    const matches = (res && res.matches) ? res.matches : (Array.isArray(res) ? res : []);
    if (!matches || !matches.length) {
      container.innerHTML = '<div class="empty-state p-10">No direct substitutes found.</div>';
      return;
    }

    container.innerHTML = matches.map(function(m) {
      const w = m.word || m;
      const kindBadge = m.kind ? `<span class="fs-2xs c-dim uppercase">${m.kind}</span>` : '';
      return `
        <div class="rhyme-item" onclick="substituteAnyWord('${escapeHtml(w)}', '${escapeHtml(word)}')">
          <b>${escapeHtml(w)}</b>
          ${kindBadge}
        </div>
      `;
    }).join('');
  } catch (err) {
    container.innerHTML = `<div class="empty-state c-rose p-10">Error: ${escapeHtml(err.message)}</div>`;
  }
}
function substituteAnyWord(newWord, targetWord) {
  if (activeRhymeTargetInput == null) return;
  const input = el(`line-input-${activeRhymeTargetInput}`);
  if (!input) return;

  const curVal = input.value;
  // Replace whole target word with boundaries
  const regex = new RegExp(`\\b${targetWord}\\b`, 'i');
  if (regex.test(curVal)) {
    input.value = curVal.replace(regex, newWord);
  } else {
    input.value = curVal + ' ' + newWord;
  }
  closeRhymeDropdown();
  runStudioAnalysis();
}
async function openRhymeDropdown(event, word, lineIdx) {
  if (event) event.stopPropagation();
  activeRhymeTargetInput = lineIdx;
  const dropdown = el('rhyme-smart-dropdown');
  if (!dropdown) return;

  const lineInput = el(`line-input-${lineIdx}`);
  const currentLineText = lineInput ? lineInput.value : '';
  const lastWord = currentLineText.trim().split(/\s+/).pop()?.replace(/[^a-zA-Z']/g, '') || '';
  const targetWord = word || lastWord || 'love';

  if (event && event.target) {
    const rect = event.target.getBoundingClientRect();
    dropdown.style.display = 'block';
    dropdown.style.top = `${Math.min(window.innerHeight - 340, rect.bottom + 6)}px`;
    dropdown.style.left = `${Math.min(window.innerWidth - 280, rect.left)}px`;
  }

  el('rhyme-dropdown-word').innerText = `Rhymes for '${targetWord}'`;
  const container = el('rhyme-dropdown-items');
  container.innerHTML = '<div class="empty-state p-10">Querying RhymeDB...</div>';

  try {
    const res = await getJSON(`/api/rhymes?word=${encodeURIComponent(targetWord)}&limit=30`);
    const matches = (res && res.matches) ? res.matches : (Array.isArray(res) ? res : []);
    if (!matches || !matches.length) {
      container.innerHTML = '<div class="empty-state p-10">No direct rhymes found. Try near-rhymes.</div>';
      return;
    }

    container.innerHTML = matches.map(function(m) {
      const w = m.word || m;
      const kindBadge = m.kind ? `<span class="fs-2xs c-dim uppercase">${m.kind}</span>` : '';
      return `
        <div class="rhyme-item" onclick="substituteEndWord('${escapeHtml(w)}', '${escapeHtml(targetWord)}')">
          <b>${escapeHtml(w)}</b>
          ${kindBadge}
        </div>
      `;
    }).join('');
  } catch (err) {
    container.innerHTML = `<div class="empty-state c-rose p-10">Error: ${escapeHtml(err.message)}</div>`;
  }
}
function substituteEndWord(newWord, oldWord) {
  if (activeRhymeTargetInput == null) return;
  const input = el(`line-input-${activeRhymeTargetInput}`);
  if (!input) return;

  let val = input.value.trim();
  if (oldWord && val.toLowerCase().endsWith(oldWord.toLowerCase())) {
    val = val.slice(0, val.length - oldWord.length).trimEnd() + ' ' + newWord;
  } else {
    val = val + ' ' + newWord;
  }
  input.value = val;
  closeRhymeDropdown();
  runStudioAnalysis();
}
function closeRhymeDropdown() {
  const dropdown = el('rhyme-smart-dropdown');
  if (dropdown) dropdown.style.display = 'none';
  activeRhymeTargetInput = null;
}
window.addEventListener('click', function(e) {
  const dropdown = el('rhyme-smart-dropdown');
  if (dropdown && dropdown.style.display !== 'none' && !dropdown.contains(e.target)) {
    closeRhymeDropdown();
  }
});
function copyUnifiedLyrics(btn) {
  const lines = getLinesFromUnifiedTable();
  const text = lines.join('\n');
  if (!text) return;
  navigator.clipboard.writeText(text);
  if (btn) {
    btn.innerText = 'Copied!';
    setTimeout(() => { btn.innerText = 'Copy Lyrics'; }, 1500);
  }
}
const debounceAnalysis = debounce(runStudioAnalysis, 400);
