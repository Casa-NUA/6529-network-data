// 6529 Monitor live chain relay: the logic (no dependencies). worker.js wires it to Cloudflare.
// Contract: handoffs/specs/2026-11-16-live-chain-contract.md. See README.md.
//
// GET  /v1/live  -> the latest envelope, byte for byte as the collector POSTed it
//                   (404 before the first one, 503 {"stale":true} when it is > 5 min old)
// POST /v1/live  -> stored only if the envelope is signed by a pinned key, is named "live.json",
//                   its payload parses with an integer generated_unix at most 120 s old and
//                   at most 30 s ahead of this clock, greater than the stored one, and the
//                   body is at most 64 KB.
// The signature is the only credential. Nothing is logged (no console calls, observability off);
// the caller's address is used only for the optional rate limit and is never stored.

// The collector's signing key and the reserve kept offline (the same two the apps pin):
// id = first 16 hex digits of SHA-256 of the key's DER SubjectPublicKeyInfo, recomputed below.
export const PINNED = Object.freeze([
  Object.freeze({ id: "9fc874c0f34c3d51", spki: "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEbrnqtEOB9jtiry03n6Z3xJU3lTKapz1ONhc1Ayyw+pu5zhqVK1tgKL9u+988A6xEJB6AMk8VY/rOhNIDLd6AIQ==" }),
  Object.freeze({ id: "04f3b06b89f126a7", spki: "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEASwFg+OUTtwfCWFHY8zlhpCF+imNGvigUAyNCzcKUc71DYtiXk1GFt7oXNyS1RJoI5ks3N3bh8cFF/4rITOM9g==" }),
]);
export const NAME = "live.json";
const PATH = "/v1/live";
const MAX_BODY = 64 * 1024;
const PAST_S = 120;
const FUTURE_S = 30;
const MEMO_MS = 3000;               // a GET answer is reused this long by the same isolate
const STALE_S = 300;
const FIELDS = ["alg", "format", "key", "name", "payload", "sig"];   // exactly sign.py's envelope

class Refused extends Error {
  constructor(status, why) { super(why); this.status = status; }
}
const NO_STORE = { "cache-control": "no-store", "x-content-type-options": "nosniff" };
const empty = (status, extra = {}) => new Response(null, { status, headers: { ...NO_STORE, ...extra } });
const json = (status, body) => new Response(body, { status, headers: { ...NO_STORE, "content-type": "application/json; charset=utf-8" } });
const nowS = () => Math.floor(Date.now() / 1000);

// Per isolate (not shared, lost on eviction; the Durable Object stays the authority):
//  lastGen  the newest generated_unix this isolate knows is stored: an older or equal POST is
//           answered 409 without reaching the Durable Object;
//  memo     the latest envelope read or stored, reused for MEMO_MS so a burst of GETs costs one
//           Durable Object request. The 5-minute staleness is still checked on every GET.
let lastGen = 0;
let memo = null;                    // { at: ms, gen, text }
export function resetIsolate() { lastGen = 0; memo = null; }   // tests: a fresh isolate

// `pins` is PINNED in production (worker.js passes nothing else). Only test.mjs adds a key.
export async function handle(req, env, pins = PINNED) {
  const url = new URL(req.url);
  if (url.pathname !== PATH) return empty(404);
  if (req.method === "GET") {
    if (env.RL_GET) {               // optional, off unless bound (see wrangler.toml / README)
      const { success } = await env.RL_GET.limit({ key: req.headers.get("cf-connecting-ip") || "unknown" });
      if (!success) return empty(429);
    }
    return get(env);
  }
  if (req.method !== "POST") return empty(405, { allow: "GET, POST" });
  // Optional rate limit (wrangler.toml [[ratelimits]] binding RL). The address is used for this
  // one check and is not stored or logged.
  if (env.RL) {
    const { success } = await env.RL.limit({ key: req.headers.get("cf-connecting-ip") || "unknown" });
    if (!success) return empty(429);
  }
  try {
    const text = await readCapped(req, MAX_BODY);
    if (text === null) return empty(413);
    const payload = await verifyEnvelope(text, NAME, pins);
    let p;
    try { p = JSON.parse(payload); } catch { throw new Refused(400, "payload"); }
    if (!p || typeof p !== "object" || Array.isArray(p)) throw new Refused(400, "payload");
    const gen = p.generated_unix;
    if (!Number.isSafeInteger(gen) || gen <= 0) throw new Refused(400, "generated_unix");
    if (nowS() - gen > PAST_S || gen - nowS() > FUTURE_S) throw new Refused(422, "clock");
    if (gen <= lastGen) return empty(409);
    let res;
    try { res = await store(env).fetch("https://live/put", { method: "POST", headers: { "x-gen": String(gen) }, body: text }); }
    catch { return empty(503); }
    if (res.status === 204) {
      lastGen = gen;
      memo = { at: Date.now(), gen, text };
    }
    return empty(res.status === 204 ? 204 : res.status === 409 ? 409 : 503);
  } catch (e) {
    return empty(e instanceof Refused ? e.status : 400);
  }
}

async function get(env) {
  if (!memo || Date.now() - memo.at >= MEMO_MS || Date.now() < memo.at) {
    let res;
    try { res = await store(env).fetch("https://live/get"); } catch { return empty(503); }
    if (res.status === 404) return empty(404);
    if (res.status !== 200) return empty(503);
    const gen = Number(res.headers.get("x-gen"));
    const text = await res.text();
    if (!Number.isSafeInteger(gen)) return empty(503);
    memo = { at: Date.now(), gen, text };
    if (gen > lastGen) lastGen = gen;
  }
  if (nowS() - memo.gen > STALE_S) return json(503, '{"stale":true}');
  return json(200, memo.text);
}

const store = (env) => env.LIVE.get(env.LIVE.idFromName("live"));

// The Durable Object: one instance ("live") holding the latest accepted envelope. Reached only
// through the LIVE binding (never from outside), so it trusts the Worker's checks and does one
// thing itself: the "newer than stored" comparison, done here so two POSTs cannot race.
export class LiveStore {
  constructor(state) { this.state = state; this.cur = undefined; }
  async fetch(req) {
    const path = new URL(req.url).pathname;
    if (path === "/get" && req.method === "GET") {
      const cur = await this.load();
      return cur ? new Response(cur.text, { headers: { "x-gen": String(cur.gen) } }) : new Response(null, { status: 404 });
    }
    if (path === "/put" && req.method === "POST") {
      const gen = Number(req.headers.get("x-gen"));
      const text = await req.text();
      if (!Number.isSafeInteger(gen)) return new Response(null, { status: 400 });
      const cur = await this.load();
      if (cur && gen <= cur.gen) return new Response(null, { status: 409 });
      this.cur = { gen, text };                   // set before awaiting: a racing POST sees it
      await this.state.storage.put("cur", this.cur);
      return new Response(null, { status: 204 });
    }
    return new Response(null, { status: 404 });
  }
  async load() {
    if (this.cur === undefined) this.cur = (await this.state.storage.get("cur")) || null;
    return this.cur;
  }
}

// ---- The envelope check: the same rules as the apps (Shared/SignedData.swift) and the push
// relay (push/worker.js verifySigned, from which this is taken), plus an exact field set.
export async function verifyEnvelope(text, name, pins = PINNED) {
  let e;
  try { e = JSON.parse(text); } catch { throw new Refused(400, "malformed"); }
  if (!e || typeof e !== "object" || Array.isArray(e)) throw new Refused(400, "malformed");
  if (Object.keys(e).sort().join() !== FIELDS.join()) throw new Refused(400, "fields");
  for (const f of ["alg", "key", "name", "payload", "sig"]) if (typeof e[f] !== "string") throw new Refused(400, "malformed");
  if (e.format !== 2 || e.alg !== "ECDSA-P256-SHA256") throw new Refused(400, "unsupported format");
  const pin = pins.find((p) => p.id === e.key);
  if (!pin) throw new Refused(403, "unknown key");
  if (e.name !== name) throw new Refused(403, "wrong name");
  const spki = b64(pin.spki);
  if (hex(new Uint8Array(await crypto.subtle.digest("SHA-256", spki))).slice(0, 16) !== pin.id) throw new Refused(403, "unknown key");
  const key = await crypto.subtle.importKey("spki", spki, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  const sig = derToRaw(b64(e.sig));
  const msg = new TextEncoder().encode(e.name + "\n" + e.payload);
  if (!(await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, sig, msg))) throw new Refused(403, "bad signature");
  return e.payload;
}

const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
function b64(s) {          // strict: only the base64 alphabet, correct padding
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(s)) throw new Refused(400, "malformed");
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}
// A DER ECDSA signature (SEQUENCE of two INTEGERs) to the 64 bytes r||s WebCrypto wants.
// Strict: exact lengths, short-form only, minimal positive integers, nothing after the end.
function derToRaw(d) {
  const bad = () => new Refused(400, "malformed");
  if (d.length < 8 || d.length > 72 || d[0] !== 0x30 || d[1] !== d.length - 2) throw bad();
  let o = 2;
  const out = new Uint8Array(64);
  for (let i = 0; i < 2; i++) {
    if (d[o] !== 0x02) throw bad();
    const n = d[o + 1];
    const v = d.subarray(o + 2, o + 2 + n);
    if (n < 1 || n > 33 || v.length !== n || (v[0] & 0x80)) throw bad();
    if (n > 1 && v[0] === 0 && !(v[1] & 0x80)) throw bad();      // not minimal
    const num = v[0] === 0 ? v.subarray(1) : v;
    if (num.length > 32 || (n === 1 && v[0] === 0)) throw bad();  // too long, or zero
    out.set(num, i * 32 + 32 - num.length);
    o += 2 + n;
  }
  if (o !== d.length) throw bad();
  return out;
}

// The text of a request body, or null when it is larger than `max`: refused on its declared
// length when there is one, and otherwise as soon as the bytes read pass it (push/worker.js).
async function readCapped(msg, max) {
  const declared = Number(msg.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > max) return null;
  if (!msg.body) return "";
  const reader = msg.body.getReader();
  const parts = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) { try { await reader.cancel(); } catch { /* already closed */ } return null; }
    parts.push(value);
  }
  const all = new Uint8Array(size);
  let o = 0;
  for (const p of parts) { all.set(p, o); o += p.byteLength; }
  try { return new TextDecoder("utf-8", { fatal: true }).decode(all); } catch { throw new Refused(400, "not utf-8"); }
}
