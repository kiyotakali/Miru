#!/usr/bin/env swift
// panel_webview.swift — Standalone Swift script: NSPanel + WKWebView overlay
// Run with: swift panel_webview.swift
// Or: chmod +x panel_webview.swift && ./panel_webview.swift

import AppKit
import WebKit

// MARK: - Research Answers
//
// Q: Can WKWebView be created directly in an NSPanel without first being in an NSWindow?
// A: YES. NSPanel is a subclass of NSWindow, so WKWebView works identically.
//    You create it with WKWebView(frame:configuration:) and add it as a subview
//    of panel.contentView. No intermediate NSWindow is needed. The view hierarchy
//    is: NSPanel -> contentView (NSView) -> WKWebView. This is confirmed working
//    in your existing test_panel_webview.swift.
//
// Q: Are there any WKWebView configuration differences needed for NSPanel vs NSWindow?
// A: No configuration differences for WKWebView itself. However, the NSPanel needs:
//    1. Override canBecomeKey -> true (so WKWebView text inputs can receive focus)
//    2. Override canBecomeMain -> true (so the panel can become the main window)
//    These are NOT WKWebView config — they are NSPanel subclass overrides needed
//    because NSPanel with .nonactivatingPanel style defaults canBecomeKey to false.
//
// Q: How to inject JavaScript into WKWebView for IPC?
// A: Two mechanisms:
//    - Swift -> JS: webView.evaluateJavaScript("code") or WKUserScript injection
//    - JS -> Swift: WKScriptMessageHandler + window.webkit.messageHandlers.<name>.postMessage()
//    See the IPC bridge setup below for a complete working example.
//
// Q: How to make WKWebView background transparent?
// A: webView.setValue(false, forKey: "drawsBackground")
//    This is a semi-private KVC property. There is no public API for this.
//    Also set the panel: isOpaque=false, backgroundColor=.clear, hasShadow=false.
//    The HTML page must also have: body { background: transparent; }

// MARK: - Custom NSPanel subclass

/// FloatingPanel overrides key/main window behavior so that WKWebView
/// text inputs and interactive elements work correctly.
/// Without these overrides, a .nonactivatingPanel refuses key window status
/// and WKWebView inputs silently drop keyboard events.
class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    /// Allow mouse events even when another app's modal dialog is up.
    /// worksWhenModal is a read-write property on NSPanel, so we set it in init.
    override init(contentRect: NSRect, styleMask style: NSWindow.StyleMask,
                  backing backingStoreType: NSWindow.BackingStoreType, defer flag: Bool) {
        super.init(contentRect: contentRect, styleMask: style, backing: backingStoreType, defer: flag)
        self.worksWhenModal = true
    }
}

// MARK: - App Delegate with WKScriptMessageHandler for IPC

class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    var panel: FloatingPanel!
    var webView: WKWebView!

    // --- IPC: JS -> Swift message handler ---
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        // message.name = handler name (e.g. "nativeBridge")
        // message.body = whatever JS sent via postMessage(...)
        print("[IPC] Received from JS handler '\(message.name)': \(message.body)")

        // Example: parse a dictionary message
        if let dict = message.body as? [String: Any],
           let action = dict["action"] as? String {
            switch action {
            case "close":
                print("[IPC] Close requested by web page")
                NSApp.terminate(nil)
            case "move":
                // Example: { action: "move", x: 100, y: 200 }
                if let x = dict["x"] as? CGFloat, let y = dict["y"] as? CGFloat {
                    panel.setFrameOrigin(NSPoint(x: x, y: y))
                    print("[IPC] Moved panel to (\(x), \(y))")
                }
            case "resize":
                if let w = dict["width"] as? CGFloat, let h = dict["height"] as? CGFloat {
                    var frame = panel.frame
                    frame.size = NSSize(width: w, height: h)
                    panel.setFrame(frame, display: true, animate: true)
                    print("[IPC] Resized panel to \(w)x\(h)")
                }
            case "setOpacity":
                if let alpha = dict["alpha"] as? CGFloat {
                    panel.alphaValue = alpha
                }
            case "ping":
                // Reply back to JS
                webView.evaluateJavaScript("window.__nativeCallback && window.__nativeCallback('pong')")
                print("[IPC] Replied pong to JS")
            default:
                print("[IPC] Unknown action: \(action)")
            }
        }
    }

    // --- WKNavigationDelegate ---
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        print("[nav] Page finished loading: \(webView.url?.absoluteString ?? "nil")")
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        print("[nav] Navigation failed: \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        print("[nav] Provisional navigation failed: \(error.localizedDescription)")
        print("[nav] Make sure your dev server is running at the target URL.")
    }

    // --- WKUIDelegate: handle JS alert/confirm/prompt ---
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        print("[JS alert] \(message)")
        completionHandler()
    }

    // MARK: - Application Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        // ---- 1. Activation Policy: Accessory (no dock icon, no menu bar) ----
        NSApp.setActivationPolicy(.accessory)

        // ---- 2. Create the NSPanel ----
        panel = FloatingPanel(
            contentRect: NSRect(x: 100, y: 100, width: 420, height: 760),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )

        // ---- 3. Window level: above fullscreen apps ----
        // Level 1002 = above screensaver level (1000) and above most system UI.
        // This ensures visibility even over fullscreen apps.
        // For reference:
        //   NSWindow.Level.normal       = 0
        //   NSWindow.Level.floating     = 3
        //   NSWindow.Level.modalPanel   = 8
        //   NSWindow.Level.mainMenu     = 24
        //   NSWindow.Level.popUpMenu    = 101
        //   NSWindow.Level.screenSaver  = 1000
        //   Custom (above all)          = 1002
        panel.level = NSWindow.Level(rawValue: 1002)

        // ---- 4. Collection behavior: visible on ALL spaces + fullscreen ----
        // .canJoinAllSpaces  — panel appears on every virtual desktop / Space
        // .fullScreenAuxiliary — panel can overlay fullscreen apps
        // .stationary — panel doesn't move with Spaces transitions (optional)
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]

        // ---- 5. Floating panel properties ----
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false  // Stay visible when app loses focus

        // ---- 6. Transparent panel ----
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false

        // ---- 7. Configure WKWebView ----
        let config = WKWebViewConfiguration()

        // Enable developer tools (right-click -> Inspect Element)
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        // Allow inline media playback
        config.preferences.setValue(true, forKey: "allowsInlineMediaPlayback")

        // --- IPC Setup: Register message handlers ---
        // JS calls: window.webkit.messageHandlers.nativeBridge.postMessage({...})
        let contentController = config.userContentController
        contentController.add(self, name: "nativeBridge")

        // --- Inject IPC bridge JavaScript at document start ---
        let bridgeScript = WKUserScript(source: """
            // Native IPC bridge — available immediately when page loads
            window.NativeBridge = {
                // Send a message to Swift
                send: function(action, data) {
                    var msg = Object.assign({ action: action }, data || {});
                    window.webkit.messageHandlers.nativeBridge.postMessage(msg);
                },
                // Close the panel
                close: function() { this.send('close'); },
                // Move panel to (x, y)
                move: function(x, y) { this.send('move', { x: x, y: y }); },
                // Resize panel
                resize: function(w, h) { this.send('resize', { width: w, height: h }); },
                // Set panel opacity (0.0 - 1.0)
                setOpacity: function(a) { this.send('setOpacity', { alpha: a }); },
                // Ping-pong test
                ping: function() { this.send('ping'); }
            };
            // Callback slot for Swift -> JS replies
            window.__nativeCallback = null;
            console.log('[NativeBridge] IPC bridge injected');
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        contentController.addUserScript(bridgeScript)

        // ---- 8. Create WKWebView DIRECTLY in the panel ----
        // WKWebView is created with the panel's content view bounds.
        // It is NEVER placed in an NSWindow first — goes directly into NSPanel.
        webView = WKWebView(frame: panel.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self

        // ---- 9. Make WKWebView background transparent ----
        // This is a semi-private KVC property. There is NO public API.
        // Works on macOS 10.12+. The HTML must also set:
        //   html, body { background: transparent !important; }
        webView.setValue(false, forKey: "drawsBackground")

        // Also make the underlying layer transparent
        webView.wantsLayer = true
        webView.layer?.backgroundColor = CGColor.clear

        // ---- 10. Add WKWebView to panel ----
        panel.contentView!.addSubview(webView)

        // ---- 11. Load the target URL ----
        let urlString = "http://127.0.0.1:5173/contextlife?mode=pet"
        if let url = URL(string: urlString) {
            webView.load(URLRequest(url: url))
            print("[setup] Loading URL: \(urlString)")
        }

        // ---- 12. Show the panel ----
        panel.orderFrontRegardless()

        // Print diagnostic info
        printDiagnostics()

        // ---- 13. Demonstrate Swift -> JS communication after 3 seconds ----
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            self?.webView.evaluateJavaScript("""
                document.title + ' — loaded at ' + new Date().toLocaleTimeString()
            """) { result, error in
                if let result = result {
                    print("[Swift->JS] Page title: \(result)")
                } else if let error = error {
                    print("[Swift->JS] Error: \(error.localizedDescription)")
                }
            }
        }
    }

    func printDiagnostics() {
        let isRealPanel = type(of: panel) == FloatingPanel.self
        let isNSPanel = (panel as Any) is NSPanel
        let level = panel.level.rawValue
        let cb = panel.collectionBehavior.rawValue
        let style = panel.styleMask.rawValue
        let frame = panel.frame

        print("")
        print("╔══════════════════════════════════════════════════════════╗")
        print("║         NSPanel + WKWebView Overlay — Running           ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print("║ Panel class:      \(String(describing: type(of: panel)).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ Is NSPanel:       \(String(describing: isNSPanel).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ Is FloatingPanel: \(String(describing: isRealPanel).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ Window level:     \(String(describing: level).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ Collection flags: \(String(describing: cb).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ Style mask:       \(String(describing: style).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ Frame:            \(String(format: "%.0fx%.0f at (%.0f, %.0f)", frame.width, frame.height, frame.origin.x, frame.origin.y).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ canBecomeKey:     \(String(describing: panel.canBecomeKey).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ canBecomeMain:    \(String(describing: panel.canBecomeMain).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ isFloatingPanel:  \(String(describing: panel.isFloatingPanel).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ hidesOnDeactivate:\(String(describing: panel.hidesOnDeactivate).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ isOpaque:         \(String(describing: panel.isOpaque).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ hasShadow:        \(String(describing: panel.hasShadow).padding(toLength: 37, withPad: " ", startingAt: 0)) ║")
        print("║ Activation policy: accessory                            ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print("║ IPC Handlers:                                           ║")
        print("║   JS -> Swift: window.webkit.messageHandlers             ║")
        print("║                .nativeBridge.postMessage({action:...})   ║")
        print("║   Swift -> JS: webView.evaluateJavaScript(...)          ║")
        print("║   Bridge API:  window.NativeBridge.close/move/resize()  ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print("║ Test: Fullscreen an app, panel should remain visible.   ║")
        print("║ Test: Open JS console, run NativeBridge.ping()          ║")
        print("║ Quit: Ctrl+C or NativeBridge.close() from JS           ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print("")
    }
}

// MARK: - Main entry point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
