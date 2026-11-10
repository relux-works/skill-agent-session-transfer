#!/usr/bin/env python3
"""Make an agent session log resumable under a different account by removing account-bound encrypted reasoning.

Verified runtime: muse (2026-11-10). Records with payload.event.kind == "reasoning_committed" keep everything
except their `encrypted_content` field; no record is removed, so the log stays replayable.
Dry run by default. --apply writes a full backup copy first, then replaces the log atomically.

  strip-reasoning.py --runtime muse --session-log PATH [--apply --backup-dir DIR]
Exit codes: 0 ok, 2 usage/refusal, 3 invalid input.
"""
import argparse, json, os, shutil, sys, tempfile, time

VERIFIED = {"muse"}


def is_target(rec):
    p = rec.get("payload")
    e = p.get("event") if isinstance(p, dict) else None
    return isinstance(e, dict) and e.get("kind") == "reasoning_committed" and "encrypted_content" in e


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True)
    ap.add_argument("--session-log", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir")
    a = ap.parse_args(argv)
    if a.runtime not in VERIFIED:
        print(f"refused: runtime '{a.runtime}' is not verified (verified: {', '.join(sorted(VERIFIED))})", file=sys.stderr)
        return 2
    src = a.session_log
    if not os.path.isfile(src) or os.path.islink(src):
        print(f"refused: {src} is not a regular file", file=sys.stderr)
        return 2
    if a.apply and not a.backup_dir:
        print("refused: --apply needs --backup-dir", file=sys.stderr)
        return 2
    total = targets = 0
    out_lines = []
    with open(src, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            total += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"invalid: line {n} is not JSON; nothing changed", file=sys.stderr)
                return 3
            if is_target(rec):
                targets += 1
                del rec["payload"]["event"]["encrypted_content"]
                out_lines.append(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
            else:
                out_lines.append(line if line.endswith("\n") else line + "\n")
    print(f"records {total}, reasoning records with encrypted_content {targets}, records kept {len(out_lines)}")
    if not a.apply:
        print("dry run: nothing changed")
        return 0
    if targets == 0:
        print("nothing to strip; log unchanged")
        return 0
    os.makedirs(a.backup_dir, mode=0o700, exist_ok=True)
    backup = os.path.join(a.backup_dir, os.path.basename(src) + "." + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    shutil.copy2(src, backup)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(src), prefix=".strip-")
    with os.fdopen(fd, "w", encoding="utf-8") as g:
        g.writelines(out_lines)
        g.flush(); os.fsync(g.fileno())
    shutil.copymode(src, tmp)
    os.replace(tmp, src)
    print(f"applied: backup {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
