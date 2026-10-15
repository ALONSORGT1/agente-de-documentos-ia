import {ApiClient} from './api.js';
import {MAX_HISTORY, safeDriveUrl} from './config.js';

const api = new ApiClient();
const el = id => document.getElementById(id);
const state = {history: [], asking: false, syncing: false, serverSyncing: false, adminRequired: false, cloud: false, accessRequired: false, accessUnlocked: false, healthLoaded: false, syncAvailable: false};

function message(id, text, status = '') {
  const target = el(id);
  target.textContent = text;
  target.dataset.state = status;
}

function createText(tag, text, className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function createSourceLink(source, className = '') {
  const href = safeDriveUrl(source.url);
  const name = typeof source.name === 'string' && source.name ? source.name : 'Documento sin nombre';
  if (!href) return createText('span', name, className);
  const link = createText('a', name, className);
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.setAttribute('aria-label', `${name} (abrir en Google Drive, nueva pestaña)`);
  return link;
}

function formatDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : new Intl.DateTimeFormat('es-MX', {dateStyle: 'medium', timeStyle: 'short'}).format(date);
}

function updateSyncButton() {
  el('sync').disabled = !state.syncAvailable || state.syncing || state.serverSyncing || state.asking;
  el('sync').textContent = state.syncing ? 'Sincronizando…' : state.serverSyncing || state.asking ? 'Operación en curso…' : 'Sincronizar desde Drive';
  el('admin-key').disabled = state.syncing;
  el('ask').disabled = !state.healthLoaded || needsAccess() || state.asking || state.syncing || state.serverSyncing;
}

function needsAccess() {
  return state.cloud && state.accessRequired && !state.accessUnlocked;
}

function showAccessPrompt() {
  el('documents').replaceChildren(createText('p', 'Introduce la clave de acceso para consultar los documentos.', 'empty-state'));
  el('documents').setAttribute('aria-busy', 'false');
  el('doc-count').textContent = '0';
  el('collection-status').textContent = 'Acceso protegido';
  el('last-sync').textContent = '';
  message('message', 'Introduce la clave de acceso en Estado del servicio para comenzar.');
}

function clearAccess() {
  api.setAccessKey('');
  state.accessUnlocked = false;
  showAccessPrompt();
  updateSyncButton();
}

async function loadHealth() {
  const result = await api.health();
  state.healthLoaded = true;
  state.cloud = result.deployment_mode === 'cloud';
  state.accessRequired = result.access_key_required === true;
  state.syncAvailable = !state.cloud && result.sync_available !== false;
  el('deployment-label').textContent = state.cloud ? 'En línea' : 'Uso local';
  el('service-description').textContent = state.cloud
    ? 'Consulta de documentos publicados. Las actualizaciones se preparan desde la instalación local.'
    : 'Google Drive → OpenAI File Search → respuesta con fuentes.';
  el('documents-description').textContent = state.cloud ? 'Documentos publicados para consulta.' : 'Archivos de la carpeta autorizada de Google Drive.';
  el('access-form').hidden = !state.cloud;
  el('sync-controls').hidden = !state.syncAvailable;
  const badge = el('health');
  badge.textContent = result.configured ? state.cloud ? 'Servicio en línea' : 'Servidor local listo' : 'Configuración pendiente';
  badge.dataset.state = result.configured ? 'ready' : 'pending';
  const details = el('config-details');
  details.replaceChildren();
  const checks = result.checks || {};
  const labels = state.cloud
    ? [['openai', 'Servicio de respuestas'], ['access_key', 'Protección de acceso'], ['document_index', 'Índice de documentos']]
    : [['openai', 'API de OpenAI'], ['drive_folder', 'Carpeta de Drive'], ['google_credentials', 'Credenciales de Google'], ['oauth_token', 'Autorización de Google']];
  for (const [key, label] of labels) {
    const row = createText('li', '');
    row.append(createText('span', label));
    const ready = checks[key] === true || checks[key] === 'configured' || checks[key] === 'ready';
    const status = createText('strong', ready ? 'Disponible' : key === 'oauth_token' && checks[key] === false ? 'Autorizar al sincronizar' : 'Pendiente');
    status.dataset.state = ready ? 'ready' : 'pending';
    row.append(status);
    details.append(row);
  }
  state.adminRequired = result.admin_key_required === true;
  el('key-optional').textContent = state.adminRequired ? '(requerida)' : '(opcional)';
  el('admin-key').required = state.adminRequired;
  state.serverSyncing = (result.operation_in_progress === true || result.sync_in_progress === true) && !state.asking && !state.syncing;
  updateSyncButton();
  if (state.serverSyncing) message('sync-message', 'Hay una consulta o sincronización en curso. Actualiza el estado para comprobar cuándo termina.');
}

async function loadDocuments() {
  const box = el('documents');
  box.setAttribute('aria-busy', 'true');
  try {
    const result = await api.documents();
    const documents = Array.isArray(result.documents) ? result.documents : [];
    el('doc-count').textContent = String(Number.isFinite(result.indexed) ? result.indexed : documents.length);
    el('collection-status').textContent = result.vector_store_configured ? 'Índice de búsqueda disponible' : state.cloud ? 'Los documentos todavía no se han publicado.' : 'Sin índice configurado. Sincroniza para comenzar.';
    const lastSync = formatDate(typeof result.last_sync === 'object' ? result.last_sync?.at : result.last_sync);
    el('last-sync').textContent = lastSync ? `Última sincronización: ${lastSync}` : '';
    box.replaceChildren();
    if (!documents.length) {
      box.append(createText('p', 'Aún no hay documentos indexados.', 'empty-state'));
      return;
    }
    for (const doc of documents) {
      if (!doc || typeof doc !== 'object') continue;
      const item = createText('div', '', 'doc-item');
      item.append(createSourceLink(doc, 'doc-name'));
      const mime = doc.mime_type || doc.mimeType || 'Tipo no disponible';
      item.append(createText('span', mime, 'doc-meta doc-mime'));
      const modified = formatDate(doc.modified_time || doc.modifiedTime);
      item.append(createText('span', modified ? `Modificado: ${modified}` : 'Fecha de modificación no disponible', 'doc-meta'));
      box.append(item);
    }
  } finally { box.setAttribute('aria-busy', 'false'); }
}

function renderSources(sources) {
  const list = el('sources');
  list.replaceChildren();
  const items = Array.isArray(sources) ? sources.filter(source => source && typeof source === 'object') : [];
  el('source-count').textContent = `${items.length} ${items.length === 1 ? 'fuente' : 'fuentes'}`;
  if (!items.length) {
    list.append(createText('li', 'No se recibió evidencia documental para esta respuesta.', 'empty-state'));
    return;
  }
  for (const source of items) {
    const verifiedExcerpt = source.evidence_kind === 'verified_excerpt';
    const item = createText('li', '', verifiedExcerpt ? 'source-item source-item-excerpt' : 'source-item');
    const heading = createText('div', '', 'source-heading');
    if (verifiedExcerpt && Number.isInteger(source.reference_number) && source.reference_number > 0) {
      heading.append(createText('span', `[${source.reference_number}]`, 'source-reference'));
    }
    heading.append(createSourceLink(source));
    item.append(heading);
    if (typeof source.snippet === 'string' && source.snippet.trim()) {
      item.append(createText('p', verifiedExcerpt ? 'Pasaje textual comprobado en los fragmentos recuperados.' : 'Primer fragmento recuperado de esta fuente; puede no ser el pasaje citado en la respuesta.', 'source-note'));
      const details = createText('details', '', 'source-details');
      details.append(createText('summary', verifiedExcerpt ? 'Ver pasaje comprobado' : 'Ver fragmento recuperado'));
      details.append(createText('blockquote', source.snippet, 'source-snippet'));
      item.append(details);
    } else {
      item.append(createText('p', 'No se recibió un fragmento de esta fuente. Revisa el documento original.', 'source-note'));
    }
    if (!safeDriveUrl(source.url)) item.append(createText('p', 'Enlace original no disponible.', 'source-note'));
    list.append(item);
  }
}

function updateHistory() {
  const count = state.history.length;
  el('history-status').textContent = count ? `${count} ${count === 1 ? 'pregunta previa' : 'preguntas previas'} como contexto (máximo ${MAX_HISTORY}).` : 'Sin preguntas previas en esta sesión.';
  el('clear-history').disabled = !count || state.asking;
}

async function ask(event) {
  event?.preventDefault();
  if (state.asking || state.syncing || state.serverSyncing) return;
  if (needsAccess()) {
    message('access-message', 'Introduce la clave de acceso para comenzar.');
    el('access-key').focus();
    return;
  }
  const question = el('question').value.trim();
  if (question.length < 3 || question.length > 3000) {
    message('message', 'Escribe una pregunta de entre 3 y 3,000 caracteres.', 'error');
    el('question').focus();
    return;
  }
  const useSearch = el('use-search').checked;
  state.asking = true;
  updateSyncButton();
  el('ask').textContent = 'Consultando…';
  el('response-panel').setAttribute('aria-busy', 'true');
  updateHistory();
  message('message', useSearch ? 'Buscando evidencia y preparando la respuesta…' : 'Consultando en modo experimental, sin recuperar documentos…');
  try {
    const result = await api.ask(question, useSearch, state.history);
    const grounded = useSearch && result.mode !== 'experimental' && result.grounded === true && Array.isArray(result.sources) && result.sources.length > 0;
    el('answer').textContent = typeof result.answer === 'string' && result.answer ? result.answer : 'El agente no devolvió una respuesta.';
    el('answered-question').textContent = `Consulta: ${question}`;
    el('answered-question').hidden = false;
    el('answer-mode').textContent = !useSearch || result.mode === 'experimental' ? 'Experimental · sin recuperación' : grounded ? 'Con evidencia documental' : 'Sin evidencia suficiente';
    el('answer-mode').dataset.state = grounded ? 'grounded' : 'warning';
    const warning = typeof result.warning === 'string' ? result.warning : !useSearch ? 'Esta respuesta se generó sin buscar en tus documentos. No está respaldada por tu base documental.' : !grounded ? 'No se pudo confirmar evidencia suficiente. Contrasta la respuesta con los documentos originales.' : '';
    el('answer-warning').textContent = warning;
    el('answer-warning').hidden = !warning;
    renderSources(useSearch ? result.sources : []);
    state.history.push({role: 'user', content: question});
    state.history = state.history.slice(-MAX_HISTORY);
    message('message', grounded ? 'Consulta finalizada. Revisa las fuentes y los fragmentos recuperados.' : 'Consulta finalizada. Revisa las advertencias y las fuentes disponibles.', grounded ? 'success' : '');
  } catch (error) {
    if (state.cloud && [401, 403].includes(error.status)) clearAccess();
    message('message', error.message, 'error');
  } finally {
    state.asking = false;
    updateSyncButton();
    el('ask').textContent = 'Consultar agente ↗';
    el('response-panel').setAttribute('aria-busy', 'false');
    updateHistory();
  }
}

function renderSyncStats(stats = {}, pendingCount = 0) {
  const target = el('sync-results');
  target.replaceChildren();
  const counters = createText('dl', '', 'sync-stats');
  for (const [key, label] of [['added', 'Nuevos'], ['updated', 'Actualizados'], ['renamed', 'Renombrados'], ['removed', 'Retirados del índice'], ['unchanged', 'Sin cambios'], ['skipped', 'Omitidos']]) {
    if (key === 'skipped' && stats[key] === undefined) continue;
    const entry = createText('div', '');
    entry.append(createText('dt', label), createText('dd', Number.isFinite(stats[key]) ? String(stats[key]) : '0'));
    counters.append(entry);
  }
  const errors = Array.isArray(stats.errors) ? stats.errors : [];
  const errorEntry = createText('div', '');
  errorEntry.append(createText('dt', 'Errores'), createText('dd', String(errors.length)));
  counters.append(errorEntry);
  const pendingEntry = createText('div', '');
  pendingEntry.append(createText('dt', 'Pendientes de resolver'), createText('dd', Number.isFinite(pendingCount) ? String(pendingCount) : '0'));
  counters.append(pendingEntry);
  target.append(counters);
  if (errors.length) {
    const list = createText('ul', '', 'sync-errors');
    for (const error of errors) {
      const text = typeof error === 'string' ? error : `${error?.file || 'Archivo'}: ${error?.error || error?.message || 'No se pudo procesar.'}`;
      list.append(createText('li', text));
    }
    target.append(list);
  }
  target.hidden = false;
  return errors.length;
}

async function sync() {
  if (!state.syncAvailable || state.syncing || state.serverSyncing || state.asking) return;
  const adminKey = el('admin-key').value;
  if (state.adminRequired && !adminKey) {
    message('sync-message', 'El servidor requiere la clave de administración para sincronizar.', 'error');
    el('admin-key').focus();
    return;
  }
  state.syncing = true;
  updateSyncButton();
  el('sync-results').hidden = true;
  message('sync-message', 'Sincronizando Drive e indexando en OpenAI. Puede tardar varios minutos…');
  try {
    const result = await api.sync(adminKey);
    const errors = renderSyncStats(result.stats, result.pending_count);
    const pending = Number.isFinite(result.pending_count) && result.pending_count > 0;
    message('sync-message', errors ? 'Sincronización finalizada con errores. Revisa el detalle de los archivos.' : pending ? 'Sincronización finalizada con operaciones pendientes. Revisa el estado antes de volver a sincronizar.' : 'Sincronización finalizada.', errors || pending ? 'error' : 'success');
  } catch (error) {
    message('sync-message', error.message, 'error');
  } finally {
    state.syncing = false;
    state.serverSyncing = false;
    updateSyncButton();
    // A timeout may leave server-side work running; re-read rather than resubmit.
    await refresh();
  }
}

async function refresh() {
  el('refresh').disabled = true;
  try {
    await loadHealth();
  } catch (error) {
    el('health').textContent = 'Sin conexión al servidor';
    el('health').dataset.state = 'error';
    el('config-details').replaceChildren(createText('li', error.message));
    el('refresh').disabled = false;
    return;
  }
  if (needsAccess()) {
    showAccessPrompt();
  } else {
    try {
      await loadDocuments();
    } catch (error) {
      if (state.cloud && [401, 403].includes(error.status)) {
        clearAccess();
        message('access-message', 'La clave de acceso ya no es válida. Vuelve a entrar.', 'error');
      } else {
        el('documents').replaceChildren(createText('p', error.message, 'empty-state'));
        el('collection-status').textContent = 'No se pudo cargar el índice.';
      }
    }
  }
  el('refresh').disabled = false;
}

async function unlockAccess(event) {
  event.preventDefault();
  const accessKey = el('access-key').value.trim();
  if (!accessKey) {
    message('access-message', 'Introduce la clave de acceso para comenzar.');
    el('access-key').focus();
    return;
  }
  el('access-submit').disabled = true;
  message('access-message', 'Comprobando acceso…');
  clearAccess();
  api.setAccessKey(accessKey);
  try {
    await loadHealth();
    await loadDocuments();
    state.accessUnlocked = true;
    el('access-key').value = '';
    message('message', '');
    message('access-message', 'Ya puedes consultar los documentos.', 'success');
  } catch (error) {
    clearAccess();
    message('access-message', [401, 403].includes(error.status) ? 'La clave no es válida. Revísala e inténtalo de nuevo.' : error.message, 'error');
  } finally {
    el('access-submit').disabled = false;
    updateSyncButton();
  }
}

el('question-form').addEventListener('submit', ask);
el('access-form').addEventListener('submit', unlockAccess);
el('sync').addEventListener('click', sync);
el('refresh').addEventListener('click', refresh);
el('question').addEventListener('input', () => { el('character-count').textContent = `${el('question').value.length.toLocaleString('es-MX')} / 3,000`; });
el('question').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    ask();
  }
});
el('use-search').addEventListener('change', () => { el('experimental-warning').hidden = el('use-search').checked; });
el('clear-history').addEventListener('click', () => { state.history = []; updateHistory(); });
refresh();
