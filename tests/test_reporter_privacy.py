"""Room metadata suppression, exact failure accounting and archive separation."""
import _bootstrap  # noqa: F401
import asyncio
from agent_connect.reporter import ANSWERED, FAILED_OUTCOME, TurnReporter
from agent_connect.events import ToolStarted, ToolFinished, PermissionAsked, FAILED

fails = 0
def check(condition, name):
    global fails
    print(("ok " if condition else "FAIL ") + name)
    fails += not condition

class Ops:
    available = True
    def __init__(self):
        self.bodies = []
    async def message(self, room, body, *args):
        self.bodies.append(body)
        return "$m"
    async def edit(self, room, event, body):
        self.bodies.append(body)

raw = "exec: code: secret_value(); path: /private/example"
async def exercise():
    ops = Ops()
    reporter = TurnReporter(ops)
    for i in range(56):
        await reporter.on_event(ToolStarted(tool_id=str(i), title=raw))
    check(ops.bodies == [], "tool activity posts nothing to the room")
    for event in (
        ToolFinished(tool_id="a", title=raw, status=FAILED),
        ToolFinished(tool_id="b", title=raw, status=FAILED),
        ToolFinished(tool_id="c", status=FAILED),
        ToolFinished(tool_id="c", status=FAILED),
        ToolFinished(status=FAILED), ToolFinished(status=FAILED),
    ):
        await reporter.on_event(event)
    await reporter.on_event(PermissionAsked(title=raw, allowed=False, reason="owner did not allow"))
    outcome = reporter.finish("Done.")
    visible = outcome.body
    check(outcome.kind == ANSWERED, "an answer is answered")
    check(raw not in visible and "— did:" not in visible, "no raw tool metadata in the answer")
    check("5 reported tool failures" in visible, "same-title and untitled failures count; duplicate ids do not")
    check("refused (1 operation)." in visible and "by the permission policy" not in visible, "neutral refusal attribution")
    archive = reporter.failed(FAILED, "bridge exited")
    check(archive.kind == FAILED_OUTCOME and raw in archive.body,
          "raw diagnostic details are retained only in the failure archive")
    diagnostics = archive.body.split("Operator diagnostics (last 50 events):\n", 1)[1].splitlines()
    check(len(diagnostics) == 50 and all(len(line) <= 500 for line in diagnostics),
          "archive diagnostics are bounded by count and size")
    repeat = TurnReporter(None)
    for _ in range(60):
        await repeat.on_event(ToolStarted(tool_id="long", title="long tool"))
    await repeat.on_event(ToolStarted(tool_id="next", title="next tool"))
    await repeat.on_event(PermissionAsked(title="Write x", allowed=False))
    archive = repeat.failed(FAILED, "bridge exited").body
    check(archive.count("long tool") == 1 and "next tool" in archive,
          "repeated in-progress updates of one tool are archived once")
    check("refused: Write x\n" in archive + "\n" and "Write x — " not in archive,
          "refusal without a reason has no dangling separator")
    clean = TurnReporter(None)
    await clean.on_event(ToolStarted(title=raw))
    check(clean.finish("Done.").body == "Done.", "successful answer has no activity footer")

asyncio.run(exercise())
print("PASS" if not fails else f"FAIL: {fails}")
raise SystemExit(bool(fails))
