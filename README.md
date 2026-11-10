# skill-agent-session-transfer

Agent skill: resume an agent CLI session on a different account by stripping account-bound encrypted reasoning from its log. Verified for Muse. See SKILL.md for the procedure.

- `scripts/strip-reasoning.py`: dry run by default; `--apply` needs `--backup-dir` and writes a full backup copy before an atomic replace. Unverified runtimes, symlinked logs and invalid JSON are refused, and the log is left unchanged.
- Tests: Swift Testing (`swift test`), black-box over temporary fixtures. They run in GitHub Actions on a hosted Ubuntu runner.
