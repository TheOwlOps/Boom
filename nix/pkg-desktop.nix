# Nix package: build Boom desktop UI (apps/desktop) into $out as static files.
#
# Build:   npm install && npm run build  (tsc && vite build)  -> dist/
# Install: cp -r dist/* $out/
#
# NOTE: repo also carries a pnpm-lock.yaml, but this package uses npm.
#       If a package-lock.json exists under apps/desktop,
#       the build is more reproducible; npm will generate/fallback as needed.
{
  pkgs,
}:

pkgs.buildNpmPackage rec {
  pname = "boom-desktop";
  version = "0.1.0";

  src = ../apps/desktop;

  nodejs = pkgs.nodejs_22;

  # >>> PLACEHOLDER HASH <<<
  # Get the real hash before use:
  #     nix run nixpkgs#prefetch-npm-deps -- ./apps/desktop/package-lock.json
  # then replace the string below with the real hash (sha256- + 44 base64 chars).
  npmDepsHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

  # buildNpmPackage runs `npm install` from package.json in src during the
  # configure phase; here we only need to build and copy the output.
  buildPhase = ''
    runHook preBuild
    npm run build
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out
    cp -r dist/* $out/
    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "Boom desktop UI (Vite build output, static files)";
    license = licenses.mit;
    platforms = platforms.all;
  };
}
