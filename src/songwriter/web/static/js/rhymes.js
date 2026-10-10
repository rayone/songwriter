// Rhyme Finder tab.

// ------------------------------------------------ Rhyme Finder Tab
// Register labels the writer has chosen to exclude. One list for every label,
// so none of them has handling of its own.
function excludedTags() {
  const boxes = document.querySelectorAll('.rhyme-tag-exclude');
  const out = [];
  for (const box of boxes) if (box.checked) out.push(box.value);
  return out;
}

// Register labels as badges. 'slang' gets its own colour because it is the one
// a lyricist scans for; everything else shares a neutral style.
function renderTags(tags) {
  if (!tags || !tags.length) return '';
  return tags.map(function (t) {
    const cls = (t === 'slang') ? 'badge badge-accent' : 'badge badge-dim';
    return '<span class="badge ' + cls +'">' + escapeHtml(t) + '</span> ';
  }).join('');
}
async function loadRhymeDbStats() {
  const host = el('rhyme-db-stats');
  if (!host) return;
  try {
    const h = await getJSON('/api/rhymes/health');
    const s = h.stats || {};
    host.textContent = (s.words || 0).toLocaleString() + ' words · ' +
      (s.pronunciations || 0).toLocaleString() + ' pronunciations · ' +
      h.checks_passed + '/' + h.checks_total + ' checks passed';
  } catch (e) {
    host.textContent = 'database not built';
  }
}
async function searchRhymes() {
  const word = val('rhyme-query').trim();
  if (!word) return;
  const host = el('rhyme-results');
  const targetHost = el('rhyme-target');
  host.innerHTML = '<div class="empty-state">Searching...</div>';
  targetHost.innerHTML = '';
  try {
    const kind = val('rhyme-kind');
    const syllables = val('rhyme-syl');
    const rankBy = val('rhyme-prefer');
    const phrases = el('rhyme-phrases').checked ? 'true' : 'false';
    const verified = el('rhyme-verified').checked ? 'true' : 'false';
    const minFreq = val('rhyme-minfreq') || '0';
    const exclude = excludedTags();

    let url = '/api/rhymes?word=' + encodeURIComponent(word) +
      '&kind=' + encodeURIComponent(kind) +
      '&limit=60' +
      '&phrases=' + phrases +
      '&verified_only=' + verified +
      '&rank_by=' + encodeURIComponent(rankBy) +
      '&min_freq=' + encodeURIComponent(minFreq);
    if (syllables) url += '&syllables=' + syllables;
    if (exclude) url += '&exclude_tags=' + encodeURIComponent(exclude);

    const res = await getJSON(url);

    // Show what the search rhymed ON, so a wrong result is diagnosable rather
    // than mysterious.
    if (res.target) {
      const t = res.target;
      const prons = (t.pronunciations || []).map(function (p) {
        return '<div class="mt-4"><span class="mono-phones">' + escapeHtml(p.arpabet) +
          '</span> &nbsp;rime <span class="mono-phones">' + escapeHtml(p.rime || '?') +
          '</span> &nbsp;<span class="c-dim fs-xs">' +
          escapeHtml(p.note || '') + (p.is_variant ? ' · variant' : '') + '</span></div>';
      }).join('');
      // The definitions. A rhyme list is unusable for writing without them:
      // 'knight' and 'night' are the same sound and different words.
      const defs = (t.glosses || []).map(function (g) {
        return '<div class="mt-3 fs-md">' +
          escapeHtml(g) + '</div>';
      }).join('');

      targetHost.innerHTML = '<div class="card"><div class="card-header"><span>' +
        escapeHtml(t.word) + '</span><span class="fs-xs c-dim">' +
        (t.corpus_freq || 0).toLocaleString() + ' uses · ' +
        (t.end_freq || 0).toLocaleString() + ' line-final</span></div>' +
        prons +
        (defs ? '<div class="mt-10 pt-8 divider-top">' +
                defs + '</div>' : '') +
        '<div class="mt-8">' +
        renderTags(t.tags) +
        (t.is_phrase ? '<span class="badge badge-info">phrase</span> ' : '') +
        (t.confidence >= 2 ? '<span class="badge badge-success">verified</span>'
                           : '<span class="badge badge-dim">single source</span>') +
        (res.unrhymable ? ' <span class="badge badge-error">listed unrhymable</span>' : '') +
        '</div></div>';
    } else if (!res.known) {
      targetHost.innerHTML = '<div class="issue">' + escapeHtml(word) +
        ' is not in the dictionary.</div>';
    }

    const matches = res.matches || [];
    if (!matches.length) {
      host.innerHTML = '<div class="empty-state">No ' + escapeHtml(res.kind) +
        ' rhymes found' + (res.known ? '' : ' (word unknown)') + '.</div>';
      return;
    }

    host.innerHTML = '<table class="data-table"><thead><tr>' +
      '<th>Word</th><th>Meaning</th><th>Phones</th><th>Syl</th>' +
      '<th>Commonness</th><th>Line-final</th><th>Labels</th>' +
      '</tr></thead><tbody>' +
      matches.map(function row(m) {
        const labels = renderTags(m.tags) +
          (m.is_phrase ? '<span class="badge badge-info">phrase</span> ' : '') +
          (m.verified ? '' : '<span class="badge badge-dim">unverified</span>');
        // The definition is the column that makes a rhyme usable: it is what
        // separates 'knight' from 'night' when the sound is identical.
        const pos = (m.pos || []).length ? m.pos.join('/') : '';
        const meaning = m.definition
          ? '<span class="fs-md">' + escapeHtml(m.definition) + '</span>'
          : '<span class="c-dim fs-xs">' +
            (pos ? escapeHtml(pos) : 'no definition') + '</span>';
        return '<tr>' +
          '<td class="fw-600 fs-lg nowrap">' + escapeHtml(m.word) + '</td>' +
          '<td class="max-w-420">' + meaning + '</td>' +
          '<td class="mono-phones nowrap">' + escapeHtml(m.arpabet) + '</td>' +
          '<td>' + escapeHtml(m.syllables) + '</td>' +
          '<td><span class="badge badge-' + escapeHtml(m.verdict) + '">' + escapeHtml(m.verdict) + '</span></td>' +
          '<td class="c-dim">' + (m.end_freq || 0).toLocaleString() + '</td>' +
          '<td class="nowrap">' + labels + '</td>' +
        '</tr>';
      }).join('') +
      '</tbody></table>';
  } catch (e) {
    host.innerHTML = '<div class="issue">Search failed: ' + escapeHtml(e.message) + '</div>';
  }
}
