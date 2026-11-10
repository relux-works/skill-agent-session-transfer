#!/usr/bin/env python3
"""Make an agent session log resumable under a different account by removing account-bound encrypted reasoning.

Verified runtime: muse (2026-11-10, two production sessions). Every record is kept, so the log stays replayable:
  1. reasoning_committed records lose their `encrypted_content` field;
  2. nested encrypted reasoning blobs (inside context-compaction / projection checkpoints, e.g.
     replay_model_messages[].content[] and resume_state copies) lose their `encrypted_content` field;
  3. nested `reasoning_replay` items that no longer carry encrypted content lose their `id`: a bare id
     references a server-side item of the OLD account ("Referenced reasoning item ... not found").
     The items themselves stay; dropping them breaks Muse's replay check.
Dry run by default. --apply writes a full backup copy first, then replaces the log atomically.

  strip-reasoning.py --runtime muse --session-log PATH [--apply --backup-dir DIR]
Exit codes: 0 ok, 2 usage/refusal, 3 invalid input.
"""
import argparse, json, os, shutil, sys, tempfile, time

VERIFIED = {"muse"}


def strip_record(rec, counts):
    """Apply steps 1-3 in place; return True if the record changed."""
    changed = False
    p = rec.get("payload")
    e = p.get("event") if isinstance(p, dict) else None
    if isinstance(e, dict) and e.get("kind") == "reasoning_committed" and "encrypted_content" in e:
        del e["encrypted_content"]; counts["reasoning_committed"] += 1; changed = True

    def walk(o, top):
        nonlocal changed
        if isinstance(o, dict):
            if not top and isinstance(o.get("encrypted_content"), str):
                del o["encrypted_content"]; counts["nested_blob"] += 1; changed = True
            if o.get("kind") == "reasoning_replay" and "id" in o and "encrypted_content" not in o:
                del o["id"]; counts["replay_id"] += 1; changed = True
            for v in o.values():
                walk(v, False)
        elif isinstance(o, list):
            for x in o:
                walk(x, False)
    if isinstance(e, dict):
        for v in e.values():
            walk(v, False)
    return changed


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
    counts = {"reasoning_committed": 0, "nested_blob": 0, "replay_id": 0}
    out_lines = []
    with open(src, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            total += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"invalid: line {n} is not JSON; nothing changed", file=sys.stderr)
                return 3
            if strip_record(rec, counts):
                targets += 1
                out_lines.append(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
            else:
                out_lines.append(line if line.endswith("\n") else line + "\n")
    print(f"records {total}, changed {targets}, records kept {len(out_lines)}; "
          f"reasoning_committed {counts['reasoning_committed']}, nested blobs {counts['nested_blob']}, "
          f"reasoning_replay ids {counts['replay_id']}")
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
