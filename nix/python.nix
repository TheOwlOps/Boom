# nix/python.nix — Python environment for Boom's agent code.
#
# Usage (from shell.nix):
#   let pythonEnv = import ./nix/python.nix { inherit pkgs; };
#   in pkgs.mkShell { buildInputs = [ pythonEnv.python ]; }
#
# `pythonEnv.python` is a Python 3.12 interpreter bundled with `httpx` and
# `pydantic` baked into its site-packages — the only two third-party deps
# boom.py needs at runtime (pyproject.toml). Nothing else to install.
#
# Verified names in nixpkgs nixos-24.11:
#   pkgs.python312                    — Python 3.12 interpreter derivation
#   pkgs.python312Packages.httpx      — HTTP client
#   pkgs.python312Packages.pydantic   — data validation / settings
{ pkgs }:

let
  # withPackages wraps the interpreter so the listed packages are importable
  # by every `python`/`python3` invocation from this derivation.
  python = pkgs.python312.withPackages (ps: with ps; [
    httpx
    pydantic
  ]);
in
{
  inherit python;
}
