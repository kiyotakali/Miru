# Building Miru

Miru has four release targets: macOS arm64, Windows x64, Android arm64, and a
Linux amd64 Docker image. The current public source baseline is Python 3.12,
Rust/Tauri 2, Node.js, and Java 21 for Android.

## Third-party Live2D files

The public source archive does not redistribute Live2D Cubism Core, the Cubism
Native Framework, or the Hiyori sample model. Obtain matching files from
Live2D under the applicable Live2D terms and place them at:

```text
assets/js/live2d/live2d.min.js
assets/js/live2d/CubismSdkForWeb-5-r.3/Core/live2dcubismcore.min.js
assets/live2d/Hiyori/
miru-mobile/android/app/src/main/cpp/CubismCore/
miru-mobile/android/app/src/main/cpp/Framework/
```

The Android Core directory must contain the arm64-v8a static library and
headers expected by `miru-mobile/android/app/src/main/cpp/CMakeLists.txt`.

## Backend and tests

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
```

Copy `.env.example` to `.env` only for direct development. Never commit model
API keys, SSH private keys, invitation codes, or production server addresses.

## macOS arm64

Prerequisites: macOS, Xcode Command Line Tools, Rust, Node.js, Python 3.12, and
the Live2D files above.

```bash
npm ci
(cd src-tauri && cargo test && cargo build --release)
bash build_mac.sh
```

The DMG is created under `dist/`. The public build is ad-hoc signed and is not
Apple-notarized.

## Windows x64

Prerequisites: Windows x64, Python 3.12, Rust, Node.js, Inno Setup 6, and the
desktop Live2D files above.

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

The installer is created under `dist\windows-installer\`. The public build is
not Authenticode-signed.

## Ubuntu desktop preview (X11)

See [UBUNTU_DESKTOP.md](UBUNTU_DESKTOP.md) for the source-only Ubuntu 22.04
desktop path. It uses GTK for the main window and PySide6/Qt WebEngine for the
transparent pet; it does not build a Tauri Linux executable.

## Android arm64

Prerequisites: Node.js, Java 21, Android SDK, NDK 27.3.13750724, Python 3.12,
and all Live2D files above.

```bash
cd miru-mobile
npm ci
npx cap sync android
cd android
JAVA_HOME=/path/to/jdk-21 ANDROID_HOME=/path/to/android-sdk ./gradlew assembleDebug
```

The current public APK is a debug-signed arm64 build. Production distribution
should use a private release keystore and must not commit that keystore.

## Linux amd64 server image

```bash
bash deploy/docker_build.sh
bash deploy/docker_export_image_tar.sh
```

The server image does not need the desktop or Android Live2D files. Its runtime
data must be mounted outside the container as documented in `deploy/README.md`.
