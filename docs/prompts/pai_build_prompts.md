# PAI build prompts — reusable patterns

> **Last updated:** 2026-05-03.
> A library of the prompt patterns that built this codebase — the
> "shape" of what works for a sprint, distilled. Use these as
> starting points, not copy-paste templates.

## The sprint-kickoff prompt pattern

Every successful sprint in this project follows the same structure.
The pattern works because it gives the assistant unambiguous
boundaries.

```
Proceed with <FEATURE NAME> V1 on staging only.

Goal:
<one-paragraph description of the user-visible outcome>

Environment rules:
- staging only
- do not touch production
- do not deploy to production
- do not modify production DB
- do not call <list of risky external systems>
- do not send WhatsApp/email
- do not call Gemini

Main principles:
1. <invariant 1>
2. <invariant 2>
3. <invariant 3>

Main scope:
A. <subsystem 1>
   Requirements:
   1. <atomic requirement>
   2. <atomic requirement>
B. <subsystem 2>
   ...
F. Safety rules
   1. <thing that must not happen>
   2. <thing that must not happen>
G. Tests
   Add tests for:
   1. <behavior to verify>
   2. <behavior to verify>
   3. no WhatsApp/Gemini calls
   4. no production coupling

Checks:
1. python -m unittest discover -v
2. flask --app run.py routes
3. flask --app run.py admin --help
4. flask db heads
5. grep/check no secrets in diff
6. git diff --stat
7. git status

Final report:
1. branch
2. commit hash
3. files changed
4. <feature-specific summary item>
5. ...
```

**Why it works:**
- The "do not" list is concrete. The assistant can map every
  potential action to "is this on the do-not list?"
- Requirements are atomic. The assistant can refuse to bundle them.
- Safety rules are repeated. The assistant can re-check them at
  every step.
- Test list is enumerated. The assistant can't forget the
  "no external calls" guard.
- Final report shape is locked. The user can compare across sprints.

## The "build a sandbox first" pattern

For any feature that will eventually call an external system (OTA,
payment, email), the V1 sprint **never makes the external call**.
Instead:

1. Build the schema, services, routes, templates, and tests for the
   internal pipeline.
2. Add a sandbox form on an admin page that drives the pipeline
   with a hand-built payload.
3. Add an AST-based isolation test that proves the new module
   doesn't import the external client by accident.
4. Document in `docs/architecture/integrations.md` that the
   external HTTP is "deferred to next sprint."

The OTA channel manager (Foundation, Import, Modify/Cancel) is
three V1 sprints all built this way. The external HTTP client
arrives last, when the schema, tests, and admin UI are already
proven.

## The "idempotency-first" pattern

For any inbound event handler:

1. Decide the event identity (`external_event_id` from upstream, or
   a deterministic hash of the payload).
2. Add a UNIQUE constraint on `(connection_id, external_event_id)`
   in a small ledger table.
3. The handler:
   - Looks up the ledger first.
   - Short-circuits if found (return ok=True / `duplicate_skipped`).
   - Does the work.
   - Writes the ledger row inside the same transaction.
4. Test the dedup branch explicitly — replay the same event_id and
   prove the second call doesn't double-write.

Pattern realized in `app/services/channel_import.py` (apply_modification,
apply_cancellation).

## The "queue-don't-decide" pattern

Whenever a workflow could either auto-apply a change or refuse it,
**never silently apply a risky change**:

1. Validate.
2. If valid AND safe → apply.
3. If valid but UNSAFE → write a `ChannelImportException` (or
   equivalent queue row) with `issue_type` and `suggested_action`,
   redirect the operator to the queue, NEVER mutate user data.

Examples:
- `apply_cancellation` refuses to cancel a checked-in booking →
  queues `cancel_unsafe_state`.
- `apply_cancellation` refuses to cancel a paid booking → queues
  `cancel_unsafe_state`.
- `import_reservation` refuses to insert if the room type has zero
  available rooms → queues `conflict`.

The queue is the operator-decision surface. The pipeline is the
auto-decision surface. **The line between them is the spec.**

## The "service layer + Result dataclass" pattern

Every state-changing operation has a service function returning a
small `Result` dataclass (or dict with `ok`, `message`, `extra`):

```python
@dataclass
class Result:
    ok: bool
    message: str
    work_order: Optional[object] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def fail(cls, msg, **extra): return cls(ok=False, message=msg, extra=extra)
    @classmethod
    def success(cls, msg, **extra): return cls(ok=True, message=msg, extra=extra)
```

Routes call services, get a Result, flash the message, and redirect.
Tests assert on `result.ok` + `result.message`. UI never has to
re-derive a "did it work?" answer from row state.

Pattern realized in `app/services/maintenance.py`,
`app/services/channels.py`, `app/services/channel_import.py`,
`app/services/cashiering.py`.

## The "AST-based isolation guard" pattern

Whenever the platform must NOT depend on a particular library /
external system, add a static guard test that walks the AST:

```python
class IsolationTests(unittest.TestCase):
    _BANNED = ('whatsapp', 'gemini', 'requests', 'urllib', 'httpx',
               'anthropic')

    def _idents(self, source):
        import ast
        names = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split('.')[0].lower())
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split('.')[0].lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, ast.Name):
                names.add(node.id.lower())
        return names

    def test_module_clean(self):
        idents = self._idents(open('app/services/<module>.py').read())
        for banned in self._BANNED:
            self.assertNotIn(banned, idents,
                             f'unexpected {banned!r} in <module>.py')
```

Substring match catches docstring sentences ("we never call
WhatsApp"); AST walk only flags real imports/calls. Pattern
realized in `tests/test_channel_import.py`,
`tests/test_maintenance.py`.

## The "live probe after deploy" pattern

After every staging deploy, before declaring the sprint done, run a
self-cleaning Python probe end-to-end on staging Postgres:

1. SSH to VPS, `source .env`, activate venv.
2. Run a Python script that:
   - Builds (or reuses) the fixtures it needs.
   - Drives every branch of the new pipeline.
   - Asserts the expected DB state after each step.
   - Cleans up everything in a `finally` block.
3. Confirms the activity log rows appeared.
4. Removes the probe script from `/tmp`.

Pattern realized in the Maintenance V1, Channel Import V1, and
Modify/Cancel V1 final reports.

## The "production untouched" assertion

Every staging-only sprint ends with:

```bash
ssh root@<vps> "systemctl show sheeza.service -p ActiveEnterTimestamp --value"
```

The timestamp must be unchanged from the same query at the start of
the sprint. This is the literal proof that production stayed quiet.
The phrase "Production untouched: CONFIRMED" with the timestamp
appears in every sprint final report.

## The "mode header" pattern (PAI-specific)

Every sprint response begins with the appropriate PAI mode header
(`PAI | NATIVE MODE` or `PAI | ALGORITHM MODE`). The header is the
contract that the assistant is operating within the sprint
conventions. See `~/.claude/CLAUDE.md`.

## The "trust the live answer" pattern (recovery)

Documentation drifts. Code doesn't. When in doubt:

| Question | Authoritative answer |
|---|---|
| What sha is in production? | `ssh root@<vps> 'cd /var/www/<prod> && git log -1'` |
| What sha is on staging? | `ssh root@<vps> 'cd /var/www/<staging> && git log -1'` |
| What's the latest migration? | `flask db heads` |
| What routes exist? | `flask routes` |
| What models exist? | `app/models.py` |

Always update the docs to match the live answer, not the other way
around.
