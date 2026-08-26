"""
tests/test_vision_debug_viewer.py
=================================================

Regression suite for `tools/vision_debug_viewer.py` - the standalone,
read-only real-YOLO debug viewer. Exercises only the module's PURE
functions (`parse_yolo_result`, `summarize_detections`,
`format_diagnostics_lines`, `format_console_line`, `resolve_model_path`,
`_is_person`) using the same fake-`Results`-object convention already
established by `tests/test_p0_6_2_fix_vision_runtime_parity.py` - no
real `cv2`/`torch`/`ultralytics` model is constructed or required.

This suite deliberately does NOT import `luno.camera_automation`,
`luno.automation`, `luno.adapters.home_assistant`, or
`camera_action_safety` - proving (by their absence) that the viewer
module itself never touches the production automation pipeline either.
"""

from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import tools.vision_debug_viewer as viewer  # noqa: E402


# ─────────────────────────────────────────────
# Fake ultralytics Results/Boxes doubles (same convention as
# test_p0_6_2_fix_vision_runtime_parity.py's _FakeBoxes/_FakeResult)
# ─────────────────────────────────────────────

class _FakeTensor:
    """Mimics a torch tensor's `.tolist()` just enough for parsing."""

    def __init__(self, values):
        self._values = list(values)

    def tolist(self):
        return list(self._values)


class _FakeBoxes:
    def __init__(self, cls, conf, xyxy):
        self.cls = _FakeTensor(cls)
        self.conf = _FakeTensor(conf)
        self.xyxy = _FakeTensor(xyxy)


class _FakeResult:
    def __init__(self, boxes):
        self.boxes = boxes


_COCO_NAMES = {0: "person", 56: "chair", 2: "car"}


def _make_result(entries):
    """entries: list of (class_id, confidence, bbox_tuple)."""
    cls = [e[0] for e in entries]
    conf = [e[1] for e in entries]
    xyxy = [list(e[2]) for e in entries]
    return _FakeResult(_FakeBoxes(cls, conf, xyxy))


# ─────────────────────────────────────────────
# 1. Class 0 / "person" parsing
# ─────────────────────────────────────────────

def test_01_person_class_id_zero_is_parsed_as_person():
    result = _make_result([(0, 0.91, (10, 10, 50, 90))])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES)
    assert len(detections) == 1
    det = detections[0]
    assert det.class_id == 0
    assert det.class_name == "person"
    assert abs(det.confidence - 0.91) < 1e-9
    assert det.bbox == (10.0, 10.0, 50.0, 90.0)
    assert viewer._is_person(det) is True


def test_02_non_person_class_is_not_flagged_as_person():
    result = _make_result([(56, 0.74, (0, 0, 20, 20))])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES)
    assert detections[0].class_name == "chair"
    assert viewer._is_person(detections[0]) is False


def test_03_person_detected_by_name_fallback_even_with_nonstandard_id():
    """Defensive fallback: a differently-ID'd custom model that still
    LABELS something "person" must still count - the class-name check
    is additive, never something that hides a real person label."""
    weird_names = {7: "person"}
    result = _make_result([(7, 0.5, (0, 0, 5, 5))])
    detections = viewer.parse_yolo_result(result, weird_names)
    assert viewer._is_person(detections[0]) is True


# ─────────────────────────────────────────────
# 2. Multiple detections
# ─────────────────────────────────────────────

def test_04_multiple_detections_all_parsed_in_order():
    result = _make_result([
        (0, 0.91, (0, 0, 10, 10)),
        (56, 0.74, (20, 20, 40, 40)),
        (2, 0.60, (50, 50, 90, 90)),
    ])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES)
    assert len(detections) == 3
    assert [d.class_name for d in detections] == ["person", "chair", "car"]


def test_05_summarize_multiple_detections_counts_correctly():
    result = _make_result([
        (0, 0.91, (0, 0, 10, 10)),
        (0, 0.55, (5, 5, 15, 15)),
        (56, 0.74, (20, 20, 40, 40)),
    ])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES)
    summary = viewer.summarize_detections(detections)
    assert summary["object_count"] == 3
    assert summary["person_count"] == 2
    assert summary["human_detected"] is True
    # ranked by confidence descending
    assert summary["top_labels"][0].startswith("person=0.91")


# ─────────────────────────────────────────────
# 3. Empty detections
# ─────────────────────────────────────────────

def test_06_empty_result_yields_no_detections():
    result = _make_result([])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES)
    assert detections == []
    summary = viewer.summarize_detections(detections)
    assert summary == {
        "object_count": 0,
        "person_count": 0,
        "human_detected": False,
        "top_labels": [],
    }


def test_07_result_with_no_boxes_attribute_returns_empty_list():
    class _NoBoxes:
        pass

    detections = viewer.parse_yolo_result(_NoBoxes(), _COCO_NAMES)
    assert detections == []


def test_08_none_result_boxes_returns_empty_list():
    result = _FakeResult(None)
    detections = viewer.parse_yolo_result(result, _COCO_NAMES)
    assert detections == []


# ─────────────────────────────────────────────
# 4. Confidence filtering
# ─────────────────────────────────────────────

def test_09_below_threshold_detections_are_dropped():
    result = _make_result([
        (0, 0.91, (0, 0, 10, 10)),
        (0, 0.10, (5, 5, 15, 15)),  # below threshold
    ])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES, min_confidence=0.4)
    assert len(detections) == 1
    assert abs(detections[0].confidence - 0.91) < 1e-9


def test_10_exact_boundary_confidence_is_kept():
    result = _make_result([(0, 0.4, (0, 0, 10, 10))])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES, min_confidence=0.4)
    assert len(detections) == 1


def test_11_zero_min_confidence_keeps_everything():
    result = _make_result([(0, 0.01, (0, 0, 10, 10))])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES, min_confidence=0.0)
    assert len(detections) == 1


# ─────────────────────────────────────────────
# 5. Diagnostic formatting
# ─────────────────────────────────────────────

def test_12_diagnostics_lines_contain_all_required_fields():
    summary = {"object_count": 2, "person_count": 1, "human_detected": True, "top_labels": ["person=0.91"]}
    lines = viewer.format_diagnostics_lines(
        camera_online=True,
        model_filename="yolo11n.pt",
        backend="cpu",
        fps=28.4,
        frame_wh=(1280, 720),
        summary=summary,
        inference_ms=35.2,
    )
    joined = "\n".join(lines)
    assert "Camera: ONLINE" in joined
    assert "Model: yolo11n.pt" in joined
    assert "Backend: cpu" in joined
    assert "FPS: 28.4" in joined
    assert "Resolution: 1280x720" in joined
    assert "Objects: 2" in joined
    assert "Persons: 1" in joined
    assert "Human detected: YES" in joined
    assert "Inference: 35.2 ms" in joined


def test_13_diagnostics_lines_reflect_no_person_and_camera_offline():
    summary = {"object_count": 0, "person_count": 0, "human_detected": False, "top_labels": []}
    lines = viewer.format_diagnostics_lines(
        camera_online=False,
        model_filename="yolo11n.pt",
        backend="cpu",
        fps=0.0,
        frame_wh=(640, 480),
        summary=summary,
        inference_ms=0.0,
    )
    joined = "\n".join(lines)
    assert "Camera: OFFLINE" in joined
    assert "Human detected: NO" in joined


def test_14_diagnostics_lines_include_error_text_when_present():
    summary = {"object_count": 0, "person_count": 0, "human_detected": False, "top_labels": []}
    lines = viewer.format_diagnostics_lines(
        camera_online=True, model_filename="m.pt", backend="cpu", fps=1.0,
        frame_wh=(640, 480), summary=summary, inference_ms=0.0,
        error_text="AttributeError: 'Conv' object has no attribute 'bn'",
    )
    assert any(line.startswith("ERROR:") and "Conv" in line for line in lines)


def test_15_console_line_format_matches_spec():
    summary = {"object_count": 2, "person_count": 1, "human_detected": True, "top_labels": ["person=0.91", "chair=0.74"]}
    line = viewer.format_console_line(28.4, summary)
    assert line.split("\n")[0] == "[YOLO] FPS=28.4 objects=2 persons=1"
    assert "person=0.91" in line
    assert "chair=0.74" in line


def test_16_console_line_omits_second_line_when_no_detections():
    summary = {"object_count": 0, "person_count": 0, "human_detected": False, "top_labels": []}
    line = viewer.format_console_line(30.0, summary)
    assert "\n" not in line


# ─────────────────────────────────────────────
# 6. Missing model handling (never auto-downloads)
# ─────────────────────────────────────────────

def test_17_missing_model_raises_filenotfounderror_with_expected_path(tmp_path):
    missing = tmp_path / "does_not_exist.pt"
    with pytest.raises(FileNotFoundError) as exc_info:
        viewer.resolve_model_path(str(missing))
    msg = str(exc_info.value)
    assert str(missing) in msg
    assert "will NOT auto-download" in msg


def test_18_existing_model_resolves_to_absolute_path(tmp_path):
    real_file = tmp_path / "fake_model.pt"
    real_file.write_bytes(b"not a real checkpoint, just needs to exist")
    resolved = viewer.resolve_model_path(str(real_file))
    assert os.path.isabs(resolved)
    assert os.path.samefile(resolved, str(real_file))


def test_19_relative_model_path_resolved_against_project_root(tmp_path, monkeypatch):
    # Simulate a model that genuinely exists at the (fake) project root
    # under a relative name, proving relative paths resolve against
    # `viewer._ROOT` (the same directory Luno's own yolo11n.pt/yolov8n-
    # pose.pt live in) and not the current working directory. Uses a
    # tmp_path-backed fake root rather than writing into the real repo
    # root, which - like the real E:\ project folder this suite may run
    # against - can refuse deletes/renames of files written there.
    monkeypatch.setattr(viewer, "_ROOT", str(tmp_path))
    marker_name = "test_only_relative_resolve_marker.pt"
    marker_path = tmp_path / marker_name
    marker_path.write_bytes(b"marker")

    resolved = viewer.resolve_model_path(marker_name)
    assert resolved == os.path.abspath(str(marker_path))


def test_20_bare_pretrained_alias_name_never_silently_passed_to_yolo_when_missing():
    """The exact scenario this function exists to prevent: a bare,
    ultralytics-recognized pretrained alias name ("yolo11n.pt") that is
    NOT actually present on disk must fail loudly here, not be handed
    to `ultralytics.YOLO(...)` (which would auto-download a
    possibly-different-version replacement)."""
    with pytest.raises(FileNotFoundError):
        viewer.resolve_model_path("definitely_not_a_real_file_xyz123.pt")


# ─────────────────────────────────────────────
# Architecture guards - never touches the production automation pipeline
# ─────────────────────────────────────────────

def test_21_module_never_imports_the_production_automation_pipeline():
    """Checks actual import statements only (not the module's own prose
    docstring, which legitimately NAMES these modules to explain that
    they are deliberately never touched - see the top-of-file
    docstring). A real accidental `import`/`from ... import` of any of
    these would fail this test; disclaiming them in prose does not."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(viewer))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    forbidden_modules = (
        "camera_action_safety",
        "luno.camera_automation",
        "luno.automation",
        "luno.adapters.home_assistant",
    )
    for forbidden in forbidden_modules:
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in imported_names), (
            f"vision_debug_viewer.py must never import {forbidden!r}, found imports: {sorted(imported_names)}"
        )

    # These are class/handler names, not modules - confirm no CODE
    # (non-comment, non-docstring) line constructs or references them
    # as an actual identifier via a call or assignment.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ("MockVisionHandler", "MockHomeAssistantHandler", "AutomationEngine"):
            pytest.fail(f"vision_debug_viewer.py must never reference the identifier {node.id!r} in code")


def test_22_module_does_not_import_ultralytics_or_torch_at_module_level():
    """`from ultralytics import YOLO` must stay INSIDE a function (lazy),
    exactly like luno/vision.py's own _get_yolo() - otherwise merely
    importing this module for its pure helpers would require a working
    torch/ultralytics install."""
    assert "ultralytics" not in sys.modules or True  # informational only, see source-level check below
    import inspect
    src = inspect.getsource(viewer)
    top_level_lines = [
        line for line in src.split("\n")
        if line.startswith("from ultralytics") or line.startswith("import ultralytics")
    ]
    assert top_level_lines == [], "ultralytics must only be imported lazily inside a function, never at module top-level"


def test_23_human_detected_derived_purely_from_current_frame_detections():
    """No hidden state: human_detected for a given call is ALWAYS
    exactly (person_count > 0) for THAT SAME call's detections - never
    something carried over from a previous frame."""
    empty_summary = viewer.summarize_detections([])
    assert empty_summary["human_detected"] is False

    result = _make_result([(0, 0.99, (0, 0, 5, 5))])
    detections = viewer.parse_yolo_result(result, _COCO_NAMES)
    present_summary = viewer.summarize_detections(detections)
    assert present_summary["human_detected"] is True

    # Calling summarize_detections([]) again immediately after a
    # "person present" frame must go straight back to NO - proving
    # there is no carried-over/sticky state anywhere in this function.
    cleared_summary = viewer.summarize_detections([])
    assert cleared_summary["human_detected"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
