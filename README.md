# Newmota SDK archives

Stripped, checksummed release archives of the SDK commits that [Newmota](https://github.com/Newmota)
pins. Each archive is built from one commit by [`scripts/make_archive.py`](scripts/make_archive.py)
with one profile from [`profiles/`](profiles), and published as a GitHub release.

- Name: `<sdk>-<version>-<commit8>-p<profile_revision>.tar.xz`, with one top-level directory of
  the same name. The release tag is the same name.
- Each release has the archive, a `.sha256` file, and a JSON manifest (repository, commit,
  `git describe`, nested submodule commits, profile and its SHA-256).
- Only files tracked at the pinned commit (including nested submodules) are packed, sorted, with
  fixed owners and the commit time, so the tar stream is reproducible. A published archive is never
  rebuilt; a strip-list change raises `profile_revision`.

## Build an archive

Run the **Build SDK archive** workflow with a profile, the full commit hash, and the SDK version.
Locally:

```sh
python3 scripts/make_archive.py --profile profiles/esp-idf.toml \
  --commit <40-hex> --version v5.5.5 --out dist
```

The scripts and profiles are licensed under the GNU GPL v3 or later ([LICENSE](LICENSE)), like
Newmota. The archives keep each SDK's own license files and notices.
