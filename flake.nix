# Nix flake for TheOwlOps/Boom.
#
# Provides a reproducible dev shell (Python 3.12, Node 22, tesseract) and
# two packages: the Python agent API (`packages.default`) and the React
# web UI (`packages.web`). Per-package module definitions live in nix/.
{
  description = "Boom - reproducible dev environment and packages";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.callPackage ./nix/shell.nix { inherit pkgs; };

        packages.default = pkgs.callPackage ./nix/pkg-api.nix { inherit pkgs; };
        packages.web = pkgs.callPackage ./nix/pkg-web.nix { inherit pkgs; };
      });
}
