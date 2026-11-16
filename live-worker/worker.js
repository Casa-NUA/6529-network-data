// 6529 Monitor live chain relay (Cloudflare Worker "monitor6529-live", no dependencies).
// Keeps the latest signed live.json envelope from the collector and hands it, unchanged, to the
// apps while their Chain page is open. All logic is in live.js; this file only wires it up, and
// always with the production keys (PINNED): there is no setting that adds a key.
import { handle, LiveStore } from "./live.js";

export { LiveStore };

export default {
  async fetch(req, env) {
    return handle(req, env);
  },
};
