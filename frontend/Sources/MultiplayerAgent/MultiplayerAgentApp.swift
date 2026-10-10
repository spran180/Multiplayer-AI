import SwiftUI
import AppKit

@main
struct MultiplayerAgentApp: App {
    @StateObject private var model = RoomModel()

    init() {
        // SwiftPM launches this executable without the normal .app activation
        // policy. Explicitly make it a foreground macOS application so the
        // WindowGroup is visible when run from Xcode or `swift run`.
        NSApplication.shared.setActivationPolicy(.regular)
    }

    var body: some Scene {
        WindowGroup {
            WorkspaceView()
                .environmentObject(model)
                .frame(minWidth: 1080, minHeight: 720)
                .preferredColorScheme(.dark)
        }
        .windowStyle(.hiddenTitleBar)
    }
}
