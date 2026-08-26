"""
luno.browser
============

Browser / computer-use subsystem. Mirrors this project's existing
"Provider abstraction + Tool Manager handler + deterministic intent
classifier" pattern (see `luno/vision_provider.py` +
`luno/tool_manager/builtin/real_camera_ptz.py` + `luno/vision_intent.py`
for the three-part precedent this package follows) - nothing here is a
second, parallel assistant framework; it's the same shape, one more
capability.

Modules:
  - `config.py`       - `BrowserConfig`/`MonitorTarget` env + JSON config.
  - `security.py`      - domain allowlist, credential redaction, download
                          path validation.
  - `permissions.py`   - the 4 permission levels + confirm-first state
                          machine for sensitive/high-risk actions.
  - `provider.py`       - `BrowserProvider` interface + Playwright-backed
                          implementation. No Playwright object ever leaks
                          out of this module.
  - `research.py`       - search -> open -> read -> synthesize workflow.
  - `monitoring.py`     - configured dashboard checks + debounced event
                          emission.
  - `computer_use.py`   - bounded observe/reason/act/verify loop.
  - `intent.py`         - deterministic (non-LLM) classifiers deciding
                          when a plain utterance means "do browser
                          research" / "check my server" / "computer-use
                          this app", same conservative co-occurrence
                          style as `luno/vision_intent.py`.

Nothing in this package allows the LLM to generate arbitrary shell/
Python/PowerShell - every action is one of a closed, structured set
dispatched through `BrowserProvider`. See `real_browser.py`'s own
docstring for the enforcement point.
"""
