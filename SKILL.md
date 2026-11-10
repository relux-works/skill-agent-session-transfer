---
name: agent-session-transfer
description: >
  Move an agent CLI session to another account and resume it natively. The log carries encrypted reasoning bound to
  the old account; strip only that field, keep every record, then resume. Verified for Muse; Codex and Claude are
  not verified yet and are refused by the script. Use when a lane's login moved to another subscription and
  `resume` fails or the session must continue on the new account. Russian triggers: "перенести сессию на другой
  аккаунт", "резюм после смены аккаунта", "вырезать reasoning", "encrypted_content", "сессия не поднимается после логина".
---

# Agent session transfer between accounts

Why resume breaks: the session log stores the model's encrypted reasoning (`encrypted_content`). That blob is bound to the account that produced it, and a session on a different account cannot replay it.

## Muse (verified 2026-11-10)

1. Stop the lane by PID: the muse process, its flock wrapper and the lane's `wake` window. Kill by PID only.
2. Back up the whole session directory: `cp -a ~/.local/share/muse/sessions/<yyyy>/<mm>/<dd>/<session-id> <backup-dir>/`.
3. Dry run, then apply:
   ```
   agent-session-strip-reasoning --runtime muse --session-log <dir>/session.jsonl
   agent-session-strip-reasoning --runtime muse --session-log <dir>/session.jsonl --apply --backup-dir <backup-dir>
   ```
   This removes `encrypted_content` from records whose `payload.event.kind` is `reasoning_committed`. Records, their order, text and sequence stay as they are.
4. Check: `muse trace inspect --session-log <dir>/session.jsonl --format json` must report `"load_status": "loaded"`. `muse usage --json` must show the new account (its tier, usage near 0).
5. Resume with the lane's usual flags and env: `muse --model … --workspace <work> … resume <session-id>`.
6. Restart the lane's wake sidecar, give the session one short turn, and check that it answers. Old 429 lines in the pane are history.

Do NOT delete the reasoning records themselves. Muse then reports "its log cannot be replayed", starts a new session that re-reads the old log and burns quota. Stop such a session at once and redo the steps from the backup.

## Codex and Claude (not verified)

The same principle probably applies: Codex rollout `reasoning` items carry `encrypted_content`, and Claude `thinking` blocks carry a signature. Neither path is tested, so the script refuses those runtimes. Add a runtime only together with a verified transfer and tests.

## Rules

- Never print, copy or move auth files or tokens. The new login is the owner's step.
- Keep the backup until the resumed session has completed at least one turn.
