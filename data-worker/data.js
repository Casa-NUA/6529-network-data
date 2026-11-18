// 6529 Monitor data relay: the logic (no dependencies). worker.js wires it to Cloudflare.
// Spec: handoffs/specs/2026-11-18-data-worker.md. See README.md.
//
// GET  /v1/data/<name> -> the latest envelope for that name, byte for byte as the collector
//                         POSTed it (404 for an unknown or not yet published name)
// POST /v1/data        -> a JSON object {"<name>": <envelope>, ...}: stored only if EVERY entry
//                         is signed by a pinned key under that exact name, the name is on the
//                         allow-list, and none is older than what is stored (all or nothing).
// The signature is the only credential. Nothing is logged (no console calls, observability off);
// the caller's address is used only for the rate limits and is never stored.

// The collector's signing key and the reserve kept offline (the same two the apps pin):
// id = first 16 hex digits of SHA-256 of the key's DER SubjectPublicKeyInfo, recomputed below.
export const PINNED = Object.freeze([
  Object.freeze({ id: "9fc874c0f34c3d51", spki: "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEbrnqtEOB9jtiry03n6Z3xJU3lTKapz1ONhc1Ayyw+pu5zhqVK1tgKL9u+988A6xEJB6AMk8VY/rOhNIDLd6AIQ==" }),
  Object.freeze({ id: "04f3b06b89f126a7", spki: "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEASwFg+OUTtwfCWFHY8zlhpCF+imNGvigUAyNCzcKUc71DYtiXk1GFt7oXNyS1RJoI5ks3N3bh8cFF/4rITOM9g==" }),
]);
// The names published under docs/data/signed/ (sign.py). cycles/index.json is published too,
// so it is allowed beside the spec's list (an unlisted name would refuse the whole batch).
const NAMES = [
  /^(network|validators|blocks|chain|awards|history|releases)\.json$/,
  /^cycles\/(index|\d{5})\.json$/,
  /^days\/(index|\d{5})\.json$/,
];
export const allowed = (name) => typeof name === "string" && NAMES.some((re) => re.test(name));
// Kept per kind (the newest by day number; index.json is not counted).
export const KEEP = Object.freeze({ days: 60, cycles: 40 });
const POST_PATH = "/v1/data";
const GET_PREFIX = "/v1/data/";
export const MAX_BODY = 6 * 1024 * 1024;
export const MAX_ENTRY = 2 * 1000 * 1000;   // under a Durable Object value's 2 MB limit
export const MAX_ENTRIES = 100;             // a storage.put() takes at most 128 keys
const FUTURE_S = 300;                       // a generated_unix this far ahead would lock the name
const MEMO_MS = 5000;                       // a GET answer is reused this long by the same isolate
const FIELDS = ["alg", "format", "key", "name", "payload", "sig"];   // exactly sign.py's envelope

class Refused extends Error {
  constructor(status, why) { super(why); this.status = status; }
}
const NO_STORE = { "cache-control": "no-store", "x-content-type-options": "nosniff" };
const empty = (status, extra = {}) => new Response(null, { status, headers: { ...NO_STORE, ...extra } });
const nowS = () => Math.floor(Date.now() / 1000);

// Per isolate (not shared, lost on eviction; the Durable Object stays the authority): the
// latest envelope read or stored per name, reused for MEMO_MS so a burst of GETs costs one
// Durable Object request per name. Only names that exist are kept (at most ~110).
let memo = new Map();               // name -> { at: ms, text }
export function resetIsolate() { memo = new Map(); }   // tests: a fresh isolate

// `pins` is PINNED in production (worker.js passes nothing else). Only test.mjs adds a key.
export async function handle(req, env, pins = PINNED) {
  const url = new URL(req.url);
  const ip = () => req.headers.get("cf-connecting-ip") || "unknown";
  if (url.pathname.startsWith(GET_PREFIX)) {
    if (req.method !== "GET") return empty(405, { allow: "GET" });
    let name;
    try { name = decodeURIComponent(url.pathname.slice(GET_PREFIX.length)); } catch { return empty(404); }
    if (!allowed(name) || url.search) return empty(404);
    if (env.RL_GET) {               // optional, off unless bound (see wrangler.toml / README)
      const { success } = await env.RL_GET.limit({ key: ip() });
      if (!success) return empty(429);
    }
    return get(env, name);
  }
  if (url.pathname !== POST_PATH) return empty(404);
  if (req.method !== "POST") return empty(405, { allow: "POST" });
  // Rate limit (wrangler.toml [[ratelimits]] binding RL). The address is used for this one
  // check and is not stored or logged.
  if (env.RL) {
    const { success } = await env.RL.limit({ key: ip() });
    if (!success) return empty(429);
  }
  try {
    const text = await readCapped(req, MAX_BODY);
    if (text === null) return empty(413);
    const entries = topLevel(text);             // [[name, raw envelope text], ...] in body order
    if (entries.length > MAX_ENTRIES) return empty(413);
    const batch = [];
    for (const [name, raw] of entries) {        // every entry first; any refusal refuses all
      if (!allowed(name)) throw new Refused(403, "name");
      if (raw.length > MAX_ENTRY) return empty(413);
      const payload = await verifyEnvelope(raw, name, pins);
      batch.push({ name, gen: genOf(payload), text: raw });
    }
    let res;
    try { res = await store(env).fetch("https://data/put", { method: "POST", body: JSON.stringify(batch) }); }
    catch { return empty(503); }
    if (res.status === 204) {
      const at = Date.now();
      for (const e of batch) memo.set(e.name, { at, text: e.text });
      for (const n of (res.headers.get("x-evicted") || "").split(",")) if (n) memo.delete(n);
      return empty(204);
    }
    return empty(res.status === 409 ? 409 : 503);
  } catch (e) {
    return empty(e instanceof Refused ? e.status : 400);
  }
}

// A payload's generated_unix: a positive integer, or null for files without one (a payload
// that is not an object, or an object without the field). Present but not an integer: 400.
function genOf(payload) {
  let p;
  try { p = JSON.parse(payload); } catch { throw new Refused(400, "payload"); }
  if (!p || typeof p !== "object" || Array.isArray(p) || !Object.hasOwn(p, "generated_unix")) return null;
  const gen = p.generated_unix;
  if (!Number.isSafeInteger(gen) || gen <= 0) throw new Refused(400, "generated_unix");
  if (gen - nowS() > FUTURE_S) throw new Refused(422, "clock");
  return gen;
}

async function get(env, name) {
  let m = memo.get(name);
  if (!m || Date.now() - m.at >= MEMO_MS || Date.now() < m.at) {
    let res;
    try { res = await store(env).fetch("https://data/get?name=" + encodeURIComponent(name)); } catch { return empty(503); }
    if (res.status === 404) { memo.delete(name); return empty(404); }
    if (res.status !== 200) return empty(503);
    m = { at: Date.now(), text: await res.text() };
    memo.set(name, m);
  }
  return new Response(m.text, { status: 200, headers: {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "public, max-age=30",
    "x-content-type-options": "nosniff",
  } });
}

const store = (env) => env.DATA.get(env.DATA.idFromName("data"));
const KEY = (name) => "e:" + name;
const dayOf = (name) => { const m = /^(days|cycles)\/(\d{5})\.json$/.exec(name); return m ? { kind: m[1], day: Number(m[2]) } : null; };

// The Durable Object: one instance ("data") holding the latest accepted envelope per name, as
// "e:<name>" -> { gen, seq, text }, plus "names": the list of stored names (so the eviction and a
// GET of a name never stored need no storage read). Reached only through the DATA binding (never
// from outside), so it trusts the Worker's signature and name checks and does the rest itself,
// so two POSTs cannot race: the no-rollback rules for the whole batch, then one atomic write
// that also drops the day files beyond KEEP.
//
// No rollback, per name:
//  - a file with generated_unix: never an older one (an equal one with other bytes is taken);
//  - a file without one (validators, cycles/*, days/*, the indexes), which on its own could be
//    an old signed copy replayed: only together with a CHANGED file that has generated_unix
//    (network.json changes on every run). The batch's sequence S is the highest generated_unix
//    among the entries it actually changes (same-bytes entries do not count); S is stored with
//    every entry the batch writes, and a file without generated_unix is replaced only by a batch
//    whose S is greater than the S stored with it. No such S: 409.
export class DataStore {
  constructor(state) { this.state = state; this.cache = new Map(); this.names = undefined; }
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/get" && req.method === "GET") {
      const cur = await this.load(url.searchParams.get("name") || "");
      return cur ? new Response(cur.text) : new Response(null, { status: 404 });
    }
    if (url.pathname === "/put" && req.method === "POST") {
      let batch;
      try { batch = JSON.parse(await req.text()); } catch { return new Response(null, { status: 400 }); }
      if (!Array.isArray(batch) || !batch.length || batch.length > MAX_ENTRIES) return new Response(null, { status: 400 });
      const changed = [];
      for (const e of batch) {
        if (!e || !allowed(e.name) || typeof e.text !== "string" || !(e.gen === null || Number.isSafeInteger(e.gen))) return new Response(null, { status: 400 });
        const cur = await this.load(e.name);
        if (cur && cur.text === e.text) continue;                       // the same bytes again: nothing to do
        if (cur && cur.gen !== null && (e.gen === null || e.gen < cur.gen)) return new Response(null, { status: 409 });
        changed.push([e, cur]);
      }
      if (!changed.length) return new Response(null, { status: 204 });
      let seq = null;
      for (const [e] of changed) if (e.gen !== null && (seq === null || e.gen > seq)) seq = e.gen;
      for (const [e, cur] of changed) {
        if (e.gen === null && (seq === null || seq <= ((cur && cur.seq) || 0))) return new Response(null, { status: 409 });
      }
      const put = new Map(changed.map(([e]) => [e.name, { gen: e.gen, seq, text: e.text }]));
      const names = new Set([...(await this.loadNames()), ...put.keys()]);
      const evicted = [];
      for (const kind of Object.keys(KEEP)) {
        const days = [...names].filter((n) => dayOf(n)?.kind === kind).sort((a, b) => dayOf(b).day - dayOf(a).day);
        evicted.push(...days.slice(KEEP[kind]));
      }
      for (const n of evicted) { names.delete(n); put.delete(n); }
      const entries = { names: [...names].sort() };
      for (const [n, v] of put) entries[KEY(n)] = v;
      // No await between the two writes: the Durable Object commits them together.
      const w = [this.state.storage.put(entries)];
      if (evicted.length) w.push(this.state.storage.delete(evicted.map(KEY)));
      await Promise.all(w);
      this.names = names;
      for (const [n, v] of put) this.cache.set(n, v);
      for (const n of evicted) this.cache.delete(n);
      return new Response(null, { status: 204, headers: evicted.length ? { "x-evicted": evicted.join(",") } : {} });
    }
    return new Response(null, { status: 404 });
  }
  // Only names in the stored list are read; nothing is remembered for a name not stored, so
  // GETs of unknown names neither read storage nor grow the cache.
  async load(name) {
    if (!allowed(name) || !(await this.loadNames()).has(name)) return null;
    let cur = this.cache.get(name);
    if (!cur) {
      cur = (await this.state.storage.get(KEY(name))) || null;
      if (cur) this.cache.set(name, cur);
    }
    return cur;
  }
  async loadNames() {
    if (this.names === undefined) this.names = new Set((await this.state.storage.get("names")) || []);
    return this.names;
  }
}

// The top-level members of a POST body, each value as its EXACT text in the body (so GET can
// return the envelope byte for byte, whatever JSON.parse would re-serialize). The body must be
// a non-empty JSON object of objects, without repeated names.
export function topLevel(text) {
  const bad = () => new Refused(400, "malformed");
  try { const v = JSON.parse(text); if (!v || typeof v !== "object" || Array.isArray(v)) throw 0; } catch { throw bad(); }
  // text is valid JSON from here on, so only the value boundaries have to be found.
  const ws = (i) => { while (i < text.length && " \t\n\r".includes(text[i])) i++; return i; };
  const strEnd = (i) => { let j = i + 1; while (text[j] !== '"') j += text[j] === "\\" ? 2 : 1; return j + 1; };
  const valEnd = (i) => {
    if (text[i] === '"') return strEnd(i);
    if (text[i] === "{" || text[i] === "[") {
      let depth = 0, j = i;
      for (;;) {
        const c = text[j];
        if (c === '"') { j = strEnd(j); continue; }
        if (c === "{" || c === "[") depth++;
        else if (c === "}" || c === "]") { depth--; if (depth === 0) return j + 1; }
        j++;
      }
    }
    let j = i; while (j < text.length && !",}] \t\n\r".includes(text[j])) j++; return j;
  };
  const out = [], seen = new Set();
  let i = ws(0) + 1;                           // past "{"
  i = ws(i);
  if (text[i] === "}") throw bad();            // nothing to store
  for (;;) {
    const ke = strEnd(i);
    const name = JSON.parse(text.slice(i, ke));
    i = ws(ws(ke) + 1);                        // past ":"
    const ve = valEnd(i);
    if (text[i] !== "{" || seen.has(name)) throw bad();
    seen.add(name);
    out.push([name, text.slice(i, ve)]);
    i = ws(ve);
    if (text[i] === "}") break;
    i = ws(i + 1);                             // past ","
  }
  return out;
}

// ---- The envelope check: copied from live-worker/live.js (the same rules as the apps'
// Shared/SignedData.swift and push/worker.js verifySigned), plus an exact field set.
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
