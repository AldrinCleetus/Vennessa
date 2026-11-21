/** API base URL resolution for local dev and deployed static builds.

 * Locally, leaving VITE_API_URL empty keeps requests relative so the Vite dev
 * proxy (/api -> http://127.0.0.1:8080) handles them. On Render, set
 * VITE_API_URL to the deployed backend origin and Vite bakes it into the build.
 */
const configuredBase = (import.meta.env.VITE_API_URL ?? '').trim().replace(/\/+$/, '');

export const API_BASE_URL = configuredBase;

export function apiUrl(path: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${configuredBase}${normalized}`;
}

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(apiUrl(path), init);
}
