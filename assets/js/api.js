import {API_ROOT, REQUEST_TIMEOUT_MS, ASK_TIMEOUT_MS, SYNC_TIMEOUT_MS, questionHistory} from './config.js';

export class ApiClient {
  #accessKey = '';
  constructor(fetchImpl = (...args) => fetch(...args)) { this.fetch = fetchImpl; }

  setAccessKey(value) { this.#accessKey = typeof value === 'string' ? value.trim() : ''; }

  async request(path, options = {}, timeout = REQUEST_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const headers = options.headers instanceof Headers || Array.isArray(options.headers)
        ? Object.fromEntries(new Headers(options.headers).entries()) : {...options.headers};
      if (this.#accessKey) headers['X-App-Key'] = this.#accessKey;
      const response = await this.fetch(API_ROOT + path, {...options, headers, signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.toLowerCase().includes('application/json')) {
        throw new Error(`El servidor devolvió una respuesta no válida (HTTP ${response.status}). Inténtalo de nuevo en unos momentos.`);
      }
      let data;
      try { data = await response.json(); }
      catch { throw new Error('No se pudo interpretar la respuesta del servidor.'); }
      if (!response.ok || data?.success === false) {
        const detail = typeof data?.detail === 'string' ? data.detail : typeof data?.error === 'string' ? data.error : null;
        const error = new Error(detail || (response.status === 401 || response.status === 403 ? 'La clave de acceso no es válida. Inténtalo de nuevo.' : response.status === 422 ? 'La solicitud contiene datos no válidos. Revisa la pregunta e inténtalo de nuevo.' : `No se pudo completar la solicitud (HTTP ${response.status}).`));
        error.status = response.status;
        throw error;
      }
      if (!data || typeof data !== 'object') throw new Error('El servidor devolvió datos no válidos.');
      return data;
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error(path === '/api/sync'
          ? 'La sincronización superó el tiempo de espera. Puede continuar en el servidor; actualiza el estado antes de reintentar.'
          : 'La solicitud superó el tiempo de espera. Inténtalo de nuevo.');
      }
      if (error instanceof TypeError) throw new Error('No se pudo conectar con el servicio. Comprueba tu conexión e inténtalo de nuevo.');
      throw error;
    } finally { clearTimeout(timer); }
  }

  health() { return this.request('/api/health'); }
  login() { return this.request('/api/session', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'AgenteDocumentalTIC'}, body: '{}'}); }
  logout() { return this.request('/api/logout', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'AgenteDocumentalTIC'}, body: '{}'}); }
  documents() { return this.request('/api/documents'); }
  ask(question, useSearch, history = []) {
    return this.request('/api/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'AgenteDocumentalTIC'},
      body: JSON.stringify({question, use_search: Boolean(useSearch), history: questionHistory(history)}),
    }, ASK_TIMEOUT_MS);
  }
  sync(adminKey = '') {
    const headers = {'Content-Type': 'application/json', 'X-Requested-With': 'AgenteDocumentalTIC'};
    if (adminKey) headers['X-Admin-Key'] = adminKey;
    return this.request('/api/sync', {method: 'POST', headers, body: JSON.stringify({})}, SYNC_TIMEOUT_MS);
  }
}
