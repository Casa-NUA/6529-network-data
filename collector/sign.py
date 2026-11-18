#!/usr/bin/env python3
"""Sign the published data files, so readers can check who published them.

For every data file `<data>/<name>` (network.json, chain.json, validators.json, blocks.json,
history.json, awards.json, cycles/index.json, cycles/<day>.json, days/index.json,
days/<day>.json) this writes `<data>/signed/<name>`:

    {"format": 2, "alg": "ECDSA-P256-SHA256", "key": "<key id>", "name": "<name>",
     "payload": "<the data file's exact text>",
     "sig": "<base64 of the DER ECDSA signature>"}

The signature is over the bytes of:  name + "\n" + payload   (UTF-8).
The name is signed too, so a signed file cannot be passed off as another one. One file
carries both the data and its signature, so a reader never pairs data with a signature
from a different moment. The key id is the first 16 hex digits of the SHA-256 of the
public key (DER, SubjectPublicKeyInfo). The public key is in
`collector/signing-key.pub.pem`.

What a valid signature means: the holder of the key published this file under this
name. It does not prove the data is true (it is one node's view), and it does not
prove the file is recent: check `generated_unix` in network.json and the `day` inside
each cycle file for that.

RUN THIS ONLY ON A DIRECTORY THE COLLECTOR ITSELF WROTE, outside any git clone. It
signs what it is given: pointed at a directory someone else can write to, it would
sign their files. It refuses symbolic links and any file name it does not expect.

A file whose text did not change keeps its envelope, so unchanged data makes no new
commit. Needs the `openssl` command; Python standard library otherwise.

    python3 -I sign.py --data /path/outside/any/clone/data --key /path/to/signing-key.pem

With --batch BODY --posted FILE instead of --key, it signs nothing: it writes to BODY the
POST body for the data Worker (data-worker/, spec 2026-11-18): a JSON object
{"<name>": <envelope>, ...} holding, as their exact text, the signed files that differ from
what FILE (the collector's own record, outside state/data) says was last accepted, and writes
FILE + ".next", the record to keep once the Worker answers 204. Nothing to send: neither is
written. See worker_batch().
"""
import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

OPENSSL = "/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else "openssl"
ALG = "ECDSA-P256-SHA256"
TOP = re.compile(r"^(network|chain|validators|blocks|history|awards)\.json$")
CYCLE = re.compile(r"^(index|[0-9]{1,6})\.json$")
SUBDIRS = ("cycles", "days")          # each holds index.json and <day>.json only
MAX_BYTES = 8 * 1024 * 1024


class Refuse(RuntimeError):
    pass


def run(args, data=None):
    p = subprocess.run([OPENSSL] + args, input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise Refuse("openssl %s failed: %s" % (args[0], p.stderr.decode("utf-8", "replace")[:200]))
    return p.stdout


def verify(pub_pem_path, message, sig):
    fd, tmp = tempfile.mkstemp(suffix=".sig")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(sig)
        p = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", pub_pem_path,
                            "-signature", tmp], input=message,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode == 0
    finally:
        os.unlink(tmp)


def real_dir(path, what):
    """`path` must be a real directory, not a symbolic link."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refuse("%s is not a plain directory: %s" % (what, path))


def read_regular(path):
    """The bytes of a regular file, never following a symbolic link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise Refuse("not a regular file of reasonable size: %s" % path)
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read()
    finally:
        os.close(fd)


def data_files(root):
    names = [n for n in sorted(os.listdir(root)) if TOP.match(n)]
    for sub in SUBDIRS:
        d = os.path.join(root, sub)
        if os.path.lexists(d):
            real_dir(d, sub)
            names += [sub + "/" + n for n in sorted(os.listdir(d)) if CYCLE.match(n)]
    return names


def envelope_text(key_id, name, payload_text, sig):
    return json.dumps({"format": 2, "alg": ALG, "key": key_id, "name": name,
                       "payload": payload_text,
                       "sig": base64.b64encode(sig).decode("ascii")},
                      separators=(",", ":"), sort_keys=True)


def public_key(key):
    """(key id, public key PEM bytes) of the private key file `key`, via openssl."""
    der = run(["ec", "-in", key, "-pubout", "-outform", "DER"])
    return hashlib.sha256(der).hexdigest()[:16], run(["ec", "-in", key, "-pubout"])


def sign_envelope(key, key_id, pub_pem_path, name, raw):
    """The envelope text for data bytes `raw` published as `name`, signed with `key` and
    checked against its own public key before it is returned. Used by main() and live.py."""
    message = name.encode("utf-8") + b"\n" + raw
    sig = run(["dgst", "-sha256", "-sign", key], message)
    if not verify(pub_pem_path, message, sig):
        raise Refuse("signature of %s does not verify" % name)
    return envelope_text(key_id, name, raw.decode("utf-8"), sig)


def write_text(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


# The names the data Worker accepts (data-worker/data.js), and how many day files it keeps.
WORKER_NAMES = (re.compile(r"^(network|validators|blocks|chain|awards|history)\.json$"),
                re.compile(r"^cycles/(index|[0-9]{5})\.json$"),
                re.compile(r"^days/(index|[0-9]{5})\.json$"))
WORKER_KEEP = {"days": 60, "cycles": 40}
WORKER_MAX_BODY = 5 * 1000 * 1000        # the Worker refuses over 6 MB
WORKER_MAX_ENTRY = 1900 * 1000           # ... one entry over 2 MB
WORKER_MAX_ENTRIES = 90                  # ... more than 100 entries
WORKER_FULL_S = 3600                     # send every file again at least this often


def _worker_order(name):
    m = re.match(r"^(cycles|days)/([0-9]{5})\.json$", name)
    if m:
        return (2, -int(m.group(2)), name)       # day files, newest first
    return (1 if "/" in name else 0, 0, name)    # top-level files, then the index files


def worker_batch(signed_root, posted, now):
    """(body text or None, the next `posted` record, number of files in the body) for one POST
    to the data Worker.

    `posted` is {"files": {name: sha256 of the envelope last accepted}, "full_unix": time of
    the last POST that held every file}. A file goes in when its envelope differs from that
    record, and every file goes in once an hour (so a Worker that lost its data is refilled;
    the Worker takes the same bytes again as a no-op). Only names the Worker accepts, and only
    the newest days/cycles it keeps. The Worker stores a batch all or nothing, so what would
    make it refuse one (too big, too many) is left for the next run instead: top-level files
    first, then the index files, then the day files newest first."""
    files = posted.get("files") if isinstance(posted, dict) else None
    files = files if isinstance(files, dict) else {}
    full_unix = posted.get("full_unix") if isinstance(posted, dict) else None
    full_unix = full_unix if isinstance(full_unix, int) else 0
    full = not (0 <= now - full_unix < WORKER_FULL_S)
    names = [n for n in data_files(signed_root) if any(r.match(n) for r in WORKER_NAMES)]
    keep = set()
    for kind, limit in WORKER_KEEP.items():
        days = sorted((n for n in names if n.startswith(kind + "/") and n != kind + "/index.json"),
                      reverse=True)
        keep.update(days[:limit])
    names = [n for n in names if "/" not in n or n.endswith("/index.json") or n in keep]
    names.sort(key=_worker_order)
    parts, size, nxt = [], 2, {}
    for name in names:
        raw = read_regular(os.path.join(signed_root, name))
        digest = hashlib.sha256(raw).hexdigest()
        if not full and files.get(name) == digest:
            nxt[name] = digest
            continue
        try:
            text = raw.decode("utf-8")
            env = json.loads(text)
            if not isinstance(env, dict) or env.get("name") != name:
                raise ValueError
        except ValueError:
            continue
        item = json.dumps(name) + ":" + text
        if len(raw) > WORKER_MAX_ENTRY or len(parts) >= WORKER_MAX_ENTRIES \
                or size + len(item.encode("utf-8")) + 1 > WORKER_MAX_BODY:
            if name in files:
                nxt[name] = files[name]
            continue
        parts.append(item)
        size += len(item.encode("utf-8")) + 1
        nxt[name] = digest
    if not parts:
        return None, posted, 0
    # A file left out (too big, unreadable) is not in the record, or keeps its old digest, so
    # the next run tries it again.
    return "{" + ",".join(parts) + "}", {"files": nxt, "full_unix": now if full else full_unix}, len(parts)


def batch_main(a):
    root = os.path.abspath(a.data)
    real_dir(root, "data directory")
    signed = os.path.join(root, "signed")
    real_dir(signed, "signed directory")
    try:
        with open(a.posted, encoding="utf-8") as f:
            posted = json.load(f)
    except (OSError, ValueError):
        posted = {}
    body, nxt, count = worker_batch(signed, posted, int(time.time()))
    if body is None:
        print("0")
        return 0
    write_text(a.batch, body)
    write_text(a.posted + ".next", json.dumps(nxt, sort_keys=True))
    print(count)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="the collector's own output directory (outside any clone)")
    ap.add_argument("--key", help="EC P-256 private key (PEM), kept outside the repository")
    ap.add_argument("--batch", help="write the data Worker's POST body here instead of signing")
    ap.add_argument("--posted", help="with --batch: the record of what the Worker last accepted")
    a = ap.parse_args()
    if a.batch:
        if not a.posted:
            ap.error("--batch needs --posted")
        return batch_main(a)
    if not a.key:
        ap.error("--key is required")
    root = os.path.abspath(a.data)
    real_dir(root, "data directory")
    out = os.path.join(root, "signed")
    for d in [out] + [os.path.join(out, sub) for sub in SUBDIRS]:
        if os.path.lexists(d):
            real_dir(d, "signed directory")
        else:
            os.mkdir(d)

    key_id, pub = public_key(a.key)
    fd, pub_pem = tempfile.mkstemp(suffix=".pem")
    os.close(fd)
    try:
        with open(pub_pem, "wb") as f:
            f.write(pub)
        wanted, signed, kept = set(), 0, 0
        for name in data_files(root):
            raw = read_regular(os.path.join(root, name))
            try:
                payload = raw.decode("utf-8")
                json.loads(payload)
            except ValueError:
                raise Refuse("%s is not JSON text" % name)
            message = name.encode("utf-8") + b"\n" + raw
            dst = os.path.join(out, name)
            wanted.add(name)
            # Keep the envelope only if it is byte for byte what we would write with
            # its own signature, and that signature verifies.
            try:
                old = read_regular(dst).decode("utf-8")
                sig = base64.b64decode(json.loads(old)["sig"], validate=True)
                if old == envelope_text(key_id, name, payload, sig) and verify(pub_pem, message, sig):
                    kept += 1
                    continue
            except (OSError, ValueError, KeyError, TypeError, Refuse):
                pass
            write_text(dst, sign_envelope(a.key, key_id, pub_pem, name, raw))
            signed += 1
        # An envelope whose data file is gone must go too. Only inside signed/,
        # signed/cycles/ and signed/days/, all checked above to be plain directories.
        for sub in ("",) + SUBDIRS:
            d = os.path.join(out, sub)
            for n in os.listdir(d):
                p = os.path.join(d, n)
                rel = (sub + "/" + n) if sub else n
                if n.endswith(".json") and rel not in wanted \
                        and stat.S_ISREG(os.lstat(p).st_mode):
                    os.unlink(p)
    finally:
        os.unlink(pub_pem)
    print("signed %d, unchanged %d, key %s" % (signed, kept, key_id))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Refuse, OSError) as e:
        print("sign: %s" % e, file=sys.stderr)
        sys.exit(1)
