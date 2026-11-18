// 6529 Monitor data relay (Cloudflare Worker "monitor6529-data", no dependencies).
// Keeps the latest signed envelope of each published data file from the collector and hands
// it, unchanged, to the apps. All logic is in data.js; this file only wires it up, and always
// with the production keys (PINNED): there is no setting that adds a key.
import { handle, DataStore } from "./data.js";

export { DataStore };

export default {
  async fetch(req, env) {
    return handle(req, env);
  },
};
