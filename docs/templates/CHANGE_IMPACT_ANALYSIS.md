# Change Impact Analysis Template

Copy this block into your change description (PR, commit message, or
task notes) for any change that touches a Protected Core subsystem or a
Contract listed in `ARCHITECTURE_GUARD.md`. Not required for
documentation-only or test-only changes.

```
FEATURE:
WHY:

FILES TO CHANGE:
-

DIRECTLY AFFECTED SUBSYSTEMS:
-

INDIRECTLY AFFECTED SUBSYSTEMS:
-

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
-

EXPECTED REGRESSION RISKS:
-

TESTS TO RUN:
-

NEW TESTS REQUIRED:
-

ROLLBACK PLAN:
-
```

## Notes

- "Directly affected" = files you are editing.
- "Indirectly affected" = consumers found by searching for references
  before changing a public interface (function signature, event type,
  config key, Event data shape) - see `ARCHITECTURE_GUARD.md` §13/§3 for
  the "search all references before deleting/renaming" rule.
- "Tests to run" should name the actual subsystem test command(s) from
  `ARCHITECTURE_GUARD.md` §5, not just "the tests."
- "Rollback plan" can be as simple as "revert this diff" for a small,
  additive change - the point is to have thought about it before
  shipping, not to over-engineer it.

This template is not enforced by tooling. It exists so a human or coding
agent has a checklist to work through before declaring a change
complete, per `ARCHITECTURE_GUARD.md`'s Feature Development Protocol.
