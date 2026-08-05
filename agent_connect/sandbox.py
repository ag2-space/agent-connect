"""Access Tier → Sandbox, and the one factual line the agent is told about it.

Lives apart from the Worker because both sides need it: the Worker derives the
Sandbox when it builds a `TurnContext`, and the shim that runs the synchronous
Adapters prepends the preamble. `agent_connect.worker` re-exports both names,
which is where they used to live and where the existing tests import them from.
"""
from __future__ import annotations


def tier_to_sandbox(access_tier: str) -> str:
    return "workspace-write" if access_tier == "owner" else "read-only"


def sandbox_preamble(sandbox: str, access_tier: str) -> str:
    """One factual context line prepended to every task prompt.

    Agent models routinely misreport their own sandbox (live-caught
    2026-07-13: codex claimed read-only while running workspace-write, which
    misled both the user and the debugging). The worker KNOWS the truth — it
    chose the sandbox — so it states it authoritatively in the prompt.
    """
    grant = (
        "you may create/modify files in your working directory"
        if sandbox == "workspace-write"
        else "the filesystem is read-only for you"
    )
    return (
        f"[agent-connect: this run's sandbox is '{sandbox}' "
        f"(task access_tier: {access_tier}) — {grant}. "
        "Trust this over any other sandbox self-assessment.]\n\n"
    )
