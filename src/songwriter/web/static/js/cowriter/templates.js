// Cowriter Step 1/2: production-template omnibox and spec defaults.

let activeTemplateGenreFilter = '';
let templateSearchTimer = null;
let cachedTemplates = [];
let highlightedTemplateIndex = -1;
let templateGenreStats = { genre_count: 0, template_count: 0 };

// Every genre in the template table, largest first, as one native select.
// 140+ genres don't fit as pills; a select stays one line and is keyboard/screen-reader friendly.
async function loadTemplateGenres() {
  const sel = el('gw-genre-filter');
  if (!sel) return;
  try {
    const data = await getJSON('/api/templates/genres');
    templateGenreStats = data;
    sel.innerHTML =
      '<option value="">All genres (' + data.template_count + ')</option>' +
      data.genres.map(function(g) {
        return '<option value="' + escapeHtml(g.genre) + '">' + escapeHtml(g.genre) + ' (' + g.count + ')</option>';
      }).join('');
    sel.value = activeTemplateGenreFilter || '';
    const badge = el('gw-template-count-badge');
    if (badge) badge.innerText = data.template_count.toLocaleString() + ' Sound Profiles · ' + data.genre_count + ' Genres';
  } catch (err) {
    sel.innerHTML = '<option value="">Genres unavailable: ' + escapeHtml(err.message) + '</option>';
  }
}
function syncGenreFilter(genre) {
  const sel = el('gw-genre-filter');
  if (sel) sel.value = genre || '';
}
async function loadTemplatesForDropdown(query = '', genre = '') {
  const listEl = el('gw-template-dropdown-list');
  const statusEl = el('gw-template-dropdown-status');
  if (!listEl) return;
  listEl.innerHTML = '<div class="empty-state">Searching sound profiles...</div>';
  try {
    let url = '/api/templates/search?limit=5000';
    if (query) url += '&q=' + encodeURIComponent(query);
    if (genre) url += '&genre=' + encodeURIComponent(genre);
    const data = await getJSON(url);
    cachedTemplates = data || [];
    if (statusEl) {
      const scope = [genre ? 'genre "' + genre + '"' : '', query ? '"' + query + '"' : ''].filter(Boolean).join(' + ');
      statusEl.innerText = scope
        ? `${cachedTemplates.length} sound profiles matching ${scope}`
        : `Showing all ${cachedTemplates.length} sound profiles across ${templateGenreStats.genre_count || '—'} genres`;
    }
    renderTemplateDropdownList(cachedTemplates);
  } catch (err) {
    listEl.innerHTML = '<div class="empty-state c-rose">Failed to load templates: ' + escapeHtml(err.message) + '</div>';
  }
}
function renderTemplateDropdownList(templates) {
  const listEl = el('gw-template-dropdown-list');
  if (!listEl) return;
  if (!templates || !templates.length) {
    listEl.innerHTML = '<div class="empty-state">No matching templates found. Try different keywords or click "All".</div>';
    return;
  }
  highlightedTemplateIndex = -1;
  listEl.innerHTML = templates.map(function(t, idx) {
    const textSnippet = t.audio_prompt || t.title;
    return `
      <div class="template-item" id="tmpl-item-${idx}"
           onmousedown="selectTemplateById(event, ${t.id})"
           onclick="selectTemplateById(event, ${t.id})">
        <div class="flex justify-between items-start mb-4 gap-8">
          <div class="fw-700 c-bright fs-md">${escapeHtml(t.title)}</div>
          <div class="ref-badges flex-shrink-0">
            ${t.bpm ? `<span class="badge badge-warning badge-xs">${t.bpm} BPM</span>` : ''}
            ${t.music_key ? `<span class="badge badge-secondary badge-xs">${escapeHtml(t.music_key)}${t.scale ? ' ' + escapeHtml(t.scale) : ''}</span>` : ''}
          </div>
        </div>
        <div class="ref-badges mb-6">
          <span class="badge badge-info badge-xs">${escapeHtml(t.genre || 'Genre')}</span>
          ${t.subgenre ? `<span class="badge badge-dim badge-xs">${escapeHtml(t.subgenre)}</span>` : ''}
          ${t.vocal ? `<span class="badge badge-info badge-xs c-cyan">${escapeHtml(t.vocal)}</span>` : ''}
        </div>
        <div class="fs-xs c-muted lh-14 line-clamp-2">
          ${escapeHtml(textSnippet)}
        </div>
      </div>
    `;
  }).join('');
}
function openTemplateDropdown() {
  const menu = el('gw-template-dropdown-menu');
  if (menu) menu.style.display = 'block';
  if (!cachedTemplates.length) {
    const q = (val('gw-style-input') || '').trim();
    loadTemplatesForDropdown(q, activeTemplateGenreFilter);
  }
}
function closeTemplateDropdown() {
  const menu = el('gw-template-dropdown-menu');
  if (menu) menu.style.display = 'none';
}
function toggleTemplateDropdown() {
  const menu = el('gw-template-dropdown-menu');
  if (!menu) return;
  if (menu.style.display === 'none' || menu.style.display === '') {
    openTemplateDropdown();
  } else {
    closeTemplateDropdown();
  }
}
function onOmniboxInput(val) {
  clearTimeout(templateSearchTimer);
  const clearBtn = el('gw-template-clear-btn');
  if (clearBtn) clearBtn.style.display = val ? 'inline-block' : 'none';
  openTemplateDropdown();
  templateSearchTimer = setTimeout(function() {
    loadTemplatesForDropdown(val.trim(), activeTemplateGenreFilter);
  }, 220);
}
function filterTemplateGenre(genre) {
  activeTemplateGenreFilter = genre || '';
  syncGenreFilter(activeTemplateGenreFilter);
  loadTemplatesForDropdown((val('gw-style-input') || '').trim(), activeTemplateGenreFilter);
}
function onTemplateSearchKeyDown(e) {
  const menu = el('gw-template-dropdown-menu');
  if (!menu || menu.style.display === 'none') {
    if (e.key === 'ArrowDown') {
      openTemplateDropdown();
      e.preventDefault();
    }
    return;
  }
  const items = menu.querySelectorAll('.template-item');
  if (!items.length) return;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    highlightedTemplateIndex = Math.min(highlightedTemplateIndex + 1, items.length - 1);
    updateTemplateHighlight(items);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    highlightedTemplateIndex = Math.max(highlightedTemplateIndex - 1, 0);
    updateTemplateHighlight(items);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (highlightedTemplateIndex >= 0 && highlightedTemplateIndex < cachedTemplates.length) {
      selectTemplateById(null, cachedTemplates[highlightedTemplateIndex].id);
    }
  } else if (e.key === 'Escape') {
    closeTemplateDropdown();
  }
}
function updateTemplateHighlight(items) {
  items.forEach(function(item, idx) {
    if (idx === highlightedTemplateIndex) {
      item.classList.add('highlighted');
      item.scrollIntoView({ block: 'nearest' });
    } else {
      item.classList.remove('highlighted');
    }
  });
}
async function selectTemplateById(e, templateId) {
  if (e) {
    if (e.preventDefault) e.preventDefault();
    if (e.stopPropagation) e.stopPropagation();
  }
  closeTemplateDropdown();
  try {
    const tmpl = await showTemplateCard(templateId);
    // Choosing a template is an explicit change: its BPM/key/vocal become the new specs.
    if (tmpl) applyDefaults(defaultsFromMeta(tmpl.metadata || {}));
  } catch (err) {
    console.error('Failed to load template detail:', err);
  }
}
// Show a template as the selected sound profile without touching any specs.
// Used when reopening a saved song, whose saved BPM/key/vocal must win.
async function showTemplateCard(templateId) {
  const tmpl = await getJSON('/api/templates/' + templateId);
  if (!tmpl) return null;
  state.activeTemplate = tmpl;
  const meta = tmpl.metadata || {};
  el('gw-selected-template-id').value = meta.id;

  const card = el('gw-selected-template-card');
  if (card) {
    card.style.display = 'block';
    el('gw-card-tmpl-title').innerText = meta.title || 'Production Template';
    const badgesEl = el('gw-card-tmpl-badges');
    if (badgesEl) {
      badgesEl.innerHTML = `
        <span class="badge badge-info">${escapeHtml(meta.genre || 'Genre')}</span>
        ${meta.subgenre ? `<span class="badge badge-dim">${escapeHtml(meta.subgenre)}</span>` : ''}
        ${meta.bpm ? `<span class="badge badge-warning">${meta.bpm} BPM</span>` : ''}
        ${meta.music_key ? `<span class="badge badge-secondary">${escapeHtml(meta.music_key)}${meta.scale ? ' ' + escapeHtml(meta.scale) : ''}</span>` : ''}
        ${meta.vocal ? `<span class="badge badge-info">${escapeHtml(meta.vocal)}</span>` : ''}
      `;
    }
    const textEl = el('gw-card-tmpl-text');
    if (textEl) textEl.innerText = meta.audio_prompt || tmpl.minimax_caption || tmpl.yue2_style || meta.title;
    const proTipEl = el('gw-card-tmpl-protip');
    if (proTipEl) {
      proTipEl.innerText = meta.pro_tip ? '💡 Pro-Tip: ' + meta.pro_tip : '';
      proTipEl.style.display = meta.pro_tip ? 'block' : 'none';
    }
  }
  const omnibox = el('gw-style-input');
  if (omnibox) omnibox.value = meta.audio_prompt || meta.title;
  const clearBtn = el('gw-template-clear-btn');
  if (clearBtn) clearBtn.style.display = 'inline-block';
  return tmpl;
}
function clearActiveTemplate() {
  state.activeTemplate = null;
  el('gw-selected-template-id').value = '';
  const card = el('gw-selected-template-card');
  if (card) card.style.display = 'none';
  const omnibox = el('gw-style-input');
  if (omnibox) omnibox.value = '';
  const clearBtn = el('gw-template-clear-btn');
  if (clearBtn) clearBtn.style.display = 'none';
  loadTemplatesForDropdown('', activeTemplateGenreFilter);
}
document.addEventListener('click', function(e) {
  const container = el('gw-search-selection-container');
  const menu = el('gw-template-dropdown-menu');
  if (container && menu && !container.contains(e.target)) {
    closeTemplateDropdown();
  }
});
function defaultsFromMeta(meta) {
  return {
    template_id: meta.id,
    template_title: meta.title,
    genre: meta.genre,
    subgenre: meta.subgenre,
    bpm: meta.bpm,
    music_key: meta.music_key,
    scale: meta.scale,
    vocal: meta.vocal,
    audio_prompt: meta.audio_prompt,
    pro_tip: meta.pro_tip,
    syllable_target: val('gw-syllables') || '8-10',
    banned_words: state.defaults.banned_words || []
  };
}
function applyDefaults(defaults) {
  state.defaults = defaults || {};
  setFieldValue('gw-bpm', defaults.bpm);
  setFieldValue('gw-key', defaults.music_key);
  setFieldValue('gw-scale', defaults.scale);
  setFieldValue('gw-vocal', defaults.vocal);
  setFieldValue('gw-syllables', defaults.syllable_target);
  if (defaults.template_id != null) el('gw-selected-template-id').value = defaults.template_id;
  if (Array.isArray(defaults.banned_words) && !val('gw-banned')) {
    el('gw-banned').value = defaults.banned_words.join(', ');
  }
  onVocalChanged();
  const info = el('gw-matched-info');
  if (info) {
    info.innerText = defaults.template_title
      ? 'Template: ' + defaults.template_title
      : 'Generic production defaults active.';
  }

  const card = el('gw-selected-template-card');
  if (card && defaults.template_id != null && defaults.template_title) {
    card.style.display = 'block';
    el('gw-card-tmpl-title').innerText = defaults.template_title;
    const badgesEl = el('gw-card-tmpl-badges');
    if (badgesEl) {
      badgesEl.innerHTML = `
        <span class="badge badge-info">${escapeHtml(defaults.genre || 'Template')}</span>
        ${defaults.subgenre ? `<span class="badge badge-dim">${escapeHtml(defaults.subgenre)}</span>` : ''}
        ${defaults.bpm ? `<span class="badge badge-warning">${defaults.bpm} BPM</span>` : ''}
        ${defaults.music_key ? `<span class="badge badge-secondary">${escapeHtml(defaults.music_key)}${defaults.scale ? ' ' + escapeHtml(defaults.scale) : ''}</span>` : ''}
        ${defaults.vocal ? `<span class="badge badge-info">${escapeHtml(defaults.vocal)}</span>` : ''}
      `;
    }
    const textEl = el('gw-card-tmpl-text');
    if (textEl && defaults.audio_prompt) {
      textEl.innerText = defaults.audio_prompt;
    }
    const proTipEl = el('gw-card-tmpl-protip');
    if (proTipEl) {
      if (defaults.pro_tip) {
        proTipEl.innerText = '💡 Pro-Tip: ' + defaults.pro_tip;
        proTipEl.style.display = 'block';
      } else {
        proTipEl.style.display = 'none';
      }
    }
    const searchInput = el('gw-template-search-input');
    if (searchInput && !searchInput.value) {
      searchInput.value = defaults.template_title;
    }
  }
}
async function refreshTemplateMatches() {
  const style = val('gw-style-input');
  if (!style) return;
  try {
    const data = await getJSON('/api/ai/recommend?q=' + encodeURIComponent(style));
    const primary = data.primary_template;
    if (primary && primary.metadata) {
      el('gw-selected-template-id').value = primary.metadata.id;
      applyDefaults(defaultsFromMeta(primary.metadata));
    }
  } catch (e) {}
}
