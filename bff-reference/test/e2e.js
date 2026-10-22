// Local end-to-end walk of the reference BFF against a stub wire api:
// login → session cookie → query → key never in any browser-facing response.
// Run: npm test (starts stub upstream + BFF, exits 0/1).
'use strict';

const assert = require('node:assert');
const crypto = require('node:crypto');
const express = require('express');
const { spawn } = require('node:child_process');
const path = require('node:path');

async function main() {
  // ── stub wire api：记录它看到的 Authorization ─────────────────────
  let sawKey = false;
  const stub = express();
  stub.use(express.json());
  stub.all('/api/v1/*', (req, res) => {
    sawKey = req.headers.authorization === 'Bearer wdk-test-key';
    res.json({ path: req.path, echoed: req.body, ok: true });
  });
  const stubServer = await new Promise((resolve) => {
    const s = stub.listen(0, '127.0.0.1', () => resolve(s));
  });

  const PORT = 18787;
  const child = spawn(process.execPath, [path.join(__dirname, '..', 'server.js')], {
    env: {
      ...process.env,
      WIRE_API_URL: `http://127.0.0.1:${stubServer.address().port}`,
      WIRE_DATA_PLANE_KEY: 'wdk-test-key',
      BFF_SESSION_SECRET: crypto.randomBytes(16).toString('hex'),
      PORT: String(PORT),
    },
    stdio: 'ignore',
  });
  try {
    await new Promise((r) => setTimeout(r, 800));
    const base = `http://127.0.0.1:${PORT}`;

    const login = await fetch(`${base}/bff/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ username: 'demo', password: 'demo' }),
    });
    assert.equal(login.status, 200, 'login ok');
    const setCookie = login.headers.get('set-cookie') || '';
    const session = /bff_session=[^;]+/.exec(setCookie)?.[0];
    assert.ok(session, 'session cookie set');
    assert.match(setCookie, /HttpOnly/i, 'cookie is HttpOnly');

    const anonymous = await fetch(`${base}/bff/api/api/v1/datasets/d1/query`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    });
    assert.equal(anonymous.status, 401, '无会话 → 401');

    const query = await fetch(`${base}/bff/api/api/v1/datasets/d1/query`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', cookie: session },
      body: JSON.stringify({ filters: [], limit: 5 }),
    });
    assert.equal(query.status, 200, 'query forwarded');
    const body = await query.json();
    assert.equal(body.path, '/api/v1/datasets/d1/query');
    assert.deepEqual(body.echoed, { filters: [], limit: 5 });
    assert.ok(sawKey, 'upstream received the wdk- key');
    assert.ok(!JSON.stringify(body).includes('wdk-'), 'key never appears in responses');
    assert.ok(!setCookie.includes('wdk-'), 'key never in cookies');

    console.log('BFF e2e: all assertions passed');
  } finally {
    child.kill();
    stubServer.close();
  }
}

main().catch((err) => {
  console.error('BFF e2e FAILED:', err.message);
  process.exit(1);
});
