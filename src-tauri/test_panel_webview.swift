import AppKit
import WebKit

// Test: Create a REAL NSPanel with a WKWebView DIRECTLY inside it
// (never created in an NSWindow first, never transferred)
// This tests whether the issue is WKWebView itself or the transfer process.

class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: NSPanel!
    var webView: WKWebView!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        // Create a real NSPanel
        panel = NSPanel(
            contentRect: NSRect(x: 200, y: 200, width: 420, height: 760),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )

        panel.level = NSWindow.Level(rawValue: 1002)
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false

        // Create WKWebView DIRECTLY in the panel
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // Make webview transparent
        webView = WKWebView(frame: panel.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")

        // Add directly to panel's content view (NOT transferred from another window)
        panel.contentView!.addSubview(webView)

        // Load the AIRI pet URL
        if let url = URL(string: "http://127.0.0.1:5173/contextlife?mode=pet") {
            webView.load(URLRequest(url: url))
        }

        panel.orderFrontRegardless()

        print("[test] NSPanel + WKWebView created DIRECTLY (no NSWindow transfer)")
        print("[test] Panel visible at 420x760. WKWebView loading pet URL.")
        print("[test] Fullscreen Safari/Chrome/Terminal and check if visible.")
        print("[test] Press Ctrl+C to quit.")
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
