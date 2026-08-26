"""
luno.automation
===============

Sprint 72 (Automation Engine Dasar). See `engine.py`'s own module
docstring for the full architecture writeup - short version:

    TRIGGER -> CONDITION -> ACTION -> VERIFY -> COOLDOWN

built entirely on the existing Event Bus / Scheduler / ToolManager
(never a second one), with a typed/allowlisted domain model (no
`eval`/`exec`, no arbitrary tool dispatch - see `models.py`).
"""

from .conditions import CONDITION_INVALID, evaluate_condition
from .engine import AutomationEngine, DEFAULT_RULES_PATH, MAX_EXECUTION_DEPTH
from .models import (
    ACTION_TYPES,
    CONDITION_TYPES,
    TIME_CONDITION_TYPE,
    TRIGGER_TYPES,
    ActionResult,
    AutomationAction,
    AutomationCondition,
    AutomationExecution,
    AutomationRule,
    AutomationRuleError,
    AutomationTrigger,
    ExecutionStatus,
    rule_from_dict,
    validate_action,
    validate_condition,
    validate_rule,
    validate_trigger,
)

__all__ = [
    "AutomationEngine",
    "DEFAULT_RULES_PATH",
    "MAX_EXECUTION_DEPTH",
    "AutomationRule",
    "AutomationTrigger",
    "AutomationCondition",
    "AutomationAction",
    "AutomationExecution",
    "ActionResult",
    "ExecutionStatus",
    "AutomationRuleError",
    "TRIGGER_TYPES",
    "CONDITION_TYPES",
    "TIME_CONDITION_TYPE",
    "ACTION_TYPES",
    "rule_from_dict",
    "validate_rule",
    "validate_trigger",
    "validate_condition",
    "validate_action",
    "evaluate_condition",
    "CONDITION_INVALID",
]
