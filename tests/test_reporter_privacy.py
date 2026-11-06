"""Reporter-generated room text must not copy raw tool metadata."""
import _bootstrap  # noqa: F401
from agent_connect.reporter import _assemble, _summary, TurnReporter, LadderSettings

raw = "exec: title: Inspect, code: secret_value(); path: /private/example"
steps = [raw] * 56
assert _summary(steps, set(), []) == ""
assert _assemble("Done.", "", steps, set(), []) == "Done."
for live in (True, False):
    reporter = TurnReporter(None, LadderSettings(live=live))
    reporter._steps = steps
    progress = reporter._progress_body()
    assert raw not in progress and "Working…" in progress

body = _assemble("Could not finish.", "Timed out.", steps, {raw}, [raw], ["Session restarted."])
assert raw not in body and "— did:" not in body and "+50 more" not in body
assert "1 reported tool failure" in body and "refused (1 operation)" in body
assert "Timed out." in body and "Session restarted." in body
assert "2 reported tool failures" in _summary([], {"a", "b"}, [])
assert "refused (2 operations)" in _summary([], set(), [raw, raw])
print("PASS — reporter omits raw metadata and preserves outcome notices")
