// Reference BFF for the wire REST data plane (wire-customer-data-engine 7.2).
//
// Discipline (the whole point of this file): the data-plane key NEVER reaches
// the browser. Browser gets an HttpOnly session cookie; this server swaps it
// for `Authorization: Bearer wdk-…` when forwarding. Upstream responses —
// including 429 rate limits and 403 allowlist rejections — pass through
// unchanged: the BFF never masks, retries or rewrites them.
'use strict';

const crypto = require('node:crypto');
const express = require('express');

const {
  WIRE_API_URL,
  WIRE_DATA_PLANE_KEY,
  BFF_SESSION_SECRET,
  PORT = '8787',
} = process.env;

if (!WIRE_API_URL || !WIRE_DATA_PLANE_KEY || !BFF_SESSION_SECRET) {
  console.error('env 缺项：需要 WIRE_API_URL / WIRE_DATA_PLANE_KEY / BFF_SESSION_SECRET');
  process.exit(1);
}

// ── session cookie（HMAC 签名，HttpOnly）────────────────────────────

const SESSION_TTL_S = 3600;

function sign(payload) {
  return crypto.createHmac('sha256', BFF_SESSION_SECRET).update(payload).digest('base64url');
}

function mintSession(username) {
  const payload = Buffer.from(
    JSON.stringify({ u: username, exp: Math.floor(Date.now() / 1000) + SESSION_TTL_S })
  ).toString('base64url');
  return `${payload}.${sign(payload)}`;
}

function readSession(cookieHeader) {
  const raw = String(cookieHeader || '')
    .split(';')
    .map((kv) => kv.trim())
    .find((kv) => kv.startsWith('bff_session='));
  if (!raw) return null;
  const [payload, sig] = raw.slice('bff_session='.length).split('.');
  if (!payload || !sig || sign(payload) !== sig) return null;
  try {
    const data = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
    if (typeof data.exp !== 'number' || data.exp < Math.floor(Date.now() / 1000)) return null;
    return data;
  } catch {
    return null;
  }
}

// ── demo user table（替换为真实用户体系；会话机制不变）──────────────

const DEMO_USERS = new Map([['demo', 'demo']]);

const app = express();
app.use(express.json({ limit: '4mb' }));
// 演示前端（参考形态的活样例：页面无任何 wdk- 凭据，全程走 /bff/*）
app.use(express.static(require('node:path').join(__dirname, 'public')));

app.post('/bff/login', (req, res) => {
  const { username, password } = req.body || {};
  if (!DEMO_USERS.get(username) || DEMO_USERS.get(username) !== password) {
    return res.status(401).json({ error: '用户名或密码不符（演示账号 demo/demo）' });
  }
  res.setHeader(
    'Set-Cookie',
    `bff_session=${mintSession(username)}; HttpOnly; Path=/; SameSite=Lax; Max-Age=${SESSION_TTL_S}`
  );
  res.json({ ok: true, username });
});

app.get('/bff/me', (req, res) => {
  const session = readSession(req.headers.cookie);
  if (!session) return res.status(401).json({ error: '未登录或会话过期' });
  res.json({ username: session.u });
});

// ── forwarding：会话 → wdk- key → wire api ──────────────────────────

app.all('/bff/api/*', async (req, res) => {
  if (!readSession(req.headers.cookie)) {
    return res.status(401).json({ error: '未登录或会话过期' });
  }
  const upstream = new URL(
    req.originalUrl.replace(/^\/bff\/api/, '').replace(/^(\?|&)/, '') || '/',
    WIRE_API_URL
  );
  const init = {
    method: req.method,
    headers: {
      Authorization: `Bearer ${WIRE_DATA_PLANE_KEY}`,
      'content-type': 'application/json',
    },
  };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    init.body = JSON.stringify(req.body ?? {});
  }
  let upstreamRes;
  try {
    upstreamRes = await fetch(upstream, init);
  } catch (err) {
    return res.status(502).json({ error: `wire api 不可达: ${err.message}` });
  }
  res.status(upstreamRes.status);
  res.setHeader('content-type', upstreamRes.headers.get('content-type') || 'application/json');
  res.send(await upstreamRes.text());
});

app.listen(Number(PORT), () => {
  console.log(`wire-bff-reference on :${PORT} → ${WIRE_API_URL}`);
});
