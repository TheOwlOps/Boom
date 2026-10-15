"""Image generation tool for Boom (grok-parity: agent tự sinh ảnh).

Module này nằm trong `agent/src/media/` của repo Boom.

Provider priority:
  1. Pollinations.ai  -- miễn phí, không cần key, không giới hạn hard.
  2. OpenAI DALL-E    -- khi có OPENAI_API_KEY (fallback chất lượng cao).

Quy ước fail-soft: mọi lỗi mạng đều trả {"ok": False, "error": ...},
KHÔNG bao giờ raise -- caller (agent loop) không bị crash vì ảnh gen lỗi.

Chạy:  python3 -m media.imagegen  (với agent/src trong PYTHONPATH)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Endpoint Pollinations: prompt được đặt thẳng vào path, đã URL-encode.
# Params: width/height (px), nologo=true (bỏ watermark), seed (optional).
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
POLLINATIONS_TIMEOUT = 90  # ảnh gen có thể mất 30-60s

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
OPENAI_VARIATIONS_URL = "https://api.openai.com/v1/images/variations"
OPENAI_TIMEOUT = 120

# Guard chống prompt rỗng / quá dài cho URL (Pollinations nhận qua path).
MAX_PROMPT_LEN = 1000

_SIZE_RE = re.compile(r"^\s*(\d{2,4})\s*[xX]\s*(\d{2,4})\s*$")


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _parse_size(size: str) -> Optional[tuple]:
    """'1024x1024' -> (1024, 1024). Trả None nếu sai format.

    Giới hạn 64..4096 px mỗi chiều để tránh URL kỳ cục hoặc request khổng lồ.
    """
    m = _SIZE_RE.match(size or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if not (64 <= w <= 4096 and 64 <= h <= 4096):
        return None
    return w, h


def _validate_prompt(prompt: str) -> Optional[str]:
    """Trả về error message nếu prompt không hợp lệ, None nếu ok."""
    if not (prompt or "").strip():
        return "prompt must be a non-empty string"
    if len(prompt) > MAX_PROMPT_LEN:
        return "prompt too long (%d chars, max %d)" % (len(prompt),
                                                       MAX_PROMPT_LEN)
    return None


def _pollinations_url(prompt: str, width: int, height: int,
                      seed: Optional[int] = None) -> str:
    """Build URL đúng format Pollinations: /prompt/{encoded}?width=&height=&nologo=true."""
    # safe="" để encode cả dấu '/' -- Pollinations đọc prompt từ path segment.
    encoded = quote(prompt.strip(), safe="")
    url = "%s/%s?width=%d&height=%d&nologo=true" % (
        POLLINATIONS_BASE, encoded, width, height)
    if seed is not None:
        url += "&seed=%d" % int(seed)
    return url


def _fetch_bytes(url: str, timeout: float) -> tuple:
    """GET url, trả (ok_bytes, error_str). Fail-soft: không raise."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.TimeoutException:
        return None, "request timed out after %ds" % int(timeout)
    except httpx.HTTPError as e:
        return None, "http error: %s" % (e,)
    except Exception as e:  # defensive: httpx có thể raise lạ
        return None, "request failed: %s" % (e,)
    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        return None, "provider returned HTTP %d: %s" % (resp.status_code,
                                                        body)
    if not resp.content:
        return None, "provider returned empty response"
    # Pollinations có thể trả JSON lỗi thay vì ảnh khi quá tải.
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return None, "provider error: %s" % (resp.text or "")[:200]
    return resp.content, None


# --------------------------------------------------------------------------
# Pollinations provider (priority 1 -- free, no key)
# --------------------------------------------------------------------------

def _gen_pollinations(prompt: str, width: int, height: int,
                      seed: Optional[int] = None) -> Dict[str, Any]:
    url = _pollinations_url(prompt, width, height, seed)
    data, err = _fetch_bytes(url, POLLINATIONS_TIMEOUT)
    if err:
        return {"ok": False, "error": err, "provider": "pollinations",
                "url": url}
    return {"ok": True, "image_bytes": data, "url": url,
            "provider": "pollinations"}


# --------------------------------------------------------------------------
# OpenAI provider (priority 2 -- cần OPENAI_API_KEY)
# --------------------------------------------------------------------------

def _openai_headers() -> Optional[Dict[str, str]]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    return {"Authorization": "Bearer " + key,
            "Content-Type": "application/json"}


def _gen_openai(prompt: str, width: int, height: int,
                model: Optional[str]) -> Dict[str, Any]:
    headers = _openai_headers()
    if not headers:
        return {"ok": False, "error": "OPENAI_API_KEY not set",
                "provider": "openai"}
    dalle_model = model if (model and model != "openai") else "dall-e-3"
    size = "%dx%d" % (width, height)
    payload = {"model": dalle_model, "prompt": prompt.strip(), "size": size,
               "n": 1}
    try:
        resp = httpx.post(OPENAI_IMAGES_URL, headers=headers, json=payload,
                          timeout=OPENAI_TIMEOUT)
    except httpx.TimeoutException:
        return {"ok": False, "error": "openai request timed out",
                "provider": "openai"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": "openai http error: %s" % (e,),
                "provider": "openai"}
    except Exception as e:
        return {"ok": False, "error": "openai request failed: %s" % (e,),
                "provider": "openai"}
    if resp.status_code != 200:
        return {"ok": False,
                "error": "openai returned HTTP %d: %s"
                         % (resp.status_code, (resp.text or "")[:300]),
                "provider": "openai"}
    try:
        body = resp.json()
        item = (body.get("data") or [{}])[0]
        img_url = item.get("url")
    except Exception as e:
        return {"ok": False, "error": "openai bad JSON response: %s" % (e,),
                "provider": "openai"}
    if not img_url:
        return {"ok": False, "error": "openai returned no image url",
                "provider": "openai"}
    # Tải bytes ảnh về để caller có image_bytes đồng nhất.
    data, err = _fetch_bytes(img_url, OPENAI_TIMEOUT)
    if err:
        return {"ok": False, "error": "download failed: " + err,
                "provider": "openai", "url": img_url}
    return {"ok": True, "image_bytes": data, "url": img_url,
            "provider": "openai"}


def _variation_openai(image_bytes: bytes, prompt: str) -> Dict[str, Any]:
    """DALL-E image variation: chỉ hỗ trợ PNG vuông, multipart upload.

    endpoint /v1/images/variations không nhận text prompt (prompt bị ignore).
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return {"ok": False, "error": "OPENAI_API_KEY not set",
                "provider": "openai"}
    headers = {"Authorization": "Bearer " + key}
    files = {"image": ("input.png", image_bytes, "image/png")}
    try:
        resp = httpx.post(OPENAI_VARIATIONS_URL, headers=headers, files=files,
                          data={"n": "1", "size": "1024x1024"},
                          timeout=OPENAI_TIMEOUT)
    except httpx.TimeoutException:
        return {"ok": False, "error": "openai request timed out",
                "provider": "openai"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": "openai http error: %s" % (e,),
                "provider": "openai"}
    except Exception as e:
        return {"ok": False, "error": "openai request failed: %s" % (e,),
                "provider": "openai"}
    if resp.status_code != 200:
        return {"ok": False,
                "error": "openai returned HTTP %d: %s"
                         % (resp.status_code, (resp.text or "")[:300]),
                "provider": "openai"}
    try:
        img_url = ((resp.json().get("data") or [{}])[0]).get("url")
    except Exception as e:
        return {"ok": False, "error": "openai bad JSON response: %s" % (e,),
                "provider": "openai"}
    if not img_url:
        return {"ok": False, "error": "openai returned no image url",
                "provider": "openai"}
    data, err = _fetch_bytes(img_url, OPENAI_TIMEOUT)
    if err:
        return {"ok": False, "error": "download failed: " + err,
                "provider": "openai", "url": img_url}
    return {"ok": True, "image_bytes": data, "url": img_url,
            "provider": "openai"}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def available_providers() -> List[str]:
    """Danh sách provider dùng được lúc này.

    - "pollinations": luôn khả dụng (httpx đã import được là dùng được,
      không cần key).
    - "openai": chỉ khi OPENAI_API_KEY có giá trị.
    """
    providers = ["pollinations"]
    if os.environ.get("OPENAI_API_KEY", "").strip():
        providers.append("openai")
    return providers


def generate(prompt: str, size: str = "1024x1024",
             model: Optional[str] = None,
             seed: Optional[int] = None) -> Dict[str, Any]:
    """Sinh ảnh từ text prompt. Không bao giờ raise vì lỗi mạng.

    model: None (tự chọn theo priority) | "pollinations" | "openai" |
           tên model DALL-E cụ thể ("dall-e-3", "dall-e-2").

    Trả về:
      {"ok": True, "image_bytes": bytes, "url": str, "provider": ...}
      {"ok": False, "error": "...", "provider": ...}
    """
    err = _validate_prompt(prompt)
    if err:
        return {"ok": False, "error": err}
    wh = _parse_size(size)
    if not wh:
        return {"ok": False,
                "error": "invalid size '%s': expected WxH, e.g. '1024x1024'"
                         % (size,)}
    width, height = wh

    want = (model or "").strip().lower()
    # Model ép buộc provider cụ thể?
    if want in ("pollinations", "pollinations.ai"):
        return _gen_pollinations(prompt, width, height, seed)
    if want in ("openai",) or want.startswith("dall-e"):
        return _gen_openai(prompt, width, height, model)

    # Priority mặc định: Pollinations (free) trước, OpenAI sau nếu lỗi.
    result = _gen_pollinations(prompt, width, height, seed)
    if result.get("ok"):
        return result
    if os.environ.get("OPENAI_API_KEY", "").strip():
        fb = _gen_openai(prompt, width, height, None)
        if fb.get("ok"):
            return fb
        # Cả hai đều lỗi: báo lỗi gộp để debug dễ.
        result["error"] = ("pollinations failed: %s; openai fallback "
                           "failed: %s" % (result.get("error"),
                                           fb.get("error")))
    return result


def generate_variation(image_bytes: bytes,
                       prompt: str = "") -> Dict[str, Any]:
    """Image-to-image variation từ bytes ảnh gốc.

    - Pollinations (free tier): KHÔNG hỗ trợ upload bytes -> trả
      {"ok": False, "error": "not supported by provider"} trung thực.
    - OpenAI (có key): dùng /v1/images/variations.
    """
    if not image_bytes:
        return {"ok": False, "error": "image_bytes is required"}
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return _variation_openai(image_bytes, prompt)
    return {"ok": False,
            "error": ("not supported by provider: image variation needs "
                      "OpenAI (OPENAI_API_KEY), Pollinations free tier only "
                      "does text-to-image")}


def save_image(image_bytes: bytes, path: str) -> str:
    """Lưu bytes ảnh ra file. Trả về path đã lưu.

    Raise ValueError/OSError nếu input sai -- đây là lỗi lập trình của
    caller (không phải lỗi mạng), nên raise cho rõ thay vì fail-soft.
    """
    if not image_bytes:
        raise ValueError("image_bytes is required")
    path = (path or "").strip()
    if not path:
        raise ValueError("path is required")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(image_bytes)
    return path


# --------------------------------------------------------------------------
# Tool registration (convention như tools/ocr_tool.py: build_tools(env))
# --------------------------------------------------------------------------

def _schema(props: Dict[str, Any], required: list) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def build_tools(env=None) -> Dict[str, Dict[str, Any]]:
    """2 tools: generate_image, generate_image_variation.

    Env không dùng -- imagegen stateless, nhưng giữ signature cho
    environment.py wire đồng nhất với các module khác.
    """
    return {
        "generate_image": {
            "name": "generate_image",
            "func": generate,
            "description": ("Generate an image from a text prompt. Free via "
                            "Pollinations.ai by default (no key needed); "
                            "falls back to OpenAI DALL-E when OPENAI_API_KEY "
                            "is set. Returns {'ok', 'image_bytes', 'url', "
                            "'provider'} -- never raises on network error."),
            "schema": _schema(
                {
                    "prompt": {"type": "string",
                               "description": "Text description of the image "
                                              "(max 1000 chars)."},
                    "size": {"type": "string",
                             "description": "WxH, e.g. '1024x1024'. "
                                            "Default '1024x1024'."},
                    "model": {"type": "string",
                              "description": "Force provider: 'pollinations', "
                                             "'openai', or a DALL-E model "
                                             "name. Default: auto priority."},
                    "seed": {"type": "integer",
                             "description": "Optional seed for reproducible "
                                            "Pollinations results."},
                },
                ["prompt"],
            ),
        },
        "generate_image_variation": {
            "name": "generate_image_variation",
            "func": generate_variation,
            "description": ("Create a variation of an existing image "
                            "(image-to-image). Requires OpenAI "
                            "(OPENAI_API_KEY); returns an honest "
                            "'not supported by provider' error otherwise."),
            "schema": _schema(
                {
                    "image_bytes": {"type": "string",
                                    "description": "Raw bytes of the source "
                                                   "image (PNG)."},
                    "prompt": {"type": "string",
                               "description": "Optional hint (ignored by "
                                              "DALL-E variations)."},
                },
                ["image_bytes"],
            ),
        },
    }


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "a cute robot"
    out = generate(p)
    print({k: (len(v) if k == "image_bytes" else v)
            for k, v in out.items()})
