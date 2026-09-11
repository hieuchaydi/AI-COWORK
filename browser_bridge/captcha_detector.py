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
) -> Optional[Dict[str, Any]]:
    """Decodes a base64 / data-URL screenshot and stores it in outputs/evidence/<job_id>_captcha_<ts>.png."""
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

        timestamp = int(time.time())
        filename = f"{job_id}_captcha_{timestamp}.png"
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
        logger.error("Failed to save evidence screenshot for %s: %s", job_id, exc)
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


def solve_puzzle_cv(
    screenshot_data: Any,
    canvas_rect: Optional[Dict[str, float]] = None,
    track_rect: Optional[Dict[str, float]] = None,
    handle_rect: Optional[Dict[str, float]] = None,
    piece_rect: Optional[Dict[str, float]] = None,
    device_pixel_ratio: float = 1.0,
) -> Dict[str, Any]:
    """Autonomous Computer Vision solver for Shopee CAPTCHA puzzle challenges.

    Analyzes screenshot data (or pre-cropped canvas) using OpenCV & Pillow:
    1. Extracts canvas crop based on CSS bounding box and devicePixelRatio (or auto-locates canvas).
    2. Uses multi-stage feature extraction:
       - Jigsaw notch & cutout slot detection (for natural colorful scenes).
       - Colored piece & recessed slot detection (e.g. floating red polygon into tablet slot).
       - Circular receptacle / pit detection (e.g. mortar & pestle).
       - Column gradient discontinuity projection fallback.
    3. Calculates precise horizontal drag displacement and scales to CSS pixels for Chrome Debugger.
    """
    try:
        import numpy as np
        import cv2
    except ImportError as err:
        logger.error("OpenCV or NumPy is not installed: %s", err)
        return {"ok": False, "error": "cv2_or_numpy_missing"}

    if screenshot_data is None:
        return {"ok": False, "error": "missing_screenshot_data"}

    # 1. Parse screenshot into BGR image
    img_bgr = None
    try:
        if isinstance(screenshot_data, np.ndarray):
            img_bgr = screenshot_data
        elif isinstance(screenshot_data, bytes):
            nparr = np.frombuffer(screenshot_data, np.uint8)
            img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(screenshot_data, str):
            raw = screenshot_data.strip()
            if "," in raw and "base64" in raw:
                raw = raw.split(",", 1)[1]
            img_bytes = base64.b64decode(raw)
            nparr = np.frombuffer(img_bytes, np.uint8)
            img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    except Exception as exc:
        logger.error("Failed to decode screenshot data: %s", exc)
        return {"ok": False, "error": f"decode_failed: {exc}"}

    if img_bgr is None or img_bgr.size == 0:
        return {"ok": False, "error": "invalid_image_data"}

    img_h, img_w, _ = img_bgr.shape
    dpr = max(0.2, float(device_pixel_ratio or 1.0))

    # 2. Extract canvas crop
    canvas_crop = None
    scale = dpr

    if canvas_rect and isinstance(canvas_rect, dict):
        css_x = float(canvas_rect.get("x", 0))
        css_y = float(canvas_rect.get("y", 0))
        css_w = float(canvas_rect.get("width", 0))
        css_h = float(canvas_rect.get("height", 0))

        if css_w > 20 and css_h > 20:
            x1 = max(0, int(round(css_x * dpr)))
            y1 = max(0, int(round(css_y * dpr)))
            x2 = min(img_w, int(round((css_x + css_w) * dpr)))
            y2 = min(img_h, int(round((css_y + css_h) * dpr)))
            if x2 > x1 + 30 and y2 > y1 + 30:
                canvas_crop = img_bgr[y1:y2, x1:x2]
                scale = (x2 - x1) / css_w

    # Auto-detection if canvas_rect missing or crop failed
    if canvas_crop is None or canvas_crop.size == 0:
        aspect = img_w / max(1, img_h)
        if 1.5 <= aspect <= 2.2 and img_w <= 600:
            canvas_crop = img_bgr
            scale = 1.0
        else:
            gray_all = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            edges_all = cv2.Canny(gray_all, 40, 120)
            cnts_all, _ = cv2.findContours(edges_all, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            best_candidate = None
            best_area = 0
            for c in cnts_all:
                x, y, w, h = cv2.boundingRect(c)
                a = w / max(1, h)
                area = w * h
                if 1.5 <= a <= 2.2 and 15000 <= area <= 200000 and area > best_area:
                    best_area = area
                    best_candidate = (x, y, w, h)
            if best_candidate:
                x, y, w, h = best_candidate
                canvas_crop = img_bgr[y:y+h, x:x+w]
                scale = 1.0
            else:
                canvas_crop = img_bgr
                scale = 1.0

    ch, cw, _ = canvas_crop.shape
    gray = cv2.cvtColor(canvas_crop, cv2.COLOR_BGR2GRAY)
    sobel_x = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))

    method = "fallback"
    confidence = 0.50
    target_x = float(cw * 0.65)
    piece_x = float(cw * 0.15)
    delta_x_phys = target_x - piece_x

    # 3. Piece detection (from DOM piece_rect or CV vertical edge pair in left quarter)
    detected_piece_x = None
    detected_piece_y = None
    detected_piece_w = None
    detected_piece_h = None

    if piece_rect and isinstance(piece_rect, dict) and canvas_rect and isinstance(canvas_rect, dict):
        try:
            p_x = float(piece_rect.get("x", 0)) - float(canvas_rect.get("x", 0))
            p_y = float(piece_rect.get("y", 0)) - float(canvas_rect.get("y", 0))
            p_w = float(piece_rect.get("width", 0))
            p_h = float(piece_rect.get("height", 0))
            if p_w >= 20 and p_h >= 20:
                detected_piece_x = max(0, int(round(p_x * scale)))
                detected_piece_y = max(0, int(round(p_y * scale)))
                detected_piece_w = int(round(p_w * scale))
                detected_piece_h = int(round(p_h * scale))
        except (TypeError, ValueError):
            pass

    if detected_piece_x is None:
        left_sobel = sobel_x[:, :int(cw * 0.28)]
        best_piece_score = -1.0
        best_y0 = int(ch * 0.3)
        best_x1, best_x2 = 0, 44

        for y in range(10, ch - 48, 4):
            band = left_sobel[y:y+44, :]
            prof = band.mean(axis=0)
            p_x1 = int(np.argmax(prof[:12]))
            p_x2 = int(35 + np.argmax(prof[35:min(55, prof.shape[0])]))
            score = float(prof[p_x1] + prof[p_x2])
            if score > best_piece_score:
                best_piece_score = score
                best_y0 = y
                best_x1, best_x2 = p_x1, p_x2

        detected_piece_x = best_x1
        detected_piece_y = best_y0
        detected_piece_w = max(30, best_x2 - best_x1)
        detected_piece_h = 44

    # 4. Multi-stage feature extraction
    # Inspect overall color saturation to differentiate natural photo puzzles (salads, goods) vs synthetic gray canvases
    hsv = cv2.cvtColor(canvas_crop, cv2.COLOR_BGR2HSV)
    mean_sat = float(np.mean(hsv[:, :, 1]))
    is_natural_scene = mean_sat > 40.0

    # Branch A: If natural colorful scene (e.g. salad, merchandise), prioritize Jigsaw Notch & Cutout Slot
    if is_natural_scene:
        piece_y0 = max(0, detected_piece_y)
        piece_y1 = min(ch, detected_piece_y + detected_piece_h)
        piece_w = detected_piece_w
        band_prof = sobel_x[piece_y0:piece_y1, :].mean(axis=0)
        gray_band = gray[piece_y0:piece_y1, :]

        best_cand_x = None
        best_cand_score = -1.0

        for tx in range(int(cw * 0.35), cw - piece_w - 5):
            edge_score = float(band_prof[tx] + band_prof[tx + piece_w])
            window_darkness = float(255.0 - gray_band[:, tx:tx+piece_w].mean())
            combined = edge_score * 1.0 + window_darkness * 1.2
            if combined > best_cand_score:
                best_cand_score = combined
                best_cand_x = tx

        if best_cand_x is not None and best_cand_x > detected_piece_x + 30:
            target_x = float(best_cand_x)
            piece_x = float(detected_piece_x)
            delta_x_phys = target_x - piece_x
            method = "jigsaw_notch"
            confidence = 0.96

    # Method 1: Colored Piece & Matching Slot (for synthetic gray canvases)
    if method == "fallback":
        b, g, r = cv2.split(canvas_crop)
        red_mask = (r > 150) & (r > g.astype(int) + 50) & (r > b.astype(int) + 50)
        red_mask[:, int(cw * 0.45):] = 0
        red_ys, red_xs = np.where(red_mask)
        if len(red_xs) > 30:
            piece_x = float(np.mean(red_xs))
            edges = cv2.Canny(gray, 30, 100)
            cnts, _ = cv2.findContours(edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            candidates = []
            for cnt in cnts:
                cx, cy, w, h = cv2.boundingRect(cnt)
                if cx > cw * 0.25 and 20 < w < cw * 0.55 and 8 < h < ch * 0.45:
                    candidates.append((cx + w / 2, cy + h / 2, w, h))
            if candidates:
                candidates.sort(key=lambda c: abs(c[1] - ch * 0.65))
                target_x = candidates[0][0]
                delta_x_phys = target_x - piece_x
                method = "colored_piece_slot"
                confidence = 0.94

    # Method 2: Circular Receptacle / Pit Insertion (e.g. Mortar & Pestle)
    if method == "fallback":
        blur = cv2.GaussianBlur(gray, (9, 9), 2)
        circles = cv2.HoughCircles(
            blur, cv2.HOUGH_GRADIENT,
            dp=1.2, minDist=30,
            param1=80, param2=30,
            minRadius=int(ch * 0.15), maxRadius=int(ch * 0.55)
        )
        if circles is not None:
            valid_circles = [c for c in circles[0] if c[0] > cw * 0.35]
            if valid_circles:
                best_circle = max(valid_circles, key=lambda c: c[2])
                cx, cy, cr = float(best_circle[0]), float(best_circle[1]), float(best_circle[2])
                search_r = int(cr * 0.45)
                x1 = max(0, int(cx - search_r))
                x2 = min(cw, int(cx + search_r))
                y1 = max(0, int(cy - search_r))
                y2 = min(ch, int(cy + search_r))
                roi = gray[y1:y2, x1:x2]
                if roi.size > 0:
                    min_val, _, min_loc, _ = cv2.minMaxLoc(cv2.GaussianBlur(roi, (5, 5), 0))
                    # Check for movable pestle on left
                    left_roi = gray[:, :int(cw * 0.45)]
                    edges_left = cv2.Canny(left_roi, 30, 100)
                    cnts_left, _ = cv2.findContours(edges_left, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    if cnts_left:
                        best_cnt = max(cnts_left, key=cv2.contourArea)
                        lx, ly, lw, lh = cv2.boundingRect(best_cnt)
                        cand_target_x = float(x1 + min_loc[0]) if min_val < 90 else cx
                        cand_piece_x = float(lx + lw)
                        cand_delta = cand_target_x - cand_piece_x
                        if cand_delta > 30 and min_val < 90:
                            target_x = cand_target_x
                            piece_x = cand_piece_x
                            delta_x_phys = cand_delta
                            method = "circular_receptacle"
                            confidence = 0.95

    # Method 3: Robust Jigsaw Notch & Cutout Slot (General Fallback)
    if method == "fallback":
        piece_y0 = max(0, detected_piece_y)
        piece_y1 = min(ch, detected_piece_y + detected_piece_h)
        piece_w = detected_piece_w
        band_prof = sobel_x[piece_y0:piece_y1, :].mean(axis=0)
        gray_band = gray[piece_y0:piece_y1, :]

        best_cand_x = None
        best_cand_score = -1.0

        for tx in range(int(cw * 0.35), cw - piece_w - 5):
            edge_score = float(band_prof[tx] + band_prof[tx + piece_w])
            window_darkness = float(255.0 - gray_band[:, tx:tx+piece_w].mean())
            combined = edge_score * 1.0 + window_darkness * 1.2
            if combined > best_cand_score:
                best_cand_score = combined
                best_cand_x = tx

        if best_cand_x is not None and best_cand_x > detected_piece_x + 20:
            target_x = float(best_cand_x)
            piece_x = float(detected_piece_x)
            delta_x_phys = target_x - piece_x
            method = "jigsaw_notch"
            confidence = 0.93

    # Method 4: Column Gradient Discontinuity Fallback
    if method == "fallback":
        sobel_x = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
        col_energy = np.sum(sobel_x[int(ch * 0.2):int(ch * 0.8), :], axis=0)
        min_x = int(cw * 0.35)
        max_x = int(cw * 0.85)
        target_x = float(min_x + np.argmax(col_energy[min_x:max_x]))
        piece_x = cw * 0.18
        delta_x_phys = target_x - piece_x
        method = "gradient_column_fallback"
        confidence = 0.70

    # 4. Convert to CSS travel with exact track-to-canvas scale ratio
    canvas_css_w = float(canvas_rect.get("width") if canvas_rect else 280.0)
    track_width_css = float(track_rect.get("width") if track_rect else canvas_css_w)
    handle_width_css = float(handle_rect.get("width") if handle_rect else 40.0)
    piece_css_w = float(detected_piece_w / max(0.1, scale)) if detected_piece_w else 44.0

    max_piece_travel = max(10.0, canvas_css_w - piece_css_w)
    max_handle_travel = max(50.0, track_width_css - handle_width_css)

    # Only apply proportional scaling if track width materially differs from canvas width
    if abs(track_width_css - canvas_css_w) > 5.0 and max_piece_travel > 0:
        scale_ratio = max_handle_travel / max_piece_travel
    else:
        scale_ratio = 1.0

    puzzle_travel_css = delta_x_phys / max(0.1, scale)
    handle_travel_css = puzzle_travel_css * scale_ratio

    bounded_travel = int(round(max(20.0, min(handle_travel_css, max_handle_travel - 3.0))))

    return {
        "ok": True,
        "travel": bounded_travel,
        "puzzle_travel": int(round(puzzle_travel_css)),
        "max_travel": int(round(max_handle_travel)),
        "scale_ratio": round(scale_ratio, 3),
        "method": method,
        "confidence": round(confidence, 2),
        "details": {
            "target_x_phys": round(target_x, 1),
            "piece_x_phys": round(piece_x, 1),
            "delta_x_phys": round(delta_x_phys, 1),
            "scale": round(scale, 3),
            "canvas_w_phys": cw,
            "canvas_h_phys": ch,
        },
    }

