# Qualification evidence

On 2026-11-20, macOS arm64, native inspection passed for each of the nine source/target combinations on disposable transcripts containing a historical marker. Readers and writers used persisted IR between stages.

| Target | Observed version | Independent observation |
| --- | --- | --- |
| Claude Code | 2.1.326, Agent SDK 0.2.186 | get_session_messages resolves the target UUID in its workspace and returns the marker |
| Codex CLI | 0.171.0 | app-server thread/resume resolves the UUID and returns the marker in the turn items |
| Muse Code | 1.4.24-R9131.1 | trace inspect loads the minted permission frame and projected records in staging and the sandbox target store; the committed dialogue contains the marker |

An absent-marker control failed all nine cells at the marker assertion after provider inspection. An earlier Codex rollout prototype resolved the UUID with empty items; the marker assertion catches that failure. A Muse frame reserialized with sorted keys failed native inspection; its original bytes are now preserved.

The reference production Claude snapshot yielded 183,287 IR facts. The post-compaction replay contained 374 tool calls, 374 results and 137 dialogue/summary events. The raw source and all payloads are local to the private bundle. No production session was published or resumed in this run.

## Authenticated fresh-session fork into Codex

On 2026-11-20, the public `convert --apply` command projected a freshly recorded transcript from each provider into Codex CLI 0.171.0 on macOS arm64. Each had a different random verification word. The local authenticated app-server created a fresh account-scoped thread through `thread/fork`. Both its UI turn items and its persisted model input matched the staged projection. All three resumed threads answered with their source word as the final, newly appended assistant message, with zero tool calls. A thread built from a neutral conversation answered UNKNOWN. The installed app-server rejected raw Claude and Muse fixture logs as fork inputs.

The Claude and Muse source logs contained the accepted test prompt and a provider failure. Only the Codex source completed its initial model turn. As targets in the nine-cell live run, Claude returned an expired-login error and Muse reported subscription quota exhaustion. These six cells produced no qualified model answer. The previous Claude-to-Muse production proof remains the admission for that route. The Codex-to-Codex proof covers a fresh fork with one account; it does not establish an account A→B move. CLI upgrades require fresh evidence.
