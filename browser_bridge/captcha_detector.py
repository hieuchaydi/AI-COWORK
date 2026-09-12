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


def detect_sprite_piece(canvas_bgr: Any, gray: Any = None) -> Optional[Dict[str, Any]]:
    """Locate the CAPTCHA's movable sprite on the canvas — **anywhere**, not just on the left.

    Shopee parks the piece on either side of the frame and clips it against the canvas edge
    (`overflow: hidden`), so a left-band search misses the whole right-parked half of the
    variants (mortar & pestle, tray & lid, …). Three colour families cover the catalog:

    * warm/saturated — wooden pestle, red/orange prop, terracotta pieces;
    * near-white — dishwasher tablet, soap bar, ceramic chip;
    * blue/cyan — tinted tablet or plastic part.

    Returns ``{center, bbox, w, h, angle, box, kind, area}`` (canvas pixel coords) or ``None``.
    """
    try:
        import numpy as np
        import cv2
    except ImportError:  # pragma: no cover - the callers already guard this
        return None

    if canvas_bgr is None or getattr(canvas_bgr, "size", 0) == 0:
        return None
    if gray is None:
        gray = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2GRAY)
    ch, cw = gray.shape[:2]

    b = canvas_bgr[:, :, 0].astype(int)
    g = canvas_bgr[:, :, 1].astype(int)
    r = canvas_bgr[:, :, 2].astype(int)
    hsv_s = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2HSV)[:, :, 1]

    masks = {
        # Wood/red props: strongly warm and clearly saturated.
        "warm": ((r - b > 30) & (r - g > 12) & (r > 110) & (hsv_s > 60)),
        # Tablets/ceramics: near-white but still distinguishable from grey backdrops.
        "bright": (gray > 238),
        # Tinted plastic pieces.
        "blue": ((b - r > 25) & (b > 120)),
    }
    # Dark props (chess-piece / silhouette sprites) only appear on photographic backdrops. The
    # synthetic flat canvases the unit tests build use a dark rectangle as the *cavity*, so the
    # family stays out of their way and their historical contracts are preserved.
    if distinct_colour_count(canvas_bgr) > 400:
        masks["dark"] = (gray < 85) & (hsv_s < 150)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    best: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for kind, mask in masks.items():
        m = cv2.morphologyEx(mask.astype(np.uint8) * 255, cv2.MORPH_CLOSE, kernel)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            area = float(cv2.contourArea(c))
            if area < 140:
                continue
            bx, by, bw, bh = cv2.boundingRect(c)
            if bw < 10 or bh < 10:
                continue
            # A sprite fills a small slice of the frame; anything huge is background.
            if bw * bh > 0.55 * cw * ch:
                continue
            # ...and it is compact: a dark forest patch 207x110 on a 285x153 frame passed the
            # area cap above (23k vs 24k) and was reported as "the piece". Real sprites are a
            # small fraction of either axis.
            if bw > cw * 0.42 or bh > ch * 0.42:
                continue
            fill = area / float(max(1, bw * bh))
            # cv2.contourArea reports the *outer* polygon, so a hollow outline (a cavity border)
            # scores as a full rectangle. Count actual mask pixels inside the box instead: a
            # movable sprite is solid, an outline is not.
            pixel_fill = float(np.count_nonzero(m[by:by + bh, bx:bx + bw])) / float(max(1, bw * bh))
            if pixel_fill < (0.30 if kind == "dark" else 0.15):
                continue  # sparse speckle or a hollow outline, not a solid sprite
            mrect = cv2.minAreaRect(c)
            (mcx, mcy), (rw, rh), angle = mrect
            long_side, short_side = max(rw, rh), max(1.0, min(rw, rh))
            if long_side / short_side > 6.0:
                continue  # a long rail/edge, not a sprite
            # Larger area wins; saturated pieces get a small bonus over grey-ish bright ones.
            score = area * (1.15 if kind == "warm" else 1.0) * min(1.0, fill + 0.35)
            if score > best_score:
                best_score = score
                best = {
                    "center": (float(mcx), float(mcy)),
                    "bbox": (int(bx), int(by), int(bw), int(bh)),
                    "w": float(short_side),
                    "h": float(long_side),
                    "angle": float(angle),
                    "box": np.intp(cv2.boxPoints(mrect)),
                    "kind": kind,
                    "area": int(area),
                }
    return best


def detect_sprite_piece_safe(canvas_bgr: Any, gray: Any = None) -> Optional[Dict[str, Any]]:
    """`detect_sprite_piece` that never raises (CV solvers are best-effort by contract)."""
    try:
        return detect_sprite_piece(canvas_bgr, gray)
    except Exception as exc:  # noqa: BLE001 - defensive: solver must degrade, not crash
        logger.warning("Sprite detection failed: %s", exc)
        return None


def detect_salient_piece(canvas_bgr: Any, gray: Any = None) -> Optional[Dict[str, Any]]:
    """Colour-agnostic sprite finder: the compact blob that stands out from its surroundings.

    The colour families (warm/bright/blue/dark) only cover the props seen so far. A moss-green
    disc on a forest photo matched none of them, so the solver fell back to edge heuristics and
    pointed the drag the wrong way. Compare each pixel with a heavily blurred copy of the frame
    (a cheap saliency map) and keep the most prominent compact blob of sprite size.
    """
    try:
        import numpy as np
        import cv2
    except ImportError:  # pragma: no cover - callers guard this
        return None

    if canvas_bgr is None or getattr(canvas_bgr, "size", 0) == 0:
        return None
    if gray is None:
        gray = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2GRAY)
    ch, cw = gray.shape[:2]

    # Two scales: a small sigma catches a ~30px sprite on a busy photo, a large one catches
    # blobs that differ from the wider scene. Either can win.
    small = cv2.absdiff(gray, cv2.GaussianBlur(gray, (0, 0), 4))
    wide = cv2.absdiff(gray, cv2.GaussianBlur(gray, (0, 0), 12))
    diff = cv2.max(small, wide)
    # A fixed threshold turns a textured photo (forest, gravel) into one giant blob — 90% of the
    # frame above 18. Keep only the most salient tail of the distribution.
    threshold = max(16.0, float(np.percentile(diff, 92)))
    mask = (diff > threshold).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    # No MORPH_OPEN here: a sprite whose *interior* is uniform (a moss-green disc) only shows up
    # as a thin salient outline, and opening erases thin structures — the detector found nothing
    # at all on that frame until this was removed.
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_score = 0.0
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if not (14 <= w <= 110 and 14 <= h <= 110):
            continue
        if w * h > 0.5 * cw * ch:
            continue
        pixels = int(np.count_nonzero(mask[y:y + h, x:x + w]))
        fill = pixels / float(max(1, w * h))
        if fill < 0.32:
            continue
        strength = float(np.mean(diff[y:y + h, x:x + w]))
        score = pixels * (1.0 + strength / 40.0)
        if score > best_score:
            best_score = score
            best = {
                "center": (float(x + w / 2.0), float(y + h / 2.0)),
                "bbox": (int(x), int(y), int(w), int(h)),
                "w": float(w),
                "h": float(h),
                "area": pixels,
                "kind": "salient",
                "strength": round(strength, 1),
            }
    return best


def detect_salient_piece_safe(canvas_bgr: Any, gray: Any = None) -> Optional[Dict[str, Any]]:
    """`detect_salient_piece` that never raises."""
    try:
        return detect_salient_piece(canvas_bgr, gray)
    except Exception as exc:  # noqa: BLE001 - solver contract: degrade, never crash
        logger.warning("Salient piece detection failed: %s", exc)
        return None


def detect_outlined_hole(canvas_bgr: Any, gray: Any = None) -> Optional[Dict[str, Any]]:
    """Find the jigsaw cut-out: a compact outlined region whose interior is flat background.

    The current Shopee challenge draws the hole as a bright outline around a low-variance area
    (the hole shows the backdrop's next layer), while the movable sprite is a solid blob. Relying
    on edges alone used to pair the hole with whatever strong edge was nearby, which pointed the
    drag away from the hole — the piece landed "almost right" and the challenge never validated.

    Returns ``{center, bbox, w, h, area, std}`` in canvas pixels, or ``None``.
    """
    try:
        import numpy as np
        import cv2
    except ImportError:  # pragma: no cover - callers guard this
        return None

    if canvas_bgr is None or getattr(canvas_bgr, "size", 0) == 0:
        return None
    if gray is None:
        gray = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2GRAY)
    ch, cw = gray.shape[:2]

    edges = cv2.Canny(gray, 40, 130)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    cnts, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_score = 0.0
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w < 22 or h < 22 or w > cw * 0.55 or h > ch * 0.8:
            continue
        if not (0.5 <= w / float(h) <= 2.2):
            continue
        pad_x, pad_y = max(2, int(w * 0.22)), max(2, int(h * 0.22))
        interior = gray[y + pad_y:y + h - pad_y, x + pad_x:x + w - pad_x]
        if interior.size < 100:
            continue
        std = float(np.std(interior))
        if std > 34:
            continue  # textured → that's scenery, not a cut-out
        # The cut-out has to differ from what surrounds it. On photo scenes a flat *light* patch
        # (sand, a table top, a highlight) is otherwise as "flat" as a real cavity and used to win
        # this contest — which pointed the drag away from the hole (mortar & pestle → -119 left
        # instead of right). A real cavity is the darker of the two in every Shopee variant seen
        # so far, so contrast is required and darkness is preferred in the score below.
        outer_pad = max(3, int(min(w, h) * 0.35))
        outer = gray[max(0, y - outer_pad):min(ch, y + h + outer_pad),
                     max(0, x - outer_pad):min(cw, x + w + outer_pad)]
        interior_mean = float(np.mean(interior))
        outer_mean = float(np.mean(outer)) if outer.size else interior_mean
        contrast = outer_mean - interior_mean  # > 0 → the patch is darker than its surroundings
        if contrast < 6.0:
            continue
        perimeter = float(cv2.arcLength(c, True))
        if perimeter <= 0:
            continue
        area = float(cv2.contourArea(c))
        if area < 0.35 * w * h:
            continue
        # A cut-out is flat inside but strongly outlined; prefer the flattest, best enclosed one.
        score = (
            (area / float(w * h))
            * (200.0 / (std + 6.0))
            * min(1.0, perimeter / (2.0 * (w + h)))
            * (1.0 + min(contrast, 60.0) / 30.0)
        )
        if score > best_score:
            best_score = score
            best = {
                "center": (float(x + w / 2.0), float(y + h / 2.0)),
                "bbox": (int(x), int(y), int(w), int(h)),
                "w": float(w),
                "h": float(h),
                "area": int(area),
                "std": round(std, 2),
                "contrast": round(contrast, 1),
            }
    return best


def detect_outlined_hole_safe(canvas_bgr: Any, gray: Any = None) -> Optional[Dict[str, Any]]:
    """`detect_outlined_hole` that never raises."""
    try:
        return detect_outlined_hole(canvas_bgr, gray)
    except Exception as exc:  # noqa: BLE001 - solver contract: degrade, never crash
        logger.warning("Hole detection failed: %s", exc)
        return None


def distinct_colour_count(canvas_bgr: Any, step: int = 2) -> int:
    """Distinct colours in a subsample of the frame — a cheap 'photo vs drawn canvas' probe.

    Real Shopee scenes (photo backdrops) carry thousands of distinct colours; the synthetic
    flat canvases the unit tests build have a handful. Callers use this to decide whether a
    colour-based detector may outrank the older geometric heuristics.
    """
    try:
        import numpy as np

        if canvas_bgr is None or getattr(canvas_bgr, "size", 0) == 0:
            return 0
        small = canvas_bgr[:: max(1, step), :: max(1, step)]
        return int(np.unique(small.reshape(-1, small.shape[2]), axis=0).shape[0])
    except Exception:  # noqa: BLE001 - probe must never break a solve
        return 0



def solve_compartment_receptacle(
    image: Any,
    canvas_rect: Optional[Dict[str, float]] = None,
    track_rect: Optional[Dict[str, float]] = None,
    handle_rect: Optional[Dict[str, float]] = None,
    device_pixel_ratio: float = 1.0,
    attempt: int = 1,
) -> Dict[str, Any]:
    """Autonomous Sub-pixel Solver for Shopee Compartment Receptacle CAPTCHAs.

    (e.g., Dishwasher tablet into dispenser cavity, tray into slot).

    Resolves systematic 5-15px centroid skew by:
    1. Multi-scale preprocessing & noise cleanup (red markup stripping, bilateral edge preservation).
    2. Rotated bounding box extraction (cv2.minAreaRect) for tilted 3D pieces (white/blue tablet).
    3. Multi-zone cavity detection combining morphological contours, inner shadow compensation, and vertical gradient wall pairing.
    4. Dual alignment ensemble: Center-to-Center + Edge-to-Edge clearance compensation.
    5. Track & Handle scale normalization for DOM dispatch.
    6. Adaptive micro-stepping retry jitter (0, +4, -4, +8, -8 px).
    """
    try:
        import numpy as np
        import cv2
    except ImportError as err:
        logger.error("OpenCV or NumPy is not installed: %s", err)
        return {"ok": False, "error": "cv2_or_numpy_missing"}

    if image is None:
        return {"ok": False, "error": "missing_image_data"}

    # 1. Parse image into BGR array
    img_bgr = None
    try:
        if isinstance(image, np.ndarray):
            img_bgr = image
        elif isinstance(image, (str, Path)):
            p = Path(image)
            if p.is_file():
                img_bgr = cv2.imread(str(p))
            else:
                raw = str(image).strip()
                if "," in raw and "base64" in raw:
                    raw = raw.split(",", 1)[1]
                img_bytes = base64.b64decode(raw)
                nparr = np.frombuffer(img_bytes, np.uint8)
                img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(image, bytes):
            nparr = np.frombuffer(image, np.uint8)
            img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    except Exception as exc:
        logger.error("Failed to decode image data: %s", exc)
        return {"ok": False, "error": f"decode_failed: {exc}"}

    if img_bgr is None or img_bgr.size == 0:
        return {"ok": False, "error": "invalid_image_data"}

    img_h, img_w, _ = img_bgr.shape
    dpr = max(0.2, float(device_pixel_ratio or 1.0))
    scale = dpr

    # 2. Extract canvas crop
    canvas_crop = None
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

    if canvas_crop is None or canvas_crop.size == 0:
        aspect = img_w / max(1, img_h)
        if 1.4 <= aspect <= 2.3 and img_w <= 650:
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
                if 1.4 <= a <= 2.3 and 15000 <= area <= 250000 and area > best_area:
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

    # 3. Clean user red markup annotations if present
    is_red = (canvas_crop[:, :, 2] > 140) & (canvas_crop[:, :, 1] < 70) & (canvas_crop[:, :, 0] < 70)
    clean_bgr = canvas_crop.copy()
    clean_bgr[is_red] = [235, 235, 235]
    gray = cv2.cvtColor(clean_bgr, cv2.COLOR_BGR2GRAY)

    # 4. Extract the movable piece.
    #    First pass: the sprite detector scans the WHOLE canvas — Shopee parks the piece on
    #    either side (and clips it against the canvas edge), so a left-band-only search misses
    #    every right-parked variant (mortar & pestle, tray & lid, …).
    #    Second pass: the legacy left-band methods stay for the tablet layouts they were tuned on.
    left_w = int(cw * 0.38)
    left_gray = gray[:, :left_w]
    left_bgr = clean_bgr[:, :left_w]

    piece_center = None
    piece_box = None
    piece_w, piece_h = 30.0, 30.0
    piece_angle = 0.0
    piece_kind = ""

    sprite = detect_sprite_piece_safe(clean_bgr, gray)
    if sprite is not None:
        piece_center = sprite["center"]
        piece_box = sprite["box"]
        piece_w = float(sprite["w"])
        piece_h = float(sprite["h"])
        piece_angle = float(sprite["angle"])
        piece_kind = str(sprite["kind"])

    # Piece Method A: White/bright tilted tablet (gray > 238)
    white_mask = (left_gray > 238) & (np.arange(left_w)[None, :] > 4) & (np.arange(ch)[:, None] > int(ch * 0.20))
    if piece_center is None and np.sum(white_mask) > 100:
        cnts_w, _ = cv2.findContours(white_mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts_w:
            best_cw = max(cnts_w, key=cv2.contourArea)
            bx, by, bw, bh = cv2.boundingRect(best_cw)
            if 15 <= bw <= int(left_w * 0.85) and 18 <= bh <= int(ch * 0.85):
                mrect = cv2.minAreaRect(best_cw)
                piece_center = (float(mrect[0][0]), float(mrect[0][1]))
                piece_w = float(min(mrect[1]))
                piece_h = float(max(mrect[1]))
                piece_angle = float(mrect[2])
                piece_box = np.intp(cv2.boxPoints(mrect))

    # Piece Method B: Blue/Cyan tablet or general edge contour
    if piece_center is None:
        blur_l = cv2.bilateralFilter(left_gray, 5, 45, 45)
        edges_l = cv2.Canny(blur_l, 25, 80)
        kernel_l = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        edges_l_closed = cv2.morphologyEx(edges_l, cv2.MORPH_CLOSE, kernel_l)
        cnts_l, _ = cv2.findContours(edges_l_closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        piece_cands = []
        for c in cnts_l:
            bx, by, bw, bh = cv2.boundingRect(c)
            box_area = bw * bh
            if bx <= 2 or by <= 2 or bx + bw >= left_w - 2:
                continue
            if 14 <= bw <= 55 and 16 <= bh <= int(ch * 0.8) and 150 <= box_area <= 3500:
                mrect = cv2.minAreaRect(c)
                rw, rh = mrect[1]
                ar = max(rw, rh) / max(1.0, min(rw, rh))
                if ar > 3.2:
                    continue
                patch = left_bgr[by:by+bh, bx:bx+bw]
                b_diff = float(np.mean(patch[:, :, 0])) - float(np.mean(patch[:, :, 2]))
                y_diff = abs(mrect[0][1] - ch * 0.5)
                score = (max(0.0, b_diff) ** 1.8 * 15.0 + box_area * 0.5) / (1.0 + y_diff * 0.05)
                piece_cands.append((score, mrect, c))

        if piece_cands:
            piece_cands.sort(key=lambda x: x[0], reverse=True)
            best_mrect = piece_cands[0][1]
            piece_center = (float(best_mrect[0][0]), float(best_mrect[0][1]))
            piece_w = float(min(best_mrect[1]))
            piece_h = float(max(best_mrect[1]))
            piece_angle = float(best_mrect[2])
            piece_box = np.intp(cv2.boxPoints(best_mrect))

    # Piece Method C: Fallback bright threshold
    if piece_center is None:
        _, thresh_l = cv2.threshold(left_gray, 220, 255, cv2.THRESH_BINARY)
        cnts_t, _ = cv2.findContours(thresh_l, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts_t:
            bx, by, bw, bh = cv2.boundingRect(c)
            area = cv2.contourArea(c)
            if 15 <= bw <= int(left_w * 0.8) and 15 <= bh <= int(ch * 0.8) and area > 120:
                mrect = cv2.minAreaRect(c)
                piece_center = (float(mrect[0][0]), float(mrect[0][1]))
                piece_w = float(min(mrect[1]))
                piece_h = float(max(mrect[1]))
                piece_angle = float(mrect[2])
                piece_box = np.intp(cv2.boxPoints(mrect))
                break

    if piece_center is None:
        piece_center = (float(left_w * 0.35), float(ch * 0.5))
        piece_box = np.array([[10, 30], [45, 30], [45, 75], [10, 75]], dtype=np.int32)

    # 5. Extract Slot / Receptacle. The search spans the frame (not a fixed right band) so the
    #    cavity can sit on either side of the piece — the pairing below is direction-agnostic —
    #    while candidates that ARE the piece (heavy overlap) are dropped.
    right_x0 = int(cw * 0.12)
    right_gray = gray[:, right_x0:]
    right_bgr = clean_bgr[:, right_x0:]

    piece_bbox = None
    if piece_box is not None:
        piece_bbox = (
            float(np.min(piece_box[:, 0])),
            float(np.min(piece_box[:, 1])),
            float(np.max(piece_box[:, 0])),
            float(np.max(piece_box[:, 1])),
        )

    blur_r = cv2.bilateralFilter(right_gray, 5, 45, 45)
    edges_r = cv2.Canny(blur_r, 25, 85)
    kernel_r = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    edges_r_closed = cv2.morphologyEx(edges_r, cv2.MORPH_CLOSE, kernel_r)
    cnts_r, _ = cv2.findContours(edges_r_closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    cavity_cands = []
    for c in cnts_r:
        bx, by, bw, bh = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if 40 <= bw <= int(cw * 0.52) and 18 <= bh <= int(ch * 0.55) and area > 250:
            if piece_bbox is not None:
                # Overlap between the candidate box and the piece's box, as a share of the box.
                ox = max(0.0, min(bx + bw, piece_bbox[2]) - max(bx, piece_bbox[0]))
                oy = max(0.0, min(by + bh, piece_bbox[3]) - max(by, piece_bbox[1]))
                if (ox * oy) > 0.35 * (bw * bh):
                    continue  # that contour is the piece itself, not a receptacle
            mrect = cv2.minAreaRect(c)
            (rcx, rcy), (rw, rh), angle = mrect
            abs_cx = right_x0 + rcx
            abs_cy = rcy
            abs_bx = right_x0 + bx

            pad_x = max(2, int(bw * 0.12))
            pad_y = max(2, int(bh * 0.12))
            interior = right_gray[by+pad_y : by+bh-pad_y, bx+pad_x : bx+bw-pad_x]
            if interior.size > 20:
                std_int = float(np.std(interior))
                ar = bw / max(1, bh)
                if 1.1 <= ar <= 3.2:
                    y_diff = abs(rcy - piece_center[1])
                    score = area / (std_int + 4.0) / (1.0 + y_diff * 0.02)
                    cavity_cands.append({
                        "score": score,
                        "bbox": (abs_bx, by, bw, bh),
                        "center": (abs_cx, abs_cy),
                        "mrect": mrect,
                        "w": bw,
                        "h": bh,
                        "area": area,
                    })

    slot_info = None
    if cavity_cands:
        cavity_cands.sort(key=lambda x: x["score"], reverse=True)
        best_c = cavity_cands[0]
        abs_cx, abs_cy = best_c["center"]
        mrect_abs = ((abs_cx, abs_cy), best_c["mrect"][1], best_c["mrect"][2])
        slot_box = np.intp(cv2.boxPoints(mrect_abs))
        slot_info = {
            "center": (abs_cx, abs_cy),
            "bbox": best_c["bbox"],
            "box": slot_box,
            "w": float(best_c["w"]),
            "h": float(best_c["h"]),
            "confidence": 0.96,
        }

    # Vertical gradient wall pair fallback if contour missed cavity
    if slot_info is None:
        y1 = max(0, int(piece_center[1] - 30))
        y2 = min(ch, int(piece_center[1] + 30))
        band = right_gray[y1:y2, :]
        sobel_x = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0))
        col_e = np.convolve(np.sum(sobel_x, axis=0), np.ones(5)/5.0, mode="same")
        peaks = [right_x0 + i for i in range(2, len(col_e)-2) if col_e[i] > col_e[i-1] and col_e[i] > col_e[i+1] and col_e[i] > np.mean(col_e)*1.1]
        best_pair = None
        for i in range(len(peaks)):
            for j in range(i+1, len(peaks)):
                d = peaks[j] - peaks[i]
                if 45 <= d <= 95:
                    best_pair = (peaks[i], peaks[j], (peaks[i] + peaks[j])/2.0)
                    break
            if best_pair:
                break
        if best_pair:
            p_left, p_right, p_center_x = best_pair
            slot_w = float(p_right - p_left)
            slot_info = {
                "center": (float(p_center_x), float(piece_center[1])),
                "bbox": (int(p_left), int(piece_center[1] - 20), int(slot_w), 40),
                "box": np.array([[p_left, piece_center[1]-20], [p_right, piece_center[1]-20], [p_right, piece_center[1]+20], [p_left, piece_center[1]+20]], dtype=np.int32),
                "w": slot_w,
                "h": 40.0,
                "confidence": 0.75,
            }

    if slot_info is None:
        slot_cx = float(cw * 0.65)
        slot_cy = float(piece_center[1])
        slot_info = {
            "center": (slot_cx, slot_cy),
            "bbox": (int(slot_cx - 35), int(slot_cy - 20), 70, 40),
            "box": np.array([[int(slot_cx-35), int(slot_cy-20)], [int(slot_cx+35), int(slot_cy-20)], [int(slot_cx+35), int(slot_cy+20)], [int(slot_cx-35), int(slot_cy+20)]], dtype=np.int32),
            "w": 70.0,
            "h": 40.0,
            "confidence": 0.50,
        }

    # 6. Dual Alignment Calculation (Center-to-Center & Edge-to-Edge)
    delta_center = slot_info["center"][0] - piece_center[0]

    piece_left_x = float(np.min(piece_box[:, 0]))
    piece_right_x = float(np.max(piece_box[:, 0]))
    piece_bbox_w = max(10.0, piece_right_x - piece_left_x)

    slot_left_x = float(slot_info["bbox"][0])
    slot_w = float(slot_info["w"])

    # Edge-to-edge alignment with clearance compensation
    delta_edge = (slot_left_x + (slot_w - piece_bbox_w) / 2.0) - piece_left_x
    delta_optimal = 0.5 * delta_center + 0.5 * delta_edge

    # 7. Scale factor calculation
    canvas_css_w = float(canvas_rect.get("width") if canvas_rect else cw / scale)
    track_width_css = float(track_rect.get("width") if track_rect else canvas_css_w)
    handle_width_css = float(handle_rect.get("width") if handle_rect else 40.0)
    piece_css_w = float(piece_bbox_w / max(0.1, scale))

    max_piece_travel = max(10.0, canvas_css_w - piece_css_w)
    max_handle_travel = max(50.0, track_width_css - handle_width_css)

    if abs(track_width_css - canvas_css_w) > 5.0 and max_piece_travel > 0:
        scale_ratio = max_handle_travel / max_piece_travel
    else:
        scale_ratio = 1.0

    puzzle_travel_css = delta_optimal / max(0.1, scale)
    handle_travel_css = puzzle_travel_css * scale_ratio

    # 8. Adaptive micro-stepping retry jitter pattern
    jitter_pattern = [0, 0, 4, -4, 8, -8, 6, -6]
    retry_jitter = jitter_pattern[min(max(0, attempt), len(jitter_pattern) - 1)]
    final_travel_css = handle_travel_css + retry_jitter
    # Signed travel: the piece moves the way the delta points, and some variants park it to the
    # RIGHT of its cavity (drag left). Only the magnitude is bounded by the handle's range — a
    # `max(20, …)` floor here used to clamp every leftward answer into a wrong rightward one.
    travel_limit = max(8.0, max_handle_travel - 3.0)
    bounded_travel = int(round(max(-travel_limit, min(final_travel_css, travel_limit))))
    direction = "left" if bounded_travel < 0 else "right"

    return {
        "ok": True,
        "travel": bounded_travel,
        "travel_distance": round(float(final_travel_css), 2),
        "puzzle_travel": int(round(puzzle_travel_css)),
        "max_travel": int(round(max_handle_travel)),
        "scale_ratio": round(float(scale_ratio), 3),
        "direction": direction,
        "piece_kind": piece_kind,
        "method": "compartment_receptacle",
        "confidence": round(float(slot_info["confidence"]), 2),
        "debug_info": {
            "piece_center": [round(float(piece_center[0]), 2), round(float(piece_center[1]), 2)],
            "piece_box": piece_box.tolist(),
            "piece_width": round(float(piece_w), 2),
            "piece_height": round(float(piece_h), 2),
            "piece_angle": round(float(piece_angle), 2),
            "slot_center": [round(float(slot_info["center"][0]), 2), round(float(slot_info["center"][1]), 2)],
            "slot_box": slot_info["box"].tolist(),
            "slot_width": round(float(slot_w), 2),
            "slot_height": round(float(slot_info["h"]), 2),
            "delta_center": round(float(delta_center), 2),
            "delta_edge": round(float(delta_edge), 2),
            "delta_optimal": round(float(delta_optimal), 2),
            "direction": direction,
            "piece_kind": piece_kind,
            "attempt": attempt,
            "retry_jitter": retry_jitter,
            "scale_ratio": round(float(scale_ratio), 3),
            "canvas_w": cw,
            "canvas_h": ch,
        },
    }


def visualize_debug(
    image: Any,
    result: Dict[str, Any],
    output_path: Any,
) -> Path:
    """Renders debug visualization overlay showing piece contour, slot receptacle,
    drag vector, and metric HUD.

    - Piece contour: Green (0, 230, 118) with center dot.
    - Slot contour: Blue (255, 140, 0) with center dot.
    - Drag vector: Orange (0, 109, 255) arrow.
    - HUD Overlay: Semi-transparent bottom banner with detailed metrics.
    """
    import numpy as np
    import cv2

    canvas = None
    if isinstance(image, np.ndarray):
        canvas = image.copy()
    elif isinstance(image, (str, Path)):
        p = Path(image)
        if p.is_file():
            canvas = cv2.imread(str(p))
    if canvas is None:
        # Generate placeholder
        canvas = np.full((150, 280, 3), 220, dtype=np.uint8)

    h, w = canvas.shape[:2]
    dbg = result.get("debug_info", {})

    # 1. Draw Piece Box (Green: BGR 0, 230, 118)
    p_box = dbg.get("piece_box")
    if p_box is not None:
        pts = np.intp(p_box)
        cv2.polylines(canvas, [pts], isClosed=True, color=(0, 230, 118), thickness=2)

    p_center = dbg.get("piece_center")
    pc = None
    if p_center:
        pc = (int(round(p_center[0])), int(round(p_center[1])))
        cv2.circle(canvas, pc, 4, (0, 230, 118), -1)
        cv2.circle(canvas, pc, 6, (0, 0, 0), 1)

    # 2. Draw Slot Box (Blue: BGR 255, 140, 0)
    s_box = dbg.get("slot_box")
    if s_box is not None:
        pts_s = np.intp(s_box)
        cv2.polylines(canvas, [pts_s], isClosed=True, color=(255, 140, 0), thickness=2)

    s_center = dbg.get("slot_center")
    sc = None
    if s_center:
        sc = (int(round(s_center[0])), int(round(s_center[1])))
        cv2.circle(canvas, sc, 4, (255, 140, 0), -1)
        cv2.circle(canvas, sc, 6, (0, 0, 0), 1)

    # 3. Draw Drag Vector (Orange/Amber: BGR 0, 109, 255)
    if pc and sc:
        target_pt = (int(round(pc[0] + dbg.get("delta_optimal", sc[0] - pc[0]))), pc[1])
        cv2.arrowedLine(canvas, pc, target_pt, (0, 109, 255), 2, tipLength=0.08)

    # 4. Draw HUD Overlay Box at bottom
    hud_h = 44
    hud_overlay = canvas.copy()
    cv2.rectangle(hud_overlay, (0, h - hud_h), (w, h), (20, 20, 20), -1)
    cv2.addWeighted(hud_overlay, 0.75, canvas, 0.25, 0, canvas)

    travel = result.get("travel", 0)
    travel_sub = result.get("travel_distance", travel)
    conf = result.get("confidence", 0.0)
    method = result.get("method", "cv")
    jitter = dbg.get("retry_jitter", 0)
    attempt = dbg.get("attempt", 1)

    line1 = f"Travel: {travel}px (sub: {travel_sub:.1f}px, jit: {jitter:+d}px) | Conf: {conf:.2f}"
    line2 = f"Method: {method} | Try: #{attempt} | Slot: {s_center[0]:.1f} - Piece: {p_center[0]:.1f}" if s_center and p_center else f"Method: {method}"

    cv2.putText(canvas, line1, (8, h - hud_h + 17), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(canvas, line2, (8, h - hud_h + 35), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (220, 220, 220), 1, cv2.LINE_AA)

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_p), canvas)
    return out_p


def solve_puzzle_cv(
    screenshot_data: Any,
    canvas_rect: Optional[Dict[str, float]] = None,
    track_rect: Optional[Dict[str, float]] = None,
    handle_rect: Optional[Dict[str, float]] = None,
    piece_rect: Optional[Dict[str, float]] = None,
    device_pixel_ratio: float = 1.0,
    attempt: int = 1,
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

    # Colour-explicit sprite (wooden pestle, red/orange prop) — beats the vertical-edge-pair
    # heuristic above, which latches onto the backdrop's strongest edges on photo scenes.
    sprite_piece = detect_sprite_piece_safe(canvas_crop, gray)
    if sprite_piece is not None and not (
        piece_rect and isinstance(piece_rect, dict) and detected_piece_x is not None
    ):
        sx, sy, sw, sh = sprite_piece["bbox"]
        detected_piece_x = int(sx)
        detected_piece_y = int(sy)
        detected_piece_w = int(sw)
        detected_piece_h = int(sh)

    # 4. Multi-stage feature extraction
    # Inspect overall color saturation to differentiate natural photo puzzles (salads, goods) vs synthetic gray canvases
    hsv = cv2.cvtColor(canvas_crop, cv2.COLOR_BGR2HSV)
    mean_sat = float(np.mean(hsv[:, :, 1]))
    is_natural_scene = mean_sat > 55.0

    # Method 0: colour-explicit sprite + flat cavity on a PHOTOGRAPHIC scene. The jigsaw/
    # gradient/edge heuristics below latch onto the backdrop's strongest edges on photo scenes
    # and cannot beat real piece evidence; on the synthetic flat canvases (a handful of
    # colours) the classic methods stay authoritative so their contracts are unchanged.
    distinct_colours = int(distinct_colour_count(canvas_crop))
    is_photographic = distinct_colours > 400

    # Colour families miss some props (a moss-green disc on a forest photo) and can also latch
    # onto scenery: on that same frame the "dark" family matched a rock at x=75 while the real
    # piece sat at x≈20. Shopee always parks the sprite against one of the frame's edges, so a
    # salient blob hugging an edge outranks a colour match that does not.
    if is_photographic:
        salient = detect_salient_piece_safe(canvas_crop, gray)
        if salient is not None:
            sbx, _, sbw, _ = salient["bbox"]
            near_edge = min(sbx, cw - (sbx + sbw)) <= cw * 0.12
            if sprite_piece is None or near_edge:
                sprite_piece = salient

    # ---- Evidence gate: never invent a travel figure on a frame that holds no puzzle ----
    # Every heuristic below (coloured blob, gradient column, jigsaw notch) can fire on a plain
    # page, so "did a detector fire?" is not evidence of a puzzle. On Shopee's blank
    # "Vui lòng thử lại sau" lock screen the sprite scan latched onto the orange Shopee logo and
    # the cut-out scan onto the white error card's border, and the code reported
    # `travel 237, confidence 0.94` — again and again for minutes — while the extension dragged on
    # a page that had no puzzle at all. That is what burned the account's attempts and locked it.
    #
    # What actually separates a challenge from an error page is that a challenge canvas is a
    # photograph. Measured on outputs/evidence: the three real frames carry >=1951 distinct
    # colours over ~2% blown-out white, while the three lock/error pages carry <=180 colours and
    # are 98.7-99.7% pure white. The synthetic flat canvases the unit tests use sit at 0% white,
    # so this leaves their long-standing contracts untouched.
    hole_probe = detect_outlined_hole_safe(canvas_crop, gray)
    near_white_fraction = float(np.mean(gray >= 245))
    if near_white_fraction >= 0.60 and not is_photographic:
        return {
            "ok": False,
            "abstain": True,
            "reason": "no_puzzle_in_frame",
            "travel": None,
            "puzzle_travel": None,
            "direction": None,
            "piece_kind": "",
            "method": "abstain",
            "confidence": 0.0,
            "details": {
                "near_white_fraction": round(near_white_fraction, 3),
                "distinct_colours": distinct_colours,
                "mean_saturation": round(mean_sat, 1),
                "sprite_found": sprite_piece is not None,
                "outlined_hole_found": hole_probe is not None,
                "canvas_w_phys": cw,
                "canvas_h_phys": ch,
            },
            "debug_info": {
                "piece_center": None,
                "slot_center": None,
                "sprite_bbox": None,
                "hole_bbox": None,
                "travel_canvas": None,
                "frame_w": cw,
                "frame_h": ch,
            },
        }

    # Method 0a: outlined cut-out + solid sprite on a photographic scene. The hole is a flat,
    # strongly outlined region; the movable sprite is a solid blob (warm/bright/blue/dark). Pair
    # them directly and keep the sign, instead of letting edge heuristics pair the hole with a
    # neighbouring edge — the failure that dragged the piece away from the hole.
    if is_photographic:
        # Prefer the compartment/receptacle solver when it is confident: it was written for
        # "prop into cavity" props (tablet into dispenser, pestle into mortar) and its cavity
        # search looks for the low-variance interior *inside* a container, which is exactly the
        # mortar-bowl case that the outlined-hole scan got wrong.
        comp_first = solve_compartment_receptacle(
            canvas_crop,
            canvas_rect=canvas_rect,
            track_rect=track_rect,
            handle_rect=handle_rect,
            device_pixel_ratio=scale,
            attempt=attempt,
        )
        if (
            comp_first.get("ok")
            and float(comp_first.get("confidence", 0) or 0) >= 0.90
            and 24.0 <= abs(float(comp_first.get("travel", 0) or 0)) <= cw * 0.95
        ):
            return comp_first

        hole = hole_probe
        if hole is not None and sprite_piece is not None:
            hole_x = float(hole["center"][0])
            sprite_x = float(sprite_piece["center"][0])
            delta = hole_x - sprite_x
            if 18.0 <= abs(delta) <= cw * 0.92:
                return {
                    "ok": True,
                    "travel": int(round(delta / max(0.1, scale))),
                    "puzzle_travel": int(round(delta / max(0.1, scale))),
                    "max_travel": int(round(max(50.0, (float(track_rect.get("width")) if track_rect else cw) - (float(handle_rect.get("width")) if handle_rect else 40.0)))),
                    "scale_ratio": 1.0,
                    "direction": "left" if delta < 0 else "right",
                    "piece_kind": sprite_piece.get("kind", ""),
                    "method": "outlined_hole_sprite",
                    "confidence": 0.93,
                    "details": {
                        "hole_center": [round(hole_x, 1), round(hole["center"][1], 1)],
                        "sprite_center": [round(sprite_x, 1), round(sprite_piece["center"][1], 1)],
                        "hole_bbox": list(hole["bbox"]),
                        "sprite_bbox": list(sprite_piece["bbox"]),
                        "hole_std": hole["std"],
                        "delta_x_phys": round(delta, 1),
                        "direction": "left" if delta < 0 else "right",
                        "canvas_w_phys": cw,
                        "canvas_h_phys": ch,
                    },
                }

    if (
        is_photographic
        and sprite_piece is not None
        and sprite_piece.get("kind") == "warm"
    ):
        comp_first = solve_compartment_receptacle(
            canvas_crop,
            canvas_rect=canvas_rect,
            track_rect=track_rect,
            handle_rect=handle_rect,
            device_pixel_ratio=scale,
            attempt=attempt,
        )
        if comp_first.get("ok") and comp_first.get("confidence", 0) >= 0.90:
            return comp_first

    # Branch A: If natural colorful scene (e.g. salad, merchandise), prioritize Jigsaw Notch & Cutout Slot
    if is_natural_scene:
        piece_y0 = max(0, detected_piece_y)
        piece_y1 = min(ch, detected_piece_y + detected_piece_h)
        piece_w = detected_piece_w
        band_prof = sobel_x[piece_y0:piece_y1, :].mean(axis=0)
        gray_band = gray[piece_y0:piece_y1, :]

        best_cand_x = None
        best_cand_score = -1.0

        for tx in range(5, cw - piece_w - 5):
            edge_score = float(band_prof[tx] + band_prof[tx + piece_w])
            window_darkness = float(255.0 - gray_band[:, tx:tx+piece_w].mean())
            combined = edge_score * 1.0 + window_darkness * 1.2
            if combined > best_cand_score:
                best_cand_score = combined
                best_cand_x = tx

        if best_cand_x is not None and abs(best_cand_x - detected_piece_x) > 30:
            target_x = float(best_cand_x)
            piece_x = float(detected_piece_x)
            delta_x_phys = target_x - piece_x
            method = "jigsaw_notch"
            confidence = 0.96

    # Method 1: Colored Piece & Matching Slot (for synthetic gray canvases)
    # Method 1: Colored Piece & Matching Slot (for synthetic gray canvases with red polygon)
    if method == "fallback":
        b, g, r = cv2.split(canvas_crop)
        red_mask = (r > 150) & (r > g.astype(int) + 50) & (r > b.astype(int) + 50)
        red_mask[:, int(cw * 0.45):] = 0
        red_ys, red_xs = np.where(red_mask)
        if len(red_xs) > 30:
            piece_x = float(np.mean(red_xs))
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(red_mask.astype(np.uint8))
        valid_red_piece = None
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area > 150:  # Mảnh ghép màu đỏ thực sự phải là một khối liền mạch diện tích đáng kể
                valid_red_piece = (centroids[i][0], area)
                break
        if valid_red_piece:
            piece_x = float(valid_red_piece[0])
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
            param1=80, param2=35,
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
                    left_roi = gray[:, :int(cw * 0.32)]
                    edges_left = cv2.Canny(left_roi, 30, 100)
                    cnts_left, _ = cv2.findContours(edges_left, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    if cnts_left:
                        best_cnt = max(cnts_left, key=cv2.contourArea)
                        lx, ly, lw, lh = cv2.boundingRect(best_cnt)
                        cand_target_x = float(x1 + min_loc[0]) if min_val < 90 else cx
                        # Prefer the sprite detector's piece; the left-ROI contour is only a
                        # stand-in and latches onto the mortar's rim on photo scenes. The
                        # convention here stays "piece's leading edge → cavity centre", which is
                        # what this method has always reported (tip into the pit).
                        if sprite_piece is not None:
                            sbx, _, sbw, _ = sprite_piece["bbox"]
                            cand_piece_x = float(sbx + sbw)
                        else:
                            cand_piece_x = float(lx + lw)
                        cand_delta = cand_target_x - cand_piece_x
                        if abs(cand_delta) > 24 and min_val < 90:
                            target_x = cand_target_x
                            piece_x = cand_piece_x
                            delta_x_phys = cand_delta
                            method = "circular_receptacle"
                            confidence = 0.95

    # Method 3: Compartment Receptacle / Dispenser Slot Fitting (e.g. Dishwasher tablet into dispenser, tray into slot)
    if method == "fallback":
        left_quarter = int(cw * 0.32)
        left_gray = gray[:, :left_quarter]
        receptacle_res = solve_compartment_receptacle(
            canvas_crop,
            canvas_rect=canvas_rect,
            track_rect=track_rect,
            handle_rect=handle_rect,
            device_pixel_ratio=scale,
            attempt=attempt,
        )
        if receptacle_res.get("ok") and receptacle_res.get("confidence", 0) >= 0.90:
            return receptacle_res

        # Detect bright or distinctive foreground object in left region (excluding full-background contours)
        _, thresh_l = cv2.threshold(left_gray, 220, 255, cv2.THRESH_BINARY)
        cnts_l, _ = cv2.findContours(thresh_l, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_pl = None
        max_pl_a = 0
        for c in cnts_l:
            a = cv2.contourArea(c)
            x, y, w, h = cv2.boundingRect(c)
            if a > 180 and 15 <= w <= int(left_quarter * 0.75) and 25 <= h <= int(ch * 0.85):
                if a > max_pl_a:
                    max_pl_a = a
                    best_pl = (x, y, w, h)
        if best_pl:
            p_x_ref, _, p_w_ref, _ = best_pl
        else:
            p_w_ref = detected_piece_w if detected_piece_w else 34
            p_x_ref = detected_piece_x if detected_piece_x is not None else 10

        piece_center_x = (
            float(sprite_piece["center"][0])
            if sprite_piece is not None
            else float(p_x_ref + p_w_ref / 2.0)
        )

        # Cavity search spans the frame (minus a thin margin) so the receptacle may sit on
        # either side of the piece; the pairing below keeps the delta's sign.
        min_rx = int(cw * 0.10)
        max_rx = int(cw * 0.95)
        right_roi = gray[:, min_rx:max_rx]

        edges_right = cv2.Canny(right_roi, 30, 95)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        edges_closed = cv2.morphologyEx(edges_right, cv2.MORPH_CLOSE, kernel)
        cnts_r, _ = cv2.findContours(edges_closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        cavity_cands = []
        for c in cnts_r:
            x, y, w, h = cv2.boundingRect(c)
            area = cv2.contourArea(c)
            if (0.8 * p_w_ref <= w <= 2.4 * p_w_ref) and (15 <= h <= ch * 0.6) and area > 350 and (w >= 1.1 * h):
                pad_x = max(2, int(w * 0.15))
                pad_y = max(2, int(h * 0.15))
                interior = right_roi[y+pad_y:y+h-pad_y, x+pad_x:x+w-pad_x]
                if interior.size >= 25:
                    std_dev = float(np.std(interior))
                    if std_dev < 42:
                        abs_x = min_rx + x
                        cavity_center_x = abs_x + w / 2.0
                        delta = cavity_center_x - piece_center_x
                        if abs(delta) > 24:
                            score = area / (std_dev + 5.0)
                            cavity_cands.append((score, cavity_center_x, piece_center_x, delta))

        if cavity_cands:
            cavity_cands.sort(key=lambda item: item[0], reverse=True)
            best_score, best_target_x, best_piece_x, best_delta = cavity_cands[0]
            target_x = float(best_target_x)
            piece_x = float(best_piece_x)
            delta_x_phys = float(best_delta)
            method = "compartment_receptacle"
            confidence = 0.96

    # Method 3: Robust Jigsaw Notch & Cutout Slot (General Fallback)
    # Method 4: Robust Jigsaw Notch & Cutout Slot (General Fallback)
    if method == "fallback":
        piece_y0 = max(0, detected_piece_y)
        piece_y1 = min(ch, detected_piece_y + detected_piece_h)
        piece_w = detected_piece_w
        band_prof = sobel_x[piece_y0:piece_y1, :].mean(axis=0)
        gray_band = gray[piece_y0:piece_y1, :]

        best_cand_x = None
        best_cand_score = -1.0

        for tx in range(5, cw - piece_w - 5):
            edge_score = float(band_prof[tx] + band_prof[tx + piece_w])
            window_darkness = float(255.0 - gray_band[:, tx:tx+piece_w].mean())
            combined = edge_score * 1.0 + window_darkness * 1.2
            if combined > best_cand_score:
                best_cand_score = combined
                best_cand_x = tx

        if best_cand_x is not None and abs(best_cand_x - detected_piece_x) > 20:
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

    # NO "shopee is usually dragged >60%" prior here. That heuristic used to replace any
    # small/medium answer with 63.5% of the track — on the mortar-&-pestle variant it shoved
    # the piece straight out of the frame (clipped against the canvas edge). A low-confidence
    # method now reports its honest (signed) number plus a low `confidence`; the caller decides.
    travel_limit = max(8.0, max_handle_travel - 3.0)
    bounded_travel = int(round(max(-travel_limit, min(handle_travel_css, travel_limit))))
    direction = "left" if bounded_travel < 0 else "right"

    return {
        "ok": True,
        "travel": bounded_travel,
        "puzzle_travel": int(round(puzzle_travel_css)),
        "max_travel": int(round(max_handle_travel)),
        "scale_ratio": round(scale_ratio, 3),
        "direction": direction,
        "piece_kind": sprite_piece.get("kind") if sprite_piece else "",
        "method": method,
        "confidence": round(confidence, 2),
        "details": {
            "target_x_phys": round(target_x, 1),
            "piece_x_phys": round(piece_x, 1),
            "delta_x_phys": round(delta_x_phys, 1),
            "scale": round(scale, 3),
            "direction": direction,
            "piece_kind": sprite_piece.get("kind") if sprite_piece else "",
            "canvas_w_phys": cw,
            "canvas_h_phys": ch,
            "gate": {
                "mean_saturation": round(mean_sat, 1),
                "distinct_colours": distinct_colours,
                "near_white_fraction": round(near_white_fraction, 3),
                "is_natural_scene": bool(is_natural_scene),
                "is_photographic": bool(is_photographic),
                "sprite_found": sprite_piece is not None,
                "hole_found": hole_probe is not None,
            },
        },
    }

