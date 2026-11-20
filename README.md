# skill-agent-session-transfer

Agent skill: move an agent CLI session to another account or provider and resume it natively. See SKILL.md for the procedures.

- `agent-session-convert --help` lists the IR capture and projection commands installed by Curator. The reproducible Python zipapp in `scripts/agent-session-convert.pyz` comes from [agent-session-convert at d084404](https://github.com/relux-works/agent-session-convert/tree/d0844044c45c24cd6e98386ef972289e52313e62). Regenerate it with that revision’s `tools/package /path/to/new/agent-session-convert.pyz` and compare bytes before replacing the distribution artifact.
- `scripts/strip-reasoning.py`: Muse account move. Dry run by default; `--apply` needs `--backup-dir` and writes a full backup copy before an atomic replace. Unverified runtimes, symlinked logs and invalid JSON are refused, and the log is left unchanged.
- `scripts/convert-claude-to-muse.py`: Claude Code -> Muse conversion. Dry run writes a bundle (IR events, fidelity report, staged native log); `--apply` publishes create-only into the Muse store (frame created by the muse CLI itself), validates staged and live with `muse trace inspect`, and can prove resume with one bounded `--verify-turn`. Message texts are exact, Claude thinking blocks are never written, tool history is summarized in replay and preserved in the bundle/source log. Turn templates: `scripts/muse-turn-template.json` (regenerable via `--emit-template`).
- Tests: Swift Testing (`swift test`), black-box over temporary fixtures. They run in GitHub Actions on a hosted Ubuntu runner.
