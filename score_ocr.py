#!/usr/bin/env python3
"""
score_ocr.py — Visual Pinball score detector via screenshot OCR

Usage:
    python score_ocr.py <image_path> [--debug] [--save-debug <output_path>]

Examples:
    python score_ocr.py screenshot.png
    python score_ocr.py screenshot.png --debug
    python score_ocr.py screenshot.png --debug --save-debug result.png
"""

import sys
import argparse
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

try:
    import pytesseract
except ImportError:
    print("ERROR: pytesseract not installed. Run: pip install pytesseract")
    sys.exit(1)

# Try EasyOCR (optional but much better for real photos)
try:
    import easyocr
    _easyocr_reader: Optional["easyocr.Reader"] = None  # initialized via warmup()
    EASYOCR_AVAILABLE = True
except ImportError:
    _easyocr_reader = None
    EASYOCR_AVAILABLE = False


def warmup() -> None:
    """
    Pre-load the EasyOCR model so the first detect_score() call is fast.
    Call this once at application startup, e.g.:
        import score_ocr
        score_ocr.warmup()
    """
    global _easyocr_reader
    if EASYOCR_AVAILABLE and _easyocr_reader is None:
        _easyocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class ScoreCandidate:
    score: str           # digits only, no punctuation
    raw_text: str        # original text as OCR read it
    confidence: float
    method: str
    bbox: Optional[tuple] = None   # (x, y, w, h) in original image coords

@dataclass
class DetectionResult:
    best: Optional[ScoreCandidate]
    all_candidates: list[ScoreCandidate] = field(default_factory=list)
    debug_images: dict = field(default_factory=dict)  # name -> np.ndarray


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_LONG_SIDE = 1200  # resize large images for speed — 1200 is enough for OCR

def resize_for_ocr(img: np.ndarray) -> tuple[np.ndarray, float]:
    """Resize image if too large. Returns (resized, scale_factor)."""
    h, w = img.shape[:2]
    long = max(h, w)
    if long <= MAX_LONG_SIDE:
        return img, 1.0
    scale = MAX_LONG_SIDE / long
    resized = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return resized, scale


def parse_numbers_from_text(text: str) -> list[str]:
    """
    Extract all plausible numbers from OCR text.
    Handles:
      - Plain digits:              1033753730
      - Comma-formatted:          1,033,753,730
      - Period-formatted (EU):    1.033.753.730
      - Space-grouped:            1 033 753 730
      - Apostrophe (Swiss):       1'033'753'730
    Returns list of digit-only strings.
    """
    results = []

    # 1. Comma/period/apostrophe-separated numbers (e.g. 1,033,753,730)
    for pattern in [
        r'\d{1,3}(?:[,\']\d{3})+',   # 1,033,753,730
        r'\d{1,3}(?:\.\d{3})+',       # 1.033.753.730
        r'\d{1,3}(?: \d{3})+',        # 1 033 753 730
    ]:
        for m in re.finditer(pattern, text):
            digits = re.sub(r'\D', '', m.group())
            if digits:
                results.append(digits)

    # 2. Raw digit sequences (4+ digits)
    for m in re.finditer(r'\d{4,}', text):
        results.append(m.group())

    return results


def is_plausible_score(digits: str) -> bool:
    """Check if a digit string could be a pinball score."""
    if not digits or len(digits) < 3:
        return False
    n = int(digits)
    # Old EM machines: hundreds. Modern: up to ~30 billion.
    return 100 <= n <= 99_999_999_999


def score_confidence(digits: str, method: str, is_formatted: bool = False,
                     y_frac: float = 0.5) -> float:
    """
    Heuristic confidence score. Longer numbers and formatted numbers rank higher.

    y_frac: vertical centre of the detection as a fraction of image height (0=top, 1=bottom).
    Score displays (DMD, backglass panel) are always near the top of a VP screenshot.
    Decorative elements (high-score cards, instruction cards) live near the bottom.
    """
    base = len(digits) * 0.2
    if is_formatted:
        base += 1.0   # comma-separated numbers are very likely scores
    if method.startswith("region"):
        base += 0.5   # found in a specific display region = bonus
    if method == "easyocr":
        base += 0.3
    # Vertical position bias
    if y_frac < 0.40:
        base += 0.8   # top 40% → strong bonus (DMD / backglass area)
    elif y_frac > 0.65:
        base -= 0.8   # bottom 35% → penalty (flipper area, high-score cards)
    return base


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def _clahe_otsu(gray: np.ndarray) -> np.ndarray:
    """CLAHE-enhanced + Otsu threshold. Most versatile single variant."""
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, out = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return out

def upscale_for_tesseract(img: np.ndarray, target_height: int = 80) -> np.ndarray:
    h, w = img.shape[:2]
    if h < target_height:
        scale = target_height / h
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    return img


# ---------------------------------------------------------------------------
# Tesseract calls
# ---------------------------------------------------------------------------

# PSM 11 = sparse text, best for "find digits anywhere in the image"
# PSM 6  = uniform block, best for a pre-cropped display region
_CFG_FULLIMAGE = r'--psm 11 --oem 3 -c tessedit_char_whitelist=0123456789,.'
_CFG_REGION    = r'--psm 6  --oem 3 -c tessedit_char_whitelist=0123456789,.'

def _tess_call(img: np.ndarray, config: str) -> list[str]:
    """One Tesseract call — returns all parsed number strings."""
    try:
        text = pytesseract.image_to_string(img, config=config)
        return parse_numbers_from_text(text)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Region-based detection — fast, no MSER
# ---------------------------------------------------------------------------

def find_high_contrast_regions(img: np.ndarray, gray: np.ndarray) -> list[tuple]:
    """
    Find rectangular regions that look like score displays.
    Uses two threshold levels and morphological closing to group digit blobs.
    Returns list of (x, y, w, h) sorted by area descending.
    """
    h_img, w_img = gray.shape
    regions = []

    # Two threshold levels cover bright-on-dark and medium-contrast displays
    for thresh_val in [100, 150]:
        _, binary = cv2.threshold(gray, thresh_val, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 12))
        closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            aspect = w / h if h > 0 else 0
            rel_area = area / (w_img * h_img)
            if 0.001 < rel_area < 0.4 and aspect > 1.5:
                regions.append((x, y, w, h))

    return _merge_boxes(regions, iou_threshold=0.3)


def _merge_boxes(boxes: list[tuple], iou_threshold: float = 0.3) -> list[tuple]:
    if not boxes:
        return []
    kept = []
    for b in sorted(boxes, key=lambda r: r[2] * r[3], reverse=True):
        x1, y1, w1, h1 = b
        overlap = False
        for k in kept:
            x2, y2, w2, h2 = k
            ix = max(0, min(x1+w1, x2+w2) - max(x1, x2))
            iy = max(0, min(y1+h1, y2+h2) - max(y1, y2))
            inter = ix * iy
            union = w1*h1 + w2*h2 - inter
            if union > 0 and inter / union > iou_threshold:
                overlap = True
                break
        if not overlap:
            kept.append(b)
    return kept


# ---------------------------------------------------------------------------
# EasyOCR (optional, much better for real-world photos)
# ---------------------------------------------------------------------------

def easyocr_detect(img: np.ndarray) -> list[ScoreCandidate]:
    """Run EasyOCR. Initializes the reader if not already done (cold start)."""
    global _easyocr_reader
    if not EASYOCR_AVAILABLE:
        return []
    if _easyocr_reader is None:
        _easyocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)

    h_img = img.shape[0]
    results = _easyocr_reader.readtext(img, allowlist='0123456789,.')
    candidates = []
    for (bbox_pts, text, conf) in results:
        numbers = parse_numbers_from_text(text)
        for num in numbers:
            if is_plausible_score(num):
                pts = np.array(bbox_pts, dtype=int)
                x, y, w, h = cv2.boundingRect(pts)
                is_fmt = any(c in text for c in ',.')
                y_frac = (y + h / 2) / h_img if h_img > 0 else 0.5
                candidates.append(ScoreCandidate(
                    score=num,
                    raw_text=text,
                    confidence=score_confidence(num, "easyocr", is_formatted=is_fmt,
                                               y_frac=y_frac) * conf,
                    method="easyocr",
                    bbox=(x, y, w, h),
                ))
    return candidates


# ---------------------------------------------------------------------------
# Main pipeline — minimal calls, sequential with early exit
# ---------------------------------------------------------------------------

# If a score candidate reaches this confidence, stop and return immediately.
_EARLY_EXIT_CONFIDENCE = 2.5

def _collect(numbers: list[str], method: str, bbox,
             candidates: list[ScoreCandidate], verbose: bool,
             y_frac: float = 0.5) -> None:
    """Helper: filter, build ScoreCandidate objects, append to list."""
    for num in numbers:
        if is_plausible_score(num):
            c = ScoreCandidate(
                score=num,
                raw_text=num,
                confidence=score_confidence(num, method, y_frac=y_frac),
                method=method,
                bbox=bbox,
            )
            candidates.append(c)
            if verbose:
                print(f"   [{method}] → {num}  (conf {c.confidence:.2f})")


def _best_so_far(candidates: list[ScoreCandidate]) -> Optional[ScoreCandidate]:
    return max(candidates, key=lambda c: c.confidence) if candidates else None


def detect_score(img: np.ndarray, verbose: bool = False) -> DetectionResult:
    """
    Detection order depends on whether EasyOCR has been pre-warmed:

    Pre-warmed (warmup() called at startup)  — recommended for integrations:
      1. EasyOCR       ~300ms  most reliable for real photos / DMD / LCD
      2. Tesseract     ~130ms  fallback for clean synthetic displays

    Cold start (CLI one-shot):
      1. Tesseract x2  ~260ms  fast, works for clean/crisp displays
      2. Tesseract regions  ~130ms each  for up to 3 regions
      3. EasyOCR       ~2s    cold-start penalty (loads PyTorch model)
    """
    result = DetectionResult(best=None)
    candidates: list[ScoreCandidate] = []

    img_work, scale = resize_for_ocr(img)
    gray = cv2.cvtColor(img_work, cv2.COLOR_BGR2GRAY)

    def _map_easy(easy_list: list[ScoreCandidate]) -> None:
        for c in easy_list:
            if c.bbox:
                x, y, w, h = c.bbox
                c.bbox = (int(x/scale), int(y/scale), int(w/scale), int(h/scale))
        candidates.extend(easy_list)
        if verbose:
            for c in easy_list:
                print(f"   [easyocr] → {c.score}  (raw: '{c.raw_text}'  conf {c.confidence:.2f})")

    def _early_return() -> Optional[DetectionResult]:
        best = _best_so_far(candidates)
        if best and best.confidence >= _EARLY_EXIT_CONFIDENCE:
            if verbose:
                print(f"   ✓ Early exit (conf {best.confidence:.2f})")
            _finalize(result, candidates)
            return result
        return None

    # ------------------------------------------------------------------ #
    # PATH A — EasyOCR pre-warmed: run it FIRST (fast + accurate)        #
    # ------------------------------------------------------------------ #
    if EASYOCR_AVAILABLE and _easyocr_reader is not None:
        if verbose:
            print("\n[1] EasyOCR (pre-warmed)...")
        _map_easy(easyocr_detect(img_work))
        r = _early_return()
        if r:
            return r

    # ------------------------------------------------------------------ #
    # Tesseract — full image (always runs; fast ~130ms per call)          #
    # ------------------------------------------------------------------ #
    if verbose:
        print("\n[2] Full-image OCR (PSM 11, CLAHE)...")
    _collect(_tess_call(_clahe_otsu(gray), _CFG_FULLIMAGE),
             "full_clahe", None, candidates, verbose)
    r = _early_return()
    if r:
        return r

    if verbose:
        print("\n[3] Full-image OCR (PSM 11, raw gray)...")
    _collect(_tess_call(gray, _CFG_FULLIMAGE),
             "full_raw", None, candidates, verbose)
    r = _early_return()
    if r:
        return r

    # ------------------------------------------------------------------ #
    # Tesseract — region crops (up to 3)                                  #
    # ------------------------------------------------------------------ #
    if verbose:
        print("\n[4] Region-based OCR...")
    regions = find_high_contrast_regions(img_work, gray)
    if verbose:
        print(f"   {len(regions)} region(s) found")

    for i, (x, y, w, h) in enumerate(regions[:3]):
        ih, iw = gray.shape
        pad = 12
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(iw, x + w + pad), min(ih, y + h + pad)
        crop = upscale_for_tesseract(gray[y0:y1, x0:x1])
        orig_bbox = (int(x0/scale), int(y0/scale),
                     int((x1-x0)/scale), int((y1-y0)/scale))
        y_frac = (y0 + (y1 - y0) / 2) / ih if ih > 0 else 0.5
        _collect(_tess_call(crop, _CFG_REGION),
                 f"region{i}", orig_bbox, candidates, verbose, y_frac=y_frac)
        r = _early_return()
        if r:
            return r

    # ------------------------------------------------------------------ #
    # PATH B — EasyOCR cold fallback (only if nothing found yet)         #
    # ------------------------------------------------------------------ #
    best = _best_so_far(candidates)
    if EASYOCR_AVAILABLE and _easyocr_reader is None and (
            best is None or best.confidence < 1.5):
        if verbose:
            print("\n[5] EasyOCR cold fallback (loading model)...")
        _map_easy(easyocr_detect(img_work))

    # ------------------------------------------------------------------ #
    # Finalize                                                            #
    # ------------------------------------------------------------------ #
    _finalize(result, candidates)
    return result


def _finalize(result: DetectionResult, candidates: list[ScoreCandidate]) -> None:
    seen: dict[str, ScoreCandidate] = {}
    for c in candidates:
        if c.score not in seen or c.confidence > seen[c.score].confidence:
            seen[c.score] = c
    result.all_candidates = sorted(seen.values(), key=lambda c: c.confidence, reverse=True)
    result.best = result.all_candidates[0] if result.all_candidates else None


# ---------------------------------------------------------------------------
# Debug drawing
# ---------------------------------------------------------------------------

DEBUG_COLORS = [
    (0, 200, 255), (0, 165, 255), (255, 0, 200),
    (200, 255, 0), (0, 255, 128), (128, 0, 255),
]

def draw_debug(img: np.ndarray, result: DetectionResult) -> np.ndarray:
    out = img.copy()

    # Draw all candidate bboxes
    for i, c in enumerate(result.all_candidates[:8]):
        if c.bbox:
            x, y, w, h = c.bbox
            color = DEBUG_COLORS[i % len(DEBUG_COLORS)]
            cv2.rectangle(out, (x, y), (x+w, y+h), color, 1)

    # Highlight the best
    best = result.best
    if best and best.bbox:
        x, y, w, h = best.bbox
        cv2.rectangle(out, (x-3, y-3), (x+w+3, y+h+3), (0, 255, 0), 3)

    # Score banner at the top
    h_img, w_img = out.shape[:2]
    banner_h = 60
    banner = np.zeros((banner_h, w_img, 3), dtype=np.uint8)
    if best:
        text = f"SCORE: {best.score}    method: {best.method}    conf: {best.confidence:.2f}"
        color = (0, 255, 80)
    else:
        text = "No score detected"
        color = (0, 80, 255)
    cv2.putText(banner, text, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    out = np.vstack([banner, out])

    # List all candidates on the side
    panel_w = 400
    panel = np.zeros((out.shape[0], panel_w, 3), dtype=np.uint8)
    cv2.putText(panel, "All candidates:", (8, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
    for i, c in enumerate(result.all_candidates[:20]):
        y_pos = 55 + i * 22
        label = f"{c.score:>13}  ({c.confidence:.2f})"
        color = (0, 255, 80) if i == 0 else (180, 180, 180)
        cv2.putText(panel, label, (8, y_pos),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1)
    out = np.hstack([out, panel])

    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Detect pinball score from a screenshot."
    )
    parser.add_argument("image", help="Path to the screenshot image")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose output")
    parser.add_argument("--save-debug", metavar="PATH",
                        help="Save annotated debug image to this path")
    parser.add_argument("--no-easyocr", action="store_true",
                        help="Skip EasyOCR even if installed")
    parser.add_argument("--warmup", action="store_true",
                        help="Pre-load EasyOCR model before detection (simulates production)")
    args = parser.parse_args()

    if args.no_easyocr:
        global EASYOCR_AVAILABLE
        EASYOCR_AVAILABLE = False

    img = cv2.imread(args.image)
    if img is None:
        print(f"ERROR: Could not load image: {args.image}")
        sys.exit(1)

    print(f"Image : {args.image}  ({img.shape[1]}x{img.shape[0]} px)")

    if args.warmup and EASYOCR_AVAILABLE:
        print("Warming up EasyOCR model...")
        t_wu = time.perf_counter()
        warmup()
        print(f"Warmup done in {(time.perf_counter()-t_wu)*1000:.0f}ms  "
              f"(one-time cost at startup)\n")
    elif EASYOCR_AVAILABLE:
        print("Engine: Tesseract + EasyOCR (cold — use --warmup to simulate production speed)")
    else:
        print("Engine: Tesseract only  (pip install easyocr for better results)")
    print()

    t0 = time.perf_counter()
    result = detect_score(img, verbose=args.debug)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    print(f"Detection time : {elapsed_ms:.0f}ms")
    print()

    if result.best:
        b = result.best
        print(f"  Score      : {b.score}")
        print(f"  Method     : {b.method}")
        print(f"  Confidence : {b.confidence:.3f}")
        print(f"  Bbox       : {b.bbox}")
    else:
        print("  No score detected.")

    if result.all_candidates:
        print(f"\n  All candidates ({len(result.all_candidates)}):")
        for c in result.all_candidates[:10]:
            print(f"    {c.score:>12}  conf={c.confidence:.3f}  [{c.method}]")

    if args.debug or args.save_debug:
        debug_img = draw_debug(img, result)
        # Scale down for display
        dh, dw = debug_img.shape[:2]
        max_dim = 1600
        if max(dh, dw) > max_dim:
            s = max_dim / max(dh, dw)
            debug_img = cv2.resize(debug_img, (int(dw*s), int(dh*s)))

        if args.save_debug:
            cv2.imwrite(args.save_debug, debug_img)
            print(f"\nDebug image saved: {args.save_debug}")
        else:
            cv2.imshow("Score OCR Debug", debug_img)
            print("\nPress any key to close...")
            cv2.waitKey(0)
            cv2.destroyAllWindows()

    return 0 if result.best else 1


if __name__ == "__main__":
    sys.exit(main())
