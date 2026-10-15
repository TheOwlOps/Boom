# nix/system.nix — System dependencies for TheOwlOps/Boom
#
# System-level packages that must be present on any machine running Boom,
# independent of the Python devShell. Mirrors the `packages` list in
# devbox.json (which is Nix-backed, channel nixos-24.11).
#
# Usage in nix/shell.nix:
#
#   let sysEnv = pkgs.callPackage ./system.nix { inherit pkgs; }; in
#   buildInputs = ... ++ sysEnv.systemPackages;
#   # and in shellHook: export TESSDATA_PREFIX="${sysEnv.tessdataPrefix}";
#
# Package name notes (nixpkgs nixos-24.11):
# - `tesseract`             — OCR engine binary (tesseract-ocr).
# - `tesseract-lang-vie`    — Vietnamese traineddata (tesseract-lang-<code> naming).
# - `tesseract-lang-eng`    — English traineddata.
# - `git`                   — VCS, needed for source operations and version checks.
# - `curl`                  — HTTP download tool used by setup/install scripts.
#
# Tesseract language data: tesseract only finds traineddata via the
# TESSDATA_PREFIX env var (or its own build-in share/tessdata). The
# tesseract-lang-* packages install into their own store paths, so we merge
# them with the binary via symlinkJoin and export the merged tessdata dir.
# Without this, `tesseract -l vie` would fail with "Error opening data file".

{ pkgs }:

let
  tesseractWithLangs = pkgs.symlinkJoin {
    name = "tesseract-with-langs";
    paths = [
      pkgs.tesseract
      pkgs.tesseract-lang-vie
      pkgs.tesseract-lang-eng
    ];
  };
in
{
  # Full list of system packages. Callers splice this into their own
  # `packages` / `buildInputs` list.
  systemPackages = [
    tesseractWithLangs
    pkgs.git
    pkgs.curl
  ];

  # Merged traineddata directory — shell.nix exports this as TESSDATA_PREFIX.
  tessdataPrefix = "${tesseractWithLangs}/share/tessdata";
}
