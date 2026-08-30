use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};

use tauri::{
    AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Monitor, PhysicalPosition, Url,
    WebviewUrl, WebviewWindow, WebviewWindowBuilder, Window,
};

#[cfg(target_os = "macos")]
use cocoa::base::id;
#[cfg(target_os = "macos")]
#[macro_use]
extern crate objc;

// ── NSPanel state (macOS) ────────────────────────────────────────────────
//
// Holds a REAL NSPanel created via alloc/init (NOT swizzled from NSWindow).
// A real NSPanel floats above ALL fullscreen apps on macOS.  The swizzling
// approach (object_setClass) does NOT work because the window server tracks
// the original class at creation time.

#[cfg(target_os = "macos")]
mod real_panel {
    use cocoa::base::id;
    use std::sync::Mutex;

    struct Ptr(id);
    unsafe impl Send for Ptr {}
    unsafe impl Sync for Ptr {}

    #[derive(Default)]
    pub struct State {
        inner: Mutex<Option<Ptr>>,
    }

    impl State {
        pub fn get(&self) -> Option<id> {
            self.inner.lock().ok()?.as_ref().map(|p| p.0)
        }
        pub fn set(&self, panel: id) {
            *self.inner.lock().unwrap() = Some(Ptr(panel));
        }
        pub fn take(&self) -> Option<id> {
            self.inner.lock().ok()?.take().map(|p| p.0)
        }
    }
}

// ── Constants (matching airi_pet.swift) ──────────────────────────────────

const PET_ASPECT: f64 = 420.0 / 760.0;
const PET_MIN_W: u32 = 160;
const PET_MAX_W: u32 = 900;
const PET_DEFAULT_W: f64 = 420.0;
const PET_DEFAULT_H: f64 = 760.0;
const PET_MARGIN: f64 = 26.0;

#[cfg(target_os = "macos")]
#[derive(Clone, Copy)]
struct AiriDragState {
    mouse_x: f64,
    mouse_y: f64,
    origin_x: f64,
    origin_y: f64,
}

#[cfg(target_os = "macos")]
static AIRI_DRAG_STATE: Mutex<Option<AiriDragState>> = Mutex::new(None);

// ── macOS coordinate helpers ─────────────────────────────────────────────

#[cfg(target_os = "macos")]
unsafe fn screen_height() -> f64 {
    let cls = objc::runtime::Class::get("NSScreen").unwrap();
    let screen: id = msg_send![cls, mainScreen];
    let frame: cocoa::foundation::NSRect = msg_send![screen, frame];
    frame.size.height
}

/// Convert Tauri top-left (x, y) to macOS bottom-left origin.
#[cfg(target_os = "macos")]
unsafe fn to_ns_origin(x: f64, y: f64, win_h: f64) -> cocoa::foundation::NSPoint {
    cocoa::foundation::NSPoint::new(x, screen_height() - y - win_h)
}

/// Convert macOS NSRect to Tauri-style (x, y, w, h) with top-left origin.
#[cfg(target_os = "macos")]
unsafe fn from_ns_frame(frame: cocoa::foundation::NSRect) -> (f64, f64, f64, f64) {
    let y = screen_height() - frame.origin.y - frame.size.height;
    (frame.origin.x, y, frame.size.width, frame.size.height)
}

#[cfg(target_os = "macos")]
fn macos_webview_window_frame(window: &WebviewWindow) -> Option<(f64, f64, f64, f64)> {
    let ns_win = window.ns_window().ok()? as id;
    Some(unsafe { from_ns_frame(msg_send![ns_win, frame]) })
}

// ── Shared state ─────────────────────────────────────────────────────────

#[derive(Clone)]
struct AiriPetMonitorHandle {
    running: Arc<AtomicBool>,
    pass_through: Arc<AtomicBool>,
}

#[derive(Default)]
struct AiriPetMonitorState {
    handles: Mutex<HashMap<String, AiriPetMonitorHandle>>,
}

/// Tracks whether the pet is hidden (for hotkey toggle).
#[derive(Default)]
struct PetVisibilityState {
    hidden: AtomicBool,
}

fn should_show_pet_for_toggle(remembered_hidden: bool, actual_visible: bool) -> bool {
    remembered_hidden || !actual_visible
}

fn pet_url_with_cache_buster() -> String {
    let url = std::env::var("AIRI_PET_URL")
        .unwrap_or_else(|_| "http://127.0.0.1:5001/pet".to_string());
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!(
        "{url}{sep}_t={ts}",
        sep = if url.contains('?') { "&" } else { "?" }
    )
}

/// Tracks the currently-registered global hotkey so we can unregister it
/// before swapping in a new one via `update_global_hotkey`.
#[derive(Default)]
struct HotkeyState {
    current: Mutex<Option<tauri_plugin_global_shortcut::Shortcut>>,
}

/// Counter for native window labels to ensure uniqueness.
static NATIVE_WIN_COUNTER: AtomicU64 = AtomicU64::new(0);

// ── Helpers ──────────────────────────────────────────────────────────────

fn airi_pet_monitor_handle(window: &Window) -> Result<AiriPetMonitorHandle, String> {
    let state = window.state::<AiriPetMonitorState>();
    let mut handles = state
        .handles
        .lock()
        .map_err(|_| "pet monitor state lock poisoned".to_string())?;

    Ok(handles
        .entry(window.label().to_string())
        .or_insert_with(|| AiriPetMonitorHandle {
            running: Arc::new(AtomicBool::new(false)),
            pass_through: Arc::new(AtomicBool::new(false)),
        })
        .clone())
}

fn airi_window_mode(url: &Url) -> String {
    url.query_pairs()
        .find(|(key, _)| key == "mode")
        .map(|(_, value)| value.into_owned())
        .unwrap_or_else(|| "pet".to_string())
}

fn airi_window_label(mode: &str) -> &'static str {
    if mode == "pet" {
        "airi-pet"
    } else {
        "airi-dock"
    }
}

fn airi_window_defaults(mode: &str) -> (f64, f64, f64, f64) {
    if mode == "pet" {
        (PET_DEFAULT_W, PET_DEFAULT_H, 200.0, 300.0)
    } else {
        (460.0, 900.0, 360.0, 640.0)
    }
}

fn preferred_monitor(app: &AppHandle) -> Result<Option<Monitor>, String> {
    if let Some(main_window) = app.get_webview_window("main") {
        if let Some(monitor) = main_window
            .current_monitor()
            .map_err(|err| format!("detect current monitor failed: {err}"))?
        {
            return Ok(Some(monitor));
        }
    }
    app.primary_monitor()
        .map_err(|err| format!("detect primary monitor failed: {err}"))
}

fn bottom_anchor_for_monitor(
    monitor: Option<Monitor>,
    width: f64,
    height: f64,
    margin: f64,
    anchor: &str,
) -> (f64, f64) {
    let Some(monitor) = monitor else {
        return (48.0, 48.0);
    };

    // work_area() returns physical pixels; position() expects logical pixels.
    // Divide by scale_factor to convert.
    let scale = monitor.scale_factor();
    let work_area = monitor.work_area();
    let area_x = f64::from(work_area.position.x) / scale;
    let area_y = f64::from(work_area.position.y) / scale;
    let area_width = f64::from(work_area.size.width) / scale;
    let area_height = f64::from(work_area.size.height) / scale;
    let max_x = area_x + (area_width - width - margin).max(0.0);
    let min_x = area_x + margin;
    let y = area_y + (area_height - height - margin).max(0.0);

    let x = match anchor {
        "bottom-left" => min_x,
        "bottom-center" => area_x + ((area_width - width) / 2.0).max(0.0),
        _ => max_x,
    };

    (x, y)
}

fn valid_scale_factor(scale_factor: f64) -> f64 {
    if scale_factor.is_finite() && scale_factor > 0.0 {
        scale_factor
    } else {
        1.0
    }
}

fn physical_dimension_to_logical(value: u32, scale_factor: f64) -> f64 {
    f64::from(value) / valid_scale_factor(scale_factor)
}

fn scaled_pet_logical_size(
    physical_width: u32,
    scale_factor: f64,
    factor: f64,
) -> (u32, u32) {
    let logical_width = physical_dimension_to_logical(physical_width, scale_factor);
    let new_width = (logical_width * factor).round() as u32;
    let new_width = new_width.clamp(PET_MIN_W, PET_MAX_W);
    let new_height = (f64::from(new_width) / PET_ASPECT).round() as u32;
    (new_width, new_height)
}

/// Convert user hotkey spec (e.g. "cmd+shift+a") to Tauri shortcut format.
fn normalize_hotkey(spec: &str) -> String {
    spec.split('+')
        .map(|part| {
            let p = part.trim().to_lowercase();
            match p.as_str() {
                "cmd" | "command" | "meta" => {
                    if cfg!(target_os = "macos") {
                        "Super".to_string()
                    } else {
                        "Ctrl".to_string()
                    }
                }
                "ctrl" | "control" => "Ctrl".to_string(),
                "shift" => "Shift".to_string(),
                "alt" | "option" | "opt" => "Alt".to_string(),
                "super" | "win" => "Super".to_string(),
                _ => p.to_uppercase(),
            }
        })
        .collect::<Vec<_>>()
        .join("+")
}

// ── macOS: fullscreen overlay setup ──────────────────────────────────────
//
// The critical combination that makes the pet stay above ALL apps including
// fullscreen:
//   1. A REAL NSPanel (alloc/init, NOT swizzled)
//   2. Level 1002 (above NSScreenSaverWindowLevel = 1000)
//   3. CollectionBehavior: canJoinAllSpaces | fullScreenAuxiliary
//   4. isFloatingPanel = true
//   5. hidesOnDeactivate = false
//   6. App activation policy = Accessory (no dock icon)
//
// The Tauri NSWindow is attached as a CHILD of this panel, inheriting its
// space and fullscreen behavior.

#[cfg(target_os = "macos")]
unsafe fn create_pet_panel_and_attach(
    ns_win: id,
    app_state: &real_panel::State,
) {
    // Create a 1x1 invisible anchor panel — the "ticket" to fullscreen Spaces.
    let tiny = cocoa::foundation::NSRect::new(
        cocoa::foundation::NSPoint::new(0.0, 0.0),
        cocoa::foundation::NSSize::new(1.0, 1.0),
    );
    let cls = objc::runtime::Class::get("NSPanel").unwrap();
    let panel: id = msg_send![cls, alloc];
    let panel: id = msg_send![panel,
        initWithContentRect: tiny
        styleMask: 128u64   // NonactivatingPanel
        backing: 2u64       // Buffered
        defer: false
    ];

    // Panel properties — exactly matching Swift's FloatingPanel
    let _: () = msg_send![panel, setFloatingPanel: true];
    let _: () = msg_send![panel, setLevel: 1002i64];
    // CanJoinAllSpaces(1) | FullScreenAuxiliary(8) | IgnoresCycle(64)
    let _: () = msg_send![panel, setCollectionBehavior: 73u64];
    let _: () = msg_send![panel, setHidesOnDeactivate: false];
    let _: () = msg_send![panel, setCanHide: false];
    let _: () = msg_send![panel, setOpaque: false];
    let clear: id =
        msg_send![objc::runtime::Class::get("NSColor").unwrap(), clearColor];
    let _: () = msg_send![panel, setBackgroundColor: clear];
    let _: () = msg_send![panel, setHasShadow: false];
    let _: () = msg_send![panel, setAlphaValue: 0.0f64]; // fully invisible
    let _: () = msg_send![panel, orderFrontRegardless];
    let _: id = msg_send![panel, retain];

    // Apply same overlay properties to the Tauri NSWindow (child)
    let _: () = msg_send![ns_win, setLevel: 1002i64];
    // CanJoinAllSpaces(1) | FullScreenAuxiliary(8) | IgnoresCycle(64)
    let _: () = msg_send![ns_win, setCollectionBehavior: 73u64];
    let _: () = msg_send![ns_win, setHidesOnDeactivate: false];
    let _: () = msg_send![ns_win, setCanHide: false];

    // Attach Tauri window as child of the panel — NSWindowAbove = 1
    let _: () = msg_send![panel, addChildWindow: ns_win ordered: 1i64];

    // Store reference
    app_state.set(panel);

    let lvl: i64 = msg_send![panel, level];
    let beh: u64 = msg_send![panel, collectionBehavior];
    log::info!(
        "pet panel: real NSPanel created — level={lvl}, behavior={beh}, child attached"
    );
}

// ── Tauri Commands ───────────────────────────────────────────────────────

#[tauri::command]
fn open_airi_window(app: AppHandle, url: String) -> Result<(), String> {
    eprintln!("[pet-debug] open_airi_window called, url={url}");
    let parsed: Url = url
        .parse()
        .map_err(|err| format!("invalid airi url: {err}"))?;
    let mode = airi_window_mode(&parsed);
    let label = airi_window_label(&mode);

    // If window already exists, just show it
    if let Some(existing) = app.get_webview_window(label) {
        #[cfg(target_os = "macos")]
        {
            let state = app.state::<real_panel::State>();
            if let Some(panel) = state.get() {
                unsafe {
                    let _: () = msg_send![panel, setIgnoresMouseEvents: false];
                    let _: () = msg_send![panel, orderFrontRegardless];
                }
                app.state::<PetVisibilityState>()
                    .hidden
                    .store(false, Ordering::Relaxed);
                return Ok(());
            }
        }
        let _ = existing.set_ignore_cursor_events(false);
        let _ = existing.show();
        let _ = existing.unminimize();
        existing
            .set_focus()
            .map_err(|err| format!("focus airi window failed: {err}"))?;
        app.state::<PetVisibilityState>()
            .hidden
            .store(false, Ordering::Relaxed);
        return Ok(());
    }

    let (default_width, default_height, min_width, min_height) = airi_window_defaults(&mode);

    let mut builder = WebviewWindowBuilder::new(&app, label, WebviewUrl::External(parsed));
    builder = builder
        .title(if mode == "pet" {
            "ContextLife AIRI Pet"
        } else {
            "ContextLife AIRI"
        })
        .inner_size(default_width, default_height)
        .min_inner_size(min_width, min_height)
        .resizable(true);

    if mode == "pet" {
        builder = builder
            .transparent(true)
            .decorations(false)
            .shadow(false)
            .skip_taskbar(true)
            .visible(false);

        // Windows: always_on_top is sufficient (no fullscreen overlay issue)
        #[cfg(not(target_os = "macos"))]
        {
            builder = builder.always_on_top(true);
        }
    } else {
        builder = builder.always_on_top(true);
    }

    let _built_window = builder
        .build()
        .map_err(|err| format!("open airi window failed: {err}"))?;

    // macOS: position + overlay + show — all via Cocoa on the main thread
    #[cfg(target_os = "macos")]
    if mode == "pet" {
        let app_for_panel = app.clone();
        let win_for_panel = _built_window.clone();
        let _ = app.run_on_main_thread(move || {
            unsafe {
                let ns_win = match win_for_panel.ns_window() {
                    Ok(w) => w as id,
                    Err(e) => {
                        log::error!("pet: get ns_window failed: {e}");
                        return;
                    }
                };

                // 1. Make window + webview fully transparent
                //    Window stays hidden (.visible(false)) — no flash at default position.
                //    Frontend's initPetBridge() will resize+snap then we show.
                let _: () = msg_send![ns_win, setOpaque: false];
                let clear: id =
                    msg_send![objc::runtime::Class::get("NSColor").unwrap(), clearColor];
                let _: () = msg_send![ns_win, setBackgroundColor: clear];
                let _: () = msg_send![ns_win, setHasShadow: false];

                let content_view: id = msg_send![ns_win, contentView];
                let subviews: id = msg_send![content_view, subviews];
                let count: usize = msg_send![subviews, count];
                for i in 0..count {
                    let sv: id = msg_send![subviews, objectAtIndex: i];
                    let sel = objc::runtime::Sel::register("_setDrawsBackground:");
                    let responds: bool = msg_send![sv, respondsToSelector: sel];
                    if responds {
                        let _: () = msg_send![sv, _setDrawsBackground: false];
                        break;
                    }
                }

                // 2. Overlay properties — stay above all apps
                let _: () = msg_send![ns_win, setLevel: 1002i64];
                // CanJoinAllSpaces(1) | FullScreenAuxiliary(8) | IgnoresCycle(64)
                let _: () = msg_send![ns_win, setCollectionBehavior: 73u64];
                let _: () = msg_send![ns_win, setHidesOnDeactivate: false];
                let _: () = msg_send![ns_win, setCanHide: false];
            }

            eprintln!("[pet-debug] cocoa setup done (window stays hidden until frontend snap)");
        });
    }

    // Non-macOS: position via Tauri API
    #[cfg(not(target_os = "macos"))]
    if mode == "pet" {
        let win_for_pos = _built_window.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(200));
            if let Ok(Some(monitor)) = win_for_pos.current_monitor() {
                let scale = monitor.scale_factor();
                let wa = monitor.work_area();
                let area_w = f64::from(wa.size.width) / scale;
                let area_h = f64::from(wa.size.height) / scale;
                let area_x = f64::from(wa.position.x) / scale;
                let area_y = f64::from(wa.position.y) / scale;
                let x = area_x + area_w - PET_DEFAULT_W - PET_MARGIN;
                let y = area_y + area_h - PET_DEFAULT_H - PET_MARGIN;
                let _ = win_for_pos.set_position(tauri::LogicalPosition::new(x, y));
            }
            let _ = win_for_pos.show();
        });
    }

    app.state::<PetVisibilityState>()
        .hidden
        .store(false, Ordering::Relaxed);

    Ok(())
}

#[tauri::command]
fn close_airi_window(window: Window) -> Result<(), String> {
    eprintln!("[pet-debug] close_airi_window called");
    let _ = stop_airi_pet_monitor(window.clone());

    #[cfg(target_os = "macos")]
    {
        if let Some(panel) = window.app_handle().state::<real_panel::State>().take() {
            unsafe {
                let _: () = msg_send![panel, close];
                let _: () = msg_send![panel, release];
            }
        }
    }

    window
        .close()
        .map_err(|err| format!("close airi window failed: {err}"))
}

#[tauri::command]
fn hide_airi_window(window: Window) -> Result<(), String> {
    eprintln!("[pet-debug] hide_airi_window called");
    let _ = stop_airi_pet_monitor(window.clone());

    window
        .app_handle()
        .state::<PetVisibilityState>()
        .hidden
        .store(true, Ordering::Relaxed);

    #[cfg(target_os = "macos")]
    {
        if let Some(panel) = window.app_handle().state::<real_panel::State>().get() {
            unsafe {
                let _: () = msg_send![panel, orderOut: cocoa::base::nil];
            }
        }
        // Also hide the child Tauri window
        let _ = window.hide();
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        window
            .hide()
            .map_err(|err| format!("hide airi window failed: {err}"))
    }
}

#[tauri::command]
fn get_airi_window_position(window: Window) -> Result<(i32, i32), String> {
    #[cfg(target_os = "macos")]
    {
        let ns_win = window.ns_window().map_err(|e| format!("{e}"))? as id;
        let (x, y, _, _) = unsafe { from_ns_frame(msg_send![ns_win, frame]) };
        return Ok((x as i32, y as i32));
    }
    #[cfg(not(target_os = "macos"))]
    {
        let pos = window
            .outer_position()
            .map_err(|err| format!("read airi window position failed: {err}"))?;
        Ok((pos.x, pos.y))
    }
}

#[tauri::command]
fn move_airi_window(window: Window, x: i32, y: i32) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        // Use Cocoa directly — Tauri set_position is unreliable on macOS
        let ns_win = window.ns_window().map_err(|e| format!("{e}"))? as id;
        unsafe {
            let frame: cocoa::foundation::NSRect = msg_send![ns_win, frame];
            let origin = to_ns_origin(x as f64, y as f64, frame.size.height);
            let _: () = msg_send![ns_win, setFrameOrigin: origin];
        }
        return Ok(());
    }
    #[cfg(not(target_os = "macos"))]
    window
        .set_position(PhysicalPosition::new(x, y))
        .map_err(|err| format!("move airi window failed: {err}"))
}

#[tauri::command]
fn start_airi_window_drag(window: Window) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let ns_win = window.ns_window().map_err(|e| format!("{e}"))? as id;
        unsafe {
            let frame: cocoa::foundation::NSRect = msg_send![ns_win, frame];
            let ns_event_cls = objc::runtime::Class::get("NSEvent").unwrap();
            let mouse: cocoa::foundation::NSPoint = msg_send![ns_event_cls, mouseLocation];
            let mut state = AIRI_DRAG_STATE
                .lock()
                .map_err(|_| "airi drag state poisoned".to_string())?;
            *state = Some(AiriDragState {
                mouse_x: mouse.x,
                mouse_y: mouse.y,
                origin_x: frame.origin.x,
                origin_y: frame.origin.y,
            });
        }
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        window
            .start_dragging()
            .map_err(|err| format!("start airi window drag failed: {err}"))
    }
}

#[tauri::command]
fn update_airi_window_drag(window: Window) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let state = {
            let state = AIRI_DRAG_STATE
                .lock()
                .map_err(|_| "airi drag state poisoned".to_string())?;
            match *state {
                Some(state) => state,
                None => return Ok(()),
            }
        };

        let ns_win = window.ns_window().map_err(|e| format!("{e}"))? as id;
        unsafe {
            let ns_event_cls = objc::runtime::Class::get("NSEvent").unwrap();
            let mouse: cocoa::foundation::NSPoint = msg_send![ns_event_cls, mouseLocation];
            let origin = cocoa::foundation::NSPoint::new(
                state.origin_x + (mouse.x - state.mouse_x),
                state.origin_y + (mouse.y - state.mouse_y),
            );
            let _: () = msg_send![ns_win, setFrameOrigin: origin];
        }
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        let _ = window;
        Ok(())
    }
}

#[tauri::command]
fn end_airi_window_drag() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let mut state = AIRI_DRAG_STATE
            .lock()
            .map_err(|_| "airi drag state poisoned".to_string())?;
        *state = None;
    }
    Ok(())
}

#[tauri::command]
fn scale_airi_window(window: Window, factor: f64) -> Result<(u32, u32), String> {
    #[cfg(target_os = "macos")]
    {
        let ns_win = window.ns_window().map_err(|e| format!("{e}"))? as id;
        let (new_width, new_height) = unsafe {
            let frame: cocoa::foundation::NSRect = msg_send![ns_win, frame];
            let cur_w = frame.size.width as u32;
            let nw = ((cur_w as f64) * factor).round() as u32;
            let nw = nw.clamp(PET_MIN_W, PET_MAX_W);
            let nh = ((nw as f64) / PET_ASPECT).round() as u32;
            let size = cocoa::foundation::NSSize::new(nw as f64, nh as f64);
            let _: () = msg_send![ns_win, setContentSize: size];
            (nw, nh)
        };
        return Ok((new_width, new_height));
    }
    #[cfg(not(target_os = "macos"))]
    {
        let size = window
            .outer_size()
            .map_err(|err| format!("read airi window size failed: {err}"))?;
        let scale_factor = window
            .scale_factor()
            .map_err(|err| format!("read airi window scale failed: {err}"))?;
        let (new_width, new_height) =
            scaled_pet_logical_size(size.width, scale_factor, factor);
        window
            .set_size(LogicalSize::new(
                f64::from(new_width),
                f64::from(new_height),
            ))
            .map_err(|err| format!("scale airi window failed: {err}"))?;
        Ok((new_width, new_height))
    }
}

/// Position using NSScreen.mainScreen.visibleFrame (macOS Cocoa — reliable on Retina)
#[cfg(target_os = "macos")]
unsafe fn snap_ns_window(ns_win: id, anchor: &str) {
    let screen_cls = objc::runtime::Class::get("NSScreen").unwrap();
    let main_screen: id = msg_send![screen_cls, mainScreen];
    let visible: cocoa::foundation::NSRect = msg_send![main_screen, visibleFrame];
    let frame: cocoa::foundation::NSRect = msg_send![ns_win, frame];
    let w = frame.size.width;
    let h = frame.size.height;
    let margin = PET_MARGIN;

    let ns_x = match anchor {
        "bottom-left" => visible.origin.x + margin,
        "bottom-center" => visible.origin.x + (visible.size.width - w) / 2.0,
        _ => visible.origin.x + visible.size.width - w - margin,
    };
    let ns_y = visible.origin.y + margin;

    let new_frame = cocoa::foundation::NSRect::new(
        cocoa::foundation::NSPoint::new(ns_x, ns_y),
        cocoa::foundation::NSSize::new(w, h),
    );
    let _: () = msg_send![ns_win, setFrame: new_frame display: true];
}

#[tauri::command]
fn snap_airi_window(window: Window, anchor: Option<String>) -> Result<(), String> {
    let anchor_str = anchor.as_deref().unwrap_or("bottom-right");
    eprintln!("[pet-debug] snap_airi_window called, anchor={anchor_str}");

    #[cfg(target_os = "macos")]
    {
        let ns_win = window.ns_window().map_err(|e| format!("{e}"))? as id;
        unsafe { snap_ns_window(ns_win, anchor_str); }
        // Show the window if still hidden from initial .visible(false)
        if !window.is_visible().unwrap_or(true) {
            eprintln!("[pet-debug] first snap — showing window");
            let _ = window.show();
        }
        window
            .app_handle()
            .state::<PetVisibilityState>()
            .hidden
            .store(false, Ordering::Relaxed);
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        let monitor = window
            .current_monitor()
            .map_err(|err| format!("detect current monitor failed: {err}"))?
            .or_else(|| window.primary_monitor().ok().flatten());
        let size = window
            .outer_size()
            .map_err(|err| format!("read airi window size failed: {err}"))?;
        let scale_factor = monitor
            .as_ref()
            .map(Monitor::scale_factor)
            .or_else(|| window.scale_factor().ok())
            .unwrap_or(1.0);
        let (x, y) = bottom_anchor_for_monitor(
            monitor,
            physical_dimension_to_logical(size.width, scale_factor),
            physical_dimension_to_logical(size.height, scale_factor),
            PET_MARGIN,
            anchor_str,
        );
        window
            .set_position(LogicalPosition::new(x, y))
            .map_err(|err| format!("snap airi window failed: {err}"))
    }
}

#[tauri::command]
fn resize_airi_window(
    window: Window,
    width: Option<u32>,
    height: Option<u32>,
    preset: Option<String>,
    anchor: Option<String>,
) -> Result<(), String> {
    eprintln!("[pet-debug] resize_airi_window called, preset={preset:?}, anchor={anchor:?}");
    let (mut w, mut h) = (PET_DEFAULT_W as u32, PET_DEFAULT_H as u32);
    if let Some(p) = preset.as_deref() {
        match p {
            "small" => {
                w = 280;
                h = (280.0 / PET_ASPECT).round() as u32;
            }
            "large" => {
                w = 560;
                h = (560.0 / PET_ASPECT).round() as u32;
            }
            _ => {}
        }
    }
    if let Some(aw) = width { w = aw; }
    if let Some(ah) = height { h = ah; }

    #[cfg(target_os = "macos")]
    {
        let ns_win = window.ns_window().map_err(|e| format!("{e}"))? as id;
        unsafe {
            let size = cocoa::foundation::NSSize::new(w as f64, h as f64);
            let _: () = msg_send![ns_win, setContentSize: size];
        }
        return snap_airi_window(window, anchor);
    }
    #[cfg(not(target_os = "macos"))]
    {
        window
            .set_size(LogicalSize::new(f64::from(w), f64::from(h)))
            .map_err(|err| format!("resize airi window failed: {err}"))?;
        snap_airi_window(window, anchor)
    }
}

#[tauri::command]
fn elevate_airi_window(window: Window) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        if let Some(panel) = window.app_handle().state::<real_panel::State>().get() {
            unsafe {
                let _: () = msg_send![panel, setLevel: 1002i64];
                let _: () = msg_send![panel, orderFrontRegardless];
            }
        }
        if let Ok(ns_win) = window.ns_window() {
            let ns_win = ns_win as id;
            unsafe {
                let _: () = msg_send![ns_win, setLevel: 1002i64];
                let _: () = msg_send![ns_win, orderFrontRegardless];
            }
        }
    }
    Ok(())
}

#[tauri::command]
fn start_airi_pet_monitor(window: Window) -> Result<(), String> {
    let handle = airi_pet_monitor_handle(&window)?;

    if handle.running.swap(true, Ordering::Relaxed) {
        return Ok(()); // Already running
    }

    let label = window.label().to_string();
    let app = window.app_handle().clone();
    let running = handle.running.clone();
    let pass_through = handle.pass_through.clone();

    thread::spawn(move || {
        let mut tick_count: u64 = 0;

        while running.load(Ordering::Relaxed) {
            thread::sleep(Duration::from_millis(48));
            tick_count += 1;

            let Some(window) = app.get_webview_window(&label) else {
                break;
            };

            // Re-apply fullscreen overlay properties every ~2 seconds
            // to counter macOS resetting them on Space switches.
            #[cfg(target_os = "macos")]
            if tick_count % 42 == 0 {
                if let Some(panel) = app.state::<real_panel::State>().get() {
                    unsafe {
                        let _: () = msg_send![panel, setLevel: 1002i64];
                        let _: () = msg_send![panel, setCollectionBehavior: 73u64];
                        let _: () = msg_send![panel, orderFrontRegardless];
                    }
                }
                // Also re-apply to the child Tauri window
                if let Ok(ns_win) = window.ns_window() {
                    let ns_win = ns_win as id;
                    unsafe {
                        let _: () = msg_send![ns_win, setLevel: 1002i64];
                        let _: () = msg_send![ns_win, setCollectionBehavior: 73u64];
                        let _: () = msg_send![ns_win, setHidesOnDeactivate: false];
                    }
                }
            }

            // Cursor tracking for pass-through.
            //
            // 2026-05-17 multi-monitor fix:
            //   Tao 0.34.5 cursor_position() has a unit-mixing bug
            //   (util/mod.rs:103): it computes
            //     `y = CGDisplay::main().pixels_high() - NSEvent.mouseLocation.y`
            //   subtracting "primary screen PHYSICAL pixels" from
            //   "Cocoa LOGICAL y". On the primary screen the frontend's
            //   windowIsPhysical heuristic (pet.html:1597) happens to
            //   normalize this away, but on a secondary screen
            //   (especially when its scale_factor differs from primary's)
            //   the heuristic fails — cursor & window end up in different
            //   coordinate systems, inside_window stays false, and the
            //   pet stays in pass-through mode (can't click/drag).
            //
            //   Workaround: do the cursor lookup ourselves with raw
            //   NSEvent.mouseLocation, convert Cocoa → top-left using
            //   the primary screen's LOGICAL height (same baseline that
            //   from_ns_frame already uses). Now cursor and window are
            //   both in logical top-left coords, multi-monitor-safe.
            #[cfg(target_os = "macos")]
            let cursor = unsafe {
                let ns_event_cls = objc::runtime::Class::get("NSEvent").unwrap();
                let mouse: cocoa::foundation::NSPoint =
                    msg_send![ns_event_cls, mouseLocation];
                tauri::PhysicalPosition::new(mouse.x, screen_height() - mouse.y)
            };
            #[cfg(not(target_os = "macos"))]
            let cursor = match app.cursor_position() {
                Ok(position) => position,
                Err(_) => continue,
            };

            let (ox, oy, ow, oh) = {
                #[cfg(target_os = "macos")]
                {
                    if let Some(frame) = macos_webview_window_frame(&window) {
                        frame
                    } else {
                        let pos = match window.outer_position() {
                            Ok(p) => p,
                            Err(_) => continue,
                        };
                        let sz = match window.outer_size() {
                            Ok(s) => s,
                            Err(_) => continue,
                        };
                        (
                            f64::from(pos.x),
                            f64::from(pos.y),
                            f64::from(sz.width),
                            f64::from(sz.height),
                        )
                    }
                }
                #[cfg(not(target_os = "macos"))]
                {
                    let pos = match window.outer_position() {
                        Ok(p) => p,
                        Err(_) => continue,
                    };
                    let sz = match window.outer_size() {
                        Ok(s) => s,
                        Err(_) => continue,
                    };
                    (
                        f64::from(pos.x),
                        f64::from(pos.y),
                        f64::from(sz.width),
                        f64::from(sz.height),
                    )
                }
            };

            let inside_window = cursor.x >= ox
                && cursor.x <= ox + ow
                && cursor.y >= oy
                && cursor.y <= oy + oh;
            let local_x = cursor.x - ox;
            let local_y = cursor.y - oy;

            let _ = window.emit(
                "contextlife://pet-cursor",
                serde_json::json!({
                    "insideWindow": inside_window,
                    "cursor": { "x": cursor.x, "y": cursor.y },
                    "window": { "x": ox, "y": oy, "width": ow, "height": oh },
                    "local": { "x": local_x, "y": local_y },
                    "coordinateSpace": if cfg!(target_os = "macos") { "logical" } else { "native" },
                    "passThrough": pass_through.load(Ordering::Relaxed)
                }),
            );
        }

        pass_through.store(false, Ordering::Relaxed);
        if let Some(window) = app.get_webview_window(&label) {
            #[cfg(target_os = "macos")]
            if let Ok(ns_win) = window.ns_window() {
                let ns_win = ns_win as id;
                unsafe {
                    let _: () = msg_send![ns_win, setIgnoresMouseEvents: false];
                }
            }
            let _ = window.set_ignore_cursor_events(false);
            let _ = window.emit("contextlife://pet-pass-through", false);
        }
    });

    Ok(())
}

#[tauri::command]
fn stop_airi_pet_monitor(window: Window) -> Result<(), String> {
    let handle = airi_pet_monitor_handle(&window)?;
    handle.running.store(false, Ordering::Relaxed);
    handle.pass_through.store(false, Ordering::Relaxed);

    #[cfg(target_os = "macos")]
    if let Ok(ns_win) = window.ns_window() {
        let ns_win = ns_win as id;
        unsafe {
            let _: () = msg_send![ns_win, setIgnoresMouseEvents: false];
        }
    }

    window
        .set_ignore_cursor_events(false)
        .map_err(|err| format!("disable airi pet pass-through failed: {err}"))?;
    let _ = window.emit("contextlife://pet-pass-through", false);
    Ok(())
}

#[tauri::command]
fn set_airi_window_pass_through(window: Window, enabled: bool) -> Result<(), String> {
    let handle = airi_pet_monitor_handle(&window)?;
    handle.pass_through.store(enabled, Ordering::Relaxed);

    #[cfg(target_os = "macos")]
    {
        if let Ok(ns_win) = window.ns_window() {
            let ns_win = ns_win as id;
            unsafe {
                let _: () = msg_send![ns_win, setIgnoresMouseEvents: enabled];
            }
        }
        if let Some(panel) = window.app_handle().state::<real_panel::State>().get() {
            unsafe {
                let _: () = msg_send![panel, setIgnoresMouseEvents: enabled];
            }
        }
        let _ = window.emit("contextlife://pet-pass-through", enabled);
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        window
            .set_ignore_cursor_events(enabled)
            .map_err(|err| format!("set airi pass-through failed: {err}"))?;
        let _ = window.emit("contextlife://pet-pass-through", enabled);
        Ok(())
    }
}

/// Open a URL: localhost URLs open in a native Tauri window; external URLs
/// open in the system default browser (matching Swift's openNativeWindow).
#[tauri::command]
fn open_external_url(app: AppHandle, url: String) -> Result<(), String> {
    if url.is_empty() {
        return Ok(());
    }

    if url.contains("127.0.0.1") || url.contains("localhost") {
        // Open in a native Tauri window (like Swift's openNativeWindow)
        let count = NATIVE_WIN_COUNTER.fetch_add(1, Ordering::Relaxed);
        let label = format!("native-{count}");

        let parsed: Url = url
            .parse()
            .map_err(|err| format!("invalid url: {err}"))?;

        let win = WebviewWindowBuilder::new(&app, &label, WebviewUrl::External(parsed))
            .title("ContextLife")
            .inner_size(1280.0, 860.0)
            .resizable(true)
            .build()
            .map_err(|err| format!("open native window failed: {err}"))?;

        // When this window closes, reset the heartbeat and restore pet visibility
        let app_for_close = app.clone();
        win.on_window_event(move |event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                // Clone again for the inner thread
                let app_inner = app_for_close.clone();
                let _ = thread::spawn(move || {
                    // Fire-and-forget: reset heartbeat on Flask
                    let _ = std::net::TcpStream::connect("127.0.0.1:5001").and_then(|mut stream| {
                        use std::io::Write;
                        stream.write_all(
                            b"POST /api/pet/ui-active HTTP/1.1\r\nHost: 127.0.0.1:5001\r\nContent-Length: 5\r\nContent-Type: text/plain\r\n\r\nreset",
                        )
                    });

                    // Restore pet visibility
                    if let Some(pet_win) = app_inner.get_webview_window("airi-pet") {
                        #[cfg(target_os = "macos")]
                        {
                            if let Some(panel) =
                                app_inner.state::<real_panel::State>().get()
                            {
                                unsafe {
                                    let _: () = msg_send![panel, orderFrontRegardless];
                                }
                            }
                        }
                        let _ = pet_win.show();
                        app_inner
                            .state::<PetVisibilityState>()
                            .hidden
                            .store(false, Ordering::Relaxed);
                    }
                });
            }
        });

        // Activate the window
        #[cfg(target_os = "macos")]
        {
            use cocoa::appkit::{NSApp, NSApplication, NSApplicationActivationPolicy};
            // Temporarily switch to Regular policy so the window appears in the dock
            // and can receive focus properly
            unsafe {
                NSApp().setActivationPolicy_(
                    NSApplicationActivationPolicy::NSApplicationActivationPolicyRegular,
                );
                NSApp().activateIgnoringOtherApps_(true);
            }
        }

        log::info!("Opened native window: {label}");
    } else {
        // Open in system default browser
        #[cfg(target_os = "macos")]
        {
            let _ = std::process::Command::new("open").arg(&url).spawn();
        }
        #[cfg(target_os = "windows")]
        {
            let _ = std::process::Command::new("cmd")
                .args(["/c", "start", "", &url])
                .spawn();
        }
        #[cfg(target_os = "linux")]
        {
            let _ = std::process::Command::new("xdg-open").arg(&url).spawn();
        }
    }

    Ok(())
}

// ── Pet global hotkey: dynamic update ────────────────────────────────────
//
// Called from the main-window settings panel (via pet.html pulling the new
// value from /api/user-settings every 5s and re-invoking this command).
// Unregisters the previous shortcut (if any) and registers the new one, so
// the user doesn't have to restart Miru after changing the hotkey.

#[tauri::command]
fn update_global_hotkey(app: AppHandle, hotkey: String) -> Result<(), String> {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;

    let normalized = normalize_hotkey(&hotkey);
    let new_shortcut = normalized
        .parse::<tauri_plugin_global_shortcut::Shortcut>()
        .map_err(|e| format!("parse '{normalized}' failed: {e}"))?;

    // Unregister current shortcut first (if any). Skip the unregister when
    // the new one is identical — no-op avoids briefly having zero shortcuts
    // registered during the swap.
    let state = app.state::<HotkeyState>();
    let already_current = {
        let current = state.current.lock().unwrap();
        current.as_ref() == Some(&new_shortcut)
    };
    if already_current {
        log::info!("update_global_hotkey: already '{normalized}', no-op");
        return Ok(());
    }

    let gs = app.global_shortcut();
    if let Some(old) = state.current.lock().unwrap().take() {
        if let Err(e) = gs.unregister(old) {
            log::warn!("unregister old hotkey failed (non-fatal): {e}");
        }
    }

    let app_handle = app.clone();
    gs.on_shortcut(new_shortcut.clone(), move |_app, _shortcut, event| {
        if event.state() == tauri_plugin_global_shortcut::ShortcutState::Pressed {
            toggle_pet_visibility(&app_handle);
        }
    })
    .map_err(|e| format!("register '{normalized}' failed: {e}"))?;

    *state.current.lock().unwrap() = Some(new_shortcut);
    log::info!("Global hotkey updated to: {normalized}");
    Ok(())
}

// ── Pet visibility toggle (for global hotkey) ────────────────────────────

fn toggle_pet_visibility(app: &AppHandle) {
    let state = app.state::<PetVisibilityState>();
    if let Some(window) = app.get_webview_window("airi-pet") {
        let remembered_hidden = state.hidden.load(Ordering::Relaxed);
        let mut actual_visible = window.is_visible().unwrap_or(false);
        #[cfg(target_os = "macos")]
        {
            if let Some(panel) = app.state::<real_panel::State>().get() {
                unsafe {
                    let panel_visible: bool = msg_send![panel, isVisible];
                    actual_visible = panel_visible;
                }
            }
        }
        let actual_hidden = !actual_visible;
        let should_show = should_show_pet_for_toggle(remembered_hidden, actual_visible);
        eprintln!(
            "[pet-debug] toggle_pet_visibility, remembered_hidden={remembered_hidden}, actual_hidden={actual_hidden}"
        );

        if should_show {
            #[cfg(target_os = "macos")]
            {
                if let Some(panel) = app.state::<real_panel::State>().get() {
                    unsafe {
                        let _: () = msg_send![panel, orderFrontRegardless];
                    }
                }
            }
            let _ = window.show();
            let _ = window.unminimize();
            let _ = window.set_ignore_cursor_events(false);
            let _ = window.set_focus();
            state.hidden.store(false, Ordering::Relaxed);
            log::info!("pet: hotkey toggle -> visible");
        } else {
            #[cfg(target_os = "macos")]
            {
                if let Some(panel) = app.state::<real_panel::State>().get() {
                    unsafe {
                        let _: () = msg_send![panel, orderOut: cocoa::base::nil];
                    }
                }
            }
            let _ = window.hide();
            state.hidden.store(true, Ordering::Relaxed);
            log::info!("pet: hotkey toggle -> hidden");
        }
    } else {
        let pet_url = pet_url_with_cache_buster();
        if let Err(err) = open_airi_window(app.clone(), pet_url) {
            log::warn!("pet: hotkey restore failed to open pet window: {err}");
        } else {
            state.hidden.store(false, Ordering::Relaxed);
            log::info!("pet: hotkey restored missing window");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        physical_dimension_to_logical, scaled_pet_logical_size, should_show_pet_for_toggle,
        PET_DEFAULT_H, PET_DEFAULT_W,
    };

    #[test]
    fn pet_toggle_uses_actual_window_visibility_when_state_drifts() {
        assert!(!should_show_pet_for_toggle(false, true));
        assert!(should_show_pet_for_toggle(false, false));
        assert!(should_show_pet_for_toggle(true, true));
        assert!(should_show_pet_for_toggle(true, false));
    }

    #[test]
    fn pet_physical_size_converts_back_to_same_logical_size_across_dpi() {
        for scale in [1.0, 1.25, 1.5, 1.75, 2.0] {
            let physical_width = (PET_DEFAULT_W * scale).round() as u32;
            let physical_height = (PET_DEFAULT_H * scale).round() as u32;
            assert_eq!(
                physical_dimension_to_logical(physical_width, scale).round() as u32,
                PET_DEFAULT_W as u32
            );
            assert_eq!(
                physical_dimension_to_logical(physical_height, scale).round() as u32,
                PET_DEFAULT_H as u32
            );
        }
    }

    #[test]
    fn pet_scaling_operates_in_logical_pixels_across_dpi() {
        for scale in [1.0, 1.25, 1.5, 1.75, 2.0] {
            let physical_width = (PET_DEFAULT_W * scale).round() as u32;
            assert_eq!(scaled_pet_logical_size(physical_width, scale, 1.0), (420, 760));
            assert_eq!(scaled_pet_logical_size(physical_width, scale, 1.1), (462, 836));
        }
    }

    #[test]
    fn invalid_dpi_scale_falls_back_to_one() {
        assert_eq!(physical_dimension_to_logical(420, 0.0), 420.0);
        assert_eq!(physical_dimension_to_logical(420, f64::NAN), 420.0);
    }
}

// ── App entry point ──────────────────────────────────────────────────────

/// Absolute path to the pet's singleton lock file.
fn singleton_lock_path() -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        return PathBuf::from(home)
            .join("Library")
            .join("Application Support")
            .join("Miru")
            .join("data")
            .join(".pet.lock");
    }
    #[cfg(target_os = "windows")]
    {
        let root = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| {
            let home = std::env::var("USERPROFILE").unwrap_or_else(|_| ".".to_string());
            PathBuf::from(home)
                .join("AppData")
                .join("Local")
                .to_string_lossy()
                .into_owned()
        });
        return PathBuf::from(root)
            .join("Miru")
            .join("data")
            .join(".pet.lock");
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        PathBuf::from(home)
            .join(".local")
            .join("share")
            .join("Miru")
            .join("data")
            .join(".pet.lock")
    }
}

/// Enforce that only one miru-pet process is running. If a prior pet's PID
/// is recorded in the lock file AND that process is still alive, exit(0) —
/// the existing pet already owns the global hotkey, and running two would
/// cause duplicate windows on keypress.
///
/// Stale lock (process gone) → overwrite with our PID and continue.
/// No `Drop` cleanup is needed: the next pet launch will detect the stale
/// PID and overwrite it. This also survives `kill -9` crashes gracefully.
#[cfg(target_os = "macos")]
fn enforce_singleton_or_exit() {
    let lock_path = singleton_lock_path();

    if let Ok(content) = std::fs::read_to_string(&lock_path) {
        if let Ok(other_pid) = content.trim().parse::<i32>() {
            // kill(pid, 0) returns 0 if the process exists and the caller
            // has permission to signal it; returns -1 (ESRCH) if gone.
            let alive = unsafe { libc::kill(other_pid, 0) } == 0;
            if alive && other_pid != std::process::id() as i32 {
                eprintln!(
                    "[pet-singleton] Another miru-pet (PID {}) already running. Exiting.",
                    other_pid
                );
                std::process::exit(0);
            }
        }
    }

    // Take ownership of the lock.
    if let Some(parent) = lock_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let my_pid = std::process::id();
    if let Err(e) = std::fs::write(&lock_path, format!("{}", my_pid)) {
        eprintln!("[pet-singleton] Could not write lock file: {}", e);
    } else {
        eprintln!("[pet-singleton] Took ownership, PID {}", my_pid);
    }
}

#[cfg(target_os = "windows")]
fn enforce_singleton_or_exit() {
    use std::os::windows::process::CommandExt;

    let lock_path = singleton_lock_path();
    if let Ok(content) = std::fs::read_to_string(&lock_path) {
        if let Ok(other_pid) = content.trim().parse::<u32>() {
            let filter = format!("PID eq {other_pid}");
            let output = std::process::Command::new("tasklist")
                .args(["/FI", &filter, "/FO", "CSV", "/NH"])
                .creation_flags(0x08000000)
                .output();
            if let Ok(output) = output {
                let text = String::from_utf8_lossy(&output.stdout).to_lowercase();
                let pid_marker = format!("\",\"{other_pid}\",");
                let is_pet = text.contains("\"miru-pet.exe\"") || text.contains("\"app.exe\"");
                if other_pid != std::process::id() && is_pet && text.contains(&pid_marker) {
                    eprintln!(
                        "[pet-singleton] Another miru-pet (PID {}) already running. Exiting.",
                        other_pid
                    );
                    std::process::exit(0);
                }
            }
        }
    }

    if let Some(parent) = lock_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let my_pid = std::process::id();
    if let Err(error) = std::fs::write(&lock_path, format!("{my_pid}")) {
        eprintln!("[pet-singleton] Could not write lock file: {error}");
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn enforce_singleton_or_exit() {
    // Linux desktop packaging is outside the v1 release scope.
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Singleton guard FIRST — before any window/hotkey setup, before Tauri
    // even starts its event loop. Otherwise a second pet would briefly
    // register the global hotkey before discovering a peer and exiting,
    // and both pets would fire on a keypress in that window.
    enforce_singleton_or_exit();

    // Read configuration from environment (set by app.py launcher)
    let hotkey_raw = std::env::var("AIRI_PET_HOTKEY").unwrap_or_else(|_| {
        if cfg!(target_os = "macos") {
            "cmd+option+m".to_string()
        } else {
            "ctrl+alt+m".to_string()
        }
    });
    let hotkey_spec = normalize_hotkey(&hotkey_raw);

    let mut builder = tauri::Builder::default()
        .manage(AiriPetMonitorState::default())
        .manage(PetVisibilityState::default())
        .manage(HotkeyState::default());

    #[cfg(target_os = "macos")]
    {
        builder = builder.manage(real_panel::State::default());
    }

    builder
        .plugin(
            tauri_plugin_global_shortcut::Builder::new().build(),
        )
        .invoke_handler(tauri::generate_handler![
            open_airi_window,
            close_airi_window,
            hide_airi_window,
            get_airi_window_position,
            move_airi_window,
            start_airi_window_drag,
            update_airi_window_drag,
            end_airi_window_drag,
            scale_airi_window,
            snap_airi_window,
            resize_airi_window,
            elevate_airi_window,
            start_airi_pet_monitor,
            stop_airi_pet_monitor,
            set_airi_window_pass_through,
            open_external_url,
            update_global_hotkey,
        ])
        .setup(move |app| {
            // Logging
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // macOS: Accessory activation policy (no dock icon, pet floats independently)
            #[cfg(target_os = "macos")]
            {
                use cocoa::appkit::{NSApp, NSApplication, NSApplicationActivationPolicy};
                unsafe {
                    NSApp().setActivationPolicy_(
                        NSApplicationActivationPolicy::NSApplicationActivationPolicyAccessory,
                    );
                }

                // Prevent App Nap so WKWebView timers run at full speed
                // NSProcessInfo.processInfo.beginActivity(...)
                let pi_cls = objc::runtime::Class::get("NSProcessInfo").unwrap();
                unsafe {
                    let pi: id = msg_send![pi_cls, processInfo];
                    // NSActivityUserInitiated (0x00FFFFFF) | NSActivityLatencyCritical (0xFF00000000)
                    let opts: u64 = 0x00FFFFFF | 0xFF00000000;
                    let reason: id = msg_send![
                        objc::runtime::Class::get("NSString").unwrap(),
                        stringWithUTF8String: b"Desktop pet needs real-time polling\0".as_ptr()
                    ];
                    let _activity: id = msg_send![pi, beginActivityWithOptions: opts reason: reason];
                    // Intentionally leak — activity must stay alive for the process lifetime
                    let _: id = msg_send![_activity, retain];
                }
                log::info!("macOS: App Nap disabled");
            }

            // Register initial global hotkey + remember it in HotkeyState
            // so `update_global_hotkey` can unregister it cleanly later.
            {
                use tauri_plugin_global_shortcut::GlobalShortcutExt;
                match hotkey_spec.parse::<tauri_plugin_global_shortcut::Shortcut>() {
                    Ok(shortcut) => {
                        let app_handle = app.handle().clone();
                        let sc_clone = shortcut.clone();
                        if let Err(e) = app
                            .handle()
                            .global_shortcut()
                            .on_shortcut(shortcut, move |_app, _shortcut, event| {
                                if event.state() == tauri_plugin_global_shortcut::ShortcutState::Pressed {
                                    toggle_pet_visibility(&app_handle);
                                }
                            })
                        {
                            log::warn!("Failed to register hotkey '{hotkey_spec}': {e}");
                        } else {
                            log::info!("Global hotkey registered: {hotkey_spec}");
                            *app.state::<HotkeyState>().current.lock().unwrap() =
                                Some(sc_clone);
                        }
                    }
                    Err(e) => {
                        log::warn!("Failed to parse hotkey '{hotkey_spec}': {e}");
                    }
                }
            }

            // Auto-open the pet window after a short delay
            let handle = app.handle().clone();
            thread::spawn(move || {
                // Give the AIRI web frontend a moment to be fully ready
                thread::sleep(Duration::from_secs(3));
                let pet_url = pet_url_with_cache_buster();
                if let Err(err) = open_airi_window(handle.clone(), pet_url) {
                    log::warn!("auto-open airi pet window failed: {err}");
                }

            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
