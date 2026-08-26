#!/usr/bin/env python3
"""
tools/vision_debug_viewer.py
=================================

Standalone, READ-ONLY diagnostic viewer for Luno's real YOLO vision
pipeline. Opens a real OpenCV window showing the live camera/RTSP feed
with every YOLO detection drawn on it, so a human can physically verify
whether the model is actually detecting people - independent of, and
without touching, the production camera automation pipeline.

This is a debug tool, NOT part of the runtime. It:
  - reuses the SAME configuration Luno's real Vision pipeline uses
    wherever practical (`luno.config.CAMERA_URL`/`CAMERA_INDEX` via
    `luno.vision.camera_source()`, `YOLO_MODEL_PATH`, `CONFIDENCE_
    THRESHOLD`, and the SAME device-selection logic via `luno.vision.
    _device_arg()` - all read-only, side-effect-free functions);
  - instantiates its OWN separate `ultralytics.YOLO` model instance
    (deliberately - see "Why a separate model instance" below), never
    touching `luno.vision`'s shared `_yolo_model` singleton or its
    `_yolo_lock`;
  - imports NOTHING from `luno.camera_automation`, `luno.automation`,
    `luno.adapters.home_assistant`, or `camera_action_safety` - it
    cannot affect, and does not even import, the production automation
    pipeline;
  - never generates a synthetic `human_detected`/`CameraEvent`, never
    uses `MockVisionHandler`/`MockHomeAssistantHandler`, and never
    infers "a person is present" from anything except THIS frame's raw
    YOLO `boxes.cls == 0` count, computed fresh every single frame.

Why a separate model instance (not `luno.vision._get_yolo()`): this
tool runs as its OWN operating-system process, entirely separate from
whatever `main.py`/an already-running Luno instance is doing in ITS
process - a "shared singleton" only means anything within one running
Python process, so there is no actual duplication risk here the way
there would be if this logic were pasted into `luno/vision.py` itself
(see that file's own P0.8.4 comment on why the TWO existing background
threads there had to share one model). Keeping this tool's model fully
separate is also what lets it run standalone, at any time, without
requiring the rest of Luno's runtime/bootstrap to be started at all.

Usage:
    python tools/vision_debug_viewer.py
    python tools/vision_debug_viewer.py --confidence 0.5 --device cpu
    python tools/vision_debug_viewer.py --rtsp rtsp://user:pass@host:554/stream1
    python tools/vision_debug_viewer.py --model yolo11n.pt --headless

Keyboard (windowed mode only):
    Q or ESC  - quit
    P         - pause / unpause the feed
    D         - toggle detailed per-box detection labels
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import cv2  # noqa: E402

from luno import config as luno_config  # noqa: E402
from luno.vision import camera_source, _device_arg  # noqa: E402  (read-only reuse, no shared state)


WINDOW_NAME = "Luno Vision Debug"
PERSON_CLASS_ID = 0  # COCO convention (yolo11n.pt / yolov8n*.pt) - see _is_person()'s own docstring.


# ─────────────────────────────────────────────────────────────
# Pure logic (unit-tested, no cv2/torch/ultralytics touched here)
# ─────────────────────────────────────────────────────────────

class Detection:
    """One parsed YOLO detection - plain data, no ultralytics/torch
    types held onto past parse time (keeps the rest of this module,
    and its tests, fully independent of whichever backend produced
    the raw result)."""

    __slots__ = ("class_id", "class_name", "confidence", "bbox")

    def __init__(self, class_id: int, class_name: str, confidence: float, bbox: Tuple[float, float, float, float]):
        self.class_id = class_id
        self.class_name = class_name
        self.confidence = confidence
        self.bbox = bbox

    def __repr__(self) -> str:  # pragma: no cover - debug convenience only
        return f"Detection(class_id={self.class_id}, class_name={self.class_name!r}, confidence={self.confidence:.3f})"


def _is_person(det: Detection) -> bool:
    """Class ID 0 is COCO's own "person" class and is what `yolo11n.pt`/
    `yolov8n-pose.pt` (Luno's actual configured models) both use - the
    PRIMARY signal. The class-name check is a defensive fallback only,
    for the (currently hypothetical) case of a differently-ordered
    custom model - it never overrides a genuine class_id==0 match, and
    a name of "person" with a non-zero id is still counted (explicit,
    not silently dropped)."""
    return det.class_id == PERSON_CLASS_ID or det.class_name.strip().lower() == "person"


def parse_yolo_result(result: Any, class_names: Dict[int, str], min_confidence: float = 0.0) -> List[Detection]:
    """Convert ONE ultralytics `Results` object (or anything shaped like
    one - `.boxes.cls`/`.conf`/`.xyxy`, each either a tensor with
    `.tolist()` or a plain list, exactly like this project's existing
    Vision test doubles) into a list of `Detection`, filtering out
    anything below `min_confidence`. Pure - never touches cv2, torch,
    or the network. Returns `[]` for a `None`/box-less result (an empty
    scene is not an error)."""
    detections: List[Detection] = []
    boxes = getattr(result, "boxes", None)
    if boxes is None:
        return detections

    def _as_list(value):
        return value.tolist() if hasattr(value, "tolist") else list(value)

    cls_list = _as_list(boxes.cls)
    conf_list = _as_list(boxes.conf)
    xyxy_list = _as_list(boxes.xyxy)

    for cls_id, conf, bbox in zip(cls_list, conf_list, xyxy_list):
        conf_f = float(conf)
        if conf_f < min_confidence:
            continue
        cls_id_i = int(cls_id)
        if isinstance(class_names, dict):
            name = class_names.get(cls_id_i, f"class_{cls_id_i}")
        else:
            name = str(cls_id_i)
        detections.append(Detection(cls_id_i, str(name), conf_f, tuple(float(v) for v in bbox)))
    return detections


def summarize_detections(detections: Sequence[Detection]) -> Dict[str, Any]:
    """Pure aggregation - `human_detected` is computed ONLY from THIS
    frame's own `detections` list (requirement: no synthetic events,
    no inference from prior state - every call is a fresh, independent
    verdict on exactly what was just detected)."""
    person_count = sum(1 for d in detections if _is_person(d))
    ranked = sorted(detections, key=lambda d: d.confidence, reverse=True)
    return {
        "object_count": len(detections),
        "person_count": person_count,
        "human_detected": person_count > 0,
        "top_labels": [f"{d.class_name}={d.confidence:.2f}" for d in ranked],
    }


def format_diagnostics_lines(
    *,
    camera_online: bool,
    model_filename: str,
    backend: str,
    fps: float,
    frame_wh: Tuple[int, int],
    summary: Dict[str, Any],
    inference_ms: float,
    error_text: Optional[str] = None,
) -> List[str]:
    """The exact text lines the on-screen diagnostics overlay shows -
    kept as a pure function (list[str] in, list[str] out) so the test
    suite can check its content without needing a real cv2 window."""
    w, h = frame_wh
    lines = [
        f"Camera: {'ONLINE' if camera_online else 'OFFLINE'}",
        f"Model: {model_filename}",
        f"Backend: {backend}",
        f"FPS: {fps:.1f}",
        f"Resolution: {w}x{h}",
        f"Objects: {summary['object_count']}",
        f"Persons: {summary['person_count']}",
        f"Human detected: {'YES' if summary['human_detected'] else 'NO'}",
        f"Inference: {inference_ms:.1f} ms",
    ]
    if error_text:
        lines.append(f"ERROR: {error_text}")
    return lines


def format_console_line(fps: float, summary: Dict[str, Any]) -> str:
    """The once-a-second console line, e.g.:
        [YOLO] FPS=28.4 objects=2 persons=1
               person=0.91 chair=0.74
    Second line only appears when there is at least one detection."""
    header = f"[YOLO] FPS={fps:.1f} objects={summary['object_count']} persons={summary['person_count']}"
    if not summary["top_labels"]:
        return header
    return header + "\n       " + " ".join(summary["top_labels"])


def resolve_model_path(model_path: str) -> str:
    """Resolves `model_path` to an absolute path and requires it to
    already exist on disk - deliberately NEVER hands a bare/relative
    name straight to `ultralytics.YOLO(...)`, because ultralytics
    recognizes bare names like "yolo11n.pt" as official pretrained
    aliases and will silently AUTO-DOWNLOAD a (possibly different-
    version) replacement if the local file is missing. That is exactly
    the behavior this diagnostic tool must NOT have (it exists to show
    the TRUTH about the model Luno actually has on disk, not paper over
    a missing one) - so a missing model is a hard, clearly-reported
    error here instead."""
    abs_path = model_path if os.path.isabs(model_path) else os.path.join(_ROOT, model_path)
    abs_path = os.path.abspath(abs_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(
            f"YOLO model file not found: {abs_path}\n"
            f"This viewer will NOT auto-download a replacement model. "
            f"Expected the same file Luno's production Vision pipeline uses "
            f"(luno.config.YOLO_MODEL_PATH = {model_path!r}). Place the "
            f"correct model file at the path above, or pass --model <path> "
            f"to point this viewer at a different one."
        )
    return abs_path


def _compute_fps(frame_times: deque) -> float:
    if len(frame_times) < 2:
        return 0.0
    span = frame_times[-1] - frame_times[0]
    if span <= 0:
        return 0.0
    return (len(frame_times) - 1) / span


# ─────────────────────────────────────────────────────────────
# cv2 drawing (thin, not unit-tested - all decision logic above is)
# ─────────────────────────────────────────────────────────────

_PERSON_COLOR = (0, 255, 0)      # bright green - class ID 0 / person
_OBJECT_COLOR = (0, 200, 255)    # amber - everything else
_OVERLAY_BG = (0, 0, 0)
_OVERLAY_FG = (255, 255, 255)
_ERROR_COLOR = (0, 0, 255)


def _draw_detections(frame, detections: Sequence[Detection], show_detail: bool) -> None:
    for det in detections:
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        is_person = _is_person(det)
        color = _PERSON_COLOR if is_person else _OBJECT_COLOR
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        if is_person:
            label = f"PERSON {det.confidence * 100:.0f}%"
        elif show_detail:
            label = f"{det.class_name} (id={det.class_id}) {det.confidence * 100:.0f}%"
        else:
            label = f"{det.class_name} {det.confidence * 100:.0f}%"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, y1), color, -1)
        cv2.putText(frame, label, (x1 + 2, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)


def _draw_overlay(frame, lines: Sequence[str]) -> None:
    pad = 6
    line_h = 20
    box_w = max((cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0][0] for line in lines), default=200) + 2 * pad
    box_h = line_h * len(lines) + pad
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (box_w, box_h), _OVERLAY_BG, -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, dst=frame)
    for i, line in enumerate(lines):
        color = _ERROR_COLOR if line.startswith("ERROR:") else _OVERLAY_FG
        cv2.putText(frame, line, (pad, pad + (i + 1) * line_h - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)


def _blank_frame(width: int = 640, height: int = 480, text: str = "NO SIGNAL"):
    frame = _np_zeros(height, width)
    cv2.putText(frame, text, (20, height // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2, cv2.LINE_AA)
    return frame


def _np_zeros(height: int, width: int):
    import numpy as np
    return np.zeros((height, width, 3), dtype="uint8")


# ─────────────────────────────────────────────────────────────
# CLI / main loop
# ─────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone real-YOLO debug viewer for Luno's Vision pipeline (read-only, does not touch the production camera automation pipeline).",
    )
    parser.add_argument("--confidence", type=float, default=None, help="Minimum detection confidence 0-1 (default: luno.config.CONFIDENCE_THRESHOLD, same as the production tracked-cycle pipeline).")
    parser.add_argument("--device", type=str, default=None, help='Inference device, e.g. "cpu" or "0" (default: same selection luno.vision._device_arg() makes from config.USE_GPU).')
    parser.add_argument("--model", type=str, default=None, help="Path to a .pt YOLO model file (default: luno.config.YOLO_MODEL_PATH). Never auto-downloaded if missing.")
    parser.add_argument("--rtsp", type=str, default=None, help="RTSP/HTTP camera URL, or a plain integer for a local device index (default: luno.vision.camera_source(), i.e. config.CAMERA_URL or config.CAMERA_INDEX).")
    parser.add_argument("--headless", action="store_true", help="No OpenCV window - inference + console-line output only (useful with no display attached).")
    return parser


def _resolve_source(cli_rtsp: Optional[str]):
    if cli_rtsp is None:
        return camera_source()
    stripped = cli_rtsp.strip()
    if stripped.isdigit():
        return int(stripped)
    return stripped


def _load_and_verify_model(model_path: str, device: str, confidence: float):
    """Loads the model AND proves it actually performs detection (not
    merely that construction succeeded) via one real inference call on
    a synthetic warmup frame, per this tool's own requirement to verify
    genuine detection capability rather than just object construction.
    Raises on failure - caller decides how to report it."""
    from ultralytics import YOLO  # deliberately lazy - see module docstring

    print(f"[Viewer] Loading model: {model_path}")
    model = YOLO(model_path)
    class_names = model.names
    print(f"[Viewer] Model loaded - {len(class_names)} classes known, device={device!r}, confidence={confidence}")

    warmup = _np_zeros(480, 640)
    warmup_results = model(warmup, verbose=False, conf=confidence, device=device)
    if not warmup_results or not hasattr(warmup_results[0], "boxes"):
        raise RuntimeError(
            "Model loaded but the warmup inference call did not return a "
            "valid Results object (missing .boxes) - this model is NOT "
            "actually performing detection."
        )
    print("[Viewer] Inference self-test OK - model genuinely runs detection, not just loads.")
    return model, class_names


def run(args: argparse.Namespace) -> int:
    model_path = resolve_model_path(args.model or luno_config.YOLO_MODEL_PATH)
    device = args.device or _device_arg()
    confidence = args.confidence if args.confidence is not None else luno_config.CONFIDENCE_THRESHOLD
    source = _resolve_source(args.rtsp)

    try:
        model, class_names = _load_and_verify_model(model_path, device, confidence)
    except Exception:
        print("[Viewer] FATAL: model failed to load or failed its own inference self-test.")
        traceback.print_exc()
        return 1

    print(f"[Viewer] Opening camera source: {source!r}")
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"[Viewer] FATAL: could not open camera/RTSP source: {source!r}")
        return 1

    if not args.headless:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)

    paused = False
    show_detail = True
    last_console_ts = 0.0
    frame_times: deque = deque(maxlen=30)
    detections: List[Detection] = []
    summary = summarize_detections(detections)
    inference_ms = 0.0
    error_text: Optional[str] = None
    frame = _blank_frame(text="STARTING...")
    camera_online = False

    print(f"[Viewer] Window '{WINDOW_NAME}' - Q/ESC quit, P pause, D toggle detail" if not args.headless else "[Viewer] Running headless (Ctrl+C to quit)")

    try:
        while True:
            if not paused:
                ok, raw_frame = cap.read()
                camera_online = bool(ok) and raw_frame is not None
                if camera_online:
                    frame = raw_frame
                    t0 = time.monotonic()
                    try:
                        results = model(frame, verbose=False, conf=confidence, device=device)
                        inference_ms = (time.monotonic() - t0) * 1000.0
                        detections = parse_yolo_result(results[0], class_names, min_confidence=confidence)
                        summary = summarize_detections(detections)
                        error_text = None
                    except Exception as ex:
                        error_text = f"{type(ex).__name__}: {ex}"
                        print(f"[Viewer] INFERENCE ERROR: {error_text}")
                        traceback.print_exc()
                        detections = []
                        summary = summarize_detections(detections)
                else:
                    frame = _blank_frame(text="NO SIGNAL")
                    detections = []
                    summary = summarize_detections(detections)
                frame_times.append(time.monotonic())

            fps = _compute_fps(frame_times)

            now = time.monotonic()
            if now - last_console_ts >= 1.0:
                print(format_console_line(fps, summary))
                last_console_ts = now

            if not args.headless:
                display_frame = frame.copy()
                if show_detail:
                    _draw_detections(display_frame, detections, show_detail=True)
                else:
                    _draw_detections(display_frame, [d for d in detections if _is_person(d)], show_detail=False)
                lines = format_diagnostics_lines(
                    camera_online=camera_online,
                    model_filename=os.path.basename(model_path),
                    backend=device,
                    fps=fps,
                    frame_wh=(display_frame.shape[1], display_frame.shape[0]),
                    summary=summary,
                    inference_ms=inference_ms,
                    error_text=error_text,
                )
                _draw_overlay(display_frame, lines)
                cv2.imshow(WINDOW_NAME, display_frame)

                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    break
                elif key in (ord("p"), ord("P")):
                    paused = not paused
                    print(f"[Viewer] {'Paused' if paused else 'Resumed'}")
                elif key in (ord("d"), ord("D")):
                    show_detail = not show_detail
                    print(f"[Viewer] Detailed labels {'ON' if show_detail else 'OFF'}")
            else:
                time.sleep(0.03)
    except KeyboardInterrupt:
        print("[Viewer] Interrupted, shutting down.")
    finally:
        cap.release()
        if not args.headless:
            cv2.destroyAllWindows()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
