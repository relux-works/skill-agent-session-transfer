import Foundation
import Testing

/// Runs scripts/strip-reasoning.py as a black box on temporary fixtures.
struct Run {
    let code: Int32
    let out: String
    let err: String
}

func script() -> URL {
    URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("scripts/strip-reasoning.py")
}

func run(_ args: [String]) throws -> Run {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = ["python3", script().path] + args
    let o = Pipe(), e = Pipe()
    p.standardOutput = o; p.standardError = e
    try p.run(); p.waitUntilExit()
    return Run(code: p.terminationStatus,
               out: String(decoding: o.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self),
               err: String(decoding: e.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self))
}

func tempDir() throws -> URL {
    let d = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
    return d
}

let fixture = """
{"id":"1","payload_type":"runtime.session","payload":{"kind":"run","event":{"kind":"model_completed","usage":{"reasoning_tokens":5}}}}
{"id":"2","payload_type":"runtime.session","payload":{"kind":"run","event":{"kind":"reasoning_committed","text":"summary","encrypted_content":"OLD-ACCOUNT-BLOB","message_id":"m1"}}}
{"id":"3","payload_type":"runtime.session","payload":{"kind":"run","event":{"kind":"reasoning_committed","text":"no blob here"}}}
{"id":"4","payload_type":"runtime.session","payload":{"kind":"task","event":{"kind":"completed","encrypted_content":"not a reasoning record"}}}

"""

func lines(_ s: String) -> [String] { s.split(separator: "\n").map(String.init) }

@Suite struct StripReasoningTests {
    /// Claim: --apply removes encrypted_content only from reasoning_committed records, keeps every record and its
    /// other fields, leaves non-reasoning encrypted_content untouched, and writes a byte-identical backup first.
    @Test func applyStripsOnlyReasoningBlobsAndKeepsRecords() throws {
        let d = try tempDir(); let log = d.appendingPathComponent("session.jsonl"); let bak = d.appendingPathComponent("bak")
        try fixture.write(to: log, atomically: true, encoding: .utf8)
        let r = try run(["--runtime", "muse", "--session-log", log.path, "--apply", "--backup-dir", bak.path])
        #expect(r.code == 0)
        let after = try String(contentsOf: log, encoding: .utf8)
        #expect(lines(after).count == 4)
        #expect(!after.contains("OLD-ACCOUNT-BLOB"))
        #expect(after.contains("\"text\":\"summary\""))
        #expect(after.contains("not a reasoning record"))
        let backups = try FileManager.default.contentsOfDirectory(atPath: bak.path)
        #expect(backups.count == 1)
        #expect(try String(contentsOf: bak.appendingPathComponent(backups[0]), encoding: .utf8) == fixture)
    }

    /// Claim: nested checkpoint copies lose their encrypted blob and the stale reasoning_replay id, but the item, its
    /// sibling text and every record stay (dropping the items broke Muse's replay in the 2026-11-10 repair).
    @Test func nestedCheckpointReasoningIsNeutralizedNotDropped() throws {
        let nested = """
        {"id":"9","payload_type":"runtime.session","payload":{"kind":"run","event":{"kind":"context_compaction_installed","replay_model_messages":[{"role":"assistant","content":[{"kind":"reasoning_replay","id":"rs_old","encrypted_content":"NESTED-OLD-BLOB"},{"kind":"text","text":"keep me"}]}]}}}

        """
        let d = try tempDir(); let log = d.appendingPathComponent("session.jsonl")
        try nested.write(to: log, atomically: true, encoding: .utf8)
        let r = try run(["--runtime", "muse", "--session-log", log.path, "--apply", "--backup-dir", d.appendingPathComponent("b").path])
        #expect(r.code == 0)
        let after = try String(contentsOf: log, encoding: .utf8)
        #expect(lines(after).count == 1)
        #expect(!after.contains("NESTED-OLD-BLOB"))
        #expect(!after.contains("rs_old"))
        #expect(after.contains("\"kind\":\"reasoning_replay\""))
        #expect(after.contains("keep me"))
    }

    /// Claim: the default dry run reports the count and changes nothing.
    @Test func dryRunChangesNothing() throws {
        let d = try tempDir(); let log = d.appendingPathComponent("session.jsonl")
        try fixture.write(to: log, atomically: true, encoding: .utf8)
        let r = try run(["--runtime", "muse", "--session-log", log.path])
        #expect(r.code == 0)
        #expect(r.out.contains("reasoning_committed 1"))
        #expect(try String(contentsOf: log, encoding: .utf8) == fixture)
    }

    /// Claim: unverified runtimes, --apply without a backup dir, symlinked logs and invalid JSON are refused
    /// with their exit code, and the log stays byte-identical (negative cases beside the positive control above).
    @Test(arguments: ["codex", "claude"]) func unverifiedRuntimeRefused(rt: String) throws {
        let d = try tempDir(); let log = d.appendingPathComponent("session.jsonl")
        try fixture.write(to: log, atomically: true, encoding: .utf8)
        let r = try run(["--runtime", rt, "--session-log", log.path, "--apply", "--backup-dir", d.appendingPathComponent("b").path])
        #expect(r.code == 2)
        #expect(try String(contentsOf: log, encoding: .utf8) == fixture)
    }

    @Test func applyWithoutBackupRefused() throws {
        let d = try tempDir(); let log = d.appendingPathComponent("session.jsonl")
        try fixture.write(to: log, atomically: true, encoding: .utf8)
        let r = try run(["--runtime", "muse", "--session-log", log.path, "--apply"])
        #expect(r.code == 2)
        #expect(try String(contentsOf: log, encoding: .utf8) == fixture)
    }

    @Test func symlinkRefused() throws {
        let d = try tempDir(); let real = d.appendingPathComponent("real.jsonl"); let link = d.appendingPathComponent("session.jsonl")
        try fixture.write(to: real, atomically: true, encoding: .utf8)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: real)
        let r = try run(["--runtime", "muse", "--session-log", link.path, "--apply", "--backup-dir", d.appendingPathComponent("b").path])
        #expect(r.code == 2)
        #expect(try String(contentsOf: real, encoding: .utf8) == fixture)
    }

    @Test func invalidJsonRefused() throws {
        let d = try tempDir(); let log = d.appendingPathComponent("session.jsonl")
        let bad = fixture + "{not json\n"
        try bad.write(to: log, atomically: true, encoding: .utf8)
        let r = try run(["--runtime", "muse", "--session-log", log.path, "--apply", "--backup-dir", d.appendingPathComponent("b").path])
        #expect(r.code == 3)
        #expect(try String(contentsOf: log, encoding: .utf8) == bad)
    }
}
