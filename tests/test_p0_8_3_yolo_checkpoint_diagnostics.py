"""
tests/test_p0_8_3_yolo_checkpoint_diagnostics.py
=================================================

LUNO P0.8.3 (Fix Real YOLO Inference Failure) - dedicated regression
suite for the ONE confirmed, evidence-based bug this sprint fixed.

Context (see docs/change_impact/camera_automation_p0_8_3.md for the full
audit): the user ran the real P0.8.2 live verification on the actual
machine. Pre-flight fully passed (credentials, network, RTSP, HA, safety
gate, runtime start all green) - the ONLY failure was YOLO detection
itself, every cycle:

    [Vision] YOLO detect gagal (non-fatal, dilewatin): 'Conv' object has
    no attribute 'bn'

with NO checkpoint-mismatch hint appended, even though
`luno/vision.py::_yolo_checkpoint_hint()` (added in P0.6.2-FIX,
specifically to recognize this exact failure signature) was already
present and already being called from both `detect_objects()` and
`detect_objects_tracked()`'s `except` blocks.

Root cause of the MISSING HINT (confirmed by direct inspection of the
user's own real, installed `torch` 2.13.0 - the actual `.venv/` this
project mounts, not a synthetic guess): the original condition was

    if isinstance(ex, AttributeError) and getattr(ex, "name", None) == "bn":

`AttributeError.name` is populated by CPython automatically ONLY for
attribute-lookup failures raised via the *implicit*, default
`object.__getattribute__` path. `Conv`/`ConvTranspose` are
`torch.nn.Module` subclasses, and `self.bn` failing after `fuse()` is
instead raised by `torch.nn.modules.module.Module.__getattr__`'s own
hand-written

    raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

- a plain, message-only construction (confirmed present, unchanged, in
`torch/nn/modules/module.py` of the actually-installed `torch==2.13.0`)
that never sets `.name`. The pre-existing `test_12_detect_objects_
tracked_records_the_conv_bn_error_distinctly` test in
`tests/test_p0_6_2_fix_vision_runtime_parity.py` had already documented
this exact gap in its own comment ("a manually-constructed
AttributeError(message) does not get `.name` populated automatically")
but worked AROUND it by manually setting `ex.name = "bn"` on its test
double - which is not how the real exception actually looks in
production. That test still passes (nothing here weakens it) but it was
never proof the real production code path worked; this file adds the
test that IS.

Fix (single, minimal, additive - see `luno/vision.py::
_yolo_checkpoint_hint()`): also match the exception's own string message
against `_YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE` (`'(?:Conv|ConvTranspose)'
object has no attribute 'bn'`) - the `.name` check is kept (not
replaced), the message check is the new, reliable primary path. No
other function, file, schema, event type, or existing test assertion
was touched.
"""

from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.vision as vision  # noqa: E402


def _raise_real_torch_module_getattr_style(class_name: str, attr_name: str) -> AttributeError:
    """Byte-for-byte reproduction of `torch.nn.modules.module.Module.
    __getattr__`'s own raise statement (confirmed against the actually-
    installed `torch==2.13.0` in this repo's `.venv/Lib/site-packages/
    torch/nn/modules/module.py`) - NOT a `.name`-carrying stand-in. This
    is what a real `Conv`/`ConvTranspose` instance's `self.bn` access
    actually raises once `.bn` has been `delattr`'d (by ultralytics'
    own `BaseModel.fuse()`) but something still calls the un-fused
    `forward()` (which references `self.bn`) instead of the fused
    `forward_fuse()`."""
    try:
        raise AttributeError(f"'{class_name}' object has no attribute '{attr_name}'")
    except AttributeError as ex:
        return ex


# ============================================================================
# A. The confirmed bug, fixed - real torch-shaped exception now matches
# ============================================================================

def test_01_real_torch_style_conv_bn_error_has_no_name_attribute():
    """Sanity-check the premise itself: a manually-raised AttributeError
    (the only kind `Module.__getattr__` ever produces) genuinely has
    `.name is None` - if this ever stops being true (e.g. a future
    torch release adds `name=` to that raise), the fix below still works
    unchanged (it checks the message too), but this test would need
    revisiting."""
    ex = _raise_real_torch_module_getattr_style("Conv", "bn")
    assert getattr(ex, "name", None) is None


def test_02_hint_fires_for_real_torch_shaped_conv_bn_error():
    ex = _raise_real_torch_module_getattr_style("Conv", "bn")
    hint = vision._yolo_checkpoint_hint(ex)
    assert hint, "the hint must fire for the REAL production exception shape, not just a .name-carrying test double"
    assert "checkpoint" in hint.lower()
    assert "ultralytics" in hint.lower()


def test_03_hint_fires_for_real_torch_shaped_convtranspose_bn_error():
    """`_yolo_checkpoint_hint()`'s own docstring says it also covers
    `ConvTranspose` - prove that branch too, not just `Conv`."""
    ex = _raise_real_torch_module_getattr_style("ConvTranspose", "bn")
    hint = vision._yolo_checkpoint_hint(ex)
    assert hint, "the hint must also fire for the ConvTranspose variant of this failure"


def test_04_hint_mentions_the_actual_configured_model_paths():
    from luno import config
    ex = _raise_real_torch_module_getattr_style("Conv", "bn")
    hint = vision._yolo_checkpoint_hint(ex)
    assert config.YOLO_MODEL_PATH in hint
    assert config.YOLO_POSE_MODEL_PATH in hint


# ============================================================================
# B. No regression - the original `.name`-based path still works
# ============================================================================

def test_05_original_name_based_path_still_works():
    """A genuinely interpreter-raised AttributeError (default
    `object.__getattribute__`, which DOES set `.name` in Python 3.10+)
    must still be recognized - the fix ADDS a path, it does not remove
    the original one."""
    class Conv:
        __slots__ = ()

    try:
        Conv().bn  # type: ignore[attr-defined]
    except AttributeError as ex:
        assert getattr(ex, "name", None) == "bn"
        hint = vision._yolo_checkpoint_hint(ex)
        assert hint, "the pre-existing .name-based recognition path must still work"
    else:
        pytest.fail("expected AttributeError")


def test_06_existing_p0_6_2_fix_test_double_still_matches():
    """The exact test double `tests/test_p0_6_2_fix_vision_runtime_
    parity.py::test_12` already used (`.name` manually set) - that
    existing, unmodified test must keep passing unchanged."""
    ex = AttributeError("'Conv' object has no attribute 'bn'")
    ex.name = "bn"
    assert vision._yolo_checkpoint_hint(ex)


# ============================================================================
# C. No false positives - every other failure kind is still ignored
# ============================================================================

def test_07_unrelated_attribute_error_is_not_matched():
    ex = _raise_real_torch_module_getattr_style("Foo", "bar")
    assert vision._yolo_checkpoint_hint(ex) == ""


def test_08_non_conv_class_missing_bn_is_not_matched():
    """Only `Conv`/`ConvTranspose` - a hypothetical unrelated class that
    happens to also be missing a `.bn` attribute must not false-positive
    into "delete your YOLO checkpoint" advice."""
    ex = _raise_real_torch_module_getattr_style("Linear", "bn")
    assert vision._yolo_checkpoint_hint(ex) == ""


def test_09_conv_missing_a_different_attribute_is_not_matched():
    ex = _raise_real_torch_module_getattr_style("Conv", "weight")
    assert vision._yolo_checkpoint_hint(ex) == ""


def test_10_non_attribute_error_is_never_matched():
    for ex in (RuntimeError("camera timeout"), ValueError("bad frame"), OSError("no such device")):
        assert vision._yolo_checkpoint_hint(ex) == ""


def test_11_key_error_is_never_matched():
    """`isinstance(ex, AttributeError)` must gate everything - a
    `KeyError` whose string repr happens to contain similar-looking text
    must not match."""
    ex = KeyError("'Conv' object has no attribute 'bn'")
    assert vision._yolo_checkpoint_hint(ex) == ""


# ============================================================================
# D. End-to-end - the fixed hint flows through the EXISTING, unmodified
#    detect_objects()/detect_objects_tracked() -> last_*_detection_error()
#    pipeline, all the way to what a real cycle would report. No new
#    plumbing added anywhere - this proves the existing plumbing, once
#    fed a real-shaped exception, now carries the hint end-to-end.
# ============================================================================

def test_12_detect_objects_tracked_end_to_end_carries_the_hint_for_real_shaped_error():
    def _boom(*a, **kw):
        raise _raise_real_torch_module_getattr_style("Conv", "bn")

    original = vision._get_yolo_tracking
    vision._get_yolo_tracking = _boom
    try:
        result = vision.detect_objects_tracked(frame=object())
        assert result == [], "the [] / never-raises contract is unchanged"
        error = vision.last_tracked_detection_error()
        assert error is not None
        assert "AttributeError" in error
        assert "bn" in error
        assert "checkpoint" in error.lower(), (
            "end-to-end: a REAL (not .name-carrying) Conv.bn exception must now "
            "carry the actionable hint all the way into last_tracked_detection_error() - "
            "this is exactly what was missing in the user's actual live-verification log"
        )
    finally:
        vision._get_yolo_tracking = original


def test_13_detect_objects_end_to_end_carries_the_hint_for_real_shaped_error():
    """Same proof for the OTHER call site (`detect_objects()`, used by
    the plain presence-watch loop and `ask_vision()`'s hint text) - the
    user's actual pasted log line ("YOLO detect gagal") came from THIS
    function, not `detect_objects_tracked()`."""
    def _boom(*a, **kw):
        raise _raise_real_torch_module_getattr_style("Conv", "bn")

    original = vision._get_yolo
    vision._get_yolo = _boom
    vision._last_presence_detection_error = None
    try:
        result = vision.detect_objects(frame=object())
        assert result == []
        error = vision.last_presence_detection_error()
        assert error is not None
        assert "checkpoint" in error.lower()
    finally:
        vision._get_yolo = original
        vision._last_presence_detection_error = None


def test_14_pose_estimation_also_carries_the_hint_for_real_shaped_error(capsys):
    """`attach_pose_keypoints()` also calls `_yolo_checkpoint_hint()` in
    its own except block (a third call site) - only reachable when a
    person was already found, so this is exercised via a direct call
    with a fake detection rather than the full detect pipeline."""
    from luno.vision_tracking import RawDetection

    def _boom(*a, **kw):
        raise _raise_real_torch_module_getattr_style("Conv", "bn")

    original = vision._get_yolo_pose
    vision._get_yolo_pose = _boom
    try:
        detections = [RawDetection(label="person", confidence=0.9, bbox=(0.0, 0.0, 1.0, 1.0))]
        result = vision.attach_pose_keypoints(object(), detections)
        assert result == detections, "must degrade gracefully - same detections, no keypoints"
        printed = capsys.readouterr().out
        assert "checkpoint" in printed.lower()
    finally:
        vision._get_yolo_pose = original


# ============================================================================
# E. Architecture guard - the fix stays exactly where it belongs
# ============================================================================

def test_15_fix_lives_only_in_yolo_checkpoint_hint_no_new_functions_invented():
    """Minimal-change guard: the fix must be entirely inside the
    existing `_yolo_checkpoint_hint()` function (plus its new module-
    level regex constant) - no new detection engine, no second
    diagnostic mechanism, no new YOLO pipeline."""
    src = inspect.getsource(vision._yolo_checkpoint_hint)
    assert "_YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE" in src
    assert 'getattr(ex, "name", None)' in src, "the original .name path must still be present in the source, not deleted"


def test_16_no_new_yolo_model_or_pipeline_was_introduced():
    """Invariants B/C from the brief - `RealVisionSource` remains the
    only real camera/YOLO pipeline, no second one was added anywhere in
    this sprint's own files."""
    vision_src = inspect.getsource(vision)
    assert vision_src.count("from ultralytics import YOLO") == 2, (
        "expected exactly the two pre-existing import sites (_get_yolo, _get_yolo_pose) - "
        "no new model-loading call site was added"
    )


def test_17_p0_8_safety_gate_source_is_untouched_by_this_sprint():
    """Invariant K - `luno/automation/camera_action_safety.py` (the
    P0.8.0 safety gate) is not imported, referenced, or modified by
    anything in `luno/vision.py`'s YOLO section."""
    src = inspect.getsource(vision)
    assert "camera_action_safety" not in src
    assert "validate_camera_ha_action" not in src


def test_18_regex_only_matches_the_exact_conv_bn_signature():
    """Guard against the regex being accidentally widened later - it
    must anchor on the literal class names and the literal 'bn'
    attribute, not become a generic AttributeError catch-all."""
    assert vision._YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE.search("'Conv' object has no attribute 'bn'")
    assert vision._YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE.search("'ConvTranspose' object has no attribute 'bn'")
    assert not vision._YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE.search("'Conv' object has no attribute 'weight'")
    assert not vision._YOLO_CHECKPOINT_ATTRIBUTE_ERROR_RE.search("'BatchNorm2d' object has no attribute 'bn'")
