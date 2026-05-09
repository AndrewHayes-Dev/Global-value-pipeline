// worker/index.js — Global Investor Data Worker
// Different request flow from the original:
//   - Uses Authorization: Bearer <token> header (instead of X-API-Token)
//   - Exposes GET /manifest  → JSON list of all available files + sizes
//   - Exposes GET /<key>     → streams R2 object
// Deploy:  wrangler deploy
// Secret:  wrangler secret put GLOBAL_API_SECRET

const STOCK_FILES = [
  'stocks/sp500.json',
  'stocks/djia.json',
  'stocks/nasdaq.json',
  'stocks/russell2000.json',
  'stocks/xao.json',
];
const META_FILES = ['indices.json', 'xao.json', 'metadata.json'];

export default {
  async fetch(request, env) {
    // ── Auth ──────────────────────────────────────────────────────────────────
    const authHeader = request.headers.get('Authorization') ?? '';
    const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : '';
    if (!token || token !== env.GLOBAL_API_SECRET) {
      return new Response('Unauthorized', { status: 401 });
    }

    const url  = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, '');

    // ── Manifest endpoint ─────────────────────────────────────────────────────
    if (path === 'manifest' || path === '') {
      const allKeys = [...META_FILES, ...STOCK_FILES];
      const entries = await Promise.all(
        allKeys.map(async (key) => {
          const head = await env.GLOBAL_R2_BUCKET.head(key);
          return { key, size: head?.size ?? null };
        })
      );
      return new Response(JSON.stringify({ files: entries }), {
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        },
      });
    }

    // ── File download ─────────────────────────────────────────────────────────
    const object = await env.GLOBAL_R2_BUCKET.get(path);
    if (!object) {
      return new Response('Not Found', { status: 404 });
    }

    const headers = new Headers();
    object.writeHttpMetadata(headers);

    if (path.endsWith('.xlsx')) {
      headers.set(
        'Content-Type',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      );
      headers.set('Content-Disposition', `attachment; filename="${path.split('/').pop()}"`);
      headers.set('Cache-Control', 'public, max-age=86400');
    } else {
      headers.set('Content-Type', 'application/json; charset=utf-8');
      headers.set('Cache-Control', 'public, max-age=86400');
    }

    return new Response(object.body, { headers });
  },
};
