"""Room metadata suppression, exact failure accounting and archive separation."""
import _bootstrap  # noqa: F401
import asyncio
from agent_connect.reporter import TurnReporter, LadderSettings
from agent_connect.events import ToolStarted, ToolFinished, PermissionAsked, TurnContext, FAILED

fails = 0
def check(condition, name):
    global fails
    print(("ok " if condition else "FAIL ") + name)
    fails += not condition

class Ops:
    available = True
    def __init__(self):
        self.bodies = []
        self.edits = 0
    async def message(self, room, body, *args):
        self.bodies.append(body)
        return "$placeholder"
    async def edit(self, room, event, body):
        self.bodies.append(body)
        self.edits += 1

raw = "exec: code: secret_value(); path: /private/example"
async def exercise(live):
    ops = Ops()
    reporter = TurnReporter(ops, LadderSettings(live=live, throttle=0), lambda: 100)
    await reporter.start(TurnContext(prompt="test", room="!room:example.org"))
    for i in range(56):
        await reporter.on_event(ToolStarted(tool_id=str(i), title=raw))
    check(ops.edits == int(live), "generic activity edited at most once")
    for event in (
        ToolFinished(tool_id="a", title=raw, status=FAILED),
        ToolFinished(tool_id="b", title=raw, status=FAILED),
        ToolFinished(tool_id="c", status=FAILED),
        ToolFinished(tool_id="c", status=FAILED),
        ToolFinished(status=FAILED), ToolFinished(status=FAILED),
    ):
        await reporter.on_event(event)
    await reporter.on_event(PermissionAsked(title=raw, allowed=False, reason="owner did not allow"))
    await reporter.finish("Done.")
    visible = "\n".join(ops.bodies)
    check(raw not in visible and "— did:" not in visible, "no raw tool metadata in room")
    check("5 reported tool failures" in visible, "same-title and untitled failures count; duplicate ids do not")
    check("refused (1 operation)." in visible and "by the permission policy" not in visible, "neutral refusal attribution")
    archive = reporter.reject(FAILED, "bridge exited")
    check(archive.startswith("[no-send]") and raw in archive, "raw diagnostic details retained only in no-send archive")
    diagnostics = archive.split("Operator diagnostics (last 50 events):\n", 1)[1].splitlines()
    check(len(diagnostics) == 50 and all(len(line) <= 500 for line in diagnostics),
          "archive diagnostics are bounded by count and size")
    clean = TurnReporter(None)
    await clean.on_event(ToolStarted(title=raw))
    check(await clean.finish("Done.") == "Done.", "successful answer has no activity footer")

for live in (True, False):
    asyncio.run(exercise(live))
print("PASS" if not fails else f"FAIL: {fails}")
raise SystemExit(bool(fails))
