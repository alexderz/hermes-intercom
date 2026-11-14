## Choosing a delivery

Choose the delivery by what the target must do and when:

- **Act now** (a correction, a stop, a handoff it is waiting on): `steer`; `interrupt` only to stop obsolete work.
- **Act after its current work**: `queue`. If you may send the same kind of update several times while it is busy (a build's state), add a `key` so only the newest one wakes it.
- **Know, but not until it next works**: `passive`. Costs nothing until then and never pulls the target off its work.
- **Repeated status** (usage meters, a build's current state, what is deployed): `passive` with a `key` per topic, so only the newest version is ever pending. Status every session should have: `to: all` with a key, a terse one-line body, and a new version only when the change matters to a reader (a threshold crossed, a decision-relevant value changed), not on every refresh.
- **Requests, answers and one-time updates**: no key. Each is its own message and must not be replaced by the next one. A request you need answered soon is not passive.
- Still never: forwarding, un-keyed notes to many sessions, status sent by waking delivery.
