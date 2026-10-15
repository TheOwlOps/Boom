# nix/node.nix — Node.js environment for TheOwlOps/Boom.
#
# PURPOSE
#   Provides the Node.js runtime used to build the Boom web UI
#   (apps/web — React 18 + Vite 5 + TypeScript) via:
#       npm install && npm run build
#
# NIXPKGS
#   Pinned to nixpkgs nixos-24.11, which ships Node.js 22 as
#   `pkgs.nodejs_22`. This module only needs Node 22; older/newer
#   Node packages (nodejs_18, nodejs_20, nodejs_24, ...) are not
#   referenced.
#
# NOTE ON npm
#   npm ships bundled inside pkgs.nodejs_22 in nixpkgs, so no separate
#   npm package needs to be added — `node` and `npm` both come from
#   this one derivation.
#
# USAGE (inside a shell.nix / flake devShell, from the repo root):
#   let
#     nodeEnv = import ./nix/node.nix { inherit pkgs; };
#   in
#   pkgs.mkShell {
#     buildInputs = [ nodeEnv.nodejs ];
#   }

{ pkgs }:

{
  # The Node.js runtime for the web UI build.
  nodejs = pkgs.nodejs_22;
}
