# nix/system.nix — System dependencies for TheOwlOps/Boom
#
# System-level packages that must be present on any machine running Boom,
# independent of the Python devShell. Mirrors the `packages` list in
# devbox.json (which is Nix-backed, channel nixos-24.11).
#
# Package notes (nixpkgs nixos-24.11):
# - `tesseract` — OCR engine binary (includes English traineddata).
#   Vietnamese OCR needs vie.traineddata — download from
#   https://github.com/tesseract-ocr/tessdata_fast/raw/main/vie.traineddata
#   into $TESSDATA_PREFIX (nixpkgs has no tesseract-lang-vie package).
# - `git`  — VCS, needed for source operations and version checks.
# - `curl` — HTTP download tool used by setup/install scripts.

{ pkgs }:

{
  # Full list of system packages. Callers splice this into their own
  # `packages` / `buildInputs` list.
  systemPackages = [
    pkgs.tesseract
    pkgs.git
    pkgs.curl
  ];

  # Tesseract's built-in tessdata dir (has eng). Override via shellHook
  # if you add vie.traineddata elsewhere.
  tessdataPrefix = "${pkgs.tesseract}/share/tessdata";
}
