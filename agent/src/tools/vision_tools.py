"""Vision analysis tool — real OCR via tesseract, honest about limits."""

from pathlib import Path
from typing import Any, Dict, List, Optional


def vision_analyze(
    image_url: str,
    question: str,
    region: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Inspect an image (local file path or http url).

    What it really does: runs OCR (tesseract) to extract text from the image.
    It does NOT do full visual question-answering — that needs a vision LLM.
    If you ask about text in the image, you get the OCR result.
    For anything else, it tells you honestly what it can and cannot do.
    """
    image_url = (image_url or "").strip()
    if not image_url:
        return {"error": "image_url is required."}

    # Use the real OCR tool.
    try:
        from tools.ocr_tool import ocr_image, ocr_image_url
    except ImportError:
        try:
            from agent.src.tools.ocr_tool import ocr_image, ocr_image_url  # type: ignore
        except ImportError:
            return {"error": "ocr_tool not available — install tesseract-ocr to enable vision_analyze."}

    if image_url.startswith(("http://", "https://")):
        ocr_result = ocr_image_url(image_url)
    else:
        ocr_result = ocr_image(image_url)

    if "error" in ocr_result:
        return {"error": ocr_result["error"], "question": question}

    text = ocr_result.get("text", "")
    q = (question or "").lower()
    # Heuristic: if the question is about text/reading, answer from OCR.
    text_keywords = ("text", "chữ", "đọc", "read", "write", "viết", "nội dung", "content")
    about_text = any(k in q for k in text_keywords)

    result: Dict[str, Any] = {
        "status": "success",
        "image_target": image_url,
        "ocr_text": text,
        "char_count": ocr_result.get("char_count", 0),
    }
    if about_text or not q:
        result["analysis"] = text if text else "(no text detected in image)"
    else:
        result["analysis"] = (
            "I can extract text from images (OCR), but I cannot answer "
            f"general visual questions like '{question}' — that needs a vision model. "
            f"OCR text found: {text[:500]!r}" if text else
            "I can extract text from images (OCR), but I cannot answer "
            f"general visual questions like '{question}' — that needs a vision model. "
            "No text detected in this image."
        )
        result["note"] = "limited_to_ocr"
    if region:
        result["region_requested"] = region
        result["note"] = "region_crop not supported — full image was analyzed"
    return result


def build_tools(env=None):  # type: ignore[no-untyped-def]
    return {
        "vision_analyze": {
            "name": "vision_analyze",
            "func": vision_analyze,
            "description": (
                "Extract text from an image (OCR). Honest: answers text questions "
                "from OCR; cannot do general visual Q&A without a vision model."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "image_url": {"type": "string", "description": "Local path or http(s) URL of the image"},
                    "question": {"type": "string", "description": "What to ask about the image"},
                    "region": {"type": "array", "items": {"type": "integer"},
                               "description": "Optional [x,y,w,h] — not supported, full image used"},
                },
                "required": ["image_url", "question"],
            },
        }
    }
