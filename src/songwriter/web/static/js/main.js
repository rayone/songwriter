// Boot: wires listeners and kicks off initial data loads. Loaded last.

async function loadDatabaseStats() {
  try {
    const data = await getJSON('/api/stats');
    const header = el('gw-search-header');
    if (header && data && data.reference_songs_count) {
      header.innerText = 'Search ' + data.reference_songs_count.toLocaleString() + ' Verified Reference Songs';
    }
  } catch (e) {}
}
window.addEventListener('DOMContentLoaded', function boot() {
  updateStepperUI();
  loadModels();
  loadFormatterTemplates();
  loadDatabaseStats();
  loadTemplateGenres().then(function() { loadTemplatesForDropdown('', ''); });
  const styleInput = el('gw-style-input');
  if (styleInput) {
    styleInput.addEventListener('input', debounce(refreshTemplateMatches, 600));
  }
  const promptInput = el('gw-prompt');
  if (promptInput) {
    promptInput.addEventListener('input', updateStepNavState);
  }
  const storyInput = el('gw-storyline-text');
  if (storyInput) {
    storyInput.addEventListener('input', updateStepNavState);
  }
  updateStepNavState();
});
