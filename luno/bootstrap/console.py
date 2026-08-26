"""
console.py
==========

`ProductionConsole` - the developer command interface, preserved
end-to-end after the launcher migration: "/status /health /session
/context /memory /memquery /plans /tasks /events /reload /restart
/debug /help. All commands should continue working after the launcher
migration."

This is a THIN RELAY, not a second copy of any subsystem's logic - it
is constructed from an ALREADY-RUNNING `Runtime`/`AdapterManager`/module
set (built by `bootstrap.modules`/`bootstrap.adapters`, started by
`main.py` before this class is ever touched) and only ever reads their
existing state or calls their existing public methods
(`runtime.health()`, `runtime.reload()`, `planner_module.
memory_retriever.retrieve_memories()`, ...) - exactly the "no business
logic in main.py" rule extended to this file too, since `main.py`
itself just constructs one `ProductionConsole` and forwards typed
lines to it.

Every command's OUTPUT FORMAT deliberately mirrors `main_runtime_demo.py`'s
own `print_*` methods (same field names, same ordering) - this is not a
redesign of what these commands show, only where the code implementing
them lives, since a production launcher composes already-built,
already-running components rather than constructing its OWN private
Runtime the way the demo console does.
"""

from __future__ import annotations

import os
from collections import deque
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional, Tuple

from luno.core.events import Event

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from .launcher_config import LauncherConfig
    from .shutdown import ShutdownCoordinator
    from .supervisor import Supervisor

HELP_TEXT = """
Luno Runtime - developer console
Type plain text to simulate speech (Whisper stand-in) - published as SpeechRecognized.

Commands:
  /help                 show this help
  /status               live runtime status snapshot
  /health                Runtime + module health report
  /session                inspect the current conversation session (state/timeout/config)
  /context                 exact LLMContext that would be sent to the LLM
  /memory                    Vision Memory inspection (objects/locations/events/long-term)
  /memquery <text>             preview Memory Retrieval for a given question (no LLM call)
  /plans                          Planner inspection (current/completed/running/failed tasks)
  /tasks                             Tool Manager inspection (current tool, timing, retries)
  /events [N]                          recent event history (default 20)
  /reload                                 reload configuration/adapters without a process restart
  /restart                                  restart every registered module
  /debug on|off                               toggle the live event firehose
  /modules                                      registered modules + adapters + state
  /history                                        conversation log (USER/LUNO/SYSTEM lines)
  /mark_test [note]                                 capture the current conversation's most
                                                       recent turn as a real-world test case
                                                       (Sprint 50 - written to
                                                       tests/real_world/candidates/)
  /quit                                             graceful shutdown and exit
"""


class _EventRecord:
    __slots__ = ("seq", "type", "source")

    def __init__(self, seq: int, event_type: str, source: Optional[str]) -> None:
        self.seq = seq
        self.type = event_type
        self.source = source


class ProductionConsole:
    def __init__(
        self,
        runtime: "Runtime",
        adapter_manager: "AdapterManager",
        modules: Dict[str, Any],
        launcher_config: "LauncherConfig",
        shutdown_coordinator: "ShutdownCoordinator",
        supervisor: Optional["Supervisor"] = None,
        history_len: int = 500,
    ) -> None:
        self.runtime = runtime
        self.adapter_manager = adapter_manager
        self.modules = modules
        self.launcher_config = launcher_config
        self.shutdown_coordinator = shutdown_coordinator
        self.supervisor = supervisor

        self.planner_module = modules["planner_module"]
        self.tool_manager_module = modules["tool_manager_module"]
        self.behavior_tree_module = modules["behavior_tree_module"]
        self.session_manager = modules["session_manager"]
        self.barge_in_module = modules["barge_in_module"]
        self.vision_module = modules["vision_module"]

        self.conversation_log: Deque[Tuple[str, str]] = deque(maxlen=history_len)
        self._events: Deque[_EventRecord] = deque(maxlen=history_len)
        self._event_seq = 0
        self._debug_enabled = False
        self._sub_ids: List[str] = []
        self._wire_listeners()
        self._register_context_providers()

    # -- wiring (display-only, never behavior) -------------------------------

    def _wire_listeners(self) -> None:
        bus = self.runtime.event_bus
        self._sub_ids.append(bus.subscribe("*", self._on_any_event, priority=-1000))
        self._sub_ids.append(bus.subscribe("speech_recognized", lambda e: self._log("USER", e.get("text", ""))))
        self._sub_ids.append(bus.subscribe("assistant_response", lambda e: self._log("LUNO", e.get("text", ""))))
        self._sub_ids.append(bus.subscribe("llm_error", lambda e: self._log("SYSTEM", f"LLM error: {e.get('error')}")))

    def _on_any_event(self, event: Event) -> None:
        self._event_seq += 1
        self._events.append(_EventRecord(self._event_seq, event.type, event.source))
        if self._debug_enabled:
            print(f"[debug] {event.type} source={event.source}")

    def _log(self, channel: str, text: str) -> None:
        self.conversation_log.append((channel, text))

    def _register_context_providers(self) -> None:
        from luno import vision_memory as vm

        cb = self.runtime.context_builder
        bb = getattr(self.behavior_tree_module, "bb", None)
        cb.register_provider("conversation_memory", lambda: [
            {"role": "user" if ch == "USER" else "assistant", "content": t}
            for ch, t in list(self.conversation_log)[-10:]
        ])
        cb.register_provider("vision_memory", lambda: vm.get_world_state().to_dict())
        cb.register_provider("behavior_tree_state", lambda: self.behavior_tree_module.status_snapshot())
        cb.register_provider("planner_state", lambda: {"last_plan_id": self.planner_module.last_plan_id})
        cb.register_provider("tool_results", lambda: [self.tool_manager_module.last_result] if self.tool_manager_module.last_result else [])
        if bb is not None:
            cb.register_provider("ha_snapshot", lambda: {"door_closed": bb.room.door_closed, "light_on": bb.room.light_on})
            cb.register_provider("current_emotion", lambda: bb.emotion)
            cb.register_provider("current_activity", lambda: bb.user.activity)
        cb.register_provider("long_term_memory", lambda: [m.statement for m in vm.get_long_term_memory()])
        cb.register_provider("conversation_session", lambda: self.session_manager.status_snapshot())

    # -- command dispatch -----------------------------------------------------

    def handle_line(self, line: str) -> bool:
        """Returns False to end the console loop. Plain text (no
        leading '/') is treated as simulated speech, exactly like
        `main_runtime_demo.py`'s own console."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            self.runtime.event_bus.publish(Event(type="speech_recognized", data={"text": line, "confidence": None}))
            return True

        parts = line.split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            print(HELP_TEXT)
        elif cmd == "/status":
            self.print_status()
        elif cmd == "/health":
            self.print_health()
        elif cmd == "/session":
            self.print_session()
        elif cmd == "/context":
            self.print_context()
        elif cmd == "/memory":
            self.print_memory()
        elif cmd == "/memquery":
            if not arg:
                print("usage: /memquery <question>")
            else:
                self.print_memquery(arg)
        elif cmd == "/plans":
            self.print_plans()
        elif cmd == "/tasks":
            self.print_tasks()
        elif cmd == "/events":
            limit = int(arg) if arg.isdigit() else 20
            self.print_events(limit)
        elif cmd == "/reload":
            self.reload()
        elif cmd == "/restart":
            self.restart_all()
        elif cmd == "/debug":
            self._debug_enabled = arg.strip().lower() != "off"
            print(f"debug firehose {'on' if self._debug_enabled else 'off'}")
        elif cmd == "/modules":
            self.print_modules()
        elif cmd == "/history":
            self.print_history()
        elif cmd == "/bargein":
            self.print_bargein()
        elif cmd == "/mark_test":
            self.mark_test(note=arg)
        elif cmd in ("/quit", "/exit"):
            self.shutdown_coordinator.request_shutdown()
            return False
        else:
            print(f"unknown command: {cmd} (try /help)")
        return True

    # -- command implementations ---------------------------------------------

    def print_status(self) -> None:
        from .banner import build_runtime_status, print_runtime_status
        status = build_runtime_status(self.runtime, self.adapter_manager, self.launcher_config)
        print_runtime_status(status)

    def print_health(self) -> None:
        report = self.runtime.health()
        print("\n-- Health --------------------------------------------------------")
        print(f"  Overall: {'Healthy' if report.healthy else 'Degraded'}")
        for name, status in report.modules.items():
            state = "Healthy" if status.healthy and not status.stalled else ("Restarting" if status.stalled else "Warning")
            print(f"    {name:<20} {state}  {status.message}")
        if report.issues:
            print("  Issues:")
            for issue in report.issues:
                print(f"    - {issue}")
        print()

    def print_session(self) -> None:
        s = self.session_manager.status_snapshot()
        print("\n-- Conversation Session (wake word + session mgmt) ------------------")
        print(f"  State            : {s['state']}  (was: {s['previous_state']})")
        print(f"  Time in state    : {s['time_in_state_s']}s")
        remaining = s["seconds_remaining"]
        print(f"  Timeout remaining: {f'{remaining:.1f}s' if remaining is not None else '(not running)'}")
        print(f"  Wake count       : {s['wake_count']}")
        cfg = s["config"]
        print(f"  wake_words       : {cfg['wake_words']} (source={cfg['wake_words_source']})")
        print(f"  session_timeout_s: {cfg['session_timeout_s']}")
        print(f"  sleep_enabled    : {cfg['sleep_enabled']}")
        print()

    def print_context(self) -> None:
        ctx = self.runtime.context_builder.build()
        print("\n-- Context that would be sent to the LLM -------------------------")
        for key, value in ctx.to_dict().items():
            print(f"  {key:<20}: {value}")
        print()

    def print_memory(self) -> None:
        from luno import vision_memory as vm
        print("\n-- Vision Memory Inspection --------------------------------------")
        state = vm.get_world_state()
        events = vm.get_recent_events(limit=10)
        ltm = vm.get_long_term_memory()
        print(f"  Known Objects    : {list(state.objects.keys())}")
        print(f"  Recent Events    : {[e.description for e in events]}")
        print(f"  Long-term Memory : {[m.statement for m in ltm]}")
        print()

    def print_memquery(self, query_text: str) -> None:
        from luno.memory_retrieval import build_memory_prompt_block
        memories = self.planner_module.memory_retriever.retrieve_memories(query_text)
        print(f"\n-- Memory Retrieval preview for {query_text!r} ----------------------")
        if not memories:
            print("  (no relevant memories found - nothing would be injected)")
        else:
            for mem in memories:
                stale_tag = " [STALE]" if mem.stale else ""
                print(f"  [{mem.source}] score={mem.score:.2f}{stale_tag}  {mem.text}")
            block = build_memory_prompt_block(memories)
            print("\n  Prompt block that would be injected:")
            for block_line in block.splitlines():
                print(f"    {block_line}")
        print()

    def print_plans(self) -> None:
        print("\n-- Planner Inspection ------------------------------------------")
        plan_id = self.planner_module.last_plan_id
        if not plan_id:
            print("  (no plan created yet)")
            print()
            return
        try:
            plan = self.planner_module.planner.get_plan(plan_id)
            status = self.planner_module.planner.get_status(plan_id)
        except Exception as ex:
            print(f"  error: {ex}")
            print()
            return
        print(f"  Current Plan     : {plan.id} ({plan.status.value}) - \"{plan.source_request}\"")
        print(f"  Completed Tasks  : {status.completed_tasks}")
        print(f"  Running Tasks    : {status.current_tasks}")
        print(f"  Waiting Tasks    : {status.remaining_tasks}")
        print(f"  Failed Tasks     : {status.failed_tasks}")
        print()

    def print_tasks(self) -> None:
        print("\n-- Tool Manager Inspection ---------------------------------------")
        result = self.tool_manager_module.last_result
        print(f"  Current Tool     : {self.tool_manager_module.last_tool}")
        if result:
            print(f"  Execution time   : {result.get('execution_time_ms')}ms")
            print(f"  Status           : {result.get('status')}")
            print(f"  Result           : {result.get('message')} data={result.get('data')}")
        else:
            print("  (no tool executed yet)")
        print()

    def print_events(self, limit: int) -> None:
        print(f"\n-- Recent Events (last {limit}) --------------------------------")
        for r in list(self._events)[-limit:]:
            print(f"  #{r.seq} {r.type:<30} src={r.source or '-'}")
        print()

    def print_modules(self) -> None:
        print("\n-- Modules & Adapters -------------------------------------------")
        for name, record in self.runtime.module_manager.all_modules().items():
            print(f"  {name:<20} {record.state.value}")
        print()

    def print_history(self) -> None:
        print("\n-- Conversation Log -----------------------------------------------")
        for channel, text in list(self.conversation_log)[-40:]:
            print(f"  {channel:<10} {text}")
        print()

    def mark_test(self, note: str = "") -> None:
        """Sprint 50 (Runtime Observability) - `/mark_test [note]`, this
        project's own real-world test-data capture mechanism (Phase 7).
        A thin relay onto `luno.test_capture.mark_test_case()`, same
        "THIN RELAY, not a second copy of any subsystem's logic" rule
        this whole class already follows for every other command."""
        from luno.test_capture import mark_test_case
        case = mark_test_case(self, note=note)
        if case is None:
            print("  (nothing to mark yet - no completed turn in this conversation)")
        else:
            print(f"  marked test case {case['id']} (status=candidate) - "
                  f"{len(case['conversation'])} conversation line(s) captured")
        print()

    def print_bargein(self) -> None:
        s = self.barge_in_module.status_snapshot()
        print("\n-- Barge-In (interruptible conversation) ---------------------------")
        for key in ("thinking", "speaking", "current_mode", "emergency_active", "current_request_id", "awaiting_confirmation", "last_action"):
            print(f"  {key:<20}: {s.get(key)}")
        print()

    def reload(self) -> None:
        self.runtime.reload()
        try:
            self.adapter_manager.restart_all()
        except Exception as ex:
            print(f"  adapter restart during /reload raised: {ex}")
        try:
            self.planner_module.memory_retriever.reload_config()
        except Exception:
            pass
        new_config = self.launcher_config.reload()
        self.launcher_config.__dict__.update(new_config.__dict__)
        print("configuration reloaded")

    def restart_all(self) -> None:
        for name in list(self.runtime.module_manager.all_modules().keys()):
            try:
                self.runtime.module_manager.restart(name)
            except Exception as ex:
                print(f"  restart '{name}' failed: {ex}")
        print("all modules restarted")
