"""Cau hinh bind address + CORS cho Boom gateway.

Muc dich: cho phep mobile PWA trong mang LAN ket noi toi gateway.
Mac dinh bind "0.0.0.0" (tat ca interfaces, thay vi 127.0.0.1) va mo
CORS rong ("*") de trinh duyet tren dien thoai goi API duoc.

Luu y bao mat: bind 0.0.0.0 + CORS "*" chi phu hop khi chay trong
mang LAN tin cay. Neu mo ra Internet, phai thu hep cors_origins va
bat auth o tang gateway.

Chi dung stdlib, khong phu thuoc uvicorn/fastapi (ham
``apply_to_uvicorn_kwargs`` chi tra ve kwargs neu sau nay gateway
duoc port sang uvicorn).
"""

import os

# Phuong thuc HTTP ma preflight cho phep.
ALLOW_METHODS = "GET, POST, PUT, DELETE, OPTIONS"
# Headers ma client duoc phep gui (gom Authorization cho token).
ALLOW_HEADERS = "Content-Type, Authorization"
# Thoi gian browser cache ket qua preflight: 1 ngay.
MAX_AGE = "86400"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8788
DEFAULT_CORS_ORIGINS = ["*"]


class BindConfig:
    """Goi 3 gia tri cau hinh: host, port, cors_origins."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, cors_origins=None):
        self.host = host
        self.port = int(port)
        # Copy list de nguoi goi khong vo tinh sua default chung.
        self.cors_origins = (
            list(cors_origins) if cors_origins is not None else list(DEFAULT_CORS_ORIGINS)
        )

    @classmethod
    def from_env(cls):
        """Doc cau hinh tu bien moi truong.

        BOOM_HOST         - dia chi bind (mac dinh "0.0.0.0")
        BOOM_PORT         - cong (mac dinh 8788; sai cu phap -> mac dinh)
        BOOM_CORS_ORIGINS - danh sach origin cach nhau boi dau phay
                            (mac dinh "*"; bo qua muc rong)
        """
        host = os.environ.get("BOOM_HOST", DEFAULT_HOST)

        raw_port = os.environ.get("BOOM_PORT", str(DEFAULT_PORT))
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            # Gia tri khong phai so: roi ve mac dinh thay vi crash.
            port = DEFAULT_PORT

        raw_origins = os.environ.get("BOOM_CORS_ORIGINS", "*")
        cors_origins = [o.strip() for o in raw_origins.split(",")]
        cors_origins = [o for o in cors_origins if o]  # bo muc rong
        if not cors_origins:
            cors_origins = list(DEFAULT_CORS_ORIGINS)

        return cls(host=host, port=port, cors_origins=cors_origins)

    def __repr__(self):
        return (
            "BindConfig(host=%r, port=%r, cors_origins=%r)"
            % (self.host, self.port, self.cors_origins)
        )


def make_cors_headers(origin=None, cors_origins=None):
    """Tra ve dict headers CORS day du cho mot origin.

    - Neu "*" nam trong cors_origins: tra "Access-Control-Allow-Origin: *"
      (che do mo rong cho LAN/PWA).
    - Neu origin nam trong danh sach cu the: reflect origin do + "Vary: Origin".
    - Neu origin khong duoc chap nhan: khong tra Access-Control-Allow-Origin
      (browser se chan request — fail closed).
    """
    origins = (
        list(cors_origins) if cors_origins is not None else list(DEFAULT_CORS_ORIGINS)
    )
    headers = {
        "Access-Control-Allow-Methods": ALLOW_METHODS,
        "Access-Control-Allow-Headers": ALLOW_HEADERS,
        "Access-Control-Max-Age": MAX_AGE,
    }
    if "*" in origins:
        headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in origins:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return headers


def handle_preflight(request_headers, cors_origins=None):
    """Xu ly OPTIONS preflight: tra (status, headers).

    request_headers: dict headers cua request (vd: {"Origin": "..."}).
    Tra ve (204, cors_headers) — 204 No Content theo chuan preflight.
    """
    origin = None
    if request_headers:
        # Ho tro ca dict thuong va http.client.HTTPMessage (get() deu duoc).
        origin = request_headers.get("Origin")
    return 204, make_cors_headers(origin, cors_origins)


def apply_to_uvicorn_kwargs(cfg):
    """Tra ve kwargs de truyen vao uvicorn.run(..., **kwargs)."""
    return {"host": cfg.host, "port": cfg.port}
