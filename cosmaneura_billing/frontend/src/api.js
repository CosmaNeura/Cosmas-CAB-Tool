// Thin fetch wrapper for the FastAPI engine. In dev, Vite proxies /api to
// uvicorn (see vite.config.js); in production, serve the built app behind the
// same origin as the API.
export async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...opts,
    body: opts.body != null ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
  return r.json();
}
