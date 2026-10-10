// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "MultiplayerAgent",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "MultiplayerAgent", targets: ["MultiplayerAgent"])],
    targets: [.executableTarget(name: "MultiplayerAgent")]
)
