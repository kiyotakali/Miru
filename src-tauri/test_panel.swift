import AppKit

// Minimal test: create a REAL NSPanel (not swizzled) and check
// if it appears above other apps' fullscreen windows on macOS 26.

class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: NSPanel!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Accessory = no dock icon, windows float independently
        NSApp.setActivationPolicy(.accessory)

        // Create a real NSPanel — NOT swizzled from NSWindow
        panel = NSPanel(
            contentRect: NSRect(x: 200, y: 200, width: 300, height: 200),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )

        panel.level = NSWindow.Level(rawValue: 1002)  // screen-saver + 2
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.backgroundColor = .red
        panel.alphaValue = 0.7
        panel.orderFrontRegardless()

        let cb = panel.collectionBehavior.rawValue
        let level = panel.level.rawValue
        let isPanel = panel is NSPanel
        print("[test] NSPanel created — level=\(level), collectionBehavior=\(cb), isPanel=\(isPanel)")
        print("[test] A red semi-transparent box should appear.")
        print("[test] Now fullscreen Safari/Chrome/Terminal and check if it stays visible.")
        print("[test] Press Ctrl+C to quit.")
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
