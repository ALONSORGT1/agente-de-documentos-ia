// Same-origin requests: the browser never receives API keys or OAuth credentials.
export const API_ROOT = '';
export const REQUEST_TIMEOUT_MS = 15_000;
export const ASK_TIMEOUT_MS = 150_000;
export const SYNC_TIMEOUT_MS = 600_000;
export const MAX_HISTORY = 6;

export function safeDriveUrl(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return null;
    if (!['drive.google.com', 'docs.google.com'].includes(url.hostname)) return null;
    return url.href;
  } catch {
    return null;
  }
}

export function questionHistory(history) {
  if (!Array.isArray(history)) return [];
  return history
    .filter(item => item?.role === 'user' && typeof item.content === 'string' && item.content.trim())
    .slice(-MAX_HISTORY)
    .map(item => ({role: 'user', content: item.content.trim().slice(0, 3000)}));
}
