# nix/

Reproducible Boom environments with plain Nix.

This directory contains the module definitions for the repo's `flake.nix`.
Together they give you a bit-for-bit reproducible dev shell and two buildable
packages (Python API + web UI), using Nix the straightforward way — no
abstraction layers on top.

> **New to this setup?** If you just want to run Boom quickly, use
> [`devbox.json`](../devbox.json) (`devbox shell`) instead. Devbox is Nix
> under the hood with a friendlier CLI. The files here are for people who
> want full, plain-Nix control.

## Requirements

1. Install Nix: https://nixos.org/download
2. Enable flakes (one of):
   - Run everything with the experimental flags:
     `nix --extra-experimental-features 'nix-command flakes' ...`
   - Or add to `~/.config/nix/nix.conf`:
     ```
     experimental-features = nix-command flakes
     ```

All commands below assume you run them from the repo root.

## `nix develop` — enter the dev shell

```bash
nix develop
```

What you get:

- **Python 3.12** with `httpx` and `pydantic` (enough to run `python3 boom.py` and the API)
- **Node.js 22** (for `apps/web`, npm)
- **tesseract** with Vietnamese (`vie`) and English (`eng`) data (for the OCR tool)
- `PYTHONPATH` and `BOOM_API_PORT=18080` set automatically

Try it:

```bash
python3 boom.py --help
./start.sh all                 # gateway + API + web UI
cd apps/web && npm install && npm run build
```

## `nix build` / `nix run` — the API package

`packages.default` builds the Python agent API server as a self-contained Nix
derivation (`boom-api`, version `0.1.0`).

```bash
nix build        # builds -> ./result/bin/boom-api
nix run          # builds and runs the API server (port 18080)
./result/bin/boom-api   # run the built binary directly
```

The package copies the repo source (excluding `.git`) into the Nix store and
ships a `makeWrapper`-based launcher that `cd`s into the source tree before
executing `python -m gateway.api`. It works identically on any machine with
Nix, regardless of where you invoke it from.

## `nix build .#web` — the web UI package

`packages.web` builds `apps/web` (React + Vite) into static files.

```bash
nix build .#web
# -> ./result/ contains the dist/ output; serve it with any static server
```

> ⚠️ **Before first use:** replace the `npmDepsHash` placeholder in
> `nix/pkg-web.nix`. Get the real hash with:
>
> ```bash
> nix run nixpkgs#prefetch-npm-deps -- ./apps/web/package-lock.json
> ```
>
> then paste the resulting `sha256-...` value over the placeholder string
> (`"sha256-AAAA..."`). Without the real hash the build will fail by design —
> the placeholder is there so nobody ships a wrong value accidentally.
>
> Note: this package builds with **npm**, matching the Dockerfile.
> `apps/web` also has a `pnpm-lock.yaml`, but the canonical build path is npm.

## File map

| File | Purpose |
|------|---------|
| `../flake.nix` | Flake entrypoint. Inputs: `nixpkgs` `nixos-24.11`, `flake-utils`. Outputs: `devShells.default`, `packages.default` (API), `packages.web`. |
| `nix/shell.nix` | `nix develop` shell: combines the Python, Node and system environments, sets `PYTHONPATH` / `BOOM_API_PORT` in `shellHook`. |
| `nix/python.nix` | Python 3.12 + `httpx` + `pydantic` environment. |
| `nix/node.nix` | Node.js 22 environment. |
| `nix/system.nix` | System deps: tesseract + `vie`/`eng` language data. |
| `nix/pkg-api.nix` | `boom-api` package: self-contained `python -m gateway.api` via `mkDerivation` + `makeWrapper`. No network fetch, so no hash needed. |
| `nix/pkg-web.nix` | `boom-web` package: `npm install && npm run build` via `buildNpmPackage`, output copied from `dist/` to `$out`. Needs a real `npmDepsHash` (see above). |
| `nix/README.md` | This file. |

## nix/ vs devbox.json vs Dockerfile

All three produce the same working environment; they differ in audience and
fidelity.

| | `nix/` (this dir) | `devbox.json` | `Dockerfile` |
|---|---|---|---|
| What it is | Plain Nix flake | Nix simplified (Devbox is Nix underneath) | OCI container image |
| Dev shell | `nix develop` | `devbox shell` | not applicable |
| Reproducibility | Strongest: pinned `nixos-24.11`, per-package hashes | Strong (Nix under the hood), less explicit | Image digest, but OS-level layers are less auditable |
| Learning curve | Steepest | Gentlest | Familiar to ops |
| Use when | You want full Nix control, or build/publish Nix packages | You want to start fast with zero Nix knowledge | You deploy to production / CI containers |

**Rule of thumb:**

- **Develop locally:** `devbox shell` (or `nix develop` if you know Nix).
- **Verify "runs identically everywhere":** `nix build` / `nix run` / `nix build .#web`.
- **Ship / deploy:** `Dockerfile` (or Nix → OCI if you prefer a fully Nix pipeline).

## Notes

- The flake pins `nixpkgs` to `nixos-24.11`. To upgrade the dependency set,
  run `nix flake update` and re-verify both packages and the dev shell.
- `nix build .#web` requires network access on first build to fetch npm deps
  (via the fixed-output derivation). After that it's cached.
- On macOS (aarch64/x86_64), `buildNpmPackage` and tesseract work, but the
  API package is marked `platforms.unix` — Linux is the primary target.
