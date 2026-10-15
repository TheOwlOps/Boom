# Nix package: build Boom web UI (apps/web) into $out as static files.
#
# Build:   npm install && npm run build  (tsc && vite build)  -> dist/
# Install: cp -r dist/* $out/
#
# NOTE: repo also carries a pnpm-lock.yaml, but the Dockerfile builds with npm,
#       so this package uses npm. If a package-lock.json exists under apps/web,
#       the build is more reproducible; npm will generate/fallback as needed.
{
  pkgs,
}:

pkgs.buildNpmPackage rec {
  pname = "boom-web";
  version = "0.1.0";

  src = ../apps/web;

  nodejs = pkgs.nodejs_22;

  # >>> PLACEHOLDER HASH <<<
  # !!! LẤY HASH THẬT TRƯỚC KHI DÙNG:
  #     nix run nixpkgs#prefetch-npm-deps -- ./apps/web/package-lock.json
  #     rồi thay toàn bộ chuỗi bên dưới bằng hash thật (định dạng sha256- + 44 ký tự base64).
  npmDepsHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

  # buildNpmPackage tự chạy `npm install` từ package.json trong src ở giai đoạn
  # configure; ở đây chỉ cần build rồi copy output.
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
    description = "Boom web UI (Vite build output, static files)";
    license = licenses.mit;
    platforms = platforms.all;
  };
}
