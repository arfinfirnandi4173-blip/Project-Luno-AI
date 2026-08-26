# Sprint 69 (Tapo C212 Authentication & Connection Recovery) - Change Impact

**Status:** DIAGNOSIS + TARGETED FIX (see `NEXT RECOMMENDED SPRINT` at the
end - live-camera confirmation from the user's own machine is the
remaining open item; this sandbox cannot reach a real Tapo camera).

## Scope

The brief's own title distinguishes this from Sprint 69/69.1/69.2
(`luno/vision.py`'s OpenCV/RTSP camera CAPTURE path): this sprint is
about `luno/tool_manager/builtin/real_camera_ptz.py`, the **pan/tilt
control TOOL** built on the `pytapo` library. Both subsystems read the
*same* `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` configuration
(`luno/config.py`), but they are two structurally independent code
paths with independent failure modes - this document is careful to
never conflate them, because that conflation is itself the most likely
source of the user's confusion (see "Root cause" below).

## Phase 0/1 - forensic audit and exact-point trace (read-only, no code
changed during this phase)

Files read in full: `luno/tool_manager/builtin/real_camera_ptz.py`,
`luno/tool_manager/builtin/camera_ptz.py` (mock), `luno/bootstrap/
adapters.py::_register_real_camera_ptz_handler()`, `luno/bootstrap/
launcher_config.py`, `luno/config.py`'s TAPO_* section, `luno/planner/
parser.py`'s `_classify_camera_ptz()`/`_classify_camera_save_preset()`,
`luno/planner/planner.py::_steps_to_tasks()`, `luno/planner/
executor.py`, `luno/tool_manager/result.py`, `luno/tool_manager/
manager.py`, `luno/tool_manager/tests/test_camera_ptz.py`, `tests/
test_camera_ptz_bootstrap.py`, and the actually-installed `pytapo`
3.4.18 library's own source (`__init__.py`, `const.py`, both transports'
`pytapo.py`/`klap.py`/`const.py`).

**Complete execution trace** (user command -> ... -> response):

1. `luno/planner/parser.py::_classify_camera_ptz(lower_clause)` matches
   a move verb + "camera"/"kamera" (or a bare direction word once
   "camera"/"kamera" already appears anywhere in the clause) -> returns
   `(action, target)`.
2. `IntentParser.parse()` yields a `ParsedStep(tool="camera_ptz",
   action=..., target=...)`.
3. `luno/planner/planner.py::_steps_to_tasks()` wraps it:
   `Task(tool_call=ToolCall(tool="camera_ptz", action=step.action,
   target=step.target, params=step.params))`.
4. `luno/planner/executor.py` resolves `handler = self.registry.
   get_handler(task.tool_call.tool)` - whichever handler is CURRENTLY
   registered for `"camera_ptz"`, decided ONCE, at bootstrap (see next
   section) - and calls `handler.execute(tool_call)`.
5. `RealCameraPTZHandler.execute()` dispatches to `_move`/`_center`/
   `_save_preset`/`_goto_preset`, each of which calls exactly one method
   on `self._client` (the `pytapo.Tapo` instance) inside a single
   `try/except Exception`.
6. `pytapo` performs the real, synchronous, authenticated HTTP exchange
   with the camera.
7. Any failure - network-unreachable, wrong credentials, expired
   session, a genuine API-level rejection - was, **before this sprint**,
   caught by that one broad `except Exception` and turned into
   `ToolResult.fail(..., message=f"Couldn't ... - {ex}",
   error_type="CameraPTZError", retryable=True)` - the exact honest
   exception text, but with **zero classification**: auth failure and
   "the WiFi is down" were indistinguishable to any caller.
8. That `ToolResult` flows back up through the executor as the outcome
   of that one task.

**Exact point that can produce the literal word "disconnect" -
definitively NOT this file.** A full-text search of `real_camera_ptz.
py`, `camera_ptz.py` (mock), and `luno/bootstrap/adapters.py` for
`disconnect` (case-insensitive) returns **zero matches**. Two
structurally distinct, evidence-based mechanisms were found instead:

- **The dashboard's Camera badge** (`luno/dashboard/static/index.html`,
  `cameraBadge()`, `CAMERA_STATE_BADGES.UNAVAILABLE = ['disconnected',
  'error']`, and the generic per-adapter fallback `x.connected ?
  badge('connected') : badge('disconnected')`) - driven entirely by
  `luno.vision`'s OWN `CameraState`/`camera_status()` machinery (Sprint
  69/69.1/69.2), fed by the SAME `TAPO_HOST` value via `config.
  CAMERA_URL`'s auto-derivation, but through the OpenCV/RTSP capture
  path, never through `pytapo`/`real_camera_ptz.py` at all.
  `camera_ptz` is registered purely as a **tool** (`ToolManager.
  registry.register("camera_ptz", ...)`) - it is never listed in
  `/api/adapters`, so the adapters-table "disconnected" badge cannot
  refer to it either. **This is the most likely source of what the user
  is calling "disconnect".**
- **A raw exception surfacing the word "disconnect" from inside
  `real_camera_ptz.py` itself**, if a PTZ command's underlying TCP
  connection to the camera is reset mid-request. `requests`/`urllib3`
  (used by `pytapo`'s HTTP layer) render this as `ConnectionError(...,
  ProtocolError('Connection aborted.', RemoteDisconnected('Remote end
  closed connection without response')))` - a real, well-documented
  `requests` failure mode (not invented), which DOES contain the
  substring "Disconnected", and which - before this sprint - would have
  reached the user as an undifferentiated `CameraPTZError`. This is now
  classified as `HOST_UNREACHABLE` (see below) so it is no longer
  reported as a bare, unlabeled exception string.

Because both mechanisms are plausible and this sandbox cannot reach the
user's camera (see "Live verification" below), this document does not
claim which one the user actually experienced - the new structured
classification and logging (below) are designed so the user's own next
occurrence is self-diagnosable from `error_type`/`error_class` instead
of a bare exception string.

## Phase 3 - evidence-based library/version audit

`pytapo` 3.4.18 (confirmed via `pip show pytapo`) is installed.
`pytapo.Tapo.__init__()` performs REAL, SYNCHRONOUS authentication at
**construction time** - `_isKLAP()` (a 2s-timeout auto-probe that
silently treats any `requests` exception as "not KLAP", never raises)
followed by an unconditional, real, authenticated `getBasicInfo()`
call. Any failure there propagates straight out of the constructor.
Two transports exist (`pytapo/transport/pytapo/pytapo.py`, legacy MD5/
SHA256-digest; `pytapo/transport/klap/klap.py`, newer "KLAP", delegating
to `python-kasa`) - selected automatically by the `_isKLAP()` probe,
never hardcoded. No evidence was found that C212 specifically requires
a protocol/endpoint pytapo 3.4.18 doesn't already support; a web search
for `pytapo C212 firmware authentication working stopped 2026`
(see Sources below) returned no C212-specific incompatibility reports.
A **different, well-corroborated** search (`pytapo Tapo C212
authentication KLAP "Invalid authentication data" 2025 2026`) found
multiple real, currently-open GitHub issues (`JurajNyiri/pytapo` #135,
#113; `JurajNyiri/HomeAssistant-Tapo-Control` #834, #478, #365, #1161,
#1372) describing exactly this failure text recurring after a **camera
firmware update**, across several Tapo models (not C212-specific
evidence, but a directly analogous, corroborated failure class). Per
Phase 3's explicit "don't invent endpoints, don't replace a library
without proven incompatibility" instruction, **no library replacement
or protocol change was made** - the evidence supports "library is
fine, but a firmware update on the camera itself may have changed
which protocol/credentials it now accepts", which the new
classification layer surfaces distinctly rather than papering over.

`pytapo` also already implements exactly **one bounded internal
re-authentication retry**: `Tapo.performRequest()` detects an
invalid-session error (`-40401`/`-1`), calls `self.close()`, and
retries the SAME request once (`MAX_LOGIN_RETRIES = 1`) before raising.
Phase 5's "bounded re-authentication" requirement is therefore
substantially already satisfied by the library itself - this sprint's
own layer classifies and reports that outcome rather than adding a
second retry loop on top of it (Sprint J/K test coverage below proves
no second retry loop was added).

## Phase 2/5 - classification layer (the actual code change)

New in `luno/tool_manager/builtin/real_camera_ptz.py`:
`classify_tapo_exception(ex)`, mapping a raised exception to one of a
small, closed, evidence-sourced set of categories
(`TapoErrorClass`), using ONLY strings/exception-type-names directly
confirmed present in `pytapo`'s own source (see the module's own long
comment block for the exact citation per marker - `pytapo/const.py`'s
`ERROR_CODES`, `klap.py`'s `"Invalid authentication data"`, the legacy
transport's `"Temporary Suspension: ..."` lockout message, and a small
set of well-known `requests`/socket-layer exception type names/text for
the network-unreachable case):

| Category | `error_type` | `retryable` | Evidence source |
|---|---|---|---|
| `AUTH_FAILED` | `CameraPTZAuthFailed` | `False` | `"Invalid authentication data"` (klap.py), `"Invalid login credentials"` (ERROR_CODES[-40209]), etc. |
| `SESSION_EXPIRED` | `CameraPTZSessionExpired` | `True` | `"Invalid stok value"` (ERROR_CODES[-40401]), `"INVALID_NONCE"` (-40413), `"TPAP_SESSION_TOKEN_INVALID"` (-40421) |
| `AUTH_RATE_LIMITED` | `CameraPTZAuthRateLimited` | `False` | `"Temporary Suspension: Try again in N seconds"` (legacy transport lockout) |
| `DEVICE_OFFLINE` | `CameraPTZUnreachable` | `True` | `"DEVICE_OFFLINE"`/`"ERROR_DEVICE_OFFLINE"` (ERROR_CODES[-1007]/[-20002]) |
| `PORT_UNREACHABLE` | `CameraPTZUnreachable` | `True` | `"Connection refused"` |
| `HOST_UNREACHABLE` | `CameraPTZUnreachable` | `True` | DNS/timeout/`RemoteDisconnected`/`ProtocolError`/`MaxRetryError` exception-type names and text |
| `UNKNOWN` (unchanged, pre-sprint behavior) | `CameraPTZError` | `True` | anything not matching the above - never guessed |

This satisfies Phase 5's exact required mapping ("wrong credentials ->
AUTH_FAILED not DISCONNECTED", "host unreachable -> UNREACHABLE",
"authenticated but PTZ fails -> stays the honest generic bucket, since
no evidence-based marker exists for e.g. 'motor busy'"). Applied inside
all four of `_move`/`_center`/`_save_preset`/`_goto_preset`'s existing
`except Exception` blocks - **additively**: an unrecognized exception
(e.g. a test's synthetic `RuntimeError("simulated camera offline")`)
keeps the exact pre-sprint `error_type="CameraPTZError"`, so this
change cannot regress any existing caller that pattern-matches on that
string (confirmed: `luno/tool_manager/manager.py`, `result.py`, `luno/
planner/executor.py` do not pattern-match `error_type` at all - it is a
free-form field, consumed only by whatever surfaces `ToolResult.
to_dict()` verbatim).

`luno/bootstrap/adapters.py::_register_real_camera_ptz_handler()`'s
own failure log line now uses the same classifier for a specific,
non-leaking diagnostic message (e.g. "classified as AUTH_FAILED: ...")
instead of a bare exception repr - **the fall-back-to-mock control flow
itself is completely unchanged** (still silent, still permanent for
the process lifetime, still logs once) - preserving Phase 6's explicit
"preserve existing mock fallback" requirement and every one of `tests/
test_camera_ptz_bootstrap.py`'s 5 pre-existing tests, unmodified and
still passing.

### Why the eager-construction/permanent-fallback architecture was
deliberately NOT changed

A tempting fix would be "retry `Tapo(...)` construction lazily on the
next PTZ command instead of giving up forever after one bootstrap-time
failure" - this would help exactly the "camera rebooted for 30 seconds
during Luno startup" scenario. It was NOT implemented, because:

1. Phase 6 explicitly requires preserving "existing mock fallback"
   architecture as-is, and explicitly forbids a second camera pipeline
   or new persistent/global camera state.
2. The existing bootstrap tests (`test_real_backend_without_credentials_
   stays_mocked`, `test_pytapo_construction_failure_stays_mocked_never_
   raises`) assert the CONCRETE class ends up as `MockCameraPTZHandler`
   - changing this would require either breaking those tests' contract
   or building a materially different wrapper class, which starts to
   look exactly like STOP CONDITION 7 ("would require camera-pipeline
   architecture changes") if pushed further without a clearer, user-
   confirmed need.
3. It cannot be verified from this sandbox whether the user's actual
   failure was transient-at-boot (where lazy retry would help) or a
   standing credential/firmware mismatch (where it would not) - see
   "Live verification" below.

This is intentionally left as the **Next Recommended Sprint** (see
end of this document) rather than guessed at now.

## Phase 4 - live verification

**LIVE VERIFICATION: NOT POSSIBLE**, for a specific, provable technical
reason: this sandbox has zero `TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD`/`CAMERA_PTZ_BACKEND` configured (confirmed directly via
`luno.config` attribute inspection - all empty/unset), and even if
credentials were supplied, this sandbox's cloud network has no route to
a private-LAN camera IP. No live PASS/FAIL staging (TCP reachability ->
API reachability -> auth -> PTZ) was therefore attempted or claimed.

## Phase 7 - security

- No credential value is ever printed, logged, hardcoded, or embedded
  in this document, the new test file, or any commit-adjacent artifact.
- New `_redact_credentials()` helper (defense-in-depth - direct source
  review of both `pytapo` transports confirmed neither embeds the raw
  password in any exception text today, so this is a backstop against a
  future library regression, not evidence of a current leak) strips the
  exact configured `TAPO_USERNAME`/`TAPO_PASSWORD` values from every
  outgoing failure message AND from the bootstrap registration log
  line - proven by `test_O_credential_never_appears_in_failure_
  message`, `test_O_redact_credentials_helper_direct`, `test_P_
  credential_never_appears_in_result_data`, `test_P_bootstrap_log_
  line_never_contains_credential`.
- `target` (the only per-call, caller-influenced string this tool
  accepts) is proven, structurally, to only ever be compared against
  the camera's own `getPresets()` names - never used to build a host,
  URL, or connection parameter (`test_S_target_is_only_ever_a_preset_
  name_never_a_host_override`, `test_Q_no_generic_url_or_host_
  parameter_exists_on_the_handler`).
- No new persistent storage (file/db/pickle) was introduced anywhere in
  `real_camera_ptz.py` (`test_R_module_source_has_no_disk_or_db_write_
  surface`, an AST-based static guard in the same spirit as Sprint
  69.1's own single-call-site regression check).
- No new network destination, HTTP client, or generic URL executor was
  introduced - the only network call this tool ever makes is via the
  existing, pre-authorized `self._client` (`pytapo.Tapo`), constructed
  exactly once, only from `luno.config`'s own TAPO_* values.

## Phase 8 - new test file

`tests/test_sprint69_tapo_c212_auth.py` - 27 tests, covering (at
minimum): valid-config registration, missing-username/missing-password
staying mocked, invalid-credential/unreachable/timeout construction
failures staying mocked AND now classified, auth success, per-command
auth failure (non-retryable) and session expiration (retryable),
proof no second/unbounded retry loop was added on top of pytapo's own
internal one, PTZ success for every action, an unclassified API
rejection staying the honest generic bucket, mock-fallback
functionality, credential-redaction (message + `data` + bootstrap log
line), no arbitrary URL/host execution surface, no persistent-storage
surface, explicit target-as-preset-name-only precedence, and no
regression to unrelated tool registrations. All fakes/mocks only - no
real password, host, or `pytapo` import.

## Phase 9 - regression

**Targeted:** `tests/test_sprint69_tapo_c212_auth.py` (27) + `luno/
tool_manager/tests/test_camera_ptz.py` (32) + `tests/
test_camera_ptz_bootstrap.py` (5, unmodified) + `tests/
test_camera_health_check_timeout.py` + `tests/test_camera_presence.py`
+ `tests/test_sprint69_camera_stability.py` + `tests/
test_sprint69_1_camera_dashboard_forensics.py` + `tests/
test_sprint69_2_camera_state_machine_hardening.py` = **134 passed, 0
failed**. `luno/tool_manager/tests/` + `luno/planner/tests/` (full
ToolManager + planner suites) = **183 passed, 0 failed** (60
pre-existing, unrelated `PytestReturnNotNoneWarning` warnings from
`test_tool_manager.py` - not touched by this sprint).

**Full repository sweep:** see the `## Sprint 69 (Tapo C212)` entry in
`docs/testing/regression_baseline.md` for the exact count and full
failure classification against the established baseline - never
claimed "zero regression" without that comparison.

## Phase 10 - persistent state

`config/*.json` (27 files, including `long_term_memory.json`)
SHA-256-hashed immediately before this sprint's first edit; re-hashed
after the full regression sweep completed - see `docs/testing/
regression_baseline.md` for the explicit before/after confirmation.
No credential/config structure was changed; no new token/session
persistence was added anywhere.

## Phase 11 - performance

All new classification logic (`classify_tapo_exception()`,
`_redact_credentials()`) is pure, synchronous, in-memory string
matching against a fixed tuple of markers - no network/disk I/O, no
loop over anything unbounded. Bootstrap's own registration path is
unchanged (still exactly one `Tapo(...)` construction attempt, gated
exactly as before).

## Known limitations

- The exact root cause of the user's specific "disconnect" observation
  remains unconfirmed - two plausible, evidence-based mechanisms are
  documented above, not one certain answer. The next real occurrence's
  `error_type`/`error_class` (or the dashboard's `camera_state`, from
  Sprint 69.2) should make it self-diagnosable.
- The eager-construction/permanent-mock-fallback architecture (a
  transient boot-time failure means Luno stays on the mock handler
  until a full restart) was deliberately left unchanged - see "Why...
  deliberately NOT changed" above.
- No live camera was reachable from this sandbox - see Phase 4.

## STOP CONDITIONS considered

- **#3 (live verification needed but unavailable): TRIGGERED**, for
  Phase 4 specifically - documented honestly above rather than guessed.
- **#1/#2/#5/#6/#7/#8: NOT triggered** - the auth mechanism, and enough
  of the firmware/failure-class evidence, WAS ascertainable from
  `pytapo`'s own source plus corroborating web search; no risky
  credential migration, generic network executor, or camera-pipeline
  architecture change was needed to deliver the classification layer
  above.
- **#4 (risky credential migration): NOT triggered** - `config/*.json`
  untouched, no new credential storage introduced.

## Sources

Web search results consulted during Phase 3 (community/GitHub evidence
for the corroborated firmware-triggered Tapo auth-failure class):

- [JurajNyiri/pytapo issue #135](https://github.com/JurajNyiri/pytapo/issues/135)
- [JurajNyiri/pytapo issue #113](https://github.com/JurajNyiri/pytapo/issues/113)
- [JurajNyiri/HomeAssistant-Tapo-Control issue #834](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/issues/834)
- [JurajNyiri/HomeAssistant-Tapo-Control issue #478](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/issues/478)
- [JurajNyiri/HomeAssistant-Tapo-Control issue #365](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/issues/365)
- [JurajNyiri/HomeAssistant-Tapo-Control issue #1161](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/issues/1161)
- [JurajNyiri/HomeAssistant-Tapo-Control issue #1372](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/issues/1372)

## Next Recommended Sprint

1. **User-side confirmation**: run a real "pan the camera left" command
   on the affected machine and capture the resulting `error_type`/
   `data.error_class` (now specific, e.g. `CameraPTZAuthFailed` vs
   `CameraPTZUnreachable`) plus the bootstrap startup log line - this
   will, for the first time, tell us definitively WHICH of the two
   documented "disconnect" mechanisms (or a third, still-unknown one)
   is actually occurring, without any guessing.
2. If the user confirms a genuinely TRANSIENT boot-time failure
   (camera was mid-reboot when Luno started), revisit the "lazy retry
   instead of permanent mock fallback" design deliberately deferred
   above - now with real evidence instead of a hypothesis, and with an
   explicit user go-ahead given it touches the preserved bootstrap
   architecture.
3. Resume and deliver Sprint 69.2 (OpenCV camera read-bound/backoff/
   dashboard-state hardening) - code-complete, tests passing, but its
   own documentation/regression/delivery was deferred in favor of this
   sprint; see that sprint's own tracked task.
