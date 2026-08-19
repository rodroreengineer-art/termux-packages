#!/usr/bin/env python3
"""Relocate Termux prebuilt packages to the com.rodroid.codestudio prefix.

Why: rebuilding rust/llvm/openjdk from source for 4 ABIs exceeds GitHub's
6h-per-job limit. Termux already publishes these prebuilt for
`com.termux`. This script downloads them, rewrites the baked-in prefix
(`/data/data/com.termux/files/usr` -> `/data/data/com.rodroid.codestudio/files/usr`)
in ELF rpaths, shebangs, and text config files, then repacks the .deb.

Usage:
    python3 relocate.py --arch aarch64 --packages packages.txt --out output
"""

import argparse
import gzip
import os
import re
import shutil
import subprocess
import sys
import urllib.request

BASE = "https://packages.termux.dev/apt/termux-main"
OLD = "/data/data/com.termux/files/usr"
NEW = "/data/data/com.rodroid.codestudio/files/usr"

# Arch -> repo component. `arm` is 32-bit ARM in termux-packages land.
ARCH_COMPONENT = {
    "aarch64": "binary-aarch64",
    "arm": "binary-arm",
    "i686": "binary-i686",
    "x86_64": "binary-x86_64",
}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch(url, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "codestudio-relocate"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_packages(data):
    """Parse a Debian Packages index into {name: {field: value}}."""
    entries = {}
    cur = {}
    last_field = None
    for raw in data.decode("utf-8", "replace").splitlines():
        if raw == "":
            if cur:
                entries[cur["Package"]] = cur
                cur = {}
            last_field = None
            continue
        if raw[0] in " \t":
            # continuation line
            if last_field:
                cur[last_field] = cur.get(last_field, "") + "\n" + raw.strip()
            continue
        if ":" in raw:
            k, v = raw.split(":", 1)
            cur[k] = v.strip()
            last_field = k
    if cur:
        entries[cur["Package"]] = cur
    return entries


def load_index(arch):
    """Load the Packages index for the given arch.

    Termux lists `Architecture: all` packages inside each arch index, so a
    single index is enough (there is no separate binary-all index upstream).
    """
    component = ARCH_COMPONENT[arch]
    url = f"{BASE}/dists/stable/main/{component}/Packages"
    log(f"  downloading index {component}")
    return parse_packages(fetch(url))


def parse_deps(entry):
    """Return the list of dependency package names from Depends/Pre-Depends."""
    raw = entry.get("Depends", "")
    if entry.get("Pre-Depends"):
        raw = raw + ", " + entry["Pre-Depends"]
    deps = []
    for alt in raw.split(","):
        alt = alt.strip()
        if not alt:
            continue
        # strip arch qualifiers e.g. "libfoo [arm]"
        alt = re.sub(r"\s*\[[^\]]*\]", "", alt)
        # handle "a | b" alternatives: keep all names, resolve first found
        names = [re.split(r"\s*[<(]", p.strip(), 1)[0] for p in alt.split("|")]
        deps.append(names)
    return deps


def resolve(targets, index):
    """Resolve transitive deps. Returns ordered dict name -> entry."""
    resolved = {}
    todo = list(targets)
    while todo:
        name = todo.pop(0)
        if name in resolved:
            continue
        if name not in index:
            log(f"  ! dependency not in repo, skipping: {name}")
            continue
        resolved[name] = index[name]
        for alternatives in parse_deps(index[name]):
            picked = next((a for a in alternatives if a in index), None)
            if picked is None:
                log(f"  ! no alternative available for {alternatives}, skipping")
                continue
            if picked not in resolved:
                todo.append(picked)
    return resolved


def is_elf(path):
    try:
        out = subprocess.run(["file", "-b", path], capture_output=True, text=True).stdout
        return "ELF" in out
    except Exception:
        return False


def is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except Exception:
        return True


def rewrite_rpath(path):
    """Rewrite com.termux rpaths using patchelf. Best effort."""
    try:
        rpath = subprocess.run(
            ["patchelf", "--print-rpath", path], capture_output=True, text=True
        ).stdout.strip()
    except Exception:
        return False
    if OLD in rpath:
        new = rpath.replace(OLD, NEW)
        try:
            subprocess.run(
                ["patchelf", "--set-rpath", new, path],
                check=True, capture_output=True,
            )
            return True
        except Exception as e:
            log(f"  ! patchelf failed on {path}: {e}")
    return False


def fix_shebang(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except Exception:
        return False
    if not head.startswith(b"#!"):
        return False
    if OLD.encode() not in head.split(b"\n", 1)[0]:
        return False
    with open(path, "rb") as f:
        data = f.read()
    new = data.replace(OLD.encode(), NEW.encode())
    with open(path, "wb") as f:
        f.write(new)
    return True


def relocate_extracted(root):
    n_elf = n_shebang = n_text = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if os.path.islink(p):
                continue
            if is_elf(p):
                if rewrite_rpath(p):
                    n_elf += 1
            elif fix_shebang(p):
                n_shebang += 1
            elif not is_binary(p):
                # text config: pkgconfig/*.pc, *.la, cmake configs, etc.
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                    if OLD.encode() in data:
                        with open(p, "wb") as f:
                            f.write(data.replace(OLD.encode(), NEW.encode()))
                        n_text += 1
                except Exception:
                    pass
    return n_elf, n_shebang, n_text


def repack(deb_path, out_dir):
    name = os.path.basename(deb_path).replace("__dl_", "", 1)
    work = deb_path + ".x"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    subprocess.run(["dpkg-deb", "-x", deb_path, work], check=True)
    # also extract control metadata (DEBIAN/control etc.) so we can rebuild
    subprocess.run(["dpkg-deb", "-e", deb_path, os.path.join(work, "DEBIAN")], check=True)
    # dpkg-deb -b rejects maintainer scripts with 0700 perms (extraction does
    # not normalize them), so enforce valid control-file permissions.
    debian = os.path.join(work, "DEBIAN")
    for fn in os.listdir(debian):
        p = os.path.join(debian, fn)
        if fn == "control":
            os.chmod(p, 0o644)
        elif fn in ("postinst", "postrm", "preinst", "prerm", "config"):
            os.chmod(p, 0o755)
        else:
            os.chmod(p, 0o644)
    data_dir = os.path.join(work, "data", "data", "com.termux", "files", "usr")
    root = data_dir if os.path.isdir(data_dir) else work
    n_elf, n_shebang, n_text = relocate_extracted(root)
    out = os.path.join(out_dir, name)
    # termux-apt-repo only understands control.tar.xz/gz and data.tar.xz, so
    # force xz (dpkg-deb otherwise defaults to zstd on modern hosts).
    subprocess.run(["dpkg-deb", "-Zxz", "-b", work, out], check=True)
    shutil.rmtree(work, ignore_errors=True)
    log(f"  {name}: elf={n_elf} shebang={n_shebang} text={n_text}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", required=True)
    ap.add_argument("--packages", required=True, help="path to packages.txt or comma list")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.arch not in ARCH_COMPONENT:
        log(f"unknown arch {args.arch}")
        sys.exit(2)

    if os.path.isfile(args.packages):
        targets = [
            l.strip()
            for l in open(args.packages)
            if l.strip() and not l.strip().startswith("#")
        ]
    else:
        targets = [p.strip() for p in args.packages.split(",") if p.strip()]

    log(f"arch={args.arch} targets={len(targets)}")

    index = load_index(args.arch)
    log(f"index entries: {len(index)}")

    resolved = resolve(targets, index)
    log(f"resolved {len(resolved)} packages (incl. deps)")

    os.makedirs(args.out, exist_ok=True)
    for name, entry in resolved.items():
        filename = entry["Filename"]
        url = f"{BASE}/{filename}"
        dest = os.path.join(args.out, "__dl_" + os.path.basename(filename))
        log(f"downloading {name} ({entry.get('Size','?')} bytes)")
        with open(dest, "wb") as f:
            f.write(fetch(url))
        repack(dest, args.out)
        os.remove(dest)

    log("done")


if __name__ == "__main__":
    main()
