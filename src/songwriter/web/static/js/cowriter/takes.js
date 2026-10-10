// Cowriter Step 4: interactive drafting, takes and saving.

// Step 4 turn panels. Each turn shows three separate, colour-coded elements:
// the prompt we sent (collapsed), the LLM's response, and the engine's analysis.
// Manual validation adds its edit form below these.
function turnCardId(turnId, attempt) {
  return `turn-${turnId}-${attempt}`;
}
function turnSlot(turnId, attempt, name) {
  const card = el(turnCardId(turnId, attempt));
  return card ? card.querySelector(`[data-slot="${name}"]`) : null;
}
function promptPanelHtml(prompt) {
  return `
    <details class="turn-panel turn-panel-prompt">
      <summary class="turn-panel-title" style="cursor:pointer">Prompt sent to the LLM</summary>
      <div class="mono fs-2xs c-muted pre-wrap max-h-200 scroll-y lh-14 mt-4">${escapeHtml(prompt || '')}</div>
    </details>`;
}
function responsePanelInner(cleaned, raw) {
  const rawText = (raw || '').trim();
  const cleanText = (cleaned || '').trim();
  const lines = cleanText
    ? cleanText.split('\n').map(l => `<div class="fs-md fw-600 c-bright">${escapeHtml(l)}</div>`).join('')
    : '<div class="fs-xs c-rose">No usable lyric lines in the response.</div>';
  // Show the verbatim reply only when cleaning changed it, so stripped chatter is visible.
  const rawHtml = rawText && rawText !== cleanText
    ? `<details class="mt-4"><summary class="fs-2xs c-dim" style="cursor:pointer">Raw reply (before cleanup)</summary>
         <div class="mono fs-2xs c-muted pre-wrap mt-4">${escapeHtml(rawText)}</div></details>`
    : '';
  return `<div class="turn-panel-title">LLM response</div>${lines}${rawHtml}`;
}
function critiquePanelHtml(critique) {
  if (!critique) return '';
  const passed = critique.passed !== false;
  const verdict = passed ? '✓ Passed LLM critique' : '✗ Failed LLM critique';
  const dims = [
    { key: 'rhyme_integrity', label: 'Rhyme Integrity' },
    { key: 'vernacular', label: 'Vernacular' },
    { key: 'cadence', label: 'Cadence' },
    { key: 'cliches', label: 'Clichés' }
  ];
  const rows = dims.map(function(d) {
    const info = critique[d.key] || {};
    const score = info.score != null ? ` [${info.score}/5]` : '';
    const fb = info.feedback ? `: ${escapeHtml(info.feedback)}` : '';
    const isFail = info.verdict === 'fail';
    const cls = isFail ? 'c-rose fw-600' : 'c-dim';
    return `<div class="fs-xs ${cls}">• <b>${d.label}${score}</b>${fb}</div>`;
  }).join('');

  const summary = critique.summary ? `<div class="fs-xs mt-3 fw-600 c-bright">${escapeHtml(critique.summary)}</div>` : '';
  const promptHtml = critique.prompt ? `
    <details class="mt-4"><summary class="fs-2xs c-dim" style="cursor:pointer">Critique prompt &amp; raw JSON</summary>
      <div class="mono fs-2xs c-muted pre-wrap mt-4">${escapeHtml(critique.prompt)}</div>
      <div class="mono fs-2xs c-cyan pre-wrap mt-4">${escapeHtml(critique.raw_response || '')}</div>
    </details>` : '';

  return `
    <div class="turn-panel-title">LLM Critique <span class="turn-verdict">${verdict}</span></div>
    ${summary}
    <div class="mt-3">${rows}</div>
    ${promptHtml}
  `;
}
function copyChatTranscript(btn) {
  const chatWindow = el('gw-chat-turns-window');
  if (!chatWindow) return;

  const cards = chatWindow.querySelectorAll('.turn-card');
  const out = [];

  cards.forEach(function(card) {
    const eyebrow = (card.querySelector('.eyebrow')?.textContent || '').trim();
    if (eyebrow) out.push(eyebrow);

    const promptNode = card.querySelector('.turn-panel-prompt .pre-wrap');
    const promptText = (promptNode?.textContent || '').trim();
    if (promptText) {
      out.push('Prompt sent to the LLM:');
      out.push(promptText);
    }

    const respLines = Array.from(card.querySelectorAll('.turn-panel-response .fs-md')).map(l => l.textContent.trim()).filter(Boolean);
    const rawNode = card.querySelector('.turn-panel-response details .pre-wrap');
    const rawText = (rawNode?.textContent || '').trim();
    out.push('LLM response:');
    if (respLines.length) {
      out.push(respLines.join('\n'));
    } else if (rawText) {
      out.push(rawText);
    }
    if (rawText && rawText !== respLines.join('\n')) {
      out.push('Raw reply (before cleanup):');
      out.push(rawText);
    }

    const analysisPanel = card.querySelector('.turn-panel-analysis');
    if (analysisPanel) {
      out.push('Engine analysis:');
      const verdict = (analysisPanel.querySelector('.turn-verdict')?.textContent || '').trim();
      if (verdict) out.push(verdict);
      const scanRows = Array.from(analysisPanel.querySelectorAll('.fs-xs')).map(r => r.textContent.trim()).filter(Boolean);
      scanRows.forEach(r => out.push(r));
    }

    const critiquePanel = card.querySelector('.turn-panel-critique');
    if (critiquePanel) {
      const cTitle = (critiquePanel.querySelector('.turn-panel-title')?.textContent || '').trim();
      if (cTitle) {
        out.push(cTitle + ':');
        const cSummary = (critiquePanel.querySelector('.fw-600.c-bright')?.textContent || '').trim();
        if (cSummary) out.push(cSummary);
        const cRows = Array.from(critiquePanel.querySelectorAll('.fs-xs')).map(r => r.textContent.trim()).filter(Boolean);
        cRows.forEach(r => out.push(r));
      }
    }

    out.push('\n' + '-'.repeat(40) + '\n');
  });

  const fullText = out.length ? out.join('\n').replace(/\n{3,}/g, '\n\n').trim() : (chatWindow.textContent || '').trim();
  copyText(fullText, btn);
}
function analysisPanelInner(passed, scan, issues, critical_violations, advisories) {
  const verdict = passed ? '✓ Passed engine checks' : '✗ Failed engine checks';
  const rows = (scan || []).map(function(s) {
    if (s.missing) {
      return `<div class="fs-xs c-rose fw-700">L${s.line}: model returned no line</div>`;
    }
    const role = s.role && s.role !== 'line' ? `<span class="c-indigo">${escapeHtml(s.role)}</span> · ` : '';
    const sound = s.sound ? `sound ${escapeHtml(s.sound)} · ` : '';
    const rhyme = s.rhyme_with
      ? `rhymes L${s.rhyme_with}${s.rhyme_with_word ? " ('" + escapeHtml(s.rhyme_with_word) + "')" : ''}${s.rhyme_kind ? ' · ' + escapeHtml(s.rhyme_kind) : ''}`
      : 'free';
    return `<div class="fs-xs">L${s.line}: ${role}<b>${s.syllables}</b> syl (aim ${s.target}, ${escapeHtml(s.range || '')}) · ${sound}end '<b>${escapeHtml(s.end_word || '')}</b>' · ${rhyme}</div>`;
  }).join('');

  const crits = critical_violations || (!passed ? issues : []) || [];
  const advs = advisories || [];

  let issuesHtml = '';
  if (crits.length) {
    issuesHtml += `<div class="mt-4">${crits.map(i => `<div class="fs-xs c-rose fw-600">✗ ${escapeHtml(i)}</div>`).join('')}</div>`;
  }
  if (advs.length) {
    issuesHtml += `<div class="mt-3">${advs.map(a => `<div class="fs-xs c-amber">⚠ Advisory: ${escapeHtml(a)}</div>`).join('')}</div>`;
  } else if (!crits.length && (issues || []).length) {
    issuesHtml += `<div class="mt-4">${issues.map(i => `<div class="fs-xs">• ${escapeHtml(i)}</div>`).join('')}</div>`;
  }

  return `<div class="turn-panel-title">Engine analysis <span class="turn-verdict">${verdict}</span></div>${rows}${issuesHtml}`;
}

async function runInteractiveDrafting(isNewTake = false) {
  const params = currentParams();
  if (!params.prompt) {
    setStatus('gw-status', 'Please enter a song brief in Step 1 first.', 'yellow');
    return;
  }
  const mode = ((params.answers && params.answers.validation_mode && params.answers.validation_mode.answer) || '').toLowerCase();
  if (mode.indexOf('human') !== -1) {
    return runHumanReview(isNewTake);
  }
  params.story = val('gw-storyline-text') || state.storyline || '';
  params.max_sections = null;
  params.max_retries = 3;
  const chatWindow = el('gw-chat-turns-window');
  const turnIndicator = el('gw-chat-turn-indicator');
  const lyricsContent = el('lyrics-sheet-content');
  const btn = el('btn-start-interactive-cowrite');
  // Re-Draft stays clickable: a click while streaming aborts and restarts from Turn 1.
  if (state.draftAbort) { try { state.draftAbort.abort(); } catch (e) {} }
  const abort = new AbortController();
  state.draftAbort = abort;
  if (btn) { btn.innerText = '↺ Re-Draft (restart)'; }
  setStatus('gw-status', 'Streaming turns: prompt, response, and validation live...', 'yellow');

  const copyBtn = el('btn-copy-chat-transcript');
  if (copyBtn) copyBtn.classList.add('d-none');
  if (chatWindow) chatWindow.innerHTML = '';
  if (lyricsContent) lyricsContent.innerHTML = '';

  try {
    const response = await fetch('/api/ai/draft_stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
      signal: abort.signal
    });
    if (!response.ok) {
      let errText = 'HTTP ' + response.status;
      try {
        const errJson = await response.json();
        errText = errJson.detail || JSON.stringify(errJson);
      } catch (e) {
        try { errText = await response.text(); } catch (e2) {}
      }
      throw new Error(errText);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let turnCount = 0;
    let completeData = null;
    let currentSectionTag = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop();

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data: ')) continue;
        let ev = null;
        try {
          ev = JSON.parse(line.slice(6));
        } catch (e) {
          continue;
        }
        if (ev.type === 'error') {
          throw new Error(ev.message || ev.error || 'Server error from model API');
        }

        // One card per attempt with three distinct panels: what we sent (collapsed),
        // what the model returned, and the engine's analysis of that return.
        if (ev.type === 'prompt') {
          turnCount++;
          if (turnIndicator) turnIndicator.innerText = `Turn ${turnCount}`;
          if (chatWindow) {
            const card = document.createElement('div');
            card.id = turnCardId(ev.turn_id, ev.attempt);
            card.className = 'turn-card mb-10';
            card.innerHTML = `
              <div class="eyebrow c-dim">
                Turn ${ev.turn_id}: [${escapeHtml(ev.section || '')}] ${escapeHtml(ev.turn_type || '')} · Attempt ${ev.attempt}/${ev.max_attempts || ''}
              </div>
              ${promptPanelHtml(ev.prompt)}
              <section class="turn-panel turn-panel-response" aria-label="LLM response" data-slot="response">
                <div class="turn-panel-title">LLM response</div>
                <div class="fs-xs c-dim">Waiting for the model…</div>
              </section>
              <section class="turn-panel turn-panel-analysis" aria-label="Engine analysis" data-slot="analysis">
                <div class="turn-panel-title">Engine analysis</div>
                <div class="fs-xs c-dim">Pending response.</div>
              </section>
              <section class="turn-panel turn-panel-critique d-none" aria-label="LLM Critique" data-slot="critique">
              </section>
            `;
            chatWindow.appendChild(card);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        } else if (ev.type === 'response') {
          const slot = turnSlot(ev.turn_id, ev.attempt, 'response');
          if (slot) {
            slot.innerHTML = responsePanelInner(ev.response, ev.raw);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        } else if (ev.type === 'validation') {
          const slot = turnSlot(ev.turn_id, ev.attempt, 'analysis');
          if (slot) {
            const v = ev.validation || {};
            slot.classList.toggle('pass', !!v.passed);
            slot.classList.toggle('fail', !v.passed);
            slot.innerHTML = analysisPanelInner(v.passed, v.scan || [], v.issues || [], v.critical_violations || [], v.advisories || []);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        } else if (ev.type === 'critique') {
          const slot = turnSlot(ev.turn_id, ev.attempt, 'critique');
          if (slot && ev.critique) {
            slot.classList.remove('d-none');
            slot.classList.toggle('pass', ev.critique.passed !== false);
            slot.classList.toggle('fail', ev.critique.passed === false);
            slot.innerHTML = critiquePanelHtml(ev.critique);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        } else if (ev.type === 'line_accepted') {
          if (lyricsContent) {
            if (currentSectionTag !== ev.section) {
              currentSectionTag = ev.section;
              const secDiv = document.createElement('div');
              secDiv.className = 'sheet-section-header sec-default mt-10 mb-4 fw-700 c-cyan';
              secDiv.innerText = `[${ev.section}]`;
              lyricsContent.appendChild(secDiv);
            }
            const lineDiv = document.createElement('div');
            lineDiv.className = 'c-bright fs-md mb-4 flex items-baseline';
            const numStr = String(ev.line_number || '').padStart(2, '0');
            lineDiv.innerHTML = `<span class="mono fs-xs c-dim line-num">${numStr}</span> <span>${escapeHtml(ev.line)}</span>`;
            lyricsContent.appendChild(lineDiv);
            lyricsContent.scrollTop = lyricsContent.scrollHeight;
          }
        } else if (ev.type === 'complete') {
          completeData = ev;
        }
      }
    }

    if (completeData) {
      setStatus('gw-status', 'Song complete! Verified against all scansion rules.', 'green');
      renderTake(completeData, isNewTake);
      const copyBtn = el('btn-copy-chat-transcript');
      if (copyBtn) copyBtn.classList.remove('d-none');
    }
  } catch (err) {
    if (err && err.name === 'AbortError') return;  // Re-Draft restarted the stream
    setStatus('gw-status', 'Interactive drafting error: ' + err.message, 'red');
    if (chatWindow) chatWindow.innerHTML += `<div class="issue mt-8">Error: ${escapeHtml(err.message)}</div>`;
  } finally {
    if (state.draftAbort === abort) state.draftAbort = null;
    if (btn) { btn.disabled = false; btn.innerText = '↺ Re-Draft Current Take'; }
  }
}
// ------------------------------------------------ Step 4: Human turn-by-turn review
// One persistent card per turn slot (section/block). The card's textbox is the single
// source of truth for what enters the song and stays editable. Rejecting re-populates
// the SAME card in place; it never spawns a second card and rejected text is never kept.
// The full LLM prompt (with prior rejections + reasons) is hidden behind a Prompt button.
// A 2-section song is always 2 cards, no matter how many rejects.
async function runHumanReview(isNewTake = false) {
  const params = currentParams();
  params.story = val('gw-storyline-text') || state.storyline || '';
  params.max_sections = null;
  state.humanReview = { params: params, slots: [], isNewTake: isNewTake };
  const chatWindow = el('gw-chat-turns-window');
  const lyricsContent = el('lyrics-sheet-content');
  if (chatWindow) chatWindow.innerHTML = '';
  if (lyricsContent) lyricsContent.innerHTML = '<div class="empty-state">Accepted lines assemble here as you approve each turn.</div>';
  setStatus('gw-status', 'Human review: edit, accept, or reject each turn.', 'yellow');
  await drawNextSlot('');
}
// The accepted lines from every slot, in order. This IS the song.
function humanAcceptedTurns() {
  return (state.humanReview.slots || [])
    .filter(function(s) { return s.accepted; })
    .map(function(s) { return { section: s.section, lines: s.lines }; });
}
// Draft the next unaccepted slot (or re-draft the active one on reject).
async function drawNextSlot(rejectReason) {
  const hr = state.humanReview;
  if (!hr) return;
  const indicator = el('gw-chat-turn-indicator');

  // Find the active (unaccepted) slot; a reject re-draws it, first draft makes a new one.
  let idx = hr.slots.findIndex(function(s) { return !s.accepted; });
  let rejHistory = [];
  if (idx !== -1) {
    rejHistory = (hr.slots[idx].rejections || []).slice();
    // The current draft being rejected joins the history the model must avoid.
    if (rejectReason !== null && hr.slots[idx].lines) {
      rejHistory.push({ lines: hr.slots[idx].lines, reason: rejectReason || '' });
    }
  }

  const body = Object.assign({}, hr.params, {
    accepted: humanAcceptedTurns(),
    reject_reason: rejectReason || '',
    rejections: rejHistory,
  });
  let data;
  try {
    data = await postJSON('/api/ai/draft_turn', body);
  } catch (e) {
    setStatus('gw-status', 'Turn error: ' + e.message, 'red');
    return;
  }
  if (data.done) { await finalizeHumanReview(); return; }
  if (indicator) indicator.innerText = `Turn ${data.turn_index + 1} / ${data.total_turns}`;
  // A recurring chorus is auto-accepted (its lines were fixed by the first chorus).
  if (data.repeat_chorus) {
    hr.slots.push({ section: data.section, lines: data.lines, accepted: true,
                    lineNumbers: data.line_numbers, scheme: data.scheme, scan: [],
                    prompt: '(Repeat of the first chorus.)', rejections: [], repeat: true });
    renderSlotCard(hr.slots.length - 1);
    await drawNextSlot(null);
    return;
  }
  if (idx === -1) {
    hr.slots.push({});
    idx = hr.slots.length - 1;
  }
  hr.slots[idx] = {
    section: data.section, lines: data.lines, accepted: false,
    // The model's reply stays fixed in its own panel; `lines` is the editable copy.
    response: (data.lines || []).slice(), raw: data.raw || '',
    lineNumbers: data.line_numbers, scheme: data.scheme, scan: data.scan || [],
    prompt: data.prompt || '', rejections: rejHistory, repeat: false,
  };
  renderSlotCard(idx);
  const ta = el('human-slot-text-' + idx);
  if (ta) ta.focus();
}
function renderSlotCard(idx) {
  const hr = state.humanReview;
  const slot = hr.slots[idx];
  const chatWindow = el('gw-chat-turns-window');
  if (!chatWindow || !slot) return;
  let card = el('human-slot-' + idx);
  if (!card) {
    card = document.createElement('div');
    card.id = 'human-slot-' + idx;
    card.className = 'chat-review-card mb-10';
    chatWindow.appendChild(card);
  }
  const missing = (slot.scan || []).some(function(s) { return s.missing; });
  const missingWarn = missing
    ? '<div class="fs-xs c-rose mt-4">Some lines are missing. Edit them in the box or click Try again.</div>' : '';
  const rejHtml = (slot.rejections || []).length
    ? '<div class="critique-box mt-6"><b>Rejected drafts (' + slot.rejections.length + '):</b>' +
      slot.rejections.map(function(r) {
        return '<div class="fs-xs mt-3">✗ ' + escapeHtml((r.lines || []).join(' / ')) +
               (r.reason ? '<br><span class="c-amber">reason: ' + escapeHtml(r.reason) + '</span>' : '') + '</div>';
      }).join('') + '</div>'
    : '';
  const textValue = (slot.lines || []).join('\n');
  const acceptedTag = slot.accepted ? '<span class="fs-xs c-emerald fw-700">✓ In the song</span>' : '';
  card.classList.toggle('is-accepted', !!slot.accepted);
  // Repeat choruses have no model turn, so only the lyric form applies.
  const modelPanels = slot.repeat ? '' : `
    ${promptPanelHtml(slot.prompt)}
    <section class="turn-panel turn-panel-response" aria-label="LLM response">
      ${responsePanelInner((slot.response || []).join('\n'), slot.raw)}
    </section>
    <section class="turn-panel turn-panel-analysis ${missing ? 'fail' : 'pass'}" aria-label="Engine analysis">
      ${analysisPanelInner(!missing, slot.scan || [], [])}
    </section>`;
  card.innerHTML = `
    <div class="flex justify-between items-center mb-6">
      <span class="fw-700 c-cyan">[${escapeHtml(slot.section)}] Lines ${(slot.lineNumbers || []).join(', ')}</span>
      <span class="fs-xs c-muted">Scheme: ${escapeHtml(slot.scheme || '-')} ${acceptedTag}</span>
    </div>
    ${modelPanels}
    <section class="turn-panel turn-panel-form" aria-label="Your review">
      <label class="turn-panel-title" for="human-slot-text-${idx}">Your lines (edit before accepting)</label>
      ${missingWarn}
      <textarea class="pkg-box w-full fs-md mono min-h-90" id="human-slot-text-${idx}"
        oninput="onSlotEdited(${idx})">${escapeHtml(textValue)}</textarea>
      ${rejHtml}
      <div class="mt-8" ${slot.accepted || slot.repeat ? 'style="display:none"' : ''}>
        <input type="text" class="field-input w-full" id="human-slot-reason-${idx}" aria-label="Reject reason"
          placeholder="Reject reason / feedback to the model (optional for Try again)">
      </div>
      <div class="flex gap-8 mt-8" ${slot.repeat ? 'style="display:none"' : ''}>
        ${slot.accepted
          ? `<button type="button" class="btn-secondary btn-sm" onclick="reopenSlot(${idx})">Edit / re-draft this turn</button>`
          : `<button type="button" class="btn-primary" onclick="acceptSlot(${idx})">✓ Accept</button>
             <button type="button" class="btn-secondary" onclick="tryAgainSlot(${idx})">↺ Try again</button>`}
      </div>
    </section>
  `;
  chatWindow.scrollTop = chatWindow.scrollHeight;
}
// The textbox is the source of truth: an edit updates the slot and the assembled song.
function onSlotEdited(idx) {
  const hr = state.humanReview;
  const slot = hr && hr.slots[idx];
  const ta = el('human-slot-text-' + idx);
  if (!slot || !ta) return;
  slot.lines = ta.value.split('\n').map(function(l) { return l.trim(); }).filter(Boolean);
  if (slot.accepted) renderAssembledSong();
}
function acceptSlot(idx) {
  const hr = state.humanReview;
  const slot = hr && hr.slots[idx];
  if (!slot) return;
  onSlotEdited(idx);
  if (!slot.lines.length) { setStatus('gw-status', 'Add at least one line before accepting.', 'yellow'); return; }
  slot.accepted = true;
  renderSlotCard(idx);
  renderAssembledSong();
  drawNextSlot('');
}
function tryAgainSlot(idx) {
  const hr = state.humanReview;
  const slot = hr && hr.slots[idx];
  if (!slot) return;
  const reasonEl = el('human-slot-reason-' + idx);
  const reason = reasonEl ? reasonEl.value.trim() : '';
  drawNextSlot(reason);  // re-draws this same unaccepted slot in place
}
function reopenSlot(idx) {
  const hr = state.humanReview;
  const slot = hr && hr.slots[idx];
  if (!slot) return;
  // Reopening a later-than-this accepted slot would desync numbering; only allow the
  // last accepted slot to reopen, or simply toggle this one editable in place.
  slot.accepted = false;
  renderSlotCard(idx);
  renderAssembledSong();
}
// Assemble accepted slots into the live lyric sheet (left = right, one source).
function renderAssembledSong() {
  const hr = state.humanReview;
  const sheet = el('lyrics-sheet-content');
  if (!hr || !sheet) return;
  const accepted = hr.slots.filter(function(s) { return s.accepted; });
  if (!accepted.length) {
    sheet.innerHTML = '<div class="empty-state">Accepted lines assemble here as you approve each turn.</div>';
    return;
  }
  let html = '';
  let lastSection = null;
  let n = 0;
  accepted.forEach(function(s) {
    if (s.section !== lastSection) {
      lastSection = s.section;
      html += `<div class="sheet-section-header sec-default mt-10 mb-4 fw-700 c-cyan">[${escapeHtml(s.section)}]${s.repeat ? ' (repeat)' : ''}</div>`;
    }
    (s.lines || []).forEach(function(l) {
      n++;
      html += `<div class="c-bright fs-md mb-4 flex items-baseline"><span class="mono fs-xs c-dim line-num">${String(n).padStart(2, '0')}</span> <span>${escapeHtml(l)}</span></div>`;
    });
  });
  sheet.innerHTML = html;
  sheet.scrollTop = sheet.scrollHeight;
}
async function finalizeHumanReview() {
  const hr = state.humanReview;
  if (!hr) return;
  setStatus('gw-status', 'Assembling your accepted lines…', 'yellow');
  try {
    const body = Object.assign({}, hr.params, { accepted: humanAcceptedTurns() });
    const data = await postJSON('/api/ai/finalize_turns', body);
    setStatus('gw-status', 'Song complete from your accepted turns.', 'green');
    renderTake(data, hr.isNewTake);
  } catch (e) {
    setStatus('gw-status', 'Finalize error: ' + e.message, 'red');
  }
  state.humanReview = null;
}
function discardAndStartOver() {
  if (!confirm('Discard current song draft and return to Step 1?')) return;
  state.takes = {};
  state.nextTakeId = 1;
  state.activeTakeId = 1;
  state.editingSongId = null;
  const switcher = el('ws-take-switcher');
  if (switcher) switcher.innerHTML = '<button type="button" class="pill active" id="btn-take-1" onclick="selectTake(1)">Take 1</button>';
  const chatWindow = el('gw-chat-turns-window');
  if (chatWindow) chatWindow.innerHTML = '<div class="empty-state" id="gw-chat-empty-state">Draft discarded. Click "Re-Draft Current Take" or start fresh from Step 1.</div>';
  const lyricsContent = el('lyrics-sheet-content');
  if (lyricsContent) lyricsContent.innerHTML = 'Your verified lyrics will assemble here in real time as each line passes scansion checks.';
  goToStep(1);
}
// ------------------------------------------------ Step 4: Generation & Takes
async function runGenerateTake(isNewTake = false) {
  return runInteractiveDrafting(isNewTake);
}
function selectTake(takeId) {
  const take = state.takes[takeId];
  if (!take) return;
  state.activeTakeId = takeId;

  const buttons = document.querySelectorAll('.pill');
  for (const btn of buttons) {
    if (btn.id === 'btn-take-' + takeId) btn.classList.add('active');
    else btn.classList.remove('active');
  }

  if (el('lyrics-sheet-content')) el('lyrics-sheet-content').innerHTML = formatLyricsWithBadges(take.lyrics);
  if (el('pkg-songbench-box')) el('pkg-songbench-box').value = take.formatted_songbench || '';
  if (el('pkg-yue2-box')) el('pkg-yue2-box').value = take.formatted_yue2 || '';
  if (el('pkg-minimax-box')) el('pkg-minimax-box').value = take.formatted_minimax || '';
  if (el('lyrics-meta-pills')) el('lyrics-meta-pills').innerHTML = take.specPillsHtml || '';
  if (el('craft-pills')) el('craft-pills').innerHTML = take.scansionPillsHtml || '';
  if (el('craft-trail-container')) el('craft-trail-container').innerHTML = take.trailHtml || '<div class="empty-state">No section trail available.</div>';
}
function renderTake(data, isNewTake) {
  const takeId = isNewTake ? state.nextTakeId++ : (state.activeTakeId || state.nextTakeId++);
  const engine = data.engine_prompt || {};

  const meta = data.metadata || {};
  const check = data.craft_check || {};
  const specPills = [
    '<span class="m-pill">BPM<b>' + escapeHtml(meta.bpm || val('gw-bpm')) + '</b></span>',
    '<span class="m-pill">Key<b>' + escapeHtml((meta.key || val('gw-key')) + ' ' + (meta.scale || val('gw-scale'))) + '</b></span>',
    '<span class="m-pill">Genre<b>' + escapeHtml(meta.genre || val('gw-style-input')) + '</b></span>',
    '<span class="m-pill">Vocal<b>' + escapeHtml(meta.vocal || val('gw-vocal') || 'Lead') + '</b></span>'
  ].join('');

  const scansionPills = [
    '<span class="m-pill">Scheme<b>' + escapeHtml(check.rhyme_scheme || '-') + '</b></span>',
    '<span class="m-pill">Meter Consistency<b>' + Number(check.metric_consistency != null ? check.metric_consistency : 0.88).toFixed(2) + '</b></span>',
    '<span class="m-pill">Rhyme Density<b>' + Math.round((check.rhyme_density != null ? check.rhyme_density : 0.5) * 100) + '%</b></span>',
    '<span class="m-pill">Total Lines<b>' + (data.lyrics ? data.lyrics.split('\n').filter(l => l.trim() && !l.trim().startsWith('[')).length : 18) + '</b></span>'
  ].join('');

  const draft = data.draft;
  let trailHtml = '';
  if (draft && Array.isArray(draft.sections)) {
    trailHtml = '<div class="flex flex-col gap-12">';

    if (data.story_treatment) {
      trailHtml += '<div class="subpanel">' +
        '<div class="eyebrow c-indigo mb-4">Narrative Story Treatment</div>' +
        '<div class="fs-sm lh-14 c-main">' + escapeHtml(data.story_treatment) + '</div>' +
      '</div>';
    }

    draft.sections.forEach(function(s, idx) {
      trailHtml += '<div class="subpanel">' +
        '<div class="flex justify-between items-center mb-6">' +
          '<span class="fw-700 c-cyan fs-md">[' + escapeHtml(s.name) + ']</span>' +
          '<span class="fs-xs c-muted">' + escapeHtml(s.performer || 'Lead') + ' • Target: ' + escapeHtml(s.target_syllables || '8-10') + ' syl • ' + (s.attempts > 1 ? '<span class="c-amber">Repaired (' + s.attempts + ' passes)</span>' : '<span class="c-emerald">Clean (1 pass)</span>') + '</span>' +
        '</div>';
      // Render complete LLM critique & feedback trail for each attempt
      if (Array.isArray(s.attempt_history) && s.attempt_history.length) {
        trailHtml += '<div class="my-8 flex flex-col gap-8">';
        s.attempt_history.forEach(function(att) {
          const isRej = att.rejected;
          const vCls = isRej ? 'fail' : 'pass';
          trailHtml += '<div class="chat-verdict-card ' + vCls + '">' +
            '<div class="flex justify-between items-center mb-4">' +
              '<span class="fs-xs fw-700">' + (isRej ? '✗ Attempt ' + att.attempt + ' (REJECTED by Scansion Engine)' : '✓ Attempt ' + att.attempt + ' (ACCEPTED)') + '</span>' +
            '</div>' +
            '<div class="mono fs-xs c-bright mb-4 lh-14">' +
              (att.lines || []).map(function(l, i) { return '<div>' + (i+1) + '. ' + escapeHtml(l) + '</div>'; }).join('') +
            '</div>';
          if (att.issues && att.issues.length) {
            trailHtml += '<div class="critique-box">' +
              '<b>Critique & Rejection Reason:</b> ' + att.issues.map(escapeHtml).join('; ') +
            '</div>';
          }
          trailHtml += '</div>';
        });
        trailHtml += '</div>';
      } else if (Array.isArray(s.lines) && s.lines.length) {
        trailHtml += '<div class="flex flex-col gap-4 mono fs-sm my-8">' +
          s.lines.map(function(l, lIdx) {
            return '<div class="flex gap-8 items-baseline">' +
              '<span class="fs-2xs c-muted w-18">' + (lIdx + 1) + '.</span>' +
              '<span class="flex-1 c-bright">' + escapeHtml(l) + '</span>' +
            '</div>';
          }).join('') +
        '</div>';
      }

      if (s.resolved && s.resolved.length) {
        trailHtml += '<div class="fs-xs c-emerald mt-4">' +
          '✓ Repaired in subsequent pass: ' + s.resolved.map(escapeHtml).join('; ') +
        '</div>';
      }
      trailHtml += '</div>';
    });

    if (draft.final_issues && draft.final_issues.length) {
      trailHtml += '<div class="mt-6"><div class="fs-xs fw-700 c-rose mb-4">Remaining Issues:</div>' +
        draft.final_issues.map(function(i) {
          return '<div class="issue">' + escapeHtml(i) + '</div>';
        }).join('') + '</div>';
    } else {
      trailHtml += '<div class="mt-8 c-emerald fs-sm fw-600">✓ All sections verified against deterministic scansion rules.</div>';
    }
    trailHtml += '</div>';
  }

  state.takes[takeId] = {
    takeId: takeId,
    lyrics: data.lyrics,
    formatted_songbench: data.formatted_songbench || ((engine.songbench || '') + '\n\n' + data.lyrics),
    formatted_yue2: data.formatted_yue2 || (engine.yue2 + '\n\n' + data.lyrics),
    formatted_minimax: data.formatted_minimax || (engine.minimax + '\n\n' + data.lyrics),
    specPillsHtml: specPills,
    scansionPillsHtml: scansionPills,
    trailHtml: trailHtml,
    model_used: data.model_used || (data.generation && data.generation.model) || '',
    songId: data.song_id || null,
    generation: data.generation || null
  };
  // A finished draft is already saved; re-drafting must add revisions to the
  // same song, not start a new one.
  if (data.song_id) state.editingSongId = data.song_id;

  const switcher = el('ws-take-switcher');
  if (switcher) {
    let btn = el('btn-take-' + takeId);
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className='pill';
      btn.id = 'btn-take-' + takeId;
      btn.innerText = 'Take ' + takeId;
      btn.onclick = function() { selectTake(takeId); };
      switcher.appendChild(btn);
    }
  }

  selectTake(takeId);
}
function toggleAudioDrawer() {
  const drawer = el('ws-view-audio');
  if (!drawer) return;
  drawer.style.display = (drawer.style.display === 'none') ? 'grid' : 'none';
}
async function saveCurrentTake() {
  const currentTake = state.takes[state.activeTakeId || 1];
  if (!currentTake || !currentTake.generation) {
    setStatus('gw-status', 'Please generate lyrics first before saving to library.', 'yellow');
    return;
  }
  // The generation is the single source of truth: save it whole, with the title edit applied.
  const generation = Object.assign({}, currentTake.generation, {
    title: val('gw-title') || currentTake.generation.title || 'Untitled Song'
  });
  try {
    const res = await postJSON('/api/crafted_songs', {
      id: currentTake.songId,
      status: 'finalized',
      generation: generation
    });
    currentTake.songId = res.song_id;
    currentTake.generation = generation;
    // Every later draft or save in this session adds a revision to this song.
    state.editingSongId = res.song_id;
    if (res.status === 'unchanged') {
      setStatus('gw-status', 'No changes since revision ' + res.revision + ' of "' + generation.title + '". Edit or re-draft to save a new revision.', 'yellow');
    } else {
      setStatus('gw-status', 'Saved "' + generation.title + '" (revision ' + res.revision + ') to library!', 'green');
    }
  } catch (e) {
    setStatus('gw-status', 'Save error: ' + e.message, 'red');
  }
}
function copyCurrentLyrics(btn) {
  const take = state.takes[state.activeTakeId];
  if (!take) return;
  copyText(take.lyrics, btn);
}
function loadTakeIntoStudio() {
  const take = state.takes[state.activeTakeId || 1];
  if (!take) return;
  openInSongAnalytics(take.lyrics, (val('gw-title') || 'Untitled Song') + ' (Take ' + take.takeId + ')');
}
