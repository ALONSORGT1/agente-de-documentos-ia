// Presentation only: API, session credentials and retrieval remain in app.js.
export function initWorkspace() {
  const noop = () => {};
  if (typeof document.querySelectorAll !== 'function') return {filterDocuments: noop, startResearch: noop, resetResearch: noop, setAnswer: noop};
  const el = id => document.getElementById(id);
  const labels = {research: 'Investigación', library: 'Biblioteca', service: 'Estado del servicio'};
  let answer = '';
  let copyTimer;
  const mobile = window.matchMedia('(max-width: 720px)');

  function closeMenu() {
    el('app-shell').dataset.menu = 'closed';
    el('menu-toggle').setAttribute('aria-expanded', 'false');
    el('menu-toggle').setAttribute('aria-label', 'Abrir navegación');
    el('navigation').inert = mobile.matches;
  }
  mobile.addEventListener('change', closeMenu);

  function navigate(focusHeading = true) {
    const route = location.hash.slice(1);
    const view = Object.hasOwn(labels, route) ? route : 'research';
    for (const [name] of Object.entries(labels)) {
      el(`${name}-view`).hidden = name !== view;
      const link = el(`nav-${name}`);
      link.classList.toggle('active', name === view);
      if (name === view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    el('page-label').textContent = labels[view];
    document.title = `${labels[view]} · Agente de documentos IA`;
    closeMenu();
    if (route === 'question') el('question').focus();
    else {
      window.scrollTo({top: 0, behavior: 'instant'});
      if (focusHeading) {
        const target = el(view === 'research' ? el('response-panel').hidden ? 'welcome-title' : 'answer-title' : view === 'library' ? 'documents-title' : 'config-title');
        target.setAttribute('tabindex', '-1');
        target.focus({preventScroll: true});
      }
    }
  }
  window.addEventListener('hashchange', navigate);
  navigate(false);

  el('menu-toggle').addEventListener('click', () => {
    const open = el('app-shell').dataset.menu !== 'open';
    el('app-shell').dataset.menu = open ? 'open' : 'closed';
    el('menu-toggle').setAttribute('aria-expanded', String(open));
    el('menu-toggle').setAttribute('aria-label', open ? 'Cerrar navegación' : 'Abrir navegación');
    el('navigation').inert = mobile.matches && !open;
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && el('app-shell').dataset.menu === 'open') { closeMenu(); el('menu-toggle').focus(); }
  });
  el('main-content').addEventListener('click', closeMenu);
  el('theme-toggle').addEventListener('click', () => applyTheme(document.documentElement.dataset.theme !== 'dark'));
  function applyTheme(dark) {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    el('theme-toggle').setAttribute('aria-label', dark ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro');
    el('theme-toggle').setAttribute('aria-pressed', String(dark));
    document.querySelector('meta[name="theme-color"]').setAttribute('content', dark ? '#1d2130' : '#171b2b');
  }
  const scheme = window.matchMedia('(prefers-color-scheme: dark)');
  applyTheme(scheme.matches);
  scheme.addEventListener('change', event => applyTheme(event.matches));

  const prompts = {
    'suggest-summary': 'Resume lo que indican los documentos sobre [tema] y señala las fuentes que lo respaldan.',
    'suggest-compare': 'Compara lo que dicen los documentos sobre [tema]. Identifica coincidencias y diferencias con sus fuentes.',
    'suggest-find': '¿Qué pasos o requisitos indican los documentos para [procedimiento]? Incluye las fuentes disponibles.'
  };
  for (const [id, text] of Object.entries(prompts)) el(id).addEventListener('click', () => {
    const field = el('question');
    field.value = text;
    field.dispatchEvent(new Event('input'));
    field.focus();
    const start = text.indexOf('[');
    field.setSelectionRange(start, text.indexOf(']') + 1);
  });

  function filterDocuments() {
    const term = el('document-filter').value.trim().toLocaleLowerCase('es');
    const cards = [...el('documents').querySelectorAll('.doc-item')];
    let visible = 0;
    for (const card of cards) {
      const matches = card.querySelector('.doc-name').textContent.toLocaleLowerCase('es').includes(term);
      card.hidden = !matches;
      if (matches) visible++;
    }
    el('filter-status').textContent = term && cards.length ? `${visible} de ${cards.length} documentos coinciden con la búsqueda.` : '';
  }
  el('document-filter').addEventListener('input', filterDocuments);
  el('copy-answer').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(answer);
      el('copy-answer').textContent = 'Respuesta copiada';
    } catch {
      el('copy-answer').textContent = 'Selecciona el texto para copiar';
    }
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => { el('copy-answer').textContent = 'Copiar respuesta'; }, 2500);
  });

  return {
    filterDocuments,
    startResearch() { el('research-view').classList.add('research-started'); },
    resetResearch() {
      answer = '';
      el('copy-answer').hidden = true;
      el('document-filter').value = '';
      el('filter-status').textContent = '';
      el('research-view').classList.remove('research-started');
      el('query-title').textContent = '¿Qué quieres descubrir?';
    },
    setAnswer(value) { answer = value; }
  };
}
