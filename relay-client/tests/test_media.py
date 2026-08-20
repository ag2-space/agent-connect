"""Media ingress: the marker, the fetch, and the file the consumer gets.

Three things are being defended here, and each of them is a scar.

**The seam.** A consumer must never see a marker or a URL — not on a success,
not on a refusal, not when the URL was nonsense. Everything the room can write
into that field arrives here, so the parser is exercised with the shapes an
ordinary phone produces: a filename with a space (which truncates `name=`) and
one with a `]` (which truncates the whole marker, URL and all).

**The credential routing (G4).** Each clause is a review finding from the
2026-07-03 series: substring matching routed bearers to look-alike hosts,
redirect-following bounced a bearer off-origin, and trusting `Content-Length`
was an OOM vector. They are checked as functions *and* through a second HTTP
server on another origin, which is the only way to prove a header did not
travel.

**The cadence (F1).** A fetch that ran on the poll thread would convert a large
attachment into a stalled loop, and a stalled loop into duplicate delivery. The
test for it is a slow broker and a stopwatch.

Run: python3 tests/test_media.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import os
import tempfile
import time
from pathlib import Path

from fake_broker import FakeBroker

from ag2_relay_client.client import RelayClient
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.egress import EgressAllowlist
from ag2_relay_client.media import (
    CAP_BYTES,
    DEFAULT_MIME,
    TOO_LARGE,
    UNFETCHABLE,
    MediaStore,
    fetchable,
    strip_markers,
)
from ag2_relay_client.transport import same_origin, under_base

fails = 0

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend this is a photograph" * 4


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def marker(url, **hints):
    """A marker exactly as `media_marker.format_media_body` writes one."""
    bits = [f"[ag2space-media: {url}"]
    for key in ("mime", "name", "size", "kind"):
        if hints.get(key):
            bits.append(f"{key}={hints[key]}")
    return " ".join(bits) + "]"


def wire_task(wire_id="task-1", body="what is the status?", **over):
    task = {
        "id": wire_id,
        "task": f"[AG2Space @alice:ag2.space] {body}",
        "source": "ag2space",
        "channel_id": "!room:ag2.space",
        "user_id": "@alice:ag2.space",
        "access_tier": "owner",
    }
    task.update(over)
    return task


def client_for(broker, tmp, **kwargs):
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    kwargs.setdefault("instance", "test")
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp, **kwargs)
    client.prepare()
    # One retry is the budget; the pause between the two is not what is under
    # test, and a test suite that spent it would spend it on every failure case.
    client.media.retry_delay = 0.01
    return client


# --- the grammar, with the traps that are not edge cases -------------------
body, markers = strip_markers(
    "[AG2Space @alice] " + marker("https://gw/relay/v1/media/s/abc?room_id=%21r",
                                  mime="image/png", name="cat.png", size="1234",
                                  kind="m.image") + " look at this")
check(body == "[AG2Space @alice] look at this",
      "the body a consumer gets carries no marker, and no gap where one was")
check("ag2space-media" not in body and "https://" not in body,
      "and no URL either — the whole point of the seam")
check(len(markers) == 1 and markers[0].url == "https://gw/relay/v1/media/s/abc?room_id=%21r",
      "the URL is read whole, query included")
check(markers[0].mime == "image/png" and markers[0].name == "cat.png",
      "the hints ride along")
check(markers[0].kind == "m.image" and markers[0].size == 1234,
      "including the msgtype and the size the sender's client claimed")

# A filename with a space: the marker is unescaped, so `name=` truncates at it.
# Matching the writer's lossiness is how the two ends agree what was said.
_, spaced = strip_markers(marker("https://gw/relay/v1/media/s/a",
                                 name="my holiday photo.png", kind="m.image"))
check(spaced[0].name == "my", "a filename with a space truncates the name hint")
check(spaced[0].url == "https://gw/relay/v1/media/s/a",
      "the URL survives it — the truncation eats the hint, not the address")

# A filename with a `]` truncates the marker itself, taking the tail with it.
truncated = "[ag2space-media: https://gw/relay/v1/media/s/a mime=image/png name=x]y].png]"
left, hit = strip_markers(truncated)
check(hit and hit[0].url == "https://gw/relay/v1/media/s/a",
      "a `]` in the filename truncates the marker at the bracket")
check("ag2space-media" not in left, "and what is left of it never reaches the consumer")

# The hints come from the tail, never from the URL: a `name=` in a query string
# is part of the address, not a filename.
_, queried = strip_markers(marker("https://gw/relay/v1/media/s/a?name=evil.sh",
                                  name="real.png"))
check(queried[0].name == "real.png", "a name= in the URL's query is not the name hint")

check(strip_markers("no media here") == ("no media here", ()),
      "a body with no marker is returned untouched")
check(strip_markers(marker("https://gw/relay/v1/media/s/a"))[0] == "",
      "a marker-only body degrades to empty — the attachment is where it went")
check(strip_markers("[ag2space-media: ]") == ("", ()),
      "an empty marker is still stripped, and names nothing")
check(len(strip_markers(marker("https://gw/relay/a") + " and " +
                        marker("https://gw/relay/b"))[1]) == 2,
      "0..N: today's wire sends one marker per task, the parser counts past one")
check(strip_markers(marker("https://gw/relay/a", size="not-a-number"))[1][0].size == 0,
      "a size hint that is not a number is no hint at all")
check(strip_markers(marker("https://gw/relay/a", mime="IMAGE/PNG; charset=x"))[1][0].mime
      == "image/png", "a mime hint is normalised to a bare type/subtype")

# --- what this client will even try to fetch --------------------------------
check(fetchable("https://gw/relay/v1/media/s/a"), "an https URL is fetchable")
check(fetchable("http://gw/relay/v1/media/s/a"), "so is an http one")
check(not fetchable("file:///etc/passwd"),
      "a file:// address is never fetched — a room message can write anything here")
check(not fetchable("https://gw:notaport/x"),
      "a malformed port answers False rather than raising at .port access time (G4)")
check(not fetchable("") and not fetchable(None) and not fetchable("://x"),
      "and so does every other shape that is not a URL")

# --- G4: the bearer's routing, as two functions -----------------------------
check(same_origin("https://relay.example/v1/media/x", "https://relay.example/relay"),
      "the same origin is the same origin")
check(not same_origin("https://relay.example.evil/v1/media/x", "https://relay.example"),
      "a look-alike host is not — this is the substring bug, refused (G4)")
check(not same_origin("http://relay.example/x", "https://relay.example"),
      "a scheme change is a different origin")
check(same_origin("https://relay.example:443/x", "https://relay.example"),
      "the default port is filled in on both sides")
check(not same_origin("https://relay.example:8443/x", "https://relay.example"),
      "a different port is a different origin")
check(not same_origin("https://relay.example:bad/x", "https://relay.example"),
      "and a malformed port is False, not an exception out of task intake")

check(under_base("https://gw/relay/v1/media/s/a", "https://gw/relay"),
      "a URL under the gateway's base path gets the bearer")
check(under_base("https://gw/relay", "https://gw/relay"),
      "so does the base path itself")
check(not under_base("https://gw/relay-evil/v1/media/s/a", "https://gw/relay"),
      "a look-alike base path does not — the `/` boundary is the whole guard")
check(not under_base("https://gw/other/x", "https://gw/relay"),
      "nor does another path on the same host")
check(not under_base("https://elsewhere/relay/x", "https://gw/relay"),
      "nor the same path on another host")

# --- the store: where bytes land and when they leave ------------------------
with tempfile.TemporaryDirectory() as tmp:
    store = MediaStore(Path(tmp) / "media")
    check(store.deletes_on_complete, "delete-on-complete is the default")
    first = store.save("task-1", PNG, "cat.png", "image/png")
    second = store.save("task-1", PNG, "cat.png", "image/png")
    check(Path(first).read_bytes() == PNG, "the bytes are on disk")
    check(first != second,
          "two attachments with one filename get two files — exclusive create, "
          "never an overwrite")
    check(Path(first).name.endswith(".png"),
          "the extension survives: the outbound media route guesses the mime "
          "from the filename, so it decides how a re-upload renders")
    check(oct(Path(tmp).joinpath("media").stat().st_mode & 0o777) == oct(0o700),
          "the media directory is private — attachments are one bearer's mail")

    # The name is a hint from a room message, and it is never the filename: a
    # traversal in it has nowhere to go, because the store names its own files.
    hostile = store.save("task-1", PNG, "../../etc/passwd", "application/pdf")
    check(Path(hostile).parent == Path(tmp) / "media",
          "a name hint that is a path traversal still lands in the media dir")
    check("passwd" in Path(hostile).name and ".." not in Path(hostile).name,
          "with the hint reduced to a legible fragment of the name")

    store.release("task-1")
    check(not Path(first).exists() and not Path(second).exists(),
          "answering the task deletes its files")

    kept = MediaStore(Path(tmp) / "kept", retention_s=3600)
    survivor = kept.save("task-2", PNG, "cat.png", "image/png")
    kept.release("task-2")
    check(Path(survivor).exists(),
          "the age-based opt-out keeps them — the shim's archives point at them")

    # The startup sweep. Under delete-on-complete every file is an orphan: the
    # queue its Task was in was memory, so nobody alive can claim it.
    orphan = MediaStore(Path(tmp) / "media")
    left_behind = orphan.save("gone", PNG, "old.png", "image/png")
    check(orphan.sweep() == 1 and not Path(left_behind).exists(),
          "the startup sweep takes what an earlier run left behind")

    aged = MediaStore(Path(tmp) / "kept", retention_s=60)
    fresh = aged.save("task-3", PNG, "new.png", "image/png")
    old = aged.save("task-4", PNG, "old.png", "image/png")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    check(aged.sweep() == 1, "under retention the sweep takes only what aged out")
    check(Path(fresh).exists() and not Path(old).exists(), "and leaves the rest")

    # A directory in there is not this library's to delete, and a sweep must
    # never be the reason a client fails to start.
    (Path(tmp) / "media" / "subdir").mkdir()
    check(MediaStore(Path(tmp) / "media").sweep() == 0, "the sweep is files only")
    check(MediaStore(Path(tmp) / "never-made").sweep() == 0,
          "and a directory that does not exist yet sweeps to nothing")

# --- a marker becomes a file, end to end ------------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    url = f"{broker.url}/v1/media/ag2.space/abc?room_id=%21room%3Aag2.space"
    broker.on("GET", "/v1/media/ag2.space/abc", body=PNG,
              headers={"Content-Type": "image/png"})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        body=marker(url, mime="application/octet-stream", name="cat.png",
                    size=str(len(PNG)), kind="m.image") + " what is this?")]})
    broker.on("POST", "/v1/results", json={"ok": True})
    client.poll_once()

    check(client.media.store.path == client.layout.media_path,
          "with no media directory named, one lives under the instance's state "
          "dir — per instance, like everything else this client writes")

    task = client.next_task(timeout=5)
    check(task is not None and len(task.attachments) == 1,
          "the Task arrives carrying one attachment")
    got = task.attachments[0]
    check(got.ok and Path(got.path).is_file(), "which is a local file that exists")
    check(Path(got.path).read_bytes() == PNG, "holding the bytes the broker served")
    check(got.mime == "image/png",
          "with the mime from the fetch's Content-Type, not the marker's hint")
    check(got.name == "cat.png" and got.kind == "m.image",
          "the marker's name and msgtype ride along as hints")
    check(got.size == len(PNG) and got.reason == "", "and the size that was read")
    check("ag2space-media" not in task.body and broker.url not in task.body,
          "the body carries no marker and no URL")
    check(task.body == "[AG2Space @alice:ag2.space] what is this?",
          "only the words the sender typed")

    fetch = broker.took("GET", "/v1/media/ag2.space/abc")[0]
    check(fetch.header("Authorization") == "Bearer SECRET",
          "the fetch carried the poll bearer — it is the gateway's own URL")
    check(fetch.header("User-Agent") and "python-urllib" not in fetch.header("User-Agent"),
          "and the explicit User-Agent the edge requires (B1)")
    check(fetch.query == "room_id=%21room%3Aag2.space",
          "with the room_id the marker carried — the membership gate reads it")

    # Delete-on-complete, through the client this time.
    saved = got.path
    client.complete(task.id, "it is a cat")
    check(not Path(saved).exists(), "answering the task deletes the file it fetched")

    # The media directory is NOT automatically sendable. Egress policy lives in
    # one place — the roots a consumer names — and this is not one of them.
    workspace = Path(tmp) / "workspace"
    workspace.mkdir()
    another = MediaStore(client.media.store.path).save("x", PNG, "cat.png", "image/png")
    check(not EgressAllowlist([workspace]).allows(another),
          "a fetched attachment is not sendable through an allowlist that does "
          "not name the media directory")
    check(EgressAllowlist([workspace, client.media.store.path]).allows(another),
          "...and is, once the consumer adds it as an explicit root")

# --- every way a fetch fails, and the Task that is delivered anyway ---------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("POST", "/v1/results", json={"ok": True})

    def failing(wire_id, path, body=b"", status=200, headers=None, wait=5):
        """One task whose marker points at `path`; return its attachment."""
        broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
            wire_id, body=marker(f"{broker.url}{path}", name="cat.png",
                                 kind="m.image") + " please look")]})
        client.poll_once()
        task = client.next_task(timeout=wait)
        return task

    # The route 502s for everything, membership refusals included — so this is
    # a fact to report and never a permission verdict to act on.
    broker.on("GET", "/v1/media/refused", status=502, body="bad gateway")
    task = failing("task-502", "/v1/media/refused")
    check(task is not None and len(task.attachments) == 1,
          "a refused fetch still delivers the Task — never held, never rejected")
    check(not task.attachments[0].ok and "502" in task.attachments[0].reason,
          "with the attachment marked failed and the status carried")
    check(task.attachments[0].path == "", "and no path, because there is no file")
    check(task.body == "[AG2Space @alice:ag2.space] please look",
          "the body is the sender's words, marker gone, however the fetch went")
    check(broker.url not in task.attachments[0].reason,
          "the reason names no URL — that is what must not cross the seam")
    check(len(broker.took("GET", "/v1/media/refused")) == 2,
          "one budgeted retry, and then the Task goes out (never five)")

    # Oversize: read cap+1 and refuse. Content-Length is never consulted, so a
    # missing or lying one cannot OOM this client.
    broker.forget()
    client.media.cap_bytes = 64
    broker.on("GET", "/v1/media/big", body=PNG * 4,
              headers={"Content-Type": "image/png"})
    task = failing("task-big", "/v1/media/big")
    check(not task.attachments[0].ok and task.attachments[0].reason == TOO_LARGE,
          "an oversize attachment is refused, not truncated onto disk")
    check(len(broker.took("GET", "/v1/media/big")) == 1,
          "and is not retried — a file does not shrink")
    check(CAP_BYTES == 25 * 1024 * 1024,
          "the shipped ceiling is the gateway's own 25 MiB")
    client.media.cap_bytes = CAP_BYTES

    # Nothing listening at all — the shape a fetch takes when the host is gone
    # rather than answering. Same outcome: the Task, and an honest reason.
    broker.forget()
    dead = FakeBroker()
    with dead:
        dead_url = dead.url
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-dead", body=marker(f"{dead_url}/v1/media/x", name="cat.png"))]})
    client.poll_once()
    task = client.next_task(timeout=5)
    check(task is not None and not task.attachments[0].ok,
          "a host that does not answer at all delivers the Task too")
    check(task.attachments[0].reason and "127.0.0.1" not in task.attachments[0].reason,
          "with a reason that names no address")

    # None of the failures above dead-lettered anything: a Task whose
    # attachment could not be read is still a Task the agent can answer.
    check(not [r for r in broker.took("POST", "/v1/results")
               if r.json.get("status") == "rejected"],
          "a failed fetch never auto-rejects the task — that would steal the "
          "agent's chance to answer in words")

    # A URL nothing can fetch: no request, no crash, an honest attachment.
    broker.forget()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-bad-url",
        body="[ag2space-media: file:///etc/passwd name=passwd kind=m.file] read this")]})
    client.poll_once()
    task = client.next_task(timeout=5)
    check(task is not None and task.attachments[0].reason == UNFETCHABLE,
          "a file:// marker is refused without a fetch (G4: malformed URLs "
          "never crash intake)")
    check(task.attachments[0].mime == DEFAULT_MIME,
          "and its mime falls back rather than being invented")
    check(client.journal.is_accepted("task-bad-url"),
          "the task is accepted and answerable — the agent can still reply in words")

# --- G4: the bearer never leaves the gateway's origin -----------------------
with FakeBroker() as broker, FakeBroker(base_path="") as elsewhere, \
        tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("POST", "/v1/results", json={"ok": True})
    elsewhere.on("GET", "/pic.png", body=PNG, headers={"Content-Type": "image/png"})

    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-offsite", body=marker(f"{elsewhere.url}/pic.png", name="pic.png"))]})
    client.poll_once()
    task = client.next_task(timeout=5)
    check(task.attachments[0].ok, "a public URL is fetched, uncredentialed")
    offsite = elsewhere.took("GET", "/pic.png")[0]
    check(offsite.header("Authorization") == "",
          "and no bearer travelled with it — the origin is not the gateway's (G4)")
    check(offsite.header("User-Agent"), "the User-Agent still goes (B1)")

    # A look-alike path on the gateway's own host: same origin, wrong base.
    broker.forget()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-lookalike",
        body=marker(broker.url.replace("/relay", "/relay-evil") + "/pic.png"))]})
    client.poll_once()
    client.next_task(timeout=5)
    asked = [r for r in broker.requests if r.path.startswith("/relay-evil")]
    check(asked and asked[0].header("Authorization") == "",
          "a `/relay-evil/` path on the gateway host gets no bearer either")

    # A redirect while credentialed: refused, and the named host never asked.
    broker.forget()
    elsewhere.forget()
    broker.on("GET", "/v1/media/hop", status=302,
              headers={"Location": f"{elsewhere.url}/pic.png"})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-hop", body=marker(f"{broker.url}/v1/media/hop"))]})
    client.poll_once()
    task = client.next_task(timeout=5)
    check(not task.attachments[0].ok,
          "a credentialed fetch that is redirected fails rather than following")
    check(not elsewhere.requests,
          "and the host the redirect named was never asked — the bearer stayed home")

# --- F1: the fetch is off the poll thread ----------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("POST", "/v1/results", json={"ok": True})
    broker.on("GET", "/v1/media/slow", body=PNG,
              headers={"Content-Type": "image/png"}, delay=0.6)
    broker.on("GET", "/v1/tasks", json={"tasks": [
        wire_task("task-media", body=marker(f"{broker.url}/v1/media/slow",
                                            name="cat.png", kind="m.image")),
        wire_task("task-text", body="and answer this too"),
    ]})

    started = time.monotonic()
    client.poll_once()
    polled_in = time.monotonic() - started
    check(polled_in < 0.3,
          "the poll returns without waiting for the download (F1: cadence is "
          "correctness — a stalled loop is duplicate delivery)")

    first = client.next_task(timeout=5)
    check(first is not None and first.id == "task-text",
          "a task with nothing to fetch is not queued behind one that has")

    # ...and the poll thread keeps turning while the fetch runs.
    polls = len(broker.took("GET", "/v1/tasks"))
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    client.poll_once()
    check(len(broker.took("GET", "/v1/tasks")) == polls + 1,
          "and the next poll happens while the download is still in flight")

    second = client.next_task(timeout=5)
    check(second is not None and second.id == "task-media",
          "the media task is delivered when its bytes are")
    check(second.attachments[0].ok and Path(second.attachments[0].path).exists(),
          "with the file on disk by the time the consumer sees it")

# --- the lifecycle across a restart ----------------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    media_dir = Path(tmp) / "attachments"
    broker.on("GET", "/v1/media/keep", body=PNG,
              headers={"Content-Type": "image/png"})
    broker.on("POST", "/v1/results", json={"ok": True})

    client = client_for(broker, tmp, media_dir=media_dir)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-orphan", body=marker(f"{broker.url}/v1/media/keep", name="cat.png"))]})
    client.poll_once()
    task = client.next_task(timeout=5)
    orphan = Path(task.attachments[0].path)
    check(orphan.exists() and orphan.parent == media_dir,
          "the consumer's media directory is where the bytes land")

    # The process dies here: the Task was in memory, so nothing can complete it.
    restarted = client_for(broker, tmp, media_dir=media_dir, instance="test")
    check(not orphan.exists(),
          "a file no live task can claim is swept when the next run starts")

    # The opt-out keeps it, because someone else's archive is pointing at it.
    broker.forget()
    keeper = client_for(broker, tmp, media_dir=media_dir, instance="keeper",
                        media_retention_s=3600)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-kept", body=marker(f"{broker.url}/v1/media/keep", name="cat.png"))]})
    keeper.poll_once()
    kept_task = keeper.next_task(timeout=5)
    kept_path = Path(kept_task.attachments[0].path)
    keeper.complete("task-kept", "answered")
    check(kept_path.exists(),
          "under the age-based opt-out, answering the task keeps the file")
    client_for(broker, tmp, media_dir=media_dir, instance="keeper",
               media_retention_s=3600)
    check(kept_path.exists(), "and a restart's sweep leaves what has not aged out")

print("\n" + ("PASS — media ingress green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
