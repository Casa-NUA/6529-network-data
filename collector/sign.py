#!/usr/bin/env python3
"""Sign the published data files, so readers can check who published them.

For every data file `<data>/<name>` (network.json, chain.json, validators.json,
cycles/index.json, cycles/<day>.json) this writes `<data>/signed/<name>`:

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

OPENSSL = "/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else "openssl"
ALG = "ECDSA-P256-SHA256"
TOP = re.compile(r"^(network|chain|validators)\.json$")
CYCLE = re.compile(r"^(index|[0-9]{1,6})\.json$")
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
    cyc = os.path.join(root, "cycles")
    if os.path.lexists(cyc):
        real_dir(cyc, "cycles")
        names += ["cycles/" + n for n in sorted(os.listdir(cyc)) if CYCLE.match(n)]
    return names


def envelope_text(key_id, name, payload_text, sig):
    return json.dumps({"format": 2, "alg": ALG, "key": key_id, "name": name,
                       "payload": payload_text,
                       "sig": base64.b64encode(sig).decode("ascii")},
                      separators=(",", ":"), sort_keys=True)


def write_text(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="the collector's own output directory (outside any clone)")
    ap.add_argument("--key", required=True, help="EC P-256 private key (PEM), kept outside the repository")
    a = ap.parse_args()
    root = os.path.abspath(a.data)
    real_dir(root, "data directory")
    out = os.path.join(root, "signed")
    for d in (out, os.path.join(out, "cycles")):
        if os.path.lexists(d):
            real_dir(d, "signed directory")
        else:
            os.mkdir(d)

    der = run(["ec", "-in", a.key, "-pubout", "-outform", "DER"])
    key_id = hashlib.sha256(der).hexdigest()[:16]
    fd, pub_pem = tempfile.mkstemp(suffix=".pem")
    os.close(fd)
    try:
        with open(pub_pem, "wb") as f:
            f.write(run(["ec", "-in", a.key, "-pubout"]))
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
            sig = run(["dgst", "-sha256", "-sign", a.key], message)
            if not verify(pub_pem, message, sig):
                raise Refuse("signature of %s does not verify" % name)
            write_text(dst, envelope_text(key_id, name, payload, sig))
            signed += 1
        # An envelope whose data file is gone must go too. Only inside signed/ and
        # signed/cycles/, both checked above to be plain directories.
        for sub in ("", "cycles"):
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
