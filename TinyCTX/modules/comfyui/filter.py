"""
modules/comfyui/filter.py

NudeNet-based safety filter.
Downloads nudenet.onnx on first use if missing.
Detects configured label classes and blacks out their bounding boxes.

Two blocking modes:
  hard_blocked_ids  — detections are censored and the image is WITHHELD from the LLM entirely.
  soft_blocked_ids  — detections are censored but the image is still sent to the LLM with a
                      notice describing what was automatically removed.

Preprocessing matches the reference implementation exactly:
  resize-to-fit -> pad-to-square -> resize-to-TARGET (fixes off-by-one)
  normalize to float32 [0,1], CHW, NCHW
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label registry  (matches Nudenet.py CLASSIDS_LABELS_MAPPING)
# ---------------------------------------------------------------------------
CLASSIDS_LABELS: dict[int, str] = {
    0:  "FEMALE_GENITALIA_COVERED",
    1:  "FACE_FEMALE",
    2:  "BUTTOCKS_EXPOSED",
    3:  "FEMALE_BREAST_EXPOSED",
    4:  "FEMALE_GENITALIA_EXPOSED",
    5:  "MALE_BREAST_EXPOSED",
    6:  "ANUS_EXPOSED",
    7:  "FEET_EXPOSED",
    8:  "BELLY_COVERED",
    9:  "FEET_COVERED",
    10: "ARMPITS_COVERED",
    11: "ARMPITS_EXPOSED",
    12: "FACE_MALE",
    13: "BELLY_EXPOSED",
    14: "MALE_GENITALIA_EXPOSED",
    15: "ANUS_COVERED",
    16: "FEMALE_BREAST_COVERED",
    17: "BUTTOCKS_COVERED",
}
LABELS_CLASSIDS: dict[str, int] = {v: k for k, v in CLASSIDS_LABELS.items()}

MODEL_URL      = "https://d2xl8ijk56kv4u.cloudfront.net/models/nudenet.onnx"
MODEL_FILENAME = "nudenet.onnx"
TARGET         = 320  # model inference resolution


# ---------------------------------------------------------------------------
# Model download
# ---------------------------------------------------------------------------

def _model_path(module_dir: Path) -> Path:
    return module_dir / "models" / MODEL_FILENAME


def _ensure_model(module_dir: Path) -> Path:
    path = _model_path(module_dir)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("filter: nudenet.onnx not found — downloading from %s", MODEL_URL)
    import urllib.request

    def _progress(count: int, block_size: int, total_size: int) -> None:
        if total_size > 0:
            pct = min(100, count * block_size * 100 // total_size)
            print(f"\rfilter: downloading nudenet.onnx {pct}%", end="", flush=True)

    urllib.request.urlretrieve(MODEL_URL, path, reporthook=_progress)
    print()
    logger.info("filter: downloaded to %s", path)
    return path


# ---------------------------------------------------------------------------
# ONNX session cache
# ---------------------------------------------------------------------------
_sessions: dict[Path, object] = {}


def _get_session(module_dir: Path):
    if module_dir not in _sessions:
        import onnxruntime as ort
        model = _ensure_model(module_dir)
        logger.info("filter: loading ONNX session from %s", model)
        _sessions[module_dir] = ort.InferenceSession(
            str(model),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
    return _sessions[module_dir]


# ---------------------------------------------------------------------------
# Pre/post processing
# ---------------------------------------------------------------------------

def _preprocess(img_rgb_u8, target: int = TARGET):
    """
    img_rgb_u8: uint8 HxWx3 numpy array in RGB order (as returned by Pillow)
    Returns: (input_tensor float32 NCHW, resize_factor, pad_left, pad_top)

    Mirrors the reference read_image() exactly, including the second resize
    that corrects off-by-one padding errors.
    """
    import numpy as np
    from PIL import Image as PilImage

    h, w = img_rgb_u8.shape[:2]
    aspect = w / h

    if h > w:
        new_h = target
        new_w = int(round(target * aspect))
    else:
        new_w = target
        new_h = int(round(target / aspect))

    resize_factor = math.sqrt((w ** 2 + h ** 2) / (new_w ** 2 + new_h ** 2))

    # Resize to fit
    pil = PilImage.fromarray(img_rgb_u8)
    pil = pil.resize((new_w, new_h), PilImage.BILINEAR)
    resized = np.array(pil, dtype=np.uint8)

    # Pad to square — use floor division matching the reference
    pad_x = target - new_w
    pad_y = target - new_h
    pad_top  = int(np.floor(pad_y / 2))
    pad_left = int(np.floor(pad_x / 2))

    canvas = np.zeros((target, target, 3), dtype=np.uint8)
    canvas[pad_top:pad_top + new_h, pad_left:pad_left + new_w] = resized

    # Second resize to TARGET x TARGET — fixes any off-by-one from rounding
    # (matches reference: img = cv2.resize(img, (target_size, target_size)))
    canvas_pil = PilImage.fromarray(canvas)
    canvas_pil = canvas_pil.resize((target, target), PilImage.BILINEAR)
    canvas = np.array(canvas_pil, dtype=np.float32)

    # Normalise + HWC -> CHW -> NCHW
    tensor = (canvas / 255.0).transpose(2, 0, 1)[np.newaxis].astype(np.float32)
    return tensor, resize_factor, pad_left, pad_top


def _postprocess(
    output,
    resize_factor: float,
    pad_left: int,
    pad_top: int,
    min_score: float,
) -> list[dict]:
    import numpy as np

    out = np.transpose(np.squeeze(output[0]))  # (num_detections, 4 + num_classes)
    boxes, scores, class_ids = [], [], []

    for row in out:
        class_scores = row[4:]
        max_score = float(class_scores.max())
        if max_score < min_score:
            continue
        class_id = int(class_scores.argmax())
        x, y, w, h = float(row[0]), float(row[1]), float(row[2]), float(row[3])
        left   = int(round((x - w * 0.5 - pad_left) * resize_factor))
        top    = int(round((y - h * 0.5 - pad_top)  * resize_factor))
        width  = int(round(w * resize_factor))
        height = int(round(h * resize_factor))
        boxes.append([left, top, width, height])
        scores.append(max_score)
        class_ids.append(class_id)

    if not boxes:
        return []

    # Greedy NMS (no cv2 dep)
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    keep: list[int] = []
    while order:
        i = order.pop(0)
        keep.append(i)
        order = [j for j in order if _iou(boxes[i], boxes[j]) < 0.45]

    return [
        {"id": class_ids[i], "score": round(scores[i], 2), "box": boxes[i]}
        for i in keep
    ]


def _iou(a: list[int], b: list[int]) -> float:
    ax1, ay1 = a[0], a[1]
    ax2, ay2 = ax1 + a[2], ay1 + a[3]
    bx1, by1 = b[0], b[1]
    bx2, by2 = bx1 + b[2], by1 + b[3]
    inter = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_blocked_ids(label_names: list[str]) -> set[int]:
    """Convert label name strings from config into class ID ints."""
    ids: set[int] = set()
    for name in label_names:
        name = name.strip().upper()
        if name not in LABELS_CLASSIDS:
            logger.warning("filter: unknown label name %r — ignoring", name)
            continue
        ids.add(LABELS_CLASSIDS[name])
    return ids


class FilterResult:
    """Result of apply_filter, describing hard and soft censorship outcomes."""

    def __init__(
        self,
        hard_blocked: list[str],
        soft_blocked: list[str],
    ) -> None:
        self.hard_blocked = hard_blocked  # labels that triggered a hard block
        self.soft_blocked = soft_blocked  # labels that triggered a soft block

    @property
    def any_censored(self) -> bool:
        return bool(self.hard_blocked or self.soft_blocked)

    @property
    def hard_triggered(self) -> bool:
        """True if the image should be withheld from the LLM entirely."""
        return bool(self.hard_blocked)

    @property
    def soft_triggered(self) -> bool:
        """True if the image should be passed to the LLM with a censor notice."""
        return bool(self.soft_blocked) and not self.hard_blocked


def apply_filter(
    image_path: Path,
    module_dir: Path,
    hard_blocked_ids: set[int],
    soft_blocked_ids: set[int],
    min_score: float = 0.2,
) -> FilterResult:
    """
    Run NudeNet on image_path. Black out bboxes for any detection whose
    class ID appears in either hard_blocked_ids or soft_blocked_ids.
    Overwrites the file in place when any region is censored.

    Hard blocks take priority: if a hard label is detected, the caller should
    withhold the image from the LLM entirely regardless of soft detections.

    Returns:
        FilterResult with .hard_blocked and .soft_blocked label name lists.
    """
    import numpy as np
    from PIL import Image as PilImage

    all_blocked_ids = hard_blocked_ids | soft_blocked_ids

    session = _get_session(module_dir)
    input_name = session.get_inputs()[0].name

    # Load as uint8 RGB — do NOT convert to float before preprocessing
    img = PilImage.open(image_path).convert("RGB")
    img_u8 = np.array(img, dtype=np.uint8)
    img_h, img_w = img_u8.shape[:2]

    tensor, resize_factor, pad_left, pad_top = _preprocess(img_u8)
    outputs = session.run(None, {input_name: tensor})
    detections = _postprocess(outputs, resize_factor, pad_left, pad_top, min_score)

    hard_labels: list[str] = []
    soft_labels: list[str] = []

    for det in detections:
        if det["id"] not in all_blocked_ids:
            continue
        bx, by, bw, bh = det["box"]
        x1 = max(0, bx)
        y1 = max(0, by)
        x2 = min(img_w, bx + bw)
        y2 = min(img_h, by + bh)
        if x2 <= x1 or y2 <= y1:
            continue

        img_u8[y1:y2, x1:x2] = 0  # black rectangle
        label = CLASSIDS_LABELS.get(det["id"], str(det["id"]))

        if det["id"] in hard_blocked_ids:
            hard_labels.append(label)
            logger.info(
                "filter: HARD-BLOCKED %s (score=%.2f) at [%d,%d,%d,%d]",
                label, det["score"], x1, y1, x2, y2,
            )
        else:
            soft_labels.append(label)
            logger.info(
                "filter: soft-blocked %s (score=%.2f) at [%d,%d,%d,%d]",
                label, det["score"], x1, y1, x2, y2,
            )

    if hard_labels or soft_labels:
        PilImage.fromarray(img_u8).save(image_path)

    return FilterResult(hard_blocked=hard_labels, soft_blocked=soft_labels)
