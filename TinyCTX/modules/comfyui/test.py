"""
modules/comfyui/test.py

Debug harness for filter.py. Run directly — no TCTX needed.

Usage:
    python test.py <image_path> [--apply] [--score 0.2] [--labels LABEL1,LABEL2]

    --apply     Write censored copy next to original (original untouched)
    --score     Min detection score threshold (default: from config.json or 0.2)
    --labels    Comma-separated label names to block (default: from config.json)

Examples:
    python test.py output.png
    python test.py output.png --apply
    python test.py output.png --apply --score 0.1 --labels FEMALE_BREAST_EXPOSED,ANUS_EXPOSED
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make filter importable without being inside the package
# ---------------------------------------------------------------------------
_here = Path(__file__).parent
sys.path.insert(0, str(_here.parent))  # so 'from anima.filter import ...' works
sys.path.insert(0, str(_here))         # so 'import filter' works too

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s  %(name)s  %(message)s",
)
logger = logging.getLogger("test")


# ---------------------------------------------------------------------------
# Load config defaults
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    cfg_path = _here / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.warning("Could not read config.json: %s", e)
    return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    cfg = _load_config()
    filter_cfg = cfg.get("safety_filter", {})

    default_score  = filter_cfg.get("min_score", 0.2)
    default_labels = filter_cfg.get("blocked_labels", [])

    parser = argparse.ArgumentParser(description="Debug filter.py against an image")
    parser.add_argument("image", help="Path to input image")
    parser.add_argument("--apply",  action="store_true", help="Write censored copy")
    parser.add_argument("--score",  type=float, default=default_score,
                        help=f"Min score threshold (default: {default_score})")
    parser.add_argument("--labels", default=",".join(default_labels),
                        help=f"Comma-separated blocked labels (default: {','.join(default_labels) or 'none'})")
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        sys.exit(f"ERROR: image not found: {image_path}")

    blocked_label_names = [l.strip().upper() for l in args.labels.split(",") if l.strip()]

    print(f"\n{'='*60}")
    print(f"  Image  : {image_path}")
    print(f"  Score  : {args.score}")
    print(f"  Labels : {blocked_label_names or '(none — detection only)'}")
    print(f"{'='*60}\n")

    # Import filter from the same directory
    import filter as f

    blocked_ids = f.resolve_blocked_ids(blocked_label_names)
    print(f"Blocked class IDs: {blocked_ids}\n")

    # ------------------------------------------------------------------
    # Run raw detection (show everything, don't censor yet)
    # ------------------------------------------------------------------
    import numpy as np
    from PIL import Image as PilImage

    session = f._get_session(_here)
    input_name = session.get_inputs()[0].name

    img = PilImage.open(image_path).convert("RGB")
    img_u8 = np.array(img, dtype=np.uint8)
    print(f"Image size: {img_u8.shape[1]}x{img_u8.shape[0]} (WxH)\n")

    tensor, resize_factor, pad_left, pad_top = f._preprocess(img_u8)
    print(f"Preprocessed tensor shape: {tensor.shape}  resize_factor={resize_factor:.4f}  pad_left={pad_left}  pad_top={pad_top}\n")

    outputs = session.run(None, {input_name: tensor})
    print(f"Raw output shape: {outputs[0].shape}\n")

    # All detections at min_score=0 so we see everything the model sees
    all_dets = f._postprocess(outputs, resize_factor, pad_left, pad_top, min_score=0.0)
    print(f"All detections (score >= 0.0): {len(all_dets)}")
    print(f"{'':4}{'LABEL':<40} {'SCORE':>6}  {'BOX (x,y,w,h)'}")
    print(f"{'':4}{'-'*40} {'------':>6}  {'-'*20}")
    for det in sorted(all_dets, key=lambda d: d["score"], reverse=True):
        label    = f.CLASSIDS_LABELS.get(det["id"], f"class_{det['id']}")
        blocked  = "  <-- BLOCKED" if det["id"] in blocked_ids and det["score"] >= args.score else ""
        flagged  = "  <-- (below threshold)" if det["id"] in blocked_ids and det["score"] < args.score else ""
        print(f"  {'*' if blocked else ' '} {label:<40} {det['score']:>6.3f}  {det['box']}{blocked}{flagged}")

    # Detections that will actually be censored
    will_censor = [d for d in all_dets if d["id"] in blocked_ids and d["score"] >= args.score]
    print(f"\nWill censor: {len(will_censor)} detection(s)\n")

    # ------------------------------------------------------------------
    # Optionally apply filter and save copy
    # ------------------------------------------------------------------
    if args.apply:
        import shutil
        out_path = image_path.with_stem(image_path.stem + "_censored")
        shutil.copy2(image_path, out_path)
        # apply_filter treats hard/soft blocked labels as two separate sets and
        # returns a FilterResult, not a (modified, censored) tuple — here we
        # just want "censor everything requested", so pass the same set as both.
        result = f.apply_filter(out_path, _here, blocked_ids, blocked_ids, args.score)
        if result.any_censored:
            print(f"Censored copy saved to: {out_path}")
            print(f"Labels censored: {result.hard_blocked + result.soft_blocked}")
        else:
            print(f"No changes — nothing to censor (copy at {out_path} is identical)")
    else:
        print("(Pass --apply to write a censored copy)")

    print()


if __name__ == "__main__":
    main()
