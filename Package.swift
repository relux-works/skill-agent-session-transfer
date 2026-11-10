// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "SessionTransfer",
    targets: [
        .testTarget(name: "SessionTransferTests", path: "Tests/SessionTransferTests")
    ]
)
