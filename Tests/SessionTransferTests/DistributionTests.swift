import Foundation
import Testing

// The installed zipapp carries its own modules and Muse template. Execute it outside the
// converter checkout so a missing package resource fails rather than importing local sources.
@Test func distributedCommandProjectsMuse() throws {
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    let dir = URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"] ?? NSTemporaryDirectory())
        .appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let exe = dir.appendingPathComponent("agent-session-convert")
    try FileManager.default.copyItem(at: root.appendingPathComponent("scripts/agent-session-convert.pyz"), to: exe)
    let input = dir.appendingPathComponent("input.jsonl"), output = dir.appendingPathComponent("bundle")
    try #"{"sessionId":"test","type":"user","message":{"content":"distribution-marker"}}"#
        .write(to: input, atomically: true, encoding: .utf8)
    let p = Process()
    p.executableURL = exe
    p.currentDirectoryURL = dir
    p.arguments = ["convert", "--from", "claude", "--to", "muse", "--source-log", input.path,
                   "--out-dir", output.path, "--workspace", dir.path]
    try p.run()
    p.waitUntilExit()
    try #require(p.terminationStatus == 0)
    let staged = try String(contentsOf: output.appendingPathComponent("projection/session.jsonl"), encoding: .utf8)
    #expect(staged.contains("distribution-marker"))
}
