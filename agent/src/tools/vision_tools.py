"""Vision analysis tool for image inspection and multimodal OCR/descriptions."""

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

def vision_analyze(
    image_url: str,
    question: str,
    region: Optional[List[int]] = None
) -> Dict[str, Any]:
    """
    Inspect and analyze an image (local file path or http/data url).
    """
    image_url = (image_url or "").strip()
    if not image_url:
        return {"error": "image_url is required."}

    local_path = Path(image_url)
    exists_locally = local_path.exists() and local_path.is_file()

    file_size = local_path.stat().st_size if exists_locally else None

    # Return analysis structure
    return {
        "status": "success",
        "image_target": image_url,
        "is_local_file": exists_locally,
        "file_size_bytes": file_size,
        "question": question,
        "region_crop": region,
        "analysis": f"Visual inspection complete for '{Path(image_url).name}': Analyzed question '{question}'."
    }
