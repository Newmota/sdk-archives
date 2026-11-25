#!/usr/bin/env python3
"""Build a stripped, deterministic SDK release archive (.tar.xz) from one pinned commit.

Standard library only (Python 3.11+). Usage:

    make_archive.py --profile profiles/esp-idf.toml --commit <40-hex> --version v5.5.5 --out dist
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import os
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
MANIFEST = "NEWMOTA_SDK.json"


def git(*arguments: str, cwd: Path, capture: bool = True) -> str:
    """Run git; uncaptured progress goes to stderr so stdout stays the JSON result."""
    if capture:
        return subprocess.run(
            ["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True
        ).stdout
    subprocess.run(["git", *arguments], cwd=cwd, check=True, stdout=sys.stderr)
    return ""


def load_profile(path: Path) -> dict:
    profile = tomllib.loads(path.read_text(encoding="utf-8"))
    allowed = {
        "name",
        "repository",
        "profile_revision",
        "submodules",
        "describe_match",
        "describe_abbrev",
        "version_file",
        "exclude_top_level_dotfiles",
        "exclude",
        "require",
    }
    unknown = set(profile) - allowed
    if unknown:
        raise SystemExit(f"{path}: unknown fields: {', '.join(sorted(unknown))}")
    for field in ("name", "repository", "profile_revision"):
        if field not in profile:
            raise SystemExit(f"{path}: missing '{field}'")
    if not str(profile["repository"]).startswith("https://"):
        raise SystemExit(f"{path}: repository must be an https URL")
    for entry in profile.get("exclude", []) + profile.get("require", []):
        pure = PurePosixPath(entry)
        if pure.is_absolute() or ".." in pure.parts or entry.endswith("/"):
            raise SystemExit(f"{path}: invalid SDK-relative path '{entry}'")
    return profile


def fetch(profile: dict, commit: str, work: Path) -> Path:
    source = work / "source"
    if (source / ".git").exists():
        git("fetch", "--filter=tree:0", "--tags", "origin", cwd=source, capture=False)
    else:
        # Commits and tags only; trees and blobs arrive on demand for the pinned commit.
        git(
            "clone",
            "--filter=tree:0",
            "--no-checkout",
            str(profile["repository"]),
            str(source),
            cwd=work,
            capture=False,
        )
    git(
        "-c", "advice.detachedHead=false", "checkout", "--detach", commit, cwd=source, capture=False
    )
    if profile.get("submodules", False):
        try:
            git(
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--depth",
                "1",
                cwd=source,
                capture=False,
            )
        except subprocess.CalledProcessError:
            git("submodule", "update", "--init", "--recursive", cwd=source, capture=False)
    if git("rev-parse", "HEAD", cwd=source).strip() != commit:
        raise SystemExit(f"checkout is not at {commit}")
    if git("status", "--porcelain", "--ignore-submodules=none", cwd=source).strip():
        raise SystemExit("checkout is not clean")
    return source


def selected_files(profile: dict, source: Path) -> list[str]:
    listed = git(
        "ls-files", "-z", "--recurse-submodules" if profile.get("submodules") else "--", cwd=source
    )
    files = sorted(name for name in listed.split("\0") if name)
    excluded = [PurePosixPath(entry) for entry in profile.get("exclude", [])]
    result = []
    for name in files:
        path = PurePosixPath(name)
        if profile.get("exclude_top_level_dotfiles", False) and path.parts[0].startswith("."):
            continue
        if any(path == entry or entry in path.parents for entry in excluded):
            continue
        result.append(name)
    return result


def check_required(profile: dict, files: list[str], generated: set[str]) -> None:
    present = set(files) | generated
    prefixes = {str(parent) for name in present for parent in PurePosixPath(name).parents}
    missing = [
        entry
        for entry in profile.get("require", [])
        if entry not in present and entry not in prefixes
    ]
    if missing:
        raise SystemExit(f"required paths missing after stripping: {', '.join(missing)}")


def describe(profile: dict, source: Path) -> str | None:
    match = profile.get("describe_match")
    if not match:
        return None
    abbrev = int(profile.get("describe_abbrev", 8))
    return git("describe", "--tags", f"--abbrev={abbrev}", f"--match={match}", cwd=source).strip()


def _info(name: str, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_archive(
    source: Path, files: list[str], generated: dict[str, bytes], root: str, mtime: int, output: Path
) -> int:
    entries: dict[str, tuple[str, object]] = {name: ("file", source / name) for name in files}
    entries.update({name: ("generated", data) for name, data in generated.items()})
    directories = sorted(
        {
            str(parent)
            for name in entries
            for parent in PurePosixPath(name).parents
            if str(parent) != "."
        }
    )
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as bundle:
        top = _info(root, mtime)
        top.type, top.mode = tarfile.DIRTYPE, 0o755
        bundle.addfile(top)
        for directory in directories:
            info = _info(f"{root}/{directory}", mtime)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            bundle.addfile(info)
        for name in sorted(entries):
            kind, value = entries[name]
            info = _info(f"{root}/{name}", mtime)
            if kind == "generated":
                data = value  # type: ignore[assignment]
                info.size, info.mode = len(data), 0o644
                bundle.addfile(info, io.BytesIO(data))
                continue
            path = Path(value)  # type: ignore[arg-type]
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, os.readlink(path)
                bundle.addfile(info)
            elif stat.S_ISREG(mode):
                info.size = path.stat().st_size
                info.mode = 0o755 if mode & stat.S_IXUSR else 0o644
                with path.open("rb") as handle:
                    bundle.addfile(info, handle)
            else:
                raise SystemExit(f"unsupported file type: {name}")
    data = raw.getvalue()
    with lzma.open(output, "wb", preset=9 | lzma.PRESET_EXTREME) as compressed:
        compressed.write(data)
    return len(data)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work", type=Path, help="reusable clone directory (default: <out>/work)")
    arguments = parser.parse_args()
    if not COMMIT.fullmatch(arguments.commit):
        raise SystemExit("--commit must be a full lowercase commit hash")
    if not VERSION.fullmatch(arguments.version):
        raise SystemExit("--version has invalid characters")

    profile = load_profile(arguments.profile)
    arguments.out.mkdir(parents=True, exist_ok=True)
    work = (arguments.work or arguments.out / "work").resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = fetch(profile, arguments.commit, work)

    base = f"{profile['name']}-{arguments.version}-{arguments.commit[:8]}-p{profile['profile_revision']}"
    mtime = int(git("log", "-1", "--format=%ct", cwd=source).strip())
    described = describe(profile, source)
    files = selected_files(profile, source)
    generated: dict[str, bytes] = {}
    if profile.get("version_file"):
        if described is None:
            raise SystemExit("version_file needs describe_match")
        generated[profile["version_file"]] = (described + "\n").encode()
    submodules = []
    if profile.get("submodules"):
        for line in git("submodule", "status", "--recursive", cwd=source).splitlines():
            revision, path = line[1:].split()[:2]
            submodules.append({"path": path, "revision": revision})
    manifest = {
        "name": profile["name"],
        "repository": profile["repository"],
        "commit": arguments.commit,
        "version": arguments.version,
        "describe": described,
        "profile": arguments.profile.name,
        "profile_revision": profile["profile_revision"],
        "profile_sha256": sha256(arguments.profile),
        "submodules": submodules,
        "files": len(files) + len(generated) + 1,
        "python": sys.version.split()[0],
        "liblzma": getattr(lzma, "LZMA_VERSION", None) or "unknown",
    }
    generated[MANIFEST] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    check_required(profile, files, set(generated))

    archive = arguments.out / f"{base}.tar.xz"
    if archive.exists():
        raise SystemExit(f"{archive} exists; a published archive is never rebuilt")
    raw_size = write_archive(source, files, generated, base, mtime, archive)
    digest = sha256(archive)
    (arguments.out / f"{base}.tar.xz.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    (arguments.out / f"{base}.json").write_bytes(generated[MANIFEST])
    print(
        json.dumps(
            {
                "asset": archive.name,
                "sha256": digest,
                "bytes": archive.stat().st_size,
                "tar_bytes": raw_size,
                "files": manifest["files"],
                "describe": described,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
