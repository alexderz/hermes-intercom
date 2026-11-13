# Implementation findings

> This document describes the legacy Unix-socket path of 0.2. Version 0.3 delivers through the gateway owner (see the README); this path is used only on a Hermes build without the owner client.

Notes from building and testing this plugin against the real Hermes Agent
codebase (fork majordave, main, Aug 2026). These are the non-obvious parts
that issue #81885's design glosses over — recorded here so future implementers
(and upstream) don't rediscover them.

## 1. There is no module-level handle to the live CLI session

`cli._active_agent_ref` exists but is insufficient:

- In a TUI session the agent may not be reachable through it at all.
- `cli = HermesCLI(...)` is a **local variable inside `main()`**
  (`cli.py:21256`), never promoted to module state.

**Solution:** find the live instance with GC:

```python
import gc
for o in gc.get_objects():
    if type(o).__name__ == "HermesCLI" and hasattr(o, "_pending_input"):
        return o
```

There is exactly one per interactive process. Ugly but robust, and it
requires zero core changes.

## 2. The auto-wake channel already exists: `_pending_input`

The interactive CLI runs a `process_loop` daemon thread that polls
`self._pending_input.get(timeout=0.1)` (`cli.py:20356`). Putting text in that
queue is indistinguishable from the user typing + pressing Enter — the
session starts a turn by itself. That single line delivers Claude Code's
"message triggers the receiving agent automatically" behavior:

```python
cli_obj._pending_input.put(text)
```

## 3. Idle-delivery rides the `pre_llm_call` plugin hook

When you can't (or shouldn't) submit a turn, park the message. The plugin
hook `pre_llm_call` fires at every turn build (`agent/turn_context.py:1278`)
and any callback returning `{"context": "..."}` gets its string injected into
the turn's user message. Draining parked messages there costs zero core
changes.

## 4. Printing to stdout from a background thread is safe in both UIs

- **TUI:** prompt_toolkit's `patch_stdout()` wraps stdout in a StdoutProxy
  that serializes writes between the input loop and background threads.
- **Classic REPL:** while idle there is no concurrent writer.

So the inbox thread can render an arrival banner directly with
`sys.stdout.write(...)`. This is what makes message arrival *visible*
immediately rather than only surfacing on the next turn.

## 5. Plugin modules load once per process

Every fix to the plugin requires restarting the **receiving** sessions too,
not just the sender. During development this bit repeatedly: the sender had
new code while the receiver kept running the old inbox. Budget for restarts
when iterating.

## 6. Delivery status taxonomy that emerged

| Status | Meaning |
|---|---|
| `submitted_as_turn` | Put into `_pending_input`; receiver will start a turn |
| `steered` | Injected into a mid-turn via `agent.steer()` |
| `parked_no_agent` / `parked_idle` | Stored for next-turn drain via `pre_llm_call` |
| `rejected_pending_cap` | Queue full |

## E2E results

Round trip CLI ↔ TUI validated live (same machine): receiver banner printed
instantly, receiver auto-started a turn, composed a reply using the
`intercom` tool unprompted, reply arrived in the original sender's context on
its next turn.
