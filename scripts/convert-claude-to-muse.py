#!/usr/bin/env python3
"""Convert a Claude Code session log into a native, resumable Muse session.

Method (verified 2026-11-19 on Muse 1.4.24, donor graft + synthesized turns):
Muse has no import surface, so the writer synthesizes target-native records.
The checksum-bound permission frame cannot be forged: in --apply mode the
converter lets `muse exec --provider echo --session-id <new>` create the
target directory and frame, then publishes a staged session.jsonl built as
frame + templated bootstrap records + one templated Muse turn per Claude
Code turn + session.end, and validates it with `muse trace inspect`.

Fidelity (spec: agent-session-convert SPEC.md, strategy target_native_writer,
profile compact): user/assistant text is exact; Claude `thinking` blocks are
never written (foreign signature, re-sign impossible) and stay only as line
references to the retained source log; tool calls/results are summarized as
inert bracketed lines inside the assistant text (full payloads stay in the
bundle IR, capped, and byte-exact in the source log); pre-compact history is
carried by Claude's own compact summary inside the synthesized migration
checkpoint turn; subagent (sidechain) records are counted and omitted in v1.

Dry run by default: writes only the bundle under --out-dir. --apply also
creates and publishes the target session (create-only; an existing target
directory is a refusal) and requires the muse CLI.

  convert-claude-to-muse.py --source-log PATH --workspace DIR --out-dir DIR
      [--session-id UUIDv7] [--template-log PATH] [--full-history]
      [--max-tool-chars N] [--apply [--muse-bin muse] [--verify-turn TEXT]]

Exit codes: 0 ok, 2 usage/refusal, 3 invalid input.
"""
import argparse, copy, hashlib, json, os, re, shutil, subprocess, sys, time, uuid

CONVERTER_VERSION = "1.0.0"
DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "muse-turn-template.json")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def refuse(msg):
    print(f"refused: {msg}", file=sys.stderr)
    return 2


def invalid(msg):
    print(f"invalid: {msg}", file=sys.stderr)
    return 3


def dump(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def uuid7_at(ms, n=0):
    rand_b = int.from_bytes(os.urandom(8), "big")
    val = (ms << 80) | (0x7 << 76) | ((n & 0x0FFF) << 64) | (0b10 << 62) | (rand_b & ((1 << 62) - 1))
    return str(uuid.UUID(int=val))


def uuid7_now():
    return uuid7_at(int(time.time() * 1000), int.from_bytes(os.urandom(2), "big"))


# ---------------------------------------------------------------- template

TURN_ALLOW = [
    ("runtime.user_intent.accepted", None),
    ("runtime.session", "started"),
    ("runtime.user_intent.materialized", None),
    ("runtime.session", "task_stream_linked"),
    ("runtime.session", "proposed"),
    ("runtime.session", "accepted"),
    ("runtime.session", "side_effect_intent"),
    ("runtime.session", "started"),
    ("runtime.session", "model_response_created"),
    ("runtime.session", "reasoning_committed"),
    ("runtime.session", "assistant_message_committed"),
    ("runtime.session", "model_completed"),
    ("runtime.session", "completed"),
    ("runtime.session", "terminal"),
]


def _event_kind(rec):
    pl = rec.get("payload") or {}
    ev = pl.get("event") or {}
    return ev.get("kind") if isinstance(ev, dict) else None


def _is_turn_record(rec):
    pl = rec.get("payload") or {}
    if rec.get("payload_type") in ("runtime.user_intent.accepted", "runtime.user_intent.materialized"):
        return True
    if rec.get("payload_type") != "runtime.session":
        return False
    if pl.get("kind") == "run":
        return _event_kind(rec) in ("started", "task_stream_linked", "model_response_created",
                                    "reasoning_committed", "assistant_message_committed",
                                    "model_completed", "terminal")
    if pl.get("kind") == "task":
        return _event_kind(rec) in ("proposed", "accepted", "side_effect_intent", "started", "completed")
    return False


def extract_template(donor_path):
    """Extract bootstrap + one text-turn template from a real Muse session.jsonl."""
    recs = []
    with open(donor_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                recs.append(json.loads(line))
    flat = []
    frame = None
    for r in recs:
        if "retained_frame" in r:
            frame = r
            for ch in r["children"]:
                flat.append(json.loads(ch["record_json"]))
        elif "retained_marker" in r:
            continue
        else:
            flat.append(r)
    flat.sort(key=lambda r: r.get("sequence") or 0)
    session_id = (flat[0].get("stream") or {}).get("id")
    meta = next((r for r in flat if r.get("payload_type") == "runtime.session.metadata"), None)
    if not session_id or meta is None or frame is None:
        raise ValueError("donor log lacks frame/metadata/stream id")
    mrec = (meta.get("payload") or {}).get("record") or {}
    accepted_at = [i for i, r in enumerate(flat) if r.get("payload_type") == "runtime.user_intent.accepted"]
    if not accepted_at:
        raise ValueError("donor log has no user turn to template from")
    header = [r for r in flat[2:accepted_at[0]]]  # seq>=3, before first turn
    # Choose a text-only turn: exactly one assistant_message_committed, no tool calls, ends at run terminal.
    chosen = None
    for n, start in enumerate(accepted_at):
        end = accepted_at[n + 1] if n + 1 < len(accepted_at) else len(flat)
        seg = flat[start:end]
        kinds = [_event_kind(r) for r in seg]
        run_id = (seg[0].get("payload") or {}).get("intent_id")
        seg = [r for r in seg if (r.get("payload") or {}).get("run_id", run_id) == run_id
               or (r.get("payload") or {}).get("intent_id") == run_id]
        if "assistant_tool_calls_committed" in kinds or kinds.count("assistant_message_committed") != 1:
            continue
        if "terminal" not in kinds:
            continue
        model_task = None
        for r in seg:
            ev = (r.get("payload") or {}).get("event") or {}
            if isinstance(ev, dict) and str(ev.get("task_kind") or "").startswith("model."):
                model_task = ev.get("task_id") or (r.get("payload") or {}).get("task_id")
                break
        chosen = []
        for r in seg:
            if not _is_turn_record(r):
                continue
            pl = r.get("payload") or {}
            ev = pl.get("event") or {}
            tid = pl.get("task_id") or (ev.get("task_id") if isinstance(ev, dict) else None)
            if model_task and tid and tid != model_task:
                continue
            chosen.append(r)
        break
    if not chosen:
        raise ValueError("donor log has no text-only turn (need one assistant_message_committed and a terminal)")
    need = {"runtime.user_intent.accepted", "assistant_message_committed", "terminal"}
    have = {r.get("payload_type") for r in chosen} | {_event_kind(r) for r in chosen}
    if not need <= have:
        raise ValueError(f"donor turn template incomplete: missing {sorted(need - have)}")
    run_id = (chosen[0].get("payload") or {}).get("intent_id")
    task_id = next(((r.get("payload") or {}).get("task_id") for r in chosen
                    if (r.get("payload") or {}).get("task_id")), None)
    resp_id = next((_event_resp(r) for r in chosen if _event_resp(r)), None)
    end_rec = next((r for r in reversed(flat) if r.get("payload_type") == "session.end"), None)
    return {
        "template_version": 1,
        "donor_session_id": session_id,
        "donor_workspace_root": mrec.get("workspace_root"),
        "provider_id": mrec.get("provider_id"),
        "model_id": mrec.get("model_id"),
        "build": mrec.get("build"),
        "donor_run_id": run_id,
        "donor_task_id": task_id,
        "donor_response_id": resp_id,
        "header_records": header,
        "turn_records": chosen,
        "end_record": end_rec,
    }


def _event_resp(rec):
    ev = (rec.get("payload") or {}).get("event") or {}
    return ev.get("response_id") if isinstance(ev, dict) else None


def load_template(args):
    if args.template_log:
        return extract_template(args.template_log), f"donor log {args.template_log}"
    path = args.template or DEFAULT_TEMPLATE
    with open(path, encoding="utf-8") as f:
        return json.load(f), f"shipped template {path}"


# ---------------------------------------------------------------- Claude reader

def _blocks(msg):
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if isinstance(c, list):
        return [b for b in c if isinstance(b, dict)]
    return []


def _text_of(blocks):
    return "\n".join(b.get("text") or "" for b in blocks if b.get("type") == "text")


def _cap(s, limit):
    if s is None:
        return None, 0
    if len(s) <= limit:
        return s, 0
    return s[:limit], len(s) - limit


def parse_claude(path, ir_fp, ir_cap):
    """Stream the Claude log. Returns (meta, turns, compact, counts). Turns hold
    ordered segments: ("text", str) | ("tool", name, input_json) | ("result", str, is_error)."""
    counts = {"records_by_type": {}, "thinking_blocks": 0, "thinking_chars": 0,
              "sidechain_records": 0, "attachments": 0, "tool_calls": 0, "tool_results": 0,
              "user_text_messages": 0, "assistant_text_segments": 0, "meta_user_messages": 0}
    meta = {"session_id": None, "cwd": None, "title": None, "first_ts": None, "last_ts": None}
    turns, compact, cur = [], None, None

    def ir(obj):
        ir_fp.write(dump(obj) + "\n")

    def new_turn(text, ts, line):
        nonlocal cur
        cur = {"user_text": text, "user_ts": ts, "source_line": line, "segments": []}
        turns.append(cur)

    with open(path, encoding="utf-8", errors="strict") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"line {n} is not JSON")
            t = r.get("type")
            counts["records_by_type"][t] = counts["records_by_type"].get(t, 0) + 1
            if r.get("sessionId") and not meta["session_id"]:
                meta["session_id"] = r["sessionId"]
            if r.get("cwd"):
                meta["cwd"] = r["cwd"]
            if t == "custom-title" and r.get("customTitle"):
                meta["title"] = r["customTitle"]
            ts = r.get("timestamp")
            if ts:
                meta["first_ts"] = meta["first_ts"] or ts
                meta["last_ts"] = ts
            if r.get("isSidechain"):
                counts["sidechain_records"] += 1
                continue
            if t == "attachment":
                counts["attachments"] += 1
                continue
            msg = r.get("message")
            if t == "user":
                blocks = _blocks(msg)
                text = _text_of(blocks).strip()
                if r.get("isCompactSummary"):
                    full = _text_of(blocks)
                    compact = {"text": full, "source_line": n, "ts": ts}
                    ir({"kind": "compaction", "source_line": n, "ts": ts, "chars": len(full)})
                    continue
                results = [b for b in blocks if b.get("type") == "tool_result"]
                if text:
                    counts["user_text_messages"] += 1
                    if r.get("isMeta"):
                        counts["meta_user_messages"] += 1
                    capped, extra = _cap(text, ir_cap)
                    ir({"kind": "user_message", "source_line": n, "ts": ts, "text": capped,
                        "truncated_chars": extra})
                    if cur is not None and not cur["segments"] and cur["user_text"]:
                        cur["user_text"] += "\n" + text
                    else:
                        new_turn(text, ts, n)
                for b in results:
                    counts["tool_results"] += 1
                    c = b.get("content")
                    s = c if isinstance(c, str) else dump(c if c is not None else "")
                    capped, extra = _cap(s, ir_cap)
                    ir({"kind": "tool_result", "source_line": n, "ts": ts,
                        "tool_use_id": b.get("tool_use_id"), "is_error": bool(b.get("is_error")),
                        "text": capped, "truncated_chars": extra})
                    if cur is None:
                        new_turn("", ts, n)
                    cur["segments"].append(("result", s, bool(b.get("is_error"))))
            elif t == "assistant":
                if cur is None:
                    new_turn("", ts, n)
                for b in _blocks(msg):
                    bt = b.get("type")
                    if bt == "text" and (b.get("text") or "").strip():
                        counts["assistant_text_segments"] += 1
                        capped, extra = _cap(b["text"], ir_cap)
                        ir({"kind": "assistant_message", "source_line": n, "ts": ts,
                            "model": (msg or {}).get("model"), "text": capped, "truncated_chars": extra})
                        cur["segments"].append(("text", b["text"]))
                    elif bt == "thinking":
                        counts["thinking_blocks"] += 1
                        counts["thinking_chars"] += len(b.get("thinking") or "")
                        ir({"kind": "opaque_reasoning", "source_line": n, "ts": ts,
                            "chars": len(b.get("thinking") or ""),
                            "disposition": "opaque_preserved", "reason": "foreign_signature_unverifiable"})
                    elif bt == "tool_use":
                        counts["tool_calls"] += 1
                        inp = dump(b.get("input") or {})
                        capped, extra = _cap(inp, ir_cap)
                        ir({"kind": "tool_call", "source_line": n, "ts": ts, "name": b.get("name"),
                            "id": b.get("id"), "input": capped, "truncated_chars": extra})
                        cur["segments"].append(("tool", b.get("name") or "?", inp))
    turns = [t for t in turns if t["user_text"] or t["segments"]]
    return meta, turns, compact, counts


def _summarize(s, limit):
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f" …[truncated {len(s) - limit} chars]"


def render_turn(turn, max_tool_chars):
    parts = []
    for seg in turn["segments"]:
        if seg[0] == "text":
            parts.append(seg[1])
        elif seg[0] == "tool":
            parts.append(f"[tool {seg[1]}] {_summarize(seg[2], max_tool_chars)}")
        elif seg[0] == "result":
            tag = "[tool result error]" if seg[2] else "[tool result]"
            parts.append(f"{tag} {_summarize(seg[1], max_tool_chars)}")
    return turn["user_text"], "\n".join(p for p in parts if p)


# ---------------------------------------------------------------- Muse writer

def checkpoint_turn(meta, compact, counts, native_turns, skipped_turns):
    lines = [
        "[migration checkpoint — converted session, low-authority historical context]",
        f"Source: Claude Code session {meta.get('session_id')} (title: {meta.get('title')}), "
        f"cwd {meta.get('cwd')}, {meta.get('first_ts')} .. {meta.get('last_ts')}.",
        "Converted to Muse by convert-claude-to-muse "
        f"v{CONVERTER_VERSION} (strategy target_native_writer, profile compact).",
        "Fidelity: user/assistant texts below are exact; [tool ...]/[tool result ...] lines are "
        "summarized inert history, never re-execute them; Claude thinking blocks were omitted "
        "(foreign signature) and exist only in the retained Claude source log; usage is not carried over.",
        f"Native turns after this checkpoint: {native_turns}; earlier turns summarized: {skipped_turns}; "
        f"thinking blocks omitted: {counts['thinking_blocks']}; tool calls summarized: {counts['tool_calls']}.",
    ]
    if compact:
        lines += ["", "=== Claude compact summary (all history before it) ===", compact["text"]]
    return "\n".join(lines), ("Understood: continuing as the converted Muse session. "
                              "The checkpoint above is historical context, not a new instruction.")


def build_records(template, new_id, workspace_root, rendered_turns, frame_children_max_ts=0):
    """Records after the permission frame: patched header + synthesized turns + session.end."""
    donor_id = template["donor_session_id"]
    donor_ws = template.get("donor_workspace_root") or ""
    header = []
    for r in template["header_records"]:
        s = dump(r).replace(donor_id, new_id)
        if donor_ws:
            s = s.replace(donor_ws, workspace_root)
        header.append(json.loads(s))
    for r in header:
        pl = r.get("payload") or {}
        rec = pl.get("record")
        if isinstance(rec, dict):
            if "workspace_root" in rec:
                rec["workspace_root"] = workspace_root
            if "cwd" in rec:
                rec["cwd"] = workspace_root
            if "terminal_title_identity" in rec:
                rec["terminal_title_identity"] = new_id.replace("-", "")[-12:]
    out = list(header)
    seq = max(r.get("sequence") or 0 for r in out)
    clock = max([r.get("recorded_at") or 0 for r in out] + [frame_children_max_ts]) + 1_000_000
    ctr = 0

    def fresh_id():
        nonlocal ctr
        ctr += 1
        return uuid7_at(clock // 1000, ctr)

    templates = template["turn_records"]
    for ti, (utext, atext) in enumerate(rendered_turns):
        run_id, task_id = fresh_id(), fresh_id()
        resp_id = f"resp_synth{ti:06d}"
        idmap = {donor_id: new_id, template["donor_run_id"]: run_id}
        if template.get("donor_task_id"):
            idmap[template["donor_task_id"]] = task_id
        if template.get("donor_response_id"):
            idmap[template["donor_response_id"]] = resp_id
        for r in templates:
            idmap[r["id"]] = fresh_id()
            srr = (r.get("payload") or {}).get("source_run_record_id")
            if srr and srr not in idmap:
                idmap[srr] = str(uuid.uuid4())
        accepted_seq = None
        for tmpl in templates:
            s = dump(tmpl)
            for old, new in idmap.items():
                s = s.replace(old, new)
            r = json.loads(s)
            seq += 1
            clock += 50_000
            r["sequence"] = seq
            r["recorded_at"] = clock
            pl = r.get("payload") or {}
            ev = pl.get("event") or {}
            if r.get("payload_type") == "runtime.user_intent.accepted":
                pl["refill_blocks"] = [{"kind": "text", "text": utext}]
                pl["model_messages"] = [{"content": [{"kind": "text", "text": utext}]}]
                accepted_seq = seq
            if isinstance(ev, dict) and ev.get("kind") == "started" and "prompt" in ev:
                ev["prompt"] = utext
            if r.get("payload_type") == "runtime.user_intent.materialized":
                pl["envelope_sequence"] = accepted_seq
                if isinstance(pl.get("outcome"), dict):
                    pl["outcome"]["cut_before"] = f"session:{new_id}:turn:{ti}"
            if isinstance(ev, dict) and ev.get("kind") == "reasoning_committed":
                ev.pop("encrypted_content", None)
                ev.pop("provider_item_id", None)
                ev["text"] = ""
                ev["message_id"] = str(uuid.uuid4())
            if isinstance(ev, dict) and ev.get("kind") == "assistant_message_committed":
                ev["text"] = atext if atext else "[source turn had no assistant text]"
                ev.pop("provider_item_id", None)
                ev["message_id"] = str(uuid.uuid4())
            if isinstance(ev, dict) and ev.get("kind") == "model_completed":
                if isinstance(ev.get("usage"), dict):
                    ev["usage"] = {k: 0 for k in ev["usage"]}
                ev["estimated_prompt_tokens"] = 0
            out.append(r)
        clock += 5_000_000
    if template.get("end_record"):
        r = json.loads(dump(template["end_record"]).replace(donor_id, new_id))
        seq += 1
        clock += 50_000
        r["sequence"] = seq
        r["recorded_at"] = clock
        r["id"] = fresh_id()
        out.append(r)
    return out


def frame_max_ts(frame_line):
    try:
        fr = json.loads(frame_line)
        return max(json.loads(ch["record_json"]).get("recorded_at") or 0 for ch in fr.get("children", []))
    except Exception:
        return 0


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def muse_store_dir(new_id):
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    now = time.localtime()
    return os.path.join(base, "muse", "sessions", f"{now.tm_year:04d}", f"{now.tm_mon:02d}",
                        f"{now.tm_mday:02d}", new_id)


def trace_loaded(muse_bin, log_path):
    p = subprocess.run([muse_bin, "trace", "inspect", "--session-log", log_path, "--format", "json"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
    try:
        return json.loads(p.stdout).get("load_status") == "loaded", p.stdout[-500:]
    except Exception:
        return False, p.stdout[-500:]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-log", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out-dir", required=True, help="bundle directory (must not exist)")
    ap.add_argument("--session-id", help="target Muse session UUIDv7 (default: fresh)")
    ap.add_argument("--template-log", help="real muse session.jsonl to extract turn templates from")
    ap.add_argument("--template", help="template JSON (default: muse-turn-template.json beside this script)")
    ap.add_argument("--emit-template", help="write template JSON extracted from --template-log and exit")
    ap.add_argument("--full-history", action="store_true",
                    help="convert pre-compact turns natively too (default: compact summary carries them)")
    ap.add_argument("--max-tool-chars", type=int, default=240)
    ap.add_argument("--ir-cap", type=int, default=20000)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--muse-bin", default="muse")
    ap.add_argument("--verify-turn", help="after publish, run one bounded resume turn with this prompt")
    a = ap.parse_args(argv)

    try:
        template, template_src = load_template(a)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return invalid(f"template: {e}")
    if a.emit_template:
        with open(a.emit_template, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=1)
        print(f"template written: {a.emit_template} (donor {template['donor_session_id']}, "
              f"build {template.get('build')})")
        return 0

    src = a.source_log
    if not os.path.isfile(src) or os.path.islink(src):
        return refuse(f"{src} is not a regular file")
    if not os.path.isdir(a.workspace):
        return refuse(f"workspace {a.workspace} is not a directory")
    workspace_root = os.path.realpath(a.workspace)
    if os.path.exists(a.out_dir):
        return refuse(f"out-dir {a.out_dir} exists (bundle must be fresh)")
    new_id = a.session_id or uuid7_now()
    if not UUID_RE.match(new_id):
        return refuse("--session-id must be a UUIDv7")
    muse_bin = shutil.which(a.muse_bin) if "/" not in a.muse_bin else a.muse_bin
    if a.apply and not (muse_bin and os.path.isfile(muse_bin)):
        return refuse(f"muse CLI not found: {a.muse_bin}")
    target_dir = muse_store_dir(new_id)
    if a.apply and os.path.exists(target_dir):
        return refuse(f"target session dir exists: {target_dir}")

    before = os.stat(src)
    os.makedirs(os.path.join(a.out_dir, "ir"))
    os.makedirs(os.path.join(a.out_dir, "staged"))
    ir_path = os.path.join(a.out_dir, "ir", "events.jsonl")
    try:
        with open(ir_path, "w", encoding="utf-8") as ir_fp:
            meta, turns, compact, counts = parse_claude(src, ir_fp, a.ir_cap)
    except ValueError as e:
        return invalid(str(e))
    after = os.stat(src)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return refuse("source changed during conversion (source_changed_during_conversion)")
    if not turns:
        return invalid("no user/assistant turns found in source")

    if compact and not a.full_history:
        native = [t for t in turns if t["source_line"] > compact["source_line"]]
        skipped = len(turns) - len(native)
    else:
        native, skipped = turns, 0
    rendered = [checkpoint_turn(meta, compact, counts, len(native), skipped)]
    empty_assistant = 0
    for t in native:
        u, ast = render_turn(t, a.max_tool_chars)
        if not ast:
            empty_assistant += 1
        rendered.append((u if u else "[tool results without a user text in source]", ast))
    rendered_chars = sum(len(u) + len(s) for u, s in rendered)

    fidelity = {
        "strategy": "target_native_writer", "profile": "compact",
        "source": {"session_id": meta["session_id"], "title": meta["title"], "cwd": meta["cwd"],
                   "first_ts": meta["first_ts"], "last_ts": meta["last_ts"],
                   "log_path": os.path.abspath(src), "log_size": before.st_size,
                   "log_sha256": sha256_file(src)},
        "target": {"session_id": new_id, "workspace": workspace_root,
                   "provider_id": template.get("provider_id"), "model_id": template.get("model_id")},
        "counts": counts,
        "turns_total": len(turns), "turns_native": len(native),
        "turns_pre_compact_summarized": skipped,
        "compact_summary": bool(compact),
        "compact_source_line": compact["source_line"] if compact else None,
        "dispositions": {
            "exact": ["user_message text", "assistant_message text"],
            "summarized": ["tool_call", "tool_result", "pre-compact history via compact summary"],
            "opaque_preserved": ["thinking blocks (source-log line refs in IR only)"],
            "synthesized": ["migration checkpoint turn", "muse envelope records", "fresh session/record ids"],
            "omitted": ["usage", "sidechain (subagent) records", "attachments", "claude runtime records"],
        },
        "empty_assistant_turns": empty_assistant,
        "rendered_chars": rendered_chars,
        "estimated_replay_tokens_rough": rendered_chars // 3,
        "template_source": template_src,
        "template_build": template.get("build"),
    }
    with open(os.path.join(a.out_dir, "fidelity.json"), "w", encoding="utf-8") as f:
        json.dump(fidelity, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out_dir, "FIDELITY.md"), "w", encoding="utf-8") as f:
        f.write("# Fidelity report\n\n```json\n" + json.dumps(fidelity, ensure_ascii=False, indent=1) + "\n```\n")

    frame_line = None
    if a.apply:
        seed = subprocess.run([muse_bin, "exec", "--provider", "echo", "--session-id", new_id,
                               "--workspace", workspace_root, "seed"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        live_log = os.path.join(target_dir, "session.jsonl")
        if seed.returncode != 0 or not os.path.isfile(live_log):
            return refuse(f"muse seed session failed: {seed.stdout[-400:]}")
        with open(live_log, encoding="utf-8") as f:
            frame_line = f.readline().rstrip("\n")

    records = build_records(template, new_id, workspace_root, rendered,
                            frame_children_max_ts=frame_max_ts(frame_line) if frame_line else 0)
    staged_path = os.path.join(a.out_dir, "staged", "session.jsonl")
    with open(staged_path, "w", encoding="utf-8") as f:
        if frame_line:
            f.write(frame_line + "\n")
        for r in records:
            f.write(dump(r) + "\n")
    state = "staged"
    print(f"source {meta['session_id']} -> target {new_id}: turns {len(turns)} "
          f"(native {len(native)}, summarized-pre-compact {skipped}), "
          f"thinking omitted {counts['thinking_blocks']}, tool calls summarized {counts['tool_calls']}, "
          f"rendered {rendered_chars} chars (~{rendered_chars // 3} tokens rough)")
    print(f"bundle: {a.out_dir} (ir/events.jsonl, fidelity.json, staged/session.jsonl"
          + ("" if frame_line else " without frame until --apply") + ")")
    if not a.apply:
        print("dry run: nothing published")
        return 0

    ok, tail = trace_loaded(muse_bin, staged_path)
    if not ok:
        return refuse(f"staged trace inspect not loaded: {tail}")
    live_log = os.path.join(target_dir, "session.jsonl")
    tmp = live_log + ".convert-tmp"
    shutil.copyfile(staged_path, tmp)
    os.replace(tmp, live_log)
    for name in os.listdir(target_dir):
        if name.startswith("cli-") and name.endswith(".log"):
            os.unlink(os.path.join(target_dir, name))
    ok, tail = trace_loaded(muse_bin, live_log)
    if not ok:
        return refuse(f"live trace inspect not loaded (rollback: rm -rf {target_dir}): {tail}")
    state = "live_validated"
    print(f"published: {live_log} (trace load_status=loaded, staged+live)")
    print(f"resume: cd {workspace_root} && muse --workspace {workspace_root} resume {new_id}")
    if a.verify_turn:
        v = subprocess.run([muse_bin, "exec", "--workspace", workspace_root, "--session-id", new_id,
                            "--disable-write", "--disable-shell", a.verify_turn],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
        print("verify turn exit", v.returncode)
        print("\n".join(v.stdout.strip().splitlines()[-6:]))
        if v.returncode != 0:
            return refuse("verify turn failed (session published; inspect before use)")
        state = "verified_turn_done"
    with open(os.path.join(a.out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"state": state, "target_session_id": new_id, "target_dir": target_dir,
                   "source_session_id": meta["session_id"], "converter_version": CONVERTER_VERSION},
                  f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
