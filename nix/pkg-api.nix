# pkg-api.nix — Nix package cho Boom API server.
#
# Lựa chọn: dùng pkgs.stdenv.mkDerivation copy source vào store (thay vì
# writeShellScriptBin bọc python chạy trên $PWD).
#
#   - Vì sao không phải writeShellScriptBin: cách đó buộc người dùng phải
#     `nix run` từ đúng thư mục chứa source, package không tự chứa code —
#     chỉ là alias cho `python -m gateway.api`, không tái tạo được như nix.
#   - Vì sao mkDerivation copy src: package tự chứa toàn bộ code trong
#     $out/share/boom, wrapper cd vào đó rồi exec python -m gateway.api.
#     Kết quả chạy giống hệt trên mọi máy có nix, đúng tinh thần "giống hệt
#     mọi máy" mà Son yêu cầu.
#
# Ghi chú về src: khi file này nằm ở nix/ (sau merge: nix/pkg-api.nix),
# src = ./.. trỏ tới ROOT của repo — tức thư mục chứa gateway/ và agent/.
# Nếu di chuyển file, sửa lại đường dẫn tương đối cho trỏ đúng root repo.
#
# Python env: python312 + httpx + pydantic (đủ chạy `python -m gateway.api`;
# gateway/api.py đã tự thêm sys.path nên chỉ cần cwd = root repo).
#
# Không fetch gì từ mạng nên KHÔNG cần hash (không fakeHash).

{ pkgs }:

let
  python = pkgs.python312.withPackages (ps: with ps; [ httpx pydantic ]);
in
pkgs.stdenv.mkDerivation {
  pname = "boom-api";
  version = "0.1.0";

  src = ./..;

  nativeBuildInputs = [ pkgs.makeWrapper ];

  # Không có configure/Makefile trong source — tắt các phase mặc định,
  # chỉ chạy installPhase copy source + tạo wrapper.
  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/boom $out/bin
    # Copy toàn bộ source vào store, loại bỏ .git cho gọn.
    tar --exclude='.git' -cf - . | tar -xf - -C $out/share/boom
    # Wrapper: cd vào thư mục chứa source rồi exec python -m gateway.api
    makeWrapper ${python}/bin/python $out/bin/boom-api \
      --chdir $out/share/boom \
      --add-flags "-m gateway.api"
    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "Boom API server (python -m gateway.api)";
    license = licenses.mit;
    platforms = platforms.unix;
  };
}
