"""
permissions.py — Named boolean capabilities. Pure data, no logic, no I/O —
mirrors contracts.py's "no logic, no I/O" rule. Every other layer imports
from here; never the reverse.

Replaces the single `users.User.permission_level` int (0-100) that used to
be compared against per-tool/per-command thresholds with no shared meaning
between them. See docs/PERMISSIONS-PLAN.md for the full design rationale;
this module is the plan's §1 and §1.2 made real.

---------------------------------------------------------------------------
Network read vs write — defined by EFFECT, not byte direction (§6.1)
---------------------------------------------------------------------------
Every network call moves bytes both ways, so at the packet level the
distinction is meaningless — the same reasoning that would make
`open(path, 'r')` a "write" since a read mutates atime, the page cache, and
the open file table. Permission systems classify by effect on the protected
asset, not by mechanism.

    NETWORK_READ  — the call's purpose is to bring remote content IN
                    (prompt-injection surface: remote bytes enter the
                    agent's context).
    NETWORK_WRITE — the call transmits local data OUT, or causes a durable
                    effect on the remote side.

Practical test: idempotence. If this call succeeded and you replayed it,
would anything be different? GET/HEAD: no. POST/PUT/PATCH/DELETE: yes. This
lands exactly on HTTP's own safe-method split — not a coincidence, HTTP made
the distinction first, for the same reason.

Honest limit: NETWORK_READ-without-WRITE is a guardrail against accident and
against a confused agent. It is NOT an exfiltration boundary — data can
leave via a URL path, a query string, a DNS label, or request timing. A real
egress boundary can only be enforced at the network layer (an allow-listed
egress proxy), which is out of scope here — see docs/PERMISSIONS-PLAN.md §6.4.

---------------------------------------------------------------------------
BACKEND_EXEC is a location permission (§6.5)
---------------------------------------------------------------------------
`backend_access=True` doesn't change what KIND of call is allowed; it
changes WHICH CONTAINER executes it, and the container determines both what
the network reaches and what the filesystem contains. Per compose.yaml, the
agent container binds both workspace/ AND the internal data dir
(agent.db, users.db, the memory graph, config.yaml) — the sandbox container
binds workspace/ only and is LAN-isolated. The data directory is
unreachable from the sandbox by MOUNT, and private addresses are
unreachable by NETWORK TOPOLOGY — neither is a policy check, and neither
can be bypassed by an application-level bug. There is deliberately no
NETWORK_PRIVATE bool and no FILE deny rule scoped to the data dir: both
would name a fact a second time, in a weaker place, and could drift from
the mount/network config that actually enforces it.

FILE_READ/FILE_WRITE carry no scope — location does. Under BACKEND_EXEC
those same bools reach config.yaml (API keys) and users.db (the permission
table itself), so BACKEND_EXEC + FILE_WRITE is sufficient to grant yourself
every other bool. That's why ROOT and BACKEND_EXEC + FILE_WRITE are grouped
together in the `operator` template (§1.1) — not a flaw to fix, it's what
"run in the main container" means.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class Permission(str, Enum):
    # ---- Filesystem. Scope is set by location, not by the bool — see the
    #      BACKEND_EXEC docstring above and §6.5.
    FILE_READ  = "file_read"
    FILE_WRITE = "file_write"

    # ---- Network. Read vs write is by effect, not byte direction — see the
    #      module docstring above and §6.1.
    NETWORK_READ  = "network_read"
    NETWORK_WRITE = "network_write"

    # ---- Execution and location.
    BACKEND_EXEC   = "backend_exec"    # run in the main container — see module docstring, §6.5
    UNTRUSTED_EXEC = "untrusted_exec"  # effects not classifiable ahead of time

    # ---- Shaping the agent's working context: /reset, branch control,
    #      spawn_fork, nudge_fork, set_auto_rag_databanks. One sentence:
    #      change the shape of the agent's working context, as opposed to
    #      reaching something outside it.
    MANAGE_CTX = "manage_ctx"
    MODEL_SWAP = "model_swap"  # /model, set_active_model

    # ---- Memory.
    MEMORY_READ  = "memory_read"
    MEMORY_WRITE = "memory_write"

    # ---- Scheduling. CRON_CREATE is checked on the CALLER at create time
    #      AND on the stored CREATOR at run time — see modules/cron and
    #      docs/PERMISSIONS-PLAN.md §8. A demoted creator's jobs are skipped
    #      wholesale, not half-executed.
    CRON_CREATE = "cron_create"
    CRON_ADMIN  = "cron_admin"  # act on other users' jobs

    # ---- Reading user records. Mutating them is ROOT.
    USER_READ = "user_read"  # user_list, user_info

    # ---- Total authority over the instance: edit anyone's permissions,
    #      rename or merge users, shut the gateway down, deliver protected
    #      system files. Deliberately a catch-all — see the ROOT docstring
    #      below, §1.1.
    ROOT = "root"

    # ---- Access and disclosure.
    DM_ACCESS = "dm_access"  # may converse in DMs at all
    # Disclosure flag, NOT an authorisation gate — decides whether the
    # system prompt tells the agent about sensitive equipment, DM-only. A
    # category of one: every other member of this enum gates an ACTION;
    # this one gates what the agent is TOLD. See docs/PERMISSIONS-PLAN.md
    # §10.3. Don't assume every Permission gates a tool call because of
    # this member.
    EQUIPMENT_TRUSTED = "equipment_trusted"

    # ---- Misc.
    IMAGE_GEN = "image_gen"  # custom_modules/anima


# ROOT is deliberately NOT wired to imply the other members of this enum
# (see _IMPLIES below). "administer the instance itself" is a distinct
# capability, not the top of a lattice — making it imply everything would
# resurrect the permission_level ladder this module replaces, and would
# make effective_permissions() output misleading to read.
#
# ROOT means "administer the instance itself": edit anyone's permissions,
# rename or merge users, shut the gateway down, deliver protected system
# files. It is deliberately a catch-all — the set of things that amount to
# *being the operator* doesn't benefit from enumeration, because holding
# any one of them gets you the rest. Splitting them would not buy an
# expressible policy; nobody wants "may merge users but may not shut down".
#
# Because ROOT is total by definition, user-permission-modification code
# needs no "must not grant a bool the caller doesn't hold" ceiling — there
# is no smaller admin bool that could escalate through it.
#
# USER_READ stays separate: read-only user_list / user_info is genuinely
# something you'd grant without instance authority.
#
# ROOT and BACKEND_EXEC + FILE_WRITE are equivalent in ultimate power. Both
# let the holder rewrite users.db and grant themselves everything. They
# belong in the `operator` template together; granting one while
# withholding the other buys nothing.

# Implications are one level deep today; make expand() a fixpoint if that
# ever stops being true.
#
# NETWORK_WRITE entails NETWORK_READ — every write-shaped request also
# returns a response body, so it carries the inbound-content risk too.
# There is no meaningful "may POST but not GET".
#
# The filesystem pair is deliberately NOT symmetric, and the asymmetry must
# stay stated so nobody adds FILE_WRITE -> FILE_READ for tidiness: `rm`
# deletes without reading, `write_file` truncates without reading.
# (`edit_file` needs both — that's the *tool* declaring two bools, not an
# implication between them.)
_IMPLIES: dict[Permission, frozenset[Permission]] = {
    Permission.NETWORK_WRITE: frozenset({Permission.NETWORK_READ}),
}


def expand(perms: Iterable[Permission]) -> frozenset[Permission]:
    """
    Expand a set of required permissions to include their implications.

    Expansion applies to the REQUIREMENT, never to the GRANT — call this on
    `needed`, never on a user's `effective_permissions()`. Expanding the
    grant would let a NETWORK_WRITE: true override silently defeat an
    explicit NETWORK_READ: false on the same user — the more specific
    statement of intent would lose. Expanding the requirement keeps
    explicit denials authoritative.
    """
    out = set(perms)
    for p in list(out):
        out |= _IMPLIES.get(p, frozenset())
    return frozenset(out)


# All 17 permission names, for validation/backfill code that needs the full
# set without importing every call site's literal.
ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)
