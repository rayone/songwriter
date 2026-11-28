// Server-owned batches continue when this page is closed.
let queueLoading = false;

async function queueRequest(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return response.json();
}

async function submitGenerationQueue() {
  const button = el('queue-submit');
  const message = el('queue-message');
  const lines = el('queue-songs').value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!lines.length || lines.length > 200 || lines.some(s => !s.includes('|') || !s.split('|')[0].trim() || !s.substring(s.indexOf('|') + 1).trim())) {
    message.textContent = 'Enter 1–200 lines, each in the form Title | story or brief.';
    return;
  }
  button.disabled = true;
  message.textContent = 'Submitting…';
  try {
    // Start from clean defaults, copying only the production settings from Cowriter.
    const settings = syncSongFromDOM(deepClone(state.song || {}));
    const defaults = await queueRequest('/api/ai/song_defaults');
    const fields = ['model', 'genre', 'subgenre', 'style', 'bpm', 'key', 'scale', 'vocal', 'moods', 'instruments', 'template_id', 'reference_song', 'section_plan', 'max_sections', 'section_lines', 'syllable_min', 'syllable_max', 'rhyme_scheme'];
    for (const field of fields) if (settings[field] !== undefined) defaults[field] = settings[field];
    const songs = lines.map(line => {
      const split = line.indexOf('|');
      return Object.assign(deepClone(defaults), {title: line.slice(0, split).trim(), brief: line.slice(split + 1).trim(), story: line.slice(split + 1).trim()});
    });
    await queueRequest('/api/generation-queue', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({songs})});
    el('queue-songs').value = '';
    message.textContent = 'Songs queued. You can close this page.';
    await loadGenerationQueue();
  } catch (error) {
    message.textContent = 'Could not queue songs: ' + error.message;
  } finally { button.disabled = false; }
}

async function queueAction(path, button, body) {
  button.disabled = true;
  try {
    await queueRequest(path, {method: 'POST', ...(body ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {})});
    el('queue-message').textContent = '';
    await loadGenerationQueue();
  } catch (error) {
    el('queue-message').textContent = error.message;
    button.disabled = false;
  }
}

async function loadGenerationQueue() {
  if (queueLoading) return;
  queueLoading = true;
  try {
    const data = await queueRequest('/api/generation-queue');
    const container = el('queue-jobs');
    container.replaceChildren();
    if (!data.jobs.length) { container.textContent = 'No queued batches yet.'; return; }
    for (const job of data.jobs) {
      const section = document.createElement('section');
      const heading = document.createElement('h3');
      const finished = job.items.filter(i => i.status === 'completed').length;
      heading.textContent = `${job.created_at} · ${job.status} · ${finished}/${job.items.length} finalized`;
      section.appendChild(heading);
      if (job.error) { const p = document.createElement('p'); p.textContent = job.error; section.appendChild(p); }
      if (job.status === 'paused') {
        const button = document.createElement('button');
        button.className = 'btn-secondary btn-sm'; button.textContent = 'Resume batch';
        button.onclick = () => queueAction(`/api/generation-queue/${encodeURIComponent(job.id)}/resume`, button);
        section.appendChild(button);
        const switchButton = document.createElement('button');
        switchButton.className = 'btn-secondary btn-sm';
        switchButton.textContent = 'Resume using Cowriter model';
        switchButton.title = 'Change the model for all remaining songs to the model selected in Cowriter';
        switchButton.onclick = () => {
          const model = el('gw-model').value;
          if (!model) { el('queue-message').textContent = 'Select a model in Cowriter first.'; return; }
          queueAction(`/api/generation-queue/${encodeURIComponent(job.id)}/resume`, switchButton, {model});
        };
        section.appendChild(switchButton);
      }
      const list = document.createElement('ul');
      for (const item of job.items) {
        const li = document.createElement('li');
        li.appendChild(document.createTextNode(`${item.title} — ${item.status}`));
        if (item.error) { const p = document.createElement('p'); p.textContent = item.error; li.appendChild(p); }
        if (item.incomplete_sections && item.incomplete_sections.length) {
          const p = document.createElement('p');
          p.textContent = item.incomplete_sections.map(s => `${s.name}: ${s.reason}${s.issues && s.issues.length ? ' — ' + s.issues.join('; ') : ''}`).join(' | ');
          li.appendChild(p);
        }
        if (item.status === 'incomplete' || item.status === 'failed') {
          const button = document.createElement('button');
          button.className = 'btn-secondary btn-sm'; button.textContent = item.status === 'incomplete' ? 'Regenerate as revision' : 'Retry';
          button.onclick = () => queueAction(`/api/generation-queue/${encodeURIComponent(job.id)}/items/${encodeURIComponent(item.id)}/retry`, button);
          li.appendChild(document.createTextNode(' ')); li.appendChild(button);
        }
        list.appendChild(li);
      }
      section.appendChild(list); container.appendChild(section);
    }
  } catch (error) {
    el('queue-message').textContent = 'Could not load queue: ' + error.message;
  } finally { queueLoading = false; }
}

window.addEventListener('DOMContentLoaded', () => {
  loadGenerationQueue();
  setInterval(() => { if (!document.hidden && el('tab-queue').classList.contains('active')) loadGenerationQueue(); }, 5000);
});
