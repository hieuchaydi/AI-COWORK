"""CAPTCHA detection, template matching, and evidence screenshot storage."""

from __future__ import annotations

import base64
import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("browser_bridge.captcha_detector")

# Common URL and text indicators for verification challenges
CAPTCHA_URL_PATTERNS = [
    r"/verify/traffic",
    r"/verify/slider",
    r"/account/verify",
    r"/challenge",
    r"captcha",
    r"recaptcha",
    r"geetest",
]

CAPTCHA_TEXT_PATTERNS = [
    r"trượt để hoàn thành",
    r"kéo thanh trượt",
    r"xác minh bạn không phải là người máy",
    r"vui lòng xác minh",
    r"xác minh danh tính",
    r"please slide to complete the puzzle",
    r"slide to verify",
    r"drag the slider",
    r"security check",
    r"traffic verification",
    r"thử thách xác minh",
]

CAPTCHA_DOM_SELECTORS = [
    ".shopee-captcha-slider",
    ".captcha_container",
    ".geetest_radar_btn",
    ".geetest_canvas_bg",
    "iframe[src*='verify']",
    "iframe[src*='captcha']",
    ".challenge-container",
    ".verify-container",
    "div[class*='captcha']",
    "div[class*='verify']",
    "div[class*='geetest']",
]


def is_captcha_detected(
    url: str = "",
    text_sample: str = "",
    html_sample: str = "",
) -> Dict[str, Any]:
    """Inspects URL, text, and DOM patterns to detect whether a CAPTCHA/verification challenge is present."""
    lowered_url = (url or "").lower()
    lowered_text = (text_sample or "").lower()
    lowered_html = (html_sample or "").lower()

    # 1. URL pattern matching
    for pat in CAPTCHA_URL_PATTERNS:
        if re.search(pat, lowered_url):
            return {
                "detected": True,
                "kind": "url",
                "pattern": pat,
                "reason": f"URL matches verification pattern: {pat}",
            }

    # 2. DOM selector check in HTML (P2-5: Scope check to class/src/id attributes to avoid raw substring false positives)
    if lowered_html:
        for sel in CAPTCHA_DOM_SELECTORS:
            token = ""
            is_src = False
            if "[class*='" in sel:
                token = sel.split("[class*='")[1].split("']")[0]
            elif "[src*='" in sel:
                token = sel.split("[src*='")[1].split("']")[0]
                is_src = True
            elif sel.startswith("."):
                token = sel[1:]
            else:
                token = sel

            if not token:
                continue

            matched = False
            if is_src:
                matched = bool(re.search(rf"""src\s*=\s*['"][^'"]*{re.escape(token)}[^'"]*['"]""", lowered_html))
            else:
                matched = bool(re.search(rf"""(?:class|id)\s*=\s*['"][^'"]*{re.escape(token)}[^'"]*['"]""", lowered_html))

            if matched:
                return {
                    "detected": True,
                    "kind": "dom_selector",
                    "pattern": sel,
                    "reason": f"DOM contains verification widget selector: {sel}",
                }

    # 3. Text pattern matching
    for pat in CAPTCHA_TEXT_PATTERNS:
        if re.search(pat, lowered_text) or re.search(pat, lowered_html):
            return {
                "detected": True,
                "kind": "text",
                "pattern": pat,
                "reason": f"Text contains verification challenge phrase: '{pat}'",
            }

    return {"detected": False, "reason": "No CAPTCHA patterns detected"}


def save_evidence_screenshot(
    job_id: str,
    screenshot_data: str,
    output_dir: Optional[Path] = None,
    server_port: int = 8766,
    tag: str = "captcha",
) -> Optional[Dict[str, Any]]:
    """Decodes a base64 / data-URL screenshot and stores it in outputs/evidence/<job_id>_<tag>_<ts>.png."""
    if not job_id or not screenshot_data:
        return None

    try:
        raw_b64 = screenshot_data.strip()
        if "," in raw_b64 and "base64" in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]

        image_bytes = base64.b64decode(raw_b64)
        if not image_bytes:
            return None

        evidence_dir = (output_dir or Path("outputs")) / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        clean_tag = re.sub(r"[^a-zA-Z0-9_-]", "_", tag or "captcha")
        timestamp = int(time.time())
        filename = f"{job_id}_{clean_tag}_{timestamp}.png"
        file_path = evidence_dir / filename
        file_path.write_bytes(image_bytes)

        rel_path = f"outputs/evidence/{filename}"
        url = f"http://127.0.0.1:{server_port}/{rel_path}"

        width, height = None, None
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(image_bytes)) as img:
                width, height = img.size
        except Exception:
            pass

        return {
            "job_id": job_id,
            "tag": clean_tag,
            "filename": filename,
            "path": str(file_path),
            "rel_path": rel_path,
            "url": url,
            "evidence_file": str(file_path),
            "evidence_url": url,
            "size_bytes": len(image_bytes),
            "width": width,
            "height": height,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        logger.error("Failed to save evidence screenshot for %s (%s): %s", job_id, tag, exc)
        return None


def match_template_grayscale(
    image_path: Path,
    template_path: Path,
    threshold: float = 0.85,
) -> Tuple[bool, float]:
    """Lightweight template matching using Pillow (grayscale normalized cross-correlation / diff).

    Returns (is_match, similarity_score).
    """
    try:
        from PIL import Image

        if not image_path.is_file() or not template_path.is_file():
            return False, 0.0

        with Image.open(image_path) as img, Image.open(template_path) as tmpl:
            img_gray = img.convert("L")
            tmpl_gray = tmpl.convert("L")

            tw, th = tmpl_gray.size
            iw, ih = img_gray.size

            if iw < tw or ih < th:
                return False, 0.0

            if abs(iw - tw) <= 10 and abs(ih - th) <= 10:
                t_resized = tmpl_gray.resize((iw, ih))
                diff = 0
                pixels_img = list(img_gray.tobytes())
                pixels_tmpl = list(t_resized.tobytes())
                total = len(pixels_img)
                for p1, p2 in zip(pixels_img, pixels_tmpl):
                    diff += abs(p1 - p2)
                avg_diff = diff / (total * 255.0)
                sim = 1.0 - avg_diff
                return sim >= threshold, round(sim, 4)

            cx, cy = iw // 2, ih // 2
            box = (max(0, cx - tw // 2), max(0, cy - th // 2), min(iw, cx + tw // 2), min(ih, cy + th // 2))
            crop = img_gray.crop(box).resize((tw, th))
            diff = 0
            pixels_crop = list(crop.tobytes())
            pixels_tmpl = list(tmpl_gray.tobytes())
            total = len(pixels_crop)
            for p1, p2 in zip(pixels_crop, pixels_tmpl):
                diff += abs(p1 - p2)
            avg_diff = diff / (total * 255.0)
            sim = 1.0 - avg_diff
            return sim >= threshold, round(sim, 4)
    except Exception as exc:
        logger.warning("Template matching error: %s", exc)
        return False, 0.0
