#!/bin/bash
# Miru APK 测试脚本
# 用法: ./test_apk.sh
# 前提: 手机已通过 USB 连接并开启 USB 调试

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

APK_PATH="android/app/build/outputs/apk/debug/app-debug.apk"
PKG="com.miru.companion"
ACTIVITY="com.miru.companion.MainActivity"

info()  { echo -e "${GREEN}[test]${NC} $1"; }
warn()  { echo -e "${YELLOW}[test]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; }
pass()  { echo -e "${GREEN}[PASS]${NC} $1"; }

# Step 0: Check device connected
info "Checking ADB device..."
DEVICE=$(adb devices | grep -w device | head -1 | cut -f1)
if [ -z "$DEVICE" ]; then
    fail "No device connected. Connect phone via USB and enable USB debugging."
    exit 1
fi
MODEL=$(adb shell getprop ro.product.model 2>/dev/null | tr -d '\r')
ANDROID=$(adb shell getprop ro.build.version.release 2>/dev/null | tr -d '\r')
pass "Device: $MODEL (Android $ANDROID) [$DEVICE]"

# Step 1: Build
info "Building APK..."
cd "$(dirname "$0")"
npx cap sync android 2>&1 | grep -E "copy|update|Sync" || true
cd android
JAVA_HOME=/opt/homebrew/opt/openjdk@21 ANDROID_HOME=/opt/homebrew/share/android-commandlinetools ./gradlew assembleDebug 2>&1 | tail -3
cd ..

if [ ! -f "$APK_PATH" ]; then
    fail "APK not found at $APK_PATH"
    exit 1
fi
pass "APK built: $(ls -lh $APK_PATH | awk '{print $5}')"

# Step 2: Install (push + pm install to work with Vivo/OPPO security dialogs)
info "Pushing APK to device..."
adb push "$APK_PATH" /data/local/tmp/miru.apk 2>&1
info "Installing APK... (如果手机弹出安装确认，请点击「继续安装」)"
adb shell pm install -r /data/local/tmp/miru.apk 2>&1
INSTALLED=$(adb shell pm list packages | grep "$PKG" || true)
if [ -z "$INSTALLED" ]; then
    fail "APK installation failed"
    exit 1
fi
pass "APK installed: $PKG"

# Step 3: Launch
info "Launching app..."
adb logcat -c  # clear log buffer
adb shell am start -n "$PKG/$ACTIVITY" 2>&1
sleep 3

# Step 4: Check logs for errors
info "Checking logs..."
ERRORS=$(adb logcat -d | grep -E "Capacitor|$PKG" | grep -iE "error|exception|crash|fatal" | grep -v "triggerEvent" | head -10)
if [ -n "$ERRORS" ]; then
    warn "Errors found in logs:"
    echo "$ERRORS"
else
    pass "No critical errors in logs"
fi

# Step 5: Check if app navigated to VPS (means connection works)
NAVIGATED=$(adb logcat -d | grep -E "Capacitor/Console.*110\.40\.|http.*5001" | head -3)
if [ -n "$NAVIGATED" ]; then
    pass "App successfully navigated to VPS"
else
    warn "Could not confirm VPS navigation (may need manual QR scan)"
fi

# Step 6: Check WebView cleartext
CLEARTEXT_ERR=$(adb logcat -d | grep -iE "ERR_CLEARTEXT_NOT_PERMITTED|net::ERR_" | head -5)
if [ -n "$CLEARTEXT_ERR" ]; then
    fail "Network errors detected:"
    echo "$CLEARTEXT_ERR"
else
    pass "No network security errors"
fi

echo ""
info "=== Test complete ==="
info "For live log monitoring: adb logcat -s Capacitor:V chromium:V | grep -v triggerEvent"
