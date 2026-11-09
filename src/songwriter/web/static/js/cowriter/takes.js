// Cowriter Step 4: interactive drafting, takes and saving.

// The model name shown in labels. Prefer the model a take actually ran with,
// falling back to the current selection so labels never read a bare "the LLM".
function modelLabel(modelName) {
  const name = (modelName || state.selectedModel || (el('gw-model') && el('gw-model').value) || '').trim();
  return name || 'the model';
}

// Step 4 turn panels. Each turn shows three separate, colour-coded elements:
// the prompt we sent (collapsed), the model's response, and the engine's analysis.
// Manual validation adds its edit form below these.
function turnCardId(turnId, attempt, takeId) {
  return `take-${takeId}-turn-${turnId}-${attempt}`;
}
function turnSlot(turnId, attempt, name, takeId) {
  const card = el(turnCardId(turnId, attempt, takeId));
  return card ? card.querySelector(`[data-slot="${name}"]`) : null;
}
function promptPanelHtml(prompt, modelName) {
  return `
    <details class="turn-panel turn-panel-prompt">
      <summary class="turn-panel-title" style="cursor:pointer">Prompt sent to ${escapeHtml(modelLabel(modelName))}</summary>
      <div class="mono fs-2xs c-muted pre-wrap max-h-200 scroll-y lh-14 mt-4">${escapeHtml(prompt || '')}</div>
    </details>`;
}
function responsePanelInner(cleaned, raw, modelName) {
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
  return `<div class="turn-panel-title">${escapeHtml(modelLabel(modelName))} response</div>${lines}${rawHtml}`;
}
function critiquePanelHtml(critique, modelName) {
  if (!critique) return '';
  const passed = critique.passed !== false;
  const verdict = critique.parsed === false
    ? '– Critique unavailable'
    : (passed ? '✓ Passed critique' : '✗ Failed critique');
  const dims = [
    { key: 'sense', label: 'Sense' },
    { key: 'story', label: 'Story & Flow' },
    { key: 'rhyme_integrity', label: 'Rhyme Integrity' },
    { key: 'vernacular', label: 'Vernacular' },
    { key: 'cadence', label: 'Cadence' },
    { key: 'cliches', label: 'Clichés' }
  ];
  const rows = dims.filter(d => critique[d.key]).map(function(d) {
    const info = critique[d.key] || {};
    const score = info.score != null ? ` [${info.score}/5]` : '';
    const fb = info.feedback ? `: ${escapeHtml(info.feedback)}` : '';
    const isFail = info.verdict === 'fail';
    const cls = isFail ? 'c-rose fw-600' : 'c-dim';
    return `<div class="fs-xs ${cls}">• <b>${d.label}${score}</b>${fb}</div>`;
  }).join('');

  // Per-line read: what the critic understood each line to mean, and its rewrite.
  const lineRows = (critique.lines || []).map(function(r) {
    const bad = r.makes_sense === false;
    const meaning = r.meaning ? `reads as “${escapeHtml(r.meaning)}”` : '';
    const forced = r.forced_word ? ` · forced: '${escapeHtml(r.forced_word)}'` : '';
    const fix = r.fix ? ` · fix: <span class="c-bright">${escapeHtml(r.fix)}</span>` : '';
    return `<div class="fs-xs ${bad ? 'c-rose' : 'c-dim'}">L${escapeHtml(String(r.line ?? ''))}: ${meaning}${forced}${fix}</div>`;
  }).join('');

  const summary = critique.summary ? `<div class="fs-xs mt-3 fw-600 c-bright">${escapeHtml(critique.summary)}</div>` : '';
  const promptHtml = critique.prompt ? `
    <details class="mt-4"><summary class="fs-2xs c-dim" style="cursor:pointer">Critique prompt &amp; raw JSON</summary>
      <div class="mono fs-2xs c-muted pre-wrap mt-4">${escapeHtml(critique.prompt)}</div>
      <div class="mono fs-2xs c-cyan pre-wrap mt-4">${escapeHtml(critique.raw_response || '')}</div>
    </details>` : '';

  return `
    <div class="turn-panel-title">${escapeHtml(modelLabel(modelName))} Critique <span class="turn-verdict">${verdict}</span></div>
    ${summary}
    ${lineRows ? `<div class="mt-3">${lineRows}</div>` : ''}
    <div class="mt-3">${rows}</div>
    ${promptHtml}
  `;
}
function copyChatTranscript(btn) {
  const take = state.takes[state.activeTakeId];
  const chatWindow = take && take.chatPane;
  if (!chatWindow) return;

  const cards = chatWindow.querySelectorAll('.turn-card');
  const out = [];
  const label = modelLabel(take.model);

  cards.forEach(function(card) {
    const eyebrow = (card.querySelector('.eyebrow')?.textContent || '').trim();
    if (eyebrow) out.push(eyebrow);

    const promptNode = card.querySelector('.turn-panel-prompt .pre-wrap');
    const promptText = (promptNode?.textContent || '').trim();
    if (promptText) {
      out.push('Prompt sent to ' + label + ':');
      out.push(promptText);
    }

    const respLines = Array.from(card.querySelectorAll('.turn-panel-response .fs-md')).map(l => l.textContent.trim()).filter(Boolean);
    const rawNode = card.querySelector('.turn-panel-response details .pre-wrap');
    const rawText = (rawNode?.textContent || '').trim();
    out.push(label + ' response:');
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

async function runInteractiveDrafting(isNewTake = false, resetTakeId = null) {
  const params = currentParams();
  if (!params.prompt) {
    setStatus('gw-status', 'Please enter a song brief in Step 1 first.', 'yellow');
    return;
  }
  const take = beginTake(isNewTake, resetTakeId);
  const takeId = take.takeId;
  params.song_id = take.songId || params.song_id;
  const answer = params.answers && params.answers.validation_mode;
  const mode = String((answer && answer.answer) || answer || '').toLowerCase();
  if (mode.indexOf('human') !== -1) {
    return runHumanReview(take, params);
  }
  params.story = val('gw-storyline-text') || state.storyline || '';
  params.max_sections = null;
  params.max_retries = 3;
  const chatWindow = take.chatPane;
  const lyricsContent = take.lyricsPane;
  const abort = take.abort;
  takeStatus(take, 'Streaming turns: prompt, response, and validation live...', 'yellow');

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
      if (!isCurrentTakeRun(take)) return;
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
          take.turnText = `Turn ${turnCount}`;
          if (state.activeTakeId === takeId) updateTakeControls(take);
          if (chatWindow) {
            const card = document.createElement('div');
            card.id = turnCardId(ev.turn_id, ev.attempt, takeId);
            card.className = 'turn-card mb-10';
            card.innerHTML = `
              <div class="eyebrow c-dim">
                Turn ${ev.turn_id}: [${escapeHtml(ev.section || '')}] ${escapeHtml(ev.turn_type || '')} · Attempt ${ev.attempt}/${ev.max_attempts || ''}
              </div>
              ${promptPanelHtml(ev.prompt, take.model)}
              <section class="turn-panel turn-panel-response" aria-label="${escapeHtml(modelLabel(take.model))} response" data-slot="response">
                <div class="turn-panel-title">${escapeHtml(modelLabel(take.model))} response</div>
                <div class="fs-xs c-dim">Waiting for ${escapeHtml(modelLabel(take.model))}…</div>
              </section>
              <section class="turn-panel turn-panel-analysis" aria-label="Engine analysis" data-slot="analysis">
                <div class="turn-panel-title">Engine analysis</div>
                <div class="fs-xs c-dim">Pending response.</div>
              </section>
              <section class="turn-panel turn-panel-critique d-none" aria-label="${escapeHtml(modelLabel(take.model))} Critique" data-slot="critique">
              </section>
            `;
            chatWindow.appendChild(card);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        } else if (ev.type === 'response') {
          const slot = turnSlot(ev.turn_id, ev.attempt, 'response', takeId);
          if (slot) {
            slot.innerHTML = responsePanelInner(ev.response, ev.raw, take.model);
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        } else if (ev.type === 'validation') {
          const slot = turnSlot(ev.turn_id, ev.attempt, 'analysis', takeId);
          if (slot) {
            const v = ev.validation || {};
            slot.classList.toggle('pass', !!v.passed);
            slot.classList.toggle('fail', !v.passed);
            // On the final attempt the loop keeps the strongest take, which may be an earlier one.
            const kept = v.kept_attempt && v.kept_attempt !== ev.attempt
              ? `<div class="fs-xs c-amber mt-3">↺ Kept attempt ${escapeHtml(String(v.kept_attempt))}, the strongest take for this turn.</div>`
              : '';
            slot.innerHTML = analysisPanelInner(v.passed, v.scan || [], v.issues || [], v.critical_violations || [], v.advisories || []) + kept;
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        } else if (ev.type === 'critique') {
          const slot = turnSlot(ev.turn_id, ev.attempt, 'critique', takeId);
          if (slot && ev.critique) {
            slot.classList.remove('d-none');
            slot.classList.toggle('pass', ev.critique.passed !== false);
            slot.classList.toggle('fail', ev.critique.passed === false);
            slot.innerHTML = critiquePanelHtml(ev.critique, take.model);
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

    if (!isCurrentTakeRun(take)) return;
    if (!completeData) throw new Error('The stream ended before the song was complete. Please re-draft this take.');
    renderTake(completeData, takeId);
    takeStatus(take, 'Song complete! Verified against all scansion rules.', 'green');
  } catch (err) {
    if (!isCurrentTakeRun(take) || (err && err.name === 'AbortError')) return;
    take.status = 'failed';
    takeStatus(take, 'Interactive drafting error: ' + err.message, 'red');
    if (chatWindow) chatWindow.innerHTML += `<div class="issue mt-8">Error: ${escapeHtml(err.message)}</div>`;
  } finally {
    if (isCurrentTakeRun(take)) {
      take.abort = null;
      updateTakeButton(take);
      if (state.activeTakeId === takeId) updateTakeControls(take);
    }
  }
}
// ------------------------------------------------ Step 4: Human turn-by-turn review
// One persistent card per turn slot (section/block). The card's textbox is the single
// source of truth for what enters the song and stays editable. Rejecting re-populates
// the SAME card in place; it never spawns a second card and rejected text is never kept.
// The full LLM prompt (with prior rejections + reasons) is hidden behind a Prompt button.
// A 2-section song is always 2 cards, no matter how many rejects.
async function runHumanReview(take, params) {
  params.story = val('gw-storyline-text') || state.storyline || '';
  params.max_sections = null;
  const hr = { params, slots: [], take, busy: false };
  take.humanReview = hr;
  state.humanReview = hr;
  take.lyricsPane.innerHTML = '<div class="empty-state">Accepted lines assemble here as you approve each turn.</div>';
  takeStatus(take, 'Human review: edit, accept, or reject each turn.', 'yellow');
  await drawNextSlot('', hr);
}
// The accepted lines from every slot, in order. This IS the song.
function humanAcceptedTurns(hr = state.humanReview) {
  return (hr.slots || [])
    .filter(function(s) { return s.accepted; })
    .map(function(s) { return { section: s.section, lines: s.lines }; });
}
// Draft the next unaccepted slot (or re-draft the active one on reject).
async function drawNextSlot(rejectReason, hr = state.humanReview) {
  if (!hr || hr.busy || !isCurrentTakeRun(hr.take)) return;
  hr.busy = true;

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
    accepted: humanAcceptedTurns(hr),
    reject_reason: rejectReason || '',
    rejections: rejHistory,
  });
  let data;
  try {
    data = await postJSON('/api/ai/draft_turn', body, hr.take.abort.signal);
  } catch (e) {
    hr.busy = false;
    if (isCurrentTakeRun(hr.take)) takeStatus(hr.take, 'Turn error: ' + e.message + '. Try again or reset this take.', 'red');
    return;
  }
  hr.busy = false;
  if (!isCurrentTakeRun(hr.take)) return;
  if (data.done) { await finalizeHumanReview(hr); return; }
  hr.take.turnText = `Turn ${data.turn_index + 1} / ${data.total_turns}`;
  if (state.activeTakeId === hr.take.takeId) updateTakeControls(hr.take);
  // A recurring chorus is auto-accepted (its lines were fixed by the first chorus).
  if (data.repeat_chorus) {
    hr.slots.push({ section: data.section, lines: data.lines, accepted: true,
                    lineNumbers: data.line_numbers, scheme: data.scheme, scan: [],
                    prompt: '(Repeat of the first chorus.)', rejections: [], repeat: true });
    renderSlotCard(hr.slots.length - 1, hr);
    await drawNextSlot(null, hr);
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
  renderSlotCard(idx, hr);
  const ta = el('human-slot-text-' + hr.take.takeId + '-' + idx);
  if (ta && state.activeTakeId === hr.take.takeId) ta.focus();
}
function renderSlotCard(idx, hr = state.humanReview) {
  const slot = hr.slots[idx];
  const chatWindow = hr.take.chatPane;
  if (!chatWindow || !slot) return;
  let card = el('human-slot-' + hr.take.takeId + '-' + idx);
  if (!card) {
    card = document.createElement('div');
    card.id = 'human-slot-' + hr.take.takeId + '-' + idx;
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
    ${promptPanelHtml(slot.prompt, hr.take.model)}
    <section class="turn-panel turn-panel-response" aria-label="${escapeHtml(modelLabel(hr.take.model))} response">
      ${responsePanelInner((slot.response || []).join('\n'), slot.raw, hr.take.model)}
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
      <label class="turn-panel-title" for="human-slot-text-${hr.take.takeId}-${idx}">Your lines (edit before accepting)</label>
      ${missingWarn}
      <textarea class="pkg-box w-full fs-md mono min-h-90" id="human-slot-text-${hr.take.takeId}-${idx}"
        oninput="onSlotEdited(${idx})">${escapeHtml(textValue)}</textarea>
      ${rejHtml}
      <div class="mt-8" ${slot.accepted || slot.repeat ? 'style="display:none"' : ''}>
        <input type="text" class="field-input w-full" id="human-slot-reason-${hr.take.takeId}-${idx}" aria-label="Reject reason"
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
  const ta = hr && el('human-slot-text-' + hr.take.takeId + '-' + idx);
  if (!slot || !ta) return;
  slot.lines = ta.value.split('\n').map(function(l) { return l.trim(); }).filter(Boolean);
  if (slot.accepted) renderAssembledSong();
}
function acceptSlot(idx) {
  const hr = state.humanReview;
  const slot = hr && hr.slots[idx];
  if (!slot || hr.busy || !isCurrentTakeRun(hr.take)) return;
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
  const reasonEl = el('human-slot-reason-' + hr.take.takeId + '-' + idx);
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
  const sheet = hr && hr.take.lyricsPane;
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
async function finalizeHumanReview(hr = state.humanReview) {
  if (!hr || hr.busy || !isCurrentTakeRun(hr.take)) return;
  hr.busy = true;
  takeStatus(hr.take, 'Assembling your accepted lines…', 'yellow');
  try {
    const body = Object.assign({}, hr.params, { accepted: humanAcceptedTurns(hr) });
    const data = await postJSON('/api/ai/finalize_turns', body, hr.take.abort.signal);
    if (!isCurrentTakeRun(hr.take)) return;
    hr.take.humanReview = null;
    if (state.humanReview === hr) state.humanReview = null;
    renderTake(data, hr.take.takeId);
    takeStatus(hr.take, 'Song complete from your accepted turns.', 'green');
  } catch (e) {
    if (isCurrentTakeRun(hr.take)) takeStatus(hr.take, 'Finalize error: ' + e.message, 'red');
  } finally {
    hr.busy = false;
  }
}
function discardAndStartOver() {
  if (!confirm('Discard current song draft and return to Step 1?')) return;
  for (const take of Object.values(state.takes)) {
    if (take.abort) take.abort.abort();
  }
  state.humanReview = null;
  closeTakeModal();
  state.takes = {};
  state.nextTakeId = 1;
  state.activeTakeId = null;
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
function isCurrentTakeRun(take) {
  return state.takes[take.takeId] === take && !(take.abort && take.abort.signal.aborted);
}
function takeStatus(take, text, color) {
  take.statusText = text;
  take.statusColor = color;
  if (state.activeTakeId === take.takeId) setStatus('gw-status', text, color);
}
function ensureTakePanes(take) {
  for (const [key, hostId, cls] of [
    ['chatPane', 'gw-chat-turns-window', 'take-chat-pane'],
    ['lyricsPane', 'lyrics-sheet-content', 'take-lyrics-pane']
  ]) {
    if (take[key]) continue;
    const host = el(hostId);
    if (!host) continue;
    if (!host.querySelector('.' + cls)) host.innerHTML = '';
    const pane = document.createElement('div');
    pane.className = cls + ' hidden';
    host.appendChild(pane);
    take[key] = pane;
  }
}
function updateTakeButton(take) {
  const switcher = el('ws-take-switcher');
  if (!switcher) return;
  let btn = el('btn-take-' + take.takeId);
  if (!btn) {
    btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pill';
    btn.id = 'btn-take-' + take.takeId;
    btn.onclick = function() { selectTake(take.takeId); };
    switcher.appendChild(btn);
  }
  const running = take.status === 'running';
  btn.innerHTML = 'Take ' + take.takeId + (running ? ' <span class="take-spinner" aria-hidden="true"></span>' : (take.status === 'failed' ? ' · Failed' : ''));
  btn.setAttribute('aria-label', 'Take ' + take.takeId + (running ? ' — in progress' : ' — ' + (take.status || 'completed')));
  btn.classList.toggle('active', state.activeTakeId === take.takeId);
}
function updateTakeControls(take) {
  const indicator = el('gw-chat-turn-indicator');
  if (indicator) indicator.innerText = take.turnText || 'Turn 0';
  const copyBtn = el('btn-copy-chat-transcript');
  if (copyBtn) copyBtn.classList.toggle('d-none', take.status !== 'completed');
  const btn = el('btn-start-interactive-cowrite');
  if (btn) btn.innerText = take.status === 'running' ? '↺ Reset Current Take' : '↺ Re-Draft Current Take';
  setStatus('gw-status', take.statusText || 'Ready to craft.', take.statusColor);
}
function beginTake(isNewTake, resetTakeId) {
  const takeId = resetTakeId || (!isNewTake && state.activeTakeId) || state.nextTakeId++;
  state.nextTakeId = Math.max(state.nextTakeId, takeId + 1);
  const previous = state.takes[takeId];
  if (previous && previous.abort) previous.abort.abort();
  if (previous) {
    if (previous.chatPane) previous.chatPane.remove();
    if (previous.lyricsPane) previous.lyricsPane.remove();
  }
  // Capture the model this take runs with, so its labels stay correct even if the
  // dropdown selection changes before the next take.
  const model = (state.selectedModel || (el('gw-model') && el('gw-model').value) || '').trim();
  const take = { takeId, status: 'running', lyrics: '', songId: previous && previous.songId,
    model: model, abort: new AbortController(), turnText: 'Turn 0' };
  state.takes[takeId] = take;
  ensureTakePanes(take);
  updateTakeButton(take);
  selectTake(takeId);
  return take;
}
async function runGenerateTake(isNewTake = false) {
  if (state.currentStep !== 4) return;
  if (isNewTake) return onNewTakeClicked();
  return onRedraftClicked();
}
function onNewTakeClicked() {
  const active = state.takes[state.activeTakeId];
  const running = active && active.status === 'running' ? active : Object.values(state.takes).find(t => t.status === 'running');
  if (running) { openTakeModal(running.takeId); return; }
  return runInteractiveDrafting(true);
}
function onRedraftClicked() {
  const take = state.takes[state.activeTakeId];
  if (take && take.status === 'running') { openTakeModal(take.takeId); return; }
  return runInteractiveDrafting(false);
}
let modalTakeId = null;
let modalReturnFocus = null;
function openTakeModal(takeId) {
  modalTakeId = takeId;
  modalReturnFocus = document.activeElement;
  el('take-modal-body').innerText = `Take ${takeId} is still in progress. Reset Take ${takeId} and restart it, or start a new take in parallel?`;
  el('take-action-modal').classList.remove('hidden');
  el('btn-modal-parallel').focus();
}
function closeTakeModal() {
  el('take-action-modal').classList.add('hidden');
  modalTakeId = null;
  if (modalReturnFocus) modalReturnFocus.focus();
}
function onModalBackdropClick(event) {
  if (event.target === el('take-action-modal')) closeTakeModal();
}
function onTakeModalReset() {
  const takeId = modalTakeId;
  closeTakeModal();
  if (state.takes[takeId]) return runInteractiveDrafting(false, takeId);
}
function onTakeModalParallel() {
  closeTakeModal();
  return runInteractiveDrafting(true);
}
document.addEventListener('keydown', function(event) {
  const modal = el('take-action-modal');
  if (!modal || modal.classList.contains('hidden')) return;
  if (event.key === 'Escape') { event.preventDefault(); closeTakeModal(); }
  if (event.key === 'Tab') {
    const buttons = Array.from(modal.querySelectorAll('button'));
    const first = buttons[0], last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
function selectTake(takeId) {
  const take = state.takes[takeId];
  if (!take) return;
  state.activeTakeId = takeId;
  state.humanReview = take.humanReview || null;
  if (take.songId) state.editingSongId = take.songId;
  ensureTakePanes(take);
  for (const other of Object.values(state.takes)) {
    if (other.chatPane) other.chatPane.classList.toggle('hidden', other !== take);
    if (other.lyricsPane) other.lyricsPane.classList.toggle('hidden', other !== take);
    updateTakeButton(other);
  }
  if (take.status === 'completed' && take.lyricsPane) take.lyricsPane.innerHTML = formatLyricsWithBadges(take.lyrics);
  updateTakeControls(take);
  if (el('pkg-songbench-box')) el('pkg-songbench-box').value = take.formatted_songbench || '';
  if (el('pkg-yue2-box')) el('pkg-yue2-box').value = take.formatted_yue2 || '';
  if (el('pkg-minimax-box')) el('pkg-minimax-box').value = take.formatted_minimax || '';
  if (el('lyrics-meta-pills')) el('lyrics-meta-pills').innerHTML = take.specPillsHtml || '';
  if (el('craft-pills')) el('craft-pills').innerHTML = take.scansionPillsHtml || '';
  if (el('craft-trail-container')) el('craft-trail-container').innerHTML = take.trailHtml || '<div class="empty-state">No section trail available.</div>';
}
function renderTake(data, takeId) {
  const take = state.takes[takeId];
  if (!take) return;
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

  Object.assign(take, {
    status: 'completed',
    takeId: takeId,
    lyrics: data.lyrics,
    formatted_songbench: data.formatted_songbench || ((engine.songbench || '') + '\n\n' + data.lyrics),
    formatted_yue2: data.formatted_yue2 || (engine.yue2 + '\n\n' + data.lyrics),
    formatted_minimax: data.formatted_minimax || (engine.minimax + '\n\n' + data.lyrics),
    specPillsHtml: specPills,
    scansionPillsHtml: scansionPills,
    trailHtml: trailHtml,
    model_used: data.model_used || (data.generation && data.generation.model) || '',
    model: take.model || data.model_used || (data.generation && data.generation.model) || '',
    songId: data.song_id || null,
    generation: data.generation || null
  });
  // A finished draft is already saved; re-drafting must add revisions to the
  // same song, not start a new one.
  if (data.song_id && state.activeTakeId === takeId) state.editingSongId = data.song_id;
  updateTakeButton(take);
  if (state.activeTakeId === takeId) selectTake(takeId);
}
function toggleAudioDrawer() {
  const drawer = el('ws-view-audio');
  if (!drawer) return;
  drawer.classList.toggle('hidden');
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
