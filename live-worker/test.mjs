// Offline test of the live relay (live.js through worker.js's wiring), with a fake Durable
// Object namespace and storage. `node test.mjs` fails loudly on a wrong result.
//
// Fixtures (fixtures/):
//   live-envelope.json      a REAL live.json from collector/live.py's dry run against the node,
//                           signed by sign.py's sign_envelope (the function live.py uses) with a
//                           THROWAWAY P-256 key made for this test (openssl). Its public key is
//                           test-key.spki.b64 and is added to the pins ONLY here, by passing an
//                           extra pin to handle(); worker.js has no way to add one.
//   real-signed-network.json  docs/data/signed/network.json as published (production key), to
//                           show the production pins verify a real envelope.
// Envelopes with other generated_unix values are signed in this file with a second throwaway
// key (WebCrypto), since the fixture's time is fixed.
import fs from "node:fs";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
const dir = fileURLToPath(new URL("./fixtures/", import.meta.url));
const LIVE_ENV = fs.readFileSync(dir + "live-envelope.json", "utf8");
const REAL_NETWORK = fs.readFileSync(dir + "real-signed-network.json", "utf8");
const TEST_SPKI = fs.readFileSync(dir + "test-key.spki.b64", "utf8").replace(/\s+/g, "");

const live = await import("./live.js");
const worker = (await import("./worker.js")).default;
const { handle, verifyEnvelope, PINNED, LiveStore, resetIsolate } = live;
const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const idOf = async (spkiBytes) => hex(await crypto.subtle.digest("SHA-256", spkiBytes)).slice(0, 16);
const testPin = { id: await idOf(Buffer.from(TEST_SPKI, "base64")), spki: TEST_SPKI };
assert.equal(JSON.parse(LIVE_ENV).key, testPin.id, "the fixture is signed by the throwaway key");
assert.ok(!PINNED.some((p) => p.id === testPin.id), "the throwaway key is not a production pin");

// A second throwaway key, signing here (WebCrypto gives r||s; envelopes carry DER like openssl).
const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
const spki2 = new Uint8Array(await crypto.subtle.exportKey("spki", kp.publicKey));
const pin2 = { id: await idOf(spki2), spki: Buffer.from(spki2).toString("base64") };
const PINS = [...PINNED, testPin, pin2];
function int(b) {                     // minimal positive DER INTEGER
  let i = 0; while (i < b.length - 1 && b[i] === 0) i++;
  const v = b.subarray(i);
  return v[0] & 0x80 ? [0x02, v.length + 1, 0, ...v] : [0x02, v.length, ...v];
}
const toDer = (raw) => { const r = int(raw.subarray(0, 32)), s = int(raw.subarray(32)); return Uint8Array.from([0x30, r.length + s.length, ...r, ...s]); };
const payloadOf = (envText) => JSON.parse(JSON.parse(envText).payload);
const BASE = payloadOf(LIVE_ENV);
async function signed(payloadObj, { name = "live.json", key = pin2.id } = {}) {
  const payload = typeof payloadObj === "string" ? payloadObj : JSON.stringify(payloadObj);
  const raw = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, kp.privateKey, new TextEncoder().encode(name + "\n" + payload)));
  return JSON.stringify({ alg: "ECDSA-P256-SHA256", format: 2, key, name, payload, sig: Buffer.from(toDer(raw)).toString("base64") });
}
const at = (gen) => ({ ...BASE, generated_unix: gen });

// ---- fakes: Durable Object namespace + storage, rate limiter, clock -----------------------
const disk = new Map();
let obj = new LiveStore({ storage: { async get(k) { return disk.get(k); }, async put(k, v) { disk.set(k, structuredClone(v)); } } });
const restartObject = () => { obj = new LiveStore({ storage: obj.state.storage }); };
let limited = false, limitKeys = [], doCalls = 0;
const env = {
  LIVE: { idFromName: (n) => ({ n }), get: (id) => { assert.equal(id.n, "live"); return { fetch: (u, init) => { doCalls++; return obj.fetch(new Request(u, init)); } }; } },
  RL: { async limit({ key }) { limitKeys.push(key); return { success: !limited }; } },
};
let NOW = BASE.generated_unix + 5;
Date.now = () => NOW * 1000;
const U = "https://monitor6529-live.example.workers.dev/v1/live";
const POST = (body, pins = PINS, headers = {}) => handle(new Request(U, { method: "POST", body, headers: { "cf-connecting-ip": "203.0.113.9", ...headers } }), env, pins);
const GET = () => handle(new Request(U), env, PINS);
const ok = [];
const check = async (label, res, status) => { assert.equal(res.status, status, label + ": got " + res.status); ok.push(label); };

// ---- 1. signature rules, directly -------------------------------------------------------
assert.equal(await verifyEnvelope(REAL_NETWORK, "network.json"), JSON.parse(REAL_NETWORK).payload);    // production pins, real file
ok.push("production pins verify a real published envelope");
assert.equal(await verifyEnvelope(LIVE_ENV, "live.json", PINS), JSON.parse(LIVE_ENV).payload);
await assert.rejects(verifyEnvelope(LIVE_ENV, "live.json"), /unknown key/);                            // production pins only
ok.push("the throwaway key is refused with production pins");

// ---- 2. GET before anything, then the real fixture --------------------------------------
await check("GET before the first publish = 404", await GET(), 404);
await check("production handler refuses the test-key envelope (403)", await worker.fetch(new Request(U, { method: "POST", body: LIVE_ENV }), env), 403);
await check("GET still 404", await GET(), 404);
await check("real live.py envelope accepted", await POST(LIVE_ENV), 204);
let r = await GET();
await check("GET 200", r, 200);
assert.equal(r.headers.get("cache-control"), "no-store");
assert.match(r.headers.get("content-type"), /^application\/json/);
assert.equal(r.headers.get("access-control-allow-origin"), null, "no CORS");
assert.equal(await r.text(), LIVE_ENV, "GET returns the envelope byte for byte");
ok.push("GET body is the POSTed text verbatim");
assert.deepEqual(limitKeys, ["unknown", "203.0.113.9"]);  // POSTs go through the limiter (the production-handler one had no address)
await check("same envelope again = 409 (not newer)", await POST(LIVE_ENV), 409);

// ---- 3. tampered / renamed / wrong key / malformed ---------------------------------------
const edit = (text, f) => { const e = JSON.parse(text); f(e); return JSON.stringify(e); };
NOW += 30;
const fresh = await signed(at(NOW));
await check("tampered payload (one vote changed)", await POST(edit(fresh, (e) => { e.payload = e.payload.replace(/"votes":(\d+)/, (_, n) => '"votes":' + (Number(n) + 1)); })), 403);
await check("tampered generated_unix", await POST(edit(fresh, (e) => { e.payload = e.payload.replace(String(NOW), String(NOW + 1)); })), 403);
await check("renamed envelope (name field)", await POST(edit(fresh, (e) => { e.name = "network.json"; })), 403);
await check("signed under another name", await POST(await signed(at(NOW), { name: "chain.json" })), 403);
await check("real network.json (production key, other name)", await POST(REAL_NETWORK), 403);
await check("key id of the reserve on a test-key signature", await POST(edit(fresh, (e) => { e.key = "04f3b06b89f126a7"; })), 403);
await check("unknown key id", await POST(edit(fresh, (e) => { e.key = "0000000000000000"; })), 403);
await check("extra envelope field", await POST(edit(fresh, (e) => { e.note = "x"; })), 400);
await check("missing sig", await POST(edit(fresh, (e) => { delete e.sig; })), 400);
await check("format 1", await POST(edit(fresh, (e) => { e.format = 1; })), 400);
await check("non-minimal DER signature", await POST(edit(fresh, (e) => { const d = Buffer.from(e.sig, "base64"); const n = d[3]; const fixed = Buffer.concat([Buffer.from([0x30, d[1] + 1, 0x02, n + 1, 0x00]), d.subarray(4)]); e.sig = fixed.toString("base64"); })), 400);
await check("sig not base64", await POST(edit(fresh, (e) => { e.sig = e.sig.slice(0, -2) + "!!"; })), 400);
await check("not JSON", await POST("{nope"), 400);
await check("payload not JSON (validly signed)", await POST(await signed("not json")), 400);
await check("payload without generated_unix", await POST(await signed({ ...BASE, generated_unix: undefined })), 400);
await check("generated_unix as string", await POST(await signed({ ...BASE, generated_unix: String(NOW) })), 400);
r = await GET(); assert.equal(await r.text(), LIVE_ENV); ok.push("nothing refused was stored");

// ---- 4. clock window and monotonic -------------------------------------------------------
await check("generated_unix 31 s in the future", await POST(await signed(at(NOW + 31))), 422);
await check("generated_unix 121 s old", await POST(await signed(at(NOW - 121))), 422);
await check("older than stored (but in the window)", await POST(await signed(at(BASE.generated_unix - 1))), 409);
await check("equal to stored", await POST(await signed(at(BASE.generated_unix))), 409);
await check("30 s in the future is accepted", await POST(await signed(at(NOW + 30))), 204);
doCalls = 0;
await check("then an older one is refused", await POST(fresh), 409);
assert.equal(doCalls, 0, "answered from the isolate's last gen, without the Durable Object");
ok.push("older POST refused before reaching the Durable Object");
NOW += 121;                                                    // stored one is now at NOW-1
const later = await signed(at(NOW));
await check("newer accepted", await POST(later), 204);
restartObject(); resetIsolate();                               // object evicted, fresh isolate
doCalls = 0;
await check("after a restart the stored one still counts (equal = 409)", await POST(later), 409);
assert.equal(doCalls, 1, "a fresh isolate asks the Durable Object");
ok.push("a fresh isolate's 409 comes from the Durable Object");
r = await GET(); assert.equal(await r.text(), later); ok.push("after a restart GET returns the stored envelope");

// ---- memo: GETs within 3 s cost one Durable Object request; a POST updates it at once
resetIsolate(); doCalls = 0;
await GET(); await GET(); assert.equal(doCalls, 1);
NOW += 3; await GET(); assert.equal(doCalls, 2);
ok.push("GETs within 3 s share one Durable Object read");
NOW += 1;
const newer = await signed(at(NOW));
await check("newer accepted", await POST(newer), 204);
doCalls = 0; r = await GET(); assert.equal(await r.text(), newer); assert.equal(doCalls, 0);
ok.push("a stored POST is served at once by the same isolate");

// ---- 5. size cap -------------------------------------------------------------------------
const pad = (n) => ({ ...at(NOW + 1), pad: "x".repeat(n) });
const big = await signed(pad(70000));
await check("oversized (declared length)", await POST(big), 413);
const stream = (text) => new ReadableStream({ start(c) { const b = new TextEncoder().encode(text); for (let i = 0; i < b.length; i += 4096) c.enqueue(b.subarray(i, i + 4096)); c.close(); } });
await check("oversized (streamed, no length)", await handle(new Request(U, { method: "POST", body: stream(big), duplex: "half" }), env, PINS), 413);
const justUnder = await signed(pad(64 * 1024 - 900 - JSON.stringify(at(NOW + 1)).length * 1.2 | 0));
assert.ok(justUnder.length <= 64 * 1024, "fixture under the cap: " + justUnder.length);
await check("just under 64 KB accepted", await POST(justUnder), 204);
await check("invalid UTF-8 body", await POST(new Uint8Array([0x7b, 0xff, 0x7d])), 400);

// ---- 6. stale, methods, paths, rate limit ------------------------------------------------
NOW = JSON.parse(JSON.parse(justUnder).payload).generated_unix + 300;   // the stored one
await check("exactly 5 min old still served", await GET(), 200);
NOW += 1;
r = await GET();
await check("older than 5 min = 503", r, 503);
assert.equal(await r.text(), '{"stale":true}');
assert.equal(r.headers.get("cache-control"), "no-store");
await check("PUT = 405", await handle(new Request(U, { method: "PUT", body: "x" }), env, PINS), 405);
await check("other path = 404", await handle(new Request("https://x/v1/other"), env, PINS), 404);
await check("trailing slash = 404", await handle(new Request(U + "/"), env, PINS), 404);
limited = true;
await check("rate limited POST = 429", await POST(await signed(at(NOW))), 429);
await check("GET is not limited by RL", await GET(), 503);
limited = false;
env.RL_GET = { async limit() { return { success: false } } };
await check("optional RL_GET limits GET = 429", await GET(), 429);
delete env.RL_GET;
await check("storage failure = 503", await (async () => { const s = obj.state.storage.put; obj.state.storage.put = async () => { throw new Error("disk"); }; const x = await POST(await signed(at(NOW))); obj.state.storage.put = s; return x; })(), 503);

// ---- 7. the payload of the real fixture is what the contract says -----------------------
const p = BASE;
assert.deepEqual(Object.keys(p).sort(), ["chain", "day", "day_blocks", "generated_unix", "keys", "slots"]);
assert.equal(p.slots.length, 60);
p.slots.forEach((s, i) => { assert.equal(s.slot, p.slots[0].slot - i); assert.equal(s.time, p.slots[0].time - i * p.chain.slot_ms); });
const nbytes = Math.ceil(p.keys.length / 8);
for (const s of p.slots) {
  if (s.height === null) { assert.deepEqual(Object.keys(s).sort(), ["height", "slot", "time"]); continue; }
  assert.deepEqual(Object.keys(s).sort(), ["final", "height", "key", "maker", "slot", "time", "v", "votes"]);
  const bits = Buffer.from(s.v, "base64"); assert.equal(bits.length, nbytes);
  const set = [...bits].reduce((n, b) => n + [...Array(8)].filter((_, i) => b >> i & 1).length, 0);
  assert.ok(set <= s.votes && (s.votes === 0) === (set === 0), "bits vs votes at slot " + s.slot);
}
ok.push("fixture payload: 60 slots newest first, bitmaps sized to keys, set bits <= votes");

console.log(ok.map((x) => "ok  " + x).join("\n"));
console.log(`\n${ok.length} checks passed`);
