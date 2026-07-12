[English](./README.md) · [中文](./README.zh.md)

# inter-session

Agent-to-agent messaging for Claude Code sessions on the same machine. Each
Claude Code session connects to a local WebSocket bus and can send messages
to other connected sessions; incoming messages are delivered to the
receiving agent as prompts and **acted on as instructions by default**.
One session can drive another.

Implemented using Claude Code's `Monitor` tool: ms-level delivery latency,
no active polling, and no token or performance cost when there are no messages.
Does NOT require claude.ai login. No configuration needed.

Localhost only and Unix-only (macOS, Linux, WSL2) for now.

![demo](./demo.svg)

## How does this compare to subagents and agent teams?

Claude Code already has two concurrency primitives:
[subagents](https://code.claude.com/docs/en/sub-agents) (the `Agent`
tool — spawn a worker inside your session for a focused subtask) and
[agent teams](https://code.claude.com/docs/en/agent-teams) (a team of
independent CC sessions launched together for one task). inter-session
is a different axis: it connects the **long-lived Claude Code sessions
you've already opened** across terminals and projects, so they can
message each other.

| Aspect          | Subagent                                          | Agent team                                                | inter-session                                                                |
| :-------------- | :------------------------------------------------ | :-------------------------------------------------------- | :--------------------------------------------------------------------------- |
| Context         | Own window; results return to the caller          | Own window; fully independent                             | Own window; fully independent; each session keeps its user-driven conversation |
| Communication   | Reports back to the main agent only               | Teammates message each other directly                     | Peer-to-peer across every connected session                                  |
| Coordination    | Main agent manages all work                       | Shared task list with self-coordination                   | Ad-hoc — each session applies its own reaction policy                        |
| Lifecycle       | Spawned per task; exits when done                 | Spawned by lead for one task                              | Not spawned — connects sessions you already opened                           |
| Driven by       | Parent agent (programmatic)                       | Lead agent + shared task list                             | You — each session is yours; the bus only lets them message                  |
| Best for        | Focused tasks where only the result matters       | Complex work needing teammate discussion in one task      | Cross-session coordination across long-running, unrelated work               |
| Token cost      | Lower: results summarized back to the main context | Higher: each teammate is a separate Claude instance       | Adds only per-message overhead to sessions you're already running            |

**Use a subagent** when you need a quick, focused worker that returns
a summary. Your main conversation stays clean.

**Use an agent team** when teammates need to share findings, challenge
each other, and coordinate inside one task — best for parallel research
with competing hypotheses, parallel code review, and feature work where
each teammate owns a separate piece.

**Use inter-session** when you have multiple Claude Code sessions
running for unrelated long-lived work and want one to drive another —
e.g. delegating a bug fix from one project's session to another's;
running iterative loops where each side's context grows in value
across many rounds; or letting two sessions with hours of accumulated
conversation history share findings or coordinate without restarting
either side. Each session keeps its own project context, conversation
history, and tool permissions; the bus just routes messages between
them.

**Transition point**: if you find yourself copy-pasting between Claude
Code sessions you already have open, or if your agent-team task spans
multiple projects you're working in separately, inter-session is the
natural fit — your existing sessions become the team.

## Prerequisites

- Python ≥ 3.10
- Claude Code ≥ 2.1.105

## Install

In any Claude Code session:

```
/plugin marketplace add https://github.com/yilunzhang/claude-code-inter-session
/plugin install inter-session
```

Then start using it:

```
/inter-session:inter-session
```

Claude handles runtime dependency install automatically on first use — no
extra setup needed.

By default the monitor starts **lazily** — it spins up the first time
you invoke any `/inter-session:inter-session` command in a given Claude
Code session. To switch to always-on auto-start at every session open,
run `/inter-session:inter-session auto-start on` (then `/reload-plugins`).

## Examples

The first example shows the simple one-shot pattern. Examples 2 and 3
show **iterative loops** — many rounds of back-and-forth where each
session's context grows in value over time. These are the cases
subagents and agent teams *can't* do well: subagents reset between
calls, agent teams exit when the task ends.

Click any to expand.

<details>
<summary><b>Example 1 — cross-project bug fix</b> · simple one-round delegation</summary>

Two Claude Code sessions, each in a different project.

**Session A** (in `~/proj/auth`):
```
/inter-session:inter-session
→ Connecting as `auth-refactor`…
```

**Session B** (in `~/proj/payments`):
```
/inter-session:inter-session
→ Connecting as `payments-debug`…
```

**Session A** (user prompt):
```
send the bug you found to payments session and ask it to fix it.
```

**Session B** receives a notification, fixes the bug, and replies:
```
[inter-session msg=q7r8 from="auth-refactor"] null deref in checkout.py:42 — user.email is unchecked; please add a guard and verify with the existing tests
→ Edits checkout.py to add the null guard
→ Runs pytest — 47 tests pass
→ Bash: send.py --to auth-refactor --text 'done: guarded user.email at checkout.py:42; 47 tests pass'
```

**Session A** sees:
```
[inter-session msg=k2m9 from="payments-debug"] done: guarded user.email at checkout.py:42; 47 tests pass
```

The receiving agent applies guardrails before acting (see the
[Reaction policy](./skills/inter-session/SKILL.md)) — destructive
operations require explicit affirmative content; ambiguous requests
prompt a `question:` clarifier first.

</details>

<details>
<summary><b>Example 2 — implementer + reviewer</b> · TDD-style iterative loop</summary>

Two sessions iterating on a complex feature. The reviewer's accumulated
catalog of edge cases makes each successive round more pointed — context
that *grows in value* with every round.

**Setup**: `impl` session in `~/proj/rate-limiter` writing a token-bucket
implementation; `reviewer` session next to it as adversarial test author.
Both stay live throughout the loop — no spawning per round.

**Round 1**
```
impl     → "v1 pushed: basic per-key bucket with refill"
reviewer → reads code, writes 4 baseline tests, runs them
         → "3 pass, 1 fails: off-by-one at exactly-burst-threshold.
            asserts at tests/limiter/test_burst.py:42"
impl     → fixes → "v2"
```

**Round 5** — reviewer references its accumulated catalog:
```
reviewer → "v5 passes everything in test_burst.py and test_concurrent.py
            from rounds 1-4. New angle: clock skew. added test_clock_skew.py
            with 3 cases (NTP step, leap second, monotonic going backward).
            1 of 3 fails — leap second causes negative refill."
```
The reviewer *knows* what's already solid (rounds 1–4 cases), so it
probes new territory instead of re-checking baseline.

**Round 12** — accumulated context shows up explicitly:
```
reviewer → "all 18 prior cases still green. tried fuzzing with 100k random
            rate/burst combos: found 1 deterministic deadlock when burst=0.
            seed in tests/limiter/fuzz_seeds.json"
```
By round 12 the reviewer has its own files: `test_burst.py`,
`test_concurrent.py`, `test_clock_skew.py`, `fuzz_seeds.json` — a
**growing artifact** that's part of its session state. A subagent or
freshly-spawned teammate would have to rediscover all of this each round.

**Outcome**: ~30 rounds, ~4 hours wall-clock, ends when the reviewer's
adversarial budget is exhausted. `impl`'s session retains the design
rationale across attempts; `reviewer`'s retains the regression suite.
You can come back tomorrow and resume — both sessions persist.

</details>

<details>
<summary><b>Example 3 — red-team + blue-team</b> · adversarial security loop</summary>

Two sessions in a long back-and-forth: attack catalog on one side, patch
lineage on the other. Both grow round by round.

**Setup**: `red` session in `~/proj/security-fuzz` with attack scriptlets;
`blue` session in `~/proj/auth-service` patching. Hours of accumulated
context on each side by the time the loop hits round 38.

**Round 1**
```
red  → "broke it: token reuse via cached redis lookup. repro:
        /tmp/red-001.sh; receives valid 401 token after rotation"
blue → "done: patched in PR-491 (drop-cache-on-rotate). retry."
```

**Round 7** — red references the patch lineage and pivots:
```
red  → "the cache fix from r1 holds. tried bypassing via header-smuggling
        (CRLF in X-Forwarded-For); session pinning bypassed in 2 of 4
        endpoints. catalog now: 7 working exploits, 6 patched."
blue → "patched header parser. retry."
```

**Round 38** — both sides have a hardened-spots map:
```
red  → "no new bypass in this 30-min budget. attack catalog: 24 working
        at peak, all patched. notable patterns:
          cache-coherence       (rounds 1, 6, 19)
          header-smuggling      (rounds 2-5)
          session-state-confusion (rounds 12-17)"
blue → "tracked in PATCH_LOG.md (24 entries). all classes hardened.
        ready for external pentest."
```

By round 38, both sessions know what's been tried, what worked, what
didn't. `red` doesn't re-attempt dead-end attack classes; `blue` knows
exactly which surfaces are hardened. The accumulated knowledge IS the
work product. Subagents (context resets per call) and agent teams (one
task, then exit) couldn't sustain this kind of multi-day adversarial
collaboration.

</details>

## Slash commands

| Command                                                        | What it does                                                   |
| :------------------------------------------------------------- | :------------------------------------------------------------- |
| `/inter-session:inter-session`                                 | Connect (alias for `connect`).                                 |
| `/inter-session:inter-session connect [name]`                  | Connect to the bus; `name` proposed from context if omitted.   |
| `/inter-session:inter-session install-deps`                    | Install runtime deps (websockets, psutil) into an isolated venv. |
| `/inter-session:inter-session list`                            | List connected sessions.                                       |
| `/inter-session:inter-session send <name> <text>`              | Send a message to one session.                                 |
| `/inter-session:inter-session broadcast <text>`                | Send to all other sessions (≤ 256 KB).                         |
| `/inter-session:inter-session rename <new-name>`               | Rename — implemented as disconnect + reconnect.                |
| `/inter-session:inter-session relabel <text>`                  | Change this session's label in place (no reconnect); `""` clears. Persists per project. |
| `/inter-session:inter-session status`                          | Heuristic connection state.                                    |
| `/inter-session:inter-session disconnect`                      | Stop the monitor.                                              |
| `/inter-session:inter-session auto-start [on\|off\|status]`    | Toggle auto-start. `on` = start at every session; `off` = lazy (default). Apply with `/reload-plugins`. |

## Session labels

Alongside its `name` (the ASCII handle used for addressing), a session can
carry an optional **label** — a short Unicode display string (up to 60
characters, e.g. `Payments 🐛 refund bug`) shown in the `list` table. Labels
are display-only; you always address a session by its `name`.

Set one when the monitor starts with the client's `--label` flag. A label set
this way is **remembered per project** — persisted in the data dir, keyed by
the git repo root (falling back to the working directory outside a repo) — so
it is reused automatically on the next restart without re-passing the flag:

- `--label "…"` — set and persist for this project.
- `--label ""` — clear the persisted label.
- `INTER_SESSION_LABEL` — a one-off runtime override; used but **not**
  persisted.

To change the label of an already-connected session **without reconnecting**
(keeping the same `session_id`), use `relabel` — it updates the label live for
all peers and persists it too:

```
/inter-session relabel "the controller"     # "" clears it
```

## Plugin configuration

The WebSocket port and idle-shutdown timeout are configurable via
`/plugin config`:

| Key                       | Type   | Default | What it does                                              |
| :------------------------ | :----- | :------ | :-------------------------------------------------------- |
| `port`                    | number | `9473`  | Localhost WebSocket port for the bus.                     |
| `idle_shutdown_minutes`   | number | `10`    | Server exits after this many minutes with no connected clients. `0` = never. |

## Security

- Server binds `127.0.0.1` only.
- Bearer token at `~/.claude/data/inter-session/token` (mode `0600`,
  directory `0700`).
- Any process running as the same Unix user can read the token and
  connect. This is acceptable for single-user, single-machine.
- The token does **not** protect against malicious code running as your
  user. If you don't trust local code, don't enable inter-session.
- The receiving agent's reaction policy (see
  [SKILL.md](./skills/inter-session/SKILL.md)) treats peer messages as
  instructions but applies the same caution as user input —
  destructive ops need explicit affirmative content, and ambiguous
  requests prompt a `question:` clarifier first.

## Limits

- WebSocket frame size: 16 MB.
- Direct `text` length: 10 MB.
- Broadcast `text` length: 256 KB.
- Stdout notification body: 400 chars (Claude Code clips monitor
  notifications at ~512 chars total; the cap leaves room for our
  prefix). Above this, the receiver sees a truncated first line plus
  a `cont` pointer line to `~/.claude/data/inter-session/messages.log`,
  where the full payload is always preserved.
- Broadcast rate: 60 / minute / session.

## Development

TDD throughout. Test runner: `pytest` + `pytest-asyncio`.

```bash
make test         # full suite — auto-bootstraps .venv on first run
make test-fast    # skip subprocess-spawning tests
make clean        # remove .venv
```

The Makefile prefers `uv` if installed, falling back to `python3 -m
venv`.

## License

MIT — see [LICENSE](./LICENSE).
