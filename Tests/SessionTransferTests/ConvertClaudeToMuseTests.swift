import Foundation
import Testing

/// Runs scripts/convert-claude-to-muse.py as a black box on temporary fixtures (dry run only;
/// publishing needs the muse CLI and is verified manually per SKILL.md).

func convertScript() -> URL {
    URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("scripts/convert-claude-to-muse.py")
}

func runConvert(_ args: [String]) throws -> Run {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = ["python3", convertScript().path] + args
    let o = Pipe(), e = Pipe()
    p.standardOutput = o; p.standardError = e
    try p.run(); p.waitUntilExit()
    return Run(code: p.terminationStatus,
               out: String(decoding: o.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self),
               err: String(decoding: e.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self))
}

let claudeFixture = """
{"type":"custom-title","customTitle":"fixture-lane","sessionId":"11111111-2222-4333-8444-555555555555"}
{"type":"user","isCompactSummary":true,"sessionId":"11111111-2222-4333-8444-555555555555","cwd":"/ws","timestamp":"2026-01-01T00:00:00.000Z","message":{"role":"user","content":"SUMMARY-TEXT of earlier history"}}
{"type":"user","sessionId":"11111111-2222-4333-8444-555555555555","cwd":"/ws","timestamp":"2026-01-01T00:01:00.000Z","message":{"role":"user","content":[{"type":"text","text":"CODEWORD apple-1 please"}]}}
{"type":"assistant","sessionId":"11111111-2222-4333-8444-555555555555","timestamp":"2026-01-01T00:01:05.000Z","message":{"role":"assistant","model":"claude-x","content":[{"type":"thinking","thinking":"THINKSECRET","signature":"SIGSECRET"},{"type":"text","text":"Working on it"}]}}
{"type":"assistant","sessionId":"11111111-2222-4333-8444-555555555555","timestamp":"2026-01-01T00:01:06.000Z","message":{"role":"assistant","model":"claude-x","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"echo hi"}}]}}
{"type":"user","sessionId":"11111111-2222-4333-8444-555555555555","timestamp":"2026-01-01T00:01:07.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"hi output"}]}}
{"type":"assistant","sessionId":"11111111-2222-4333-8444-555555555555","timestamp":"2026-01-01T00:01:08.000Z","message":{"role":"assistant","model":"claude-x","content":[{"type":"text","text":"Done apple-1"}]}}
{"type":"user","sessionId":"11111111-2222-4333-8444-555555555555","cwd":"/ws","timestamp":"2026-01-01T00:02:00.000Z","message":{"role":"user","content":"second question"}}
{"type":"assistant","sessionId":"11111111-2222-4333-8444-555555555555","timestamp":"2026-01-01T00:02:05.000Z","message":{"role":"assistant","model":"claude-x","content":[{"type":"text","text":"second answer"}]}}

"""

@Suite struct ConvertClaudeToMuseTests {
    /// Claim: dry run emits a staged muse log whose sequences are contiguous from 3, user/assistant
    /// texts and the compact summary survive exactly, the tool call is summarized as an inert line,
    /// and neither the thinking text nor its signature is ever written to the target log or the IR.
    @Test func dryRunConvertsMessagesAndOmitsThinking() throws {
        let d = try tempDir()
        let src = d.appendingPathComponent("claude.jsonl")
        let bundle = d.appendingPathComponent("bundle")
        try claudeFixture.write(to: src, atomically: true, encoding: .utf8)
        let r = try runConvert(["--source-log", src.path, "--workspace", d.path,
                                "--out-dir", bundle.path,
                                "--session-id", "01a1f4ac-904b-7c40-ba6f-090edb5b740f"])
        #expect(r.code == 0)
        let staged = try String(contentsOf: bundle.appendingPathComponent("staged/session.jsonl"), encoding: .utf8)
        #expect(staged.contains("CODEWORD apple-1 please"))
        #expect(staged.contains("Done apple-1"))
        #expect(staged.contains("SUMMARY-TEXT of earlier history"))
        #expect(staged.contains("[tool Bash]"))
        #expect(staged.contains("[tool result] hi output"))
        #expect(staged.contains("second answer"))
        #expect(!staged.contains("SIGSECRET"))
        #expect(!staged.contains("THINKSECRET"))
        #expect(!staged.contains("encrypted_content"))
        var seqs: [Int] = []
        for line in staged.split(separator: "\n") {
            let obj = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
            seqs.append(obj?["sequence"] as? Int ?? -1)
        }
        #expect(seqs == Array(3..<(3 + seqs.count)))
        let fidelity = try String(contentsOf: bundle.appendingPathComponent("fidelity.json"), encoding: .utf8)
        #expect(fidelity.contains("\"thinking_blocks\": 1"))
        #expect(fidelity.contains("\"turns_native\": 2"))
        let ir = try String(contentsOf: bundle.appendingPathComponent("ir/events.jsonl"), encoding: .utf8)
        #expect(ir.contains("\"kind\":\"opaque_reasoning\""))
        #expect(!ir.contains("THINKSECRET"))
    }

    /// Claim: invalid JSON is invalid-input (3), and missing source, a non-v7 target id and an
    /// already-existing bundle dir are refusals (2) that publish nothing.
    @Test func refusals() throws {
        let d = try tempDir()
        let bad = d.appendingPathComponent("bad.jsonl")
        try (claudeFixture + "{oops\n").write(to: bad, atomically: true, encoding: .utf8)
        var r = try runConvert(["--source-log", bad.path, "--workspace", d.path,
                                "--out-dir", d.appendingPathComponent("b1").path])
        #expect(r.code == 3)

        r = try runConvert(["--source-log", d.appendingPathComponent("missing.jsonl").path,
                            "--workspace", d.path, "--out-dir", d.appendingPathComponent("b2").path])
        #expect(r.code == 2)

        let src = d.appendingPathComponent("claude.jsonl")
        try claudeFixture.write(to: src, atomically: true, encoding: .utf8)
        r = try runConvert(["--source-log", src.path, "--workspace", d.path,
                            "--out-dir", d.appendingPathComponent("b3").path,
                            "--session-id", "11111111-2222-4333-8444-555555555555"])
        #expect(r.code == 2)

        let existing = d.appendingPathComponent("b4")
        try FileManager.default.createDirectory(at: existing, withIntermediateDirectories: true)
        r = try runConvert(["--source-log", src.path, "--workspace", d.path, "--out-dir", existing.path])
        #expect(r.code == 2)

        r = try runConvert(["--source-log", src.path, "--workspace", d.path,
                            "--out-dir", d.appendingPathComponent("b5").path,
                            "--apply", "--muse-bin", "/nonexistent/muse"])
        #expect(r.code == 2)
    }
}
