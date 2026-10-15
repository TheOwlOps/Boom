{ pkgs }:

let
  pythonEnv = pkgs.callPackage ./python.nix { inherit pkgs; };
  nodeEnv = pkgs.callPackage ./node.nix { inherit pkgs; };
  sysEnv = pkgs.callPackage ./system.nix { inherit pkgs; };
in
pkgs.mkShell {
  buildInputs = [ pythonEnv.python nodeEnv.nodejs ] ++ sysEnv.systemPackages;

  shellHook = ''
    echo "=============================="
    echo "  Boom dev shell"
    echo "=============================="
    echo ""
    echo "  Lệnh dùng nhanh:"
    echo "    python3 boom.py              # chạy agent CLI"
    echo "    ./start.sh all               # chạy gateway + API + web UI"
    echo "    cd apps/web && npm install && npm run build"
    echo ""

    export PYTHONPATH="$PWD:$PWD/agent/src"
    export BOOM_API_PORT=18080
    # Tesseract tìm traineddata (vie/eng) qua TESSDATA_PREFIX — đường dẫn
    # này được Nix interpolate lúc eval (không phải shell var).
    export TESSDATA_PREFIX="${sysEnv.tessdataPrefix}"

    echo "  PYTHONPATH=$PYTHONPATH"
    echo "  BOOM_API_PORT=$BOOM_API_PORT"
    echo "  TESSDATA_PREFIX=$TESSDATA_PREFIX"
    echo ""
  '';
}
