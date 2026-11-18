// Offline test of the data relay (data.js through worker.js's wiring), with a fake Durable
// Object namespace and storage. `node test.mjs` fails loudly on a wrong result.
//
// Fixtures (fixtures/): real-*.json are files from docs/data/signed/ as published (production
// key 9fc874c0f34c3d51), copied unchanged: network, chain, cycles/20775, cycles/index,
// days/index, days/20774. They show the production pins verify real files and that GET returns
// them byte for byte. Envelopes with other contents are signed in this file with throwaway
// WebCrypto keys: TEST is added to the pins ONLY here (by passing an extra pin to handle(),
// as live-worker/test.mjs does; worker.js has no way to add one), OTHER is never pinned.
import fs from "node:fs";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
const dir = fileURLToPath(new URL("./fixtures/", import.meta.url));
const REAL = {};
for (const n of ["network.json", "chain.json", "cycles/20775.json", "cycles/index.json", "days/index.json", "days/20774.json"]) {
  REAL[n] = fs.readFileSync(dir + "real-" + n.replace("/", "-"), "utf8");
}

const data = await import("./data.js");
const worker = (await import("./worker.js")).default;
const { handle, verifyEnvelope, PINNED, DataStore, resetIsolate, topLevel, allowed } = data;
const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const idOf = async (spkiBytes) => hex(await crypto.subtle.digest("SHA-256", spkiBytes)).slice(0, 16);

async function makeKey() {
  const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const spki = new Uint8Array(await crypto.subtle.exportKey("spki", kp.publicKey));
  return { kp, pin: { id: await idOf(spki), spki: Buffer.from(spki).toString("base64") } };
}
const TEST = await makeKey(), OTHER = await makeKey();
const PINS = [...PINNED, TEST.pin];
assert.ok(!PINNED.some((p) => p.id === TEST.pin.id || p.id === OTHER.pin.id), "throwaway keys are not production pins");
function int(b) {                     // minimal positive DER INTEGER
  let i = 0; while (i < b.length - 1 && b[i] === 0) i++;
  const v = b.subarray(i);
  return v[0] & 0x80 ? [0x02, v.length + 1, 0, ...v] : [0x02, v.length, ...v];
}
const toDer = (raw) => { const r = int(raw.subarray(0, 32)), s = int(raw.subarray(32)); return Uint8Array.from([0x30, r.length + s.length, ...r, ...s]); };
// The envelope sign.py would write (sorted keys, compact), signed by `by`.
async function signed(name, payloadObj, by = TEST, { key = by.pin.id, as = name } = {}) {
  const payload = typeof payloadObj === "string" ? payloadObj : JSON.stringify(payloadObj);
  const raw = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, by.kp.privateKey, new TextEncoder().encode(as + "\n" + payload)));
  return JSON.stringify({ alg: "ECDSA-P256-SHA256", format: 2, key, name, payload, sig: Buffer.from(toDer(raw)).toString("base64") });
}
// A POST body made the way the collector makes it: each envelope's exact text as the value.
const body = (pairs) => "{" + pairs.map(([n, t]) => JSON.stringify(n) + ":" + t).join(",") + "}";
const genOf = (t) => JSON.parse(JSON.parse(t).payload).generated_unix;
// A fresh network.json, newer than any before: what every collector batch carries.
let netGen = 0;
const net = async () => { netGen = Math.max(netGen + 1, NOW); return ["network.json", await signed("network.json", { generated_unix: netGen })]; };

// ---- fakes: Durable Object namespace + storage, rate limiters, clock ----------------------
const disk = new Map();
let reads = 0;
const storage = {
  async get(k) { reads++; return structuredClone(disk.get(k)); },
  async put(k, v) { if (typeof k === "object") { for (const [a, b] of Object.entries(k)) disk.set(a, structuredClone(b)); } else disk.set(k, structuredClone(v)); },
  async delete(k) { for (const x of [].concat(k)) disk.delete(x); },
};
let obj = new DataStore({ storage });
const restartObject = () => { obj = new DataStore({ storage }); };
let limited = false, limitKeys = [], doCalls = 0;
const env = {
  DATA: { idFromName: (n) => ({ n }), get: (id) => { assert.equal(id.n, "data"); return { fetch: (u, init) => { doCalls++; return obj.fetch(new Request(u, init)); } }; } },
  RL: { async limit({ key }) { limitKeys.push(key); return { success: !limited }; } },
};
let NOW = genOf(REAL["network.json"]) + 60;
Date.now = () => NOW * 1000;
const U = "https://monitor6529-data.example.workers.dev/v1/data";
const POST = (b, pins = PINS, headers = {}) => handle(new Request(U, { method: "POST", body: b, headers: { "cf-connecting-ip": "203.0.113.9", ...headers } }), env, pins);
const GET = (name, pins = PINS) => handle(new Request(U + "/" + name), env, pins);
const text = async (name) => { const r = await GET(name); assert.equal(r.status, 200, "GET " + name + ": " + r.status); return r.text(); };
const ok = [];
const check = async (label, res, status) => { assert.equal(res.status, status, label + ": got " + res.status); ok.push(label); };

// ---- 1. signature rules, directly ---------------------------------------------------------
for (const [n, t] of Object.entries(REAL)) assert.equal(await verifyEnvelope(t, n), JSON.parse(t).payload);
ok.push("production pins verify 6 real published envelopes");
const otherNet = await signed("network.json", { generated_unix: NOW }, OTHER);
await assert.rejects(verifyEnvelope(otherNet, "network.json", PINS), /unknown key/);
await assert.rejects(verifyEnvelope(await signed("network.json", { generated_unix: NOW }), "network.json"), /unknown key/);
ok.push("unpinned keys are refused by the verifier");
for (const n of ["network.json", "validators.json", "blocks.json", "chain.json", "awards.json", "history.json",
  "releases.json", "cycles/20775.json", "cycles/index.json", "days/index.json", "days/20774.json"]) assert.ok(allowed(n), n);
for (const n of ["live.json", "own.json", "releases.json.sig", "Releases.json", "releases.json/", "signed/releases.json", "signed/network.json", "../network.json", "/network.json", "network.json/",
  "days/1.json", "days/207740.json", "days/x.json", "cycles/2077.json", "days/20774.json.tmp", "Network.json",
  "days/index2.json", "days\\20774.json", "network.json\n", "", "cycles/20775.JSON"]) assert.ok(!allowed(n), JSON.stringify(n));
ok.push("name allow-list: the 11 published kinds yes (releases.json included), look-alikes no");

// ---- 2. the real files, through the production handler ----------------------------------
await check("GET before the first publish = 404", await GET("network.json"), 404);
const realBody = body(Object.entries(REAL));
await check("production handler stores a batch of 6 real envelopes (204)", await worker.fetch(new Request(U, { method: "POST", body: realBody }), env), 204);
let r = await GET("cycles/20775.json");
await check("GET 200", r, 200);
assert.match(r.headers.get("content-type"), /^application\/json/);
assert.equal(r.headers.get("cache-control"), "public, max-age=30");
assert.equal(r.headers.get("access-control-allow-origin"), null, "no CORS");
assert.equal(r.headers.get("set-cookie"), null);
for (const [n, t] of Object.entries(REAL)) assert.equal(await text(n), t, n);
resetIsolate();
for (const [n, t] of Object.entries(REAL)) assert.equal(await text(n), t, n + " (from the Durable Object)");
ok.push("GET returns each real file byte for byte (memo and Durable Object)");
await check("GET a name not stored = 404", await GET("days/20000.json"), 404);
await check("GET a name not on the allow-list = 404", await GET("own.json"), 404);
await check("GET signed/ prefix = 404", await GET("signed/network.json"), 404);
await check("GET with a query = 404", await handle(new Request(U + "/network.json?x=1"), env, PINS), 404);
assert.equal(await text("days%2Findex.json"), REAL["days/index.json"]); ok.push("GET of an encoded slash works");
await check("production handler refuses a test-key batch (403)", await worker.fetch(new Request(U, { method: "POST", body: body([["network.json", await signed("network.json", { generated_unix: NOW })]]) }), env), 403);
await check("the same real batch again = 204 (nothing changes)", await POST(realBody), 204);
assert.equal(limitKeys.at(-1), "203.0.113.9"); ok.push("POSTs go through the rate limiter by address");

// A body laid out differently (spaces, newlines): each value is still stored as its exact text.
const net2 = await signed("network.json", { generated_unix: NOW, n: "café ☃ \\\"}{" });
await check("spaced body accepted", await POST('\n {  "network.json" :\t' + net2 + '\n }\n'), 204);
assert.equal(await text("network.json"), net2); ok.push("GET is byte for byte for a spaced body, escapes and non-ASCII");
assert.deepEqual(topLevel('{"a":{"x":"}"},"b" : {"y":[1,{"z":"\\""}]} }'), [["a", '{"x":"}"}'], ["b", '{"y":[1,{"z":"\\""}]}']]);
ok.push("topLevel finds exact value boundaries");

// ---- 3. unpinned key, tampering, names ----------------------------------------------------
const before = await text("network.json");
NOW += 10;
await check("envelope signed by an unpinned test key = 403", await POST(body([["network.json", await signed("network.json", { generated_unix: NOW }, OTHER)]])), 403);
const fresh = await signed("network.json", { generated_unix: NOW, votes: 5 });
const edit = (t, f) => { const e = JSON.parse(t); f(e); return JSON.stringify(e); };
await check("tampered payload", await POST(body([["network.json", edit(fresh, (e) => { e.payload = e.payload.replace('"votes":5', '"votes":6'); })]])), 403);
await check("unpinned key id claiming the reserve", await POST(body([["network.json", edit(fresh, (e) => { e.key = "04f3b06b89f126a7"; })]])), 403);
await check("map key not equal to the envelope name", await POST(body([["chain.json", fresh]])), 403);
await check("renamed envelope (name field)", await POST(body([["chain.json", edit(fresh, (e) => { e.name = "chain.json"; })]])), 403);
await check("signed as another name", await POST(body([["chain.json", await signed("chain.json", { generated_unix: NOW }, TEST, { as: "network.json" })]])), 403);
await check("name not on the allow-list (validly signed)", await POST(body([["own.json", await signed("own.json", { generated_unix: NOW })]])), 403);
await check("traversal name (validly signed)", await POST(body([["../network.json", await signed("../network.json", { generated_unix: NOW })]])), 403);
await check("live.json is not accepted here", await POST(body([["live.json", await signed("live.json", { generated_unix: NOW })]])), 403);
assert.equal(await text("network.json"), before); ok.push("nothing refused was stored");

// ---- 4. malformed (400) -------------------------------------------------------------------
await check("not JSON", await POST("{nope"), 400);
await check("body is an array", await POST("[" + fresh + "]"), 400);
await check("body is an empty object", await POST("{}"), 400);
await check("empty body", await POST(""), 400);
await check("value is a string, not an envelope", await POST(body([["network.json", JSON.stringify(fresh)]])), 400);
await check("same name twice", await POST(body([["network.json", fresh], ["network.json", fresh]])), 400);
await check("extra envelope field", await POST(body([["network.json", edit(fresh, (e) => { e.note = "x"; })]])), 400);
await check("missing sig", await POST(body([["network.json", edit(fresh, (e) => { delete e.sig; })]])), 400);
await check("format 1", await POST(body([["network.json", edit(fresh, (e) => { e.format = 1; })]])), 400);
await check("sig not base64", await POST(body([["network.json", edit(fresh, (e) => { e.sig = e.sig.slice(0, -2) + "!!"; })]])), 400);
await check("non-minimal DER signature", await POST(body([["network.json", edit(fresh, (e) => { const d = Buffer.from(e.sig, "base64"); const n = d[3]; e.sig = Buffer.concat([Buffer.from([0x30, d[1] + 1, 0x02, n + 1, 0x00]), d.subarray(4)]).toString("base64"); })]])), 400);
await check("payload not JSON (validly signed)", await POST(body([["network.json", await signed("network.json", "not json")]])), 400);
await check("generated_unix as a string", await POST(body([["network.json", await signed("network.json", { generated_unix: String(NOW) })]])), 400);
await check("generated_unix 0", await POST(body([["network.json", await signed("network.json", { generated_unix: 0 })]])), 400);
await check("invalid UTF-8 body", await POST(new Uint8Array([0x7b, 0xff, 0x7d])), 400);
await check("generated_unix more than 5 min ahead = 422", await POST(body([["network.json", await signed("network.json", { generated_unix: NOW + 301 })]])), 422);
assert.equal(await text("network.json"), before); ok.push("nothing malformed was stored");

// ---- 5. all or nothing ---------------------------------------------------------------------
const chainNew = await signed("chain.json", { generated_unix: NOW });
const badSig = edit(await signed("blocks.json", { generated_unix: NOW }), (e) => { e.payload = e.payload.replace(String(NOW), String(NOW - 1)); });
await check("valid + bad signature in one batch = 403", await POST(body([["chain.json", chainNew], ["blocks.json", badSig]])), 403);
await check("valid + unknown name in one batch = 403", await POST(body([["chain.json", chainNew], ["own.json", await signed("own.json", {})]])), 403);
await check("valid + malformed in one batch = 400", await POST(body([["chain.json", chainNew], ["blocks.json", "{}"]])), 400);
const oldNet = await signed("network.json", { generated_unix: genOf(REAL["network.json"]) - 1 });
await check("valid + rollback in one batch = 409", await POST(body([["chain.json", chainNew], ["network.json", oldNet]])), 409);
assert.equal(await text("chain.json"), REAL["chain.json"]); resetIsolate(); assert.equal(await text("chain.json"), REAL["chain.json"]);
ok.push("the valid entry of a refused batch was not stored");
await check("the same valid entry alone = 204", await POST(body([["chain.json", chainNew]])), 204);
assert.equal(await text("chain.json"), chainNew);

// ---- 6. rollback (409) ---------------------------------------------------------------------
await check("older generated_unix = 409", await POST(body([["chain.json", await signed("chain.json", { generated_unix: NOW - 1 })]])), 409);
const chainEq = await signed("chain.json", { generated_unix: NOW, other: 1 });
await check("equal generated_unix, other bytes = 204", await POST(body([["chain.json", chainEq]])), 204);
await check("then the earlier one (same generated_unix) = 204", await POST(body([["chain.json", chainNew]])), 204);
await check("a file with generated_unix replaced by one without = 409", await POST(body([["chain.json", await signed("chain.json", { x: 1 })]])), 409);
await check("real network.json after a newer one = 409", await POST(body([["network.json", REAL["network.json"]]])), 409);
const idx2 = await signed("days/index.json", { days: [20774, 20775] });
await check("file without generated_unix, changed = 204", await POST(body([await net(), ["days/index.json", idx2]])), 204);
await check("file without generated_unix, the same bytes again = 204", await POST(body([["days/index.json", idx2]])), 204);
await check("list payload (cycles/index.json) changed = 204", await POST(body([await net(), ["cycles/index.json", await signed("cycles/index.json", [{ day: 20776 }])]])), 204);
restartObject(); resetIsolate(); doCalls = 0;
await check("after a restart the stored generated_unix still counts", await POST(body([["chain.json", await signed("chain.json", { generated_unix: NOW - 1 })]])), 409);
assert.equal(doCalls, 1);
assert.equal(await text("days/index.json"), idx2); ok.push("after a restart GET returns the stored envelope");

// ---- 6b. replay of an old signed file without generated_unix --------------------------------
const v1 = await signed("validators.json", { cycle_day: 1 }), v2 = await signed("validators.json", { cycle_day: 2 });
await check("validators v1 with a new network.json = 204", await POST(body([await net(), ["validators.json", v1]])), 204);
await check("validators v2 with a new network.json = 204", await POST(body([await net(), ["validators.json", v2]])), 204);
const curNet = await text("network.json");
await check("replay of the old validators.json alone = 409", await POST(body([["validators.json", v1]])), 409);
await check("... with an old network.json = 409", await POST(body([["network.json", REAL["network.json"]], ["validators.json", v1]])), 409);
await check("... with the current network.json, same bytes = 409", await POST(body([["network.json", curNet], ["validators.json", v1]])), 409);
const chainSame = await signed("chain.json", { generated_unix: genOf(await text("chain.json")), replay: 1 });
await check("... with a changed file whose generated_unix is not newer than the stored sequence = 409",
  await POST(body([["chain.json", chainSame], ["validators.json", v1]])), 409);
await check("a day file alone (never stored) = 409", await POST(body([["days/20500.json", await signed("days/20500.json", {})]])), 409);
assert.equal(await text("validators.json"), v2); ok.push("the replayed validators.json was not stored");
const v3 = await signed("validators.json", { cycle_day: 3 });
await check("a normal collector batch (new network.json + changed files + same bytes) = 204",
  await POST(body([await net(), ["chain.json", await text("chain.json")], ["validators.json", v3], ["cycles/index.json", await signed("cycles/index.json", [{ day: 1 }])]])), 204);
assert.equal(await text("validators.json"), v3);
await check("then v2 again with the now-current network.json (same bytes) = 409", await POST(body([["network.json", await text("network.json")], ["validators.json", v2]])), 409);

// ---- 6c. GETs of names never stored: no storage read, nothing remembered ---------------------
restartObject(); resetIsolate();
await GET("chain.json");                                          // loads the names list
reads = 0; const size0 = obj.cache.size;
for (let d = 20000; d < 20100; d++) await check("GET never-stored days/" + d, await GET("days/" + d + ".json"), 404);
assert.equal(reads, 0, "no storage read for a name not in the list");
assert.equal(obj.cache.size, size0, "the cache did not grow");
ok.splice(ok.length - 100, 100, "100 GETs of never-stored names: 404, no storage read, cache unchanged");

// ---- 7. memo: GETs within 5 s cost one Durable Object request per name ---------------------
resetIsolate(); doCalls = 0;
await GET("chain.json"); await GET("chain.json"); await GET("days/index.json"); assert.equal(doCalls, 2);
NOW += 5; await GET("chain.json"); assert.equal(doCalls, 3);
ok.push("GETs within 5 s share one Durable Object read per name");
const chainNext = await signed("chain.json", { generated_unix: NOW });
await check("newer accepted", await POST(body([["chain.json", chainNext]])), 204);
doCalls = 0; assert.equal(await text("chain.json"), chainNext); assert.equal(doCalls, 0);
ok.push("a stored POST is served at once by the same isolate");

// ---- 8. eviction: newest 60 days/ and 40 cycles/, index files kept -------------------------
const dayBatch = [];
for (let d = 20700; d < 20762; d++) dayBatch.push(["days/" + d + ".json", await signed("days/" + d + ".json", { day: d })]);
await GET("days/20700.json");                                     // nothing stored yet: 404, not memoized
await check("62 day files in one batch = 204", await POST(body([await net(), ...dayBatch])), 204);
// days/20774.json (real) is stored too: 63 day files, keep the newest 60 -> 20700..20702 dropped
for (const d of [20700, 20701, 20702]) await check("evicted days/" + d, await GET("days/" + d + ".json"), 404);
for (const d of [20703, 20761]) assert.equal(await text("days/" + d + ".json"), dayBatch[d - 20700][1]);
assert.equal(await text("days/20774.json"), REAL["days/20774.json"]);
assert.equal(await text("days/index.json"), idx2);
let stored = JSON.parse(JSON.stringify(disk.get("names")));
assert.equal(stored.filter((n) => /^days\/\d{5}/.test(n)).length, 60);
assert.equal([...disk.keys()].filter((k) => /^e:days\/\d{5}/.test(k)).length, 60);
ok.push("days/: 60 kept (newest), index.json kept, older ones gone from storage");
const old = await signed("days/20600.json", { day: 20600 });
await check("an older day than the 60 kept = 204 (stored, then dropped)", await POST(body([await net(), ["days/20600.json", old]])), 204);
await check("it is not served", await GET("days/20600.json"), 404);
const cycBatch = [];
for (let d = 20730; d < 20772; d++) cycBatch.push(["cycles/" + d + ".json", await signed("cycles/" + d + ".json", { day: d })]);
await check("42 cycle files in one batch = 204", await POST(body([await net(), ...cycBatch])), 204);
// plus cycles/20775.json (real): 43, keep 40 -> 20730..20732 dropped
for (const d of [20730, 20731, 20732]) await check("evicted cycles/" + d, await GET("cycles/" + d + ".json"), 404);
assert.equal(await text("cycles/20733.json"), cycBatch[3][1]);
assert.equal(await text("cycles/20775.json"), REAL["cycles/20775.json"]);
assert.equal([...disk.keys()].filter((k) => /^e:cycles\/\d{5}/.test(k)).length, 40);
ok.push("cycles/: 40 kept (newest), index.json kept");
// memo of an evicted name is dropped at once by the isolate that stored the batch
assert.equal(await text("days/20703.json"), dayBatch[3][1]);
const three = await Promise.all([20780, 20781, 20782].map(async (d) => ["days/" + d + ".json", await signed("days/" + d + ".json", { day: d })]));
await check("3 more days = 204", await POST(body([await net(), ...three])), 204);
await check("a day evicted by it is 404 at once (memo dropped)", await GET("days/20703.json"), 404);
restartObject(); resetIsolate();
await check("and after a restart", await GET("days/20703.json"), 404);
assert.equal(await text("days/20782.json"), three[2][1]);
ok.push("eviction survives a restart");

// ---- 9. size caps (413) --------------------------------------------------------------------
const many = [];
for (let d = 20800; d < 20901; d++) many.push(["days/" + d + ".json", await signed("days/" + d + ".json", { day: d })]);
await check("101 entries = 413", await POST(body(many)), 413);
await check("100 entries = 204", await POST(body([await net(), ...many.slice(0, 99)])), 204);
const bigEntry = await signed("validators.json", { pad: "x".repeat(2 * 1000 * 1000) });
await check("one entry over 2 MB = 413", await POST(body([["validators.json", bigEntry]])), 413);
await check("validators.json just under 2 MB = 204", await POST(body([await net(), ["validators.json", await signed("validators.json", { pad: "x".repeat(1990000) })]])), 204);
const huge = body([["awards.json", await signed("awards.json", { generated_unix: NOW, pad: "x".repeat(6 * 1024 * 1024) })]]);
await check("body over 6 MB (declared length) = 413", await POST(huge), 413);
const stream = (t) => new ReadableStream({ start(c) { const b = new TextEncoder().encode(t); for (let i = 0; i < b.length; i += 65536) c.enqueue(b.subarray(i, i + 65536)); c.close(); } });
await check("body over 6 MB (streamed, no length) = 413", await handle(new Request(U, { method: "POST", body: stream(huge), duplex: "half" }), env, PINS), 413);
const fourBig = [];
for (const n of ["awards.json", "history.json", "blocks.json"]) fourBig.push([n, await signed(n, { generated_unix: NOW, pad: "y".repeat(1900000) })]);
await check("three 1.9 MB entries (5.7 MB body) = 204", await POST(body(fourBig)), 204);
assert.equal(await text("history.json"), fourBig[1][1]);

// ---- 10. methods, paths, rate limits, storage failure --------------------------------------
await check("PUT /v1/data = 405", await handle(new Request(U, { method: "PUT", body: "x" }), env, PINS), 405);
await check("GET /v1/data = 405", await handle(new Request(U), env, PINS), 405);
await check("POST /v1/data/network.json = 405", await handle(new Request(U + "/network.json", { method: "POST", body: "{}" }), env, PINS), 405);
await check("other path = 404", await handle(new Request("https://x/v1/other"), env, PINS), 404);
await check("trailing slash = 404", await handle(new Request(U + "/"), env, PINS), 404);
await check("/v1/live is not served here = 404", await handle(new Request("https://x/v1/live"), env, PINS), 404);
limited = true;
await check("rate limited POST = 429", await POST(body([["chain.json", await signed("chain.json", { generated_unix: NOW })]])), 429);
await check("GET is not limited by RL", await GET("chain.json"), 200);
limited = false;
env.RL_GET = { async limit() { return { success: false }; } };
await check("RL_GET limits GET = 429", await GET("chain.json"), 429);
delete env.RL_GET;
const savedPut = storage.put;
storage.put = async () => { throw new Error("disk"); };
NOW += 1;
const failing = await signed("chain.json", { generated_unix: NOW });
await check("storage failure = 503", await POST(body([["chain.json", failing]])), 503);
storage.put = savedPut;
assert.equal(await text("chain.json"), chainNext); ok.push("a failed write changes nothing");
await check("then the same POST = 204", await POST(body([["chain.json", failing]])), 204);

// ---- releases.json (our republished copy of 6529's verified release feed) -----------------
NOW += 10;
const rel1 = await signed("releases.json", { generated_unix: NOW, verified_unix: NOW, source: "https://6529.io/validator/releases.json", feed: { format: 1, sequence: 2 } });
await check("releases.json with a new network.json = 204", await POST(body([await net(), ["releases.json", rel1]])), 204);
assert.equal(await text("releases.json"), rel1); ok.push("GET releases.json is byte for byte");
await check("an older releases.json = 409", await POST(body([await net(), ["releases.json", await signed("releases.json", { generated_unix: NOW - 100, feed: { format: 1, sequence: 1 } })]])), 409);
await check("releases.json signed as network.json = 403", await POST(body([["releases.json", await signed("releases.json", { generated_unix: NOW + 1 }, TEST, { as: "network.json" })]])), 403);
assert.equal(await text("releases.json"), rel1); ok.push("refused releases.json changed nothing");

console.log(ok.map((x) => "ok  " + x).join("\n"));
console.log(`\n${ok.length} checks passed`);
