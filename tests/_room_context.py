"""Synthetic broker packet shared by end-to-end consumer regressions."""
import time


def wire(task_id="context-task", room="!opaque-room", tier="owner", **over):
    now = int(time.time())
    binding = {
        "schema_version": 1, "context_id": "context-one",
        "agent_id": "@worker:example.test", "room_id": room,
        "task_id": task_id, "anchor_event_id": "$anchor",
        "access_tier": tier, "policy_revision": "policy-one",
        "membership_epoch": "membership-one", "visibility": "all",
        "observed_at": now, "expires_at": now + 600,
    }
    context = dict(binding, status="partial",
                   coverage={"completeness": "unknown", "truncated": False},
                   room={"name": "Synthetic project", "topic": "context fixture", "category": "Research"},
                   readme={"text": "README_SENTINEL: release uses amber labels.",
                           "content_hash": "hash-one", "truncated": False},
                   members=[{"user_id": "@alice:example.test", "display_name": "Alice"}],
                   messages=[{"event_id": "$old", "sender": "@alice:example.test",
                              "body": "HISTORY_SENTINEL: decision is amber.", "selection": "policy"}])
    task = {"id": task_id, "task": "CURRENT_ASK: What is the decision?",
            "channel_id": room, "user_id": "@alice:example.test",
            "source_message_id": "$anchor", "access_tier": tier,
            "room_context_binding": binding, "room_context": context}
    task.update(over)
    return task
