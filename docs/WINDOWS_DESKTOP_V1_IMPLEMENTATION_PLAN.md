# Miru Windows Desktop v1 实现、测试与发布计划

状态：Internal RC 已实现并完成 Windows 本地/远端/部署隔离验收；公开发布门槛仍未全部通过
日期：2026-08-25
源码仓库：Miru-Internal
开发分支：`codex/windows-desktop-v1`
已验证运行时血统：`internal-baseline-2026-07-23-base-url-resolution`

## 1. 文档目的

本文是 Miru Windows Desktop v1 的实施规格、测试计划和发布门槛。它用于约束 Codex、Claude Code 和人工验收，避免 Windows 适配过程中发生以下偏移：

- 从废弃 Windows 分支或公开仓库自动生成的旧 Source code 起步。
- 把 Windows 做成只会打开网页的功能缩水客户端。
- 为了适配 Windows 改坏 Mac、本地单设备、Android 或 Docker 已验证逻辑。
- 在多设备账号中把本机截屏、桌宠快捷键等设备设置写成服务器共享状态。
- 只验证 UI 能打开，却没有验证 AttentionEngine、Curator、记忆写入和账号切换生命周期。
- 打包时混入 API key、邀请码、用户数据、测试服务器地址或私有仓库凭据。

本文不是临时 TODO。实现阶段的每个提交、测试证据和 Release 都必须能映射到本文的验收项。

## 2. 已确认的产品决策

### 2.1 支持范围

- Windows v1 只支持当前测试环境代表的 Windows x64 桌面系统。
- 不支持 Windows ARM64。
- 不承诺 Linux 桌面客户端。
- 正式多设备验收范围是一台 Mac、一台 Windows 和一台 Android 手机连接同一个服务器账号。
- 当前设备注册和同名设备去重逻辑保持不变，不为多台同平台设备做专项重构。
- 更多设备不会被主动禁止，但不属于 Windows v1 的精细体验和验收范围。

### 2.2 功能范围

Windows 版不是瘦身版。必须完整支持：

- 本地单设备模式。
- 从 Windows 首次启动向导部署新的 Linux VPS Miru instance。
- 使用已有长邀请码连接现有服务器账号。
- Windows 桌宠、快捷键、拖动、缩放、显隐和消息呈现。
- Windows 自动截屏、变化检测、截图上传和状态诊断。
- 三层 API 配置、逐项真实测试和 `setup_complete` runtime gate。
- 初见问卷、欢迎语、聊天、SSE、记忆浏览、日记、DDL 和情绪页面。
- 当前主聊天 agent、AttentionEngine、Curator、Sleep Agent、ScreenAnalyzer、ScreenSemanticGate、ScreenSlotWriter、Pass 4、Persona Writer、晚间维护和已有阈值/筛选逻辑。

任何 Windows 平台差异只能出现在平台壳、系统 API、路径、权限提示、进程生命周期和打包层。不得以“Windows 首版”为理由关闭、替代或简化 AI、记忆和主动陪伴链路。

### 2.3 截屏行为

- 默认间隔 30 秒，允许范围继续保持 5 到 3600 秒。
- 截屏默认关闭，必须由用户主动开启。
- 跟随鼠标所在显示器截取单个显示器，与 Mac 行为一致。
- 不拼接所有显示器，不默认截焦点窗口。
- 关闭截屏后不再进行新的系统截屏和上传。
- Windows 首次开启时执行真实 capture probe。探针失败时显示 Windows 专属提示，并保持截屏关闭。

Windows 常规桌面截图 API 不等价于 macOS TCC Screen Recording 权限。实现和 UI 不得假设存在完全相同的授权开关。Windows 提示应基于真实能力检测，区分：

- 截图成功。
- API 调用失败。
- 桌面被锁定或处于安全桌面。
- 当前会话不可捕获。
- 图片尺寸非法或结果为空。
- 未知系统错误。

不得通过“画面较暗”判断截图无权限，因为纯黑壁纸或深色应用可能是合法画面。失败提示至少提供重试和打开诊断信息；只有系统存在可定位的相关设置入口时才显示“打开设置”。

### 2.4 生命周期

- 关闭主窗口等同完整退出 Windows Miru。
- 完整退出必须停止主窗口、Windows ScreenSensor、heartbeat、桌宠、本地 Flask 和本地用户后台线程。
- 远端服务器模式退出 Windows 客户端时，不停止 VPS instance、VPS Curator 或其他设备连接。
- 最小化主窗口时继续运行，不退出、不停止截屏。
- 不做无窗口、无状态指示的隐蔽后台常驻。
- Windows v1 不默认开机自启动。
- Windows v1 不做自动更新。

### 2.5 快捷键和签名

- Windows 桌宠默认快捷键固定为 `Ctrl+Alt+M`。
- Windows v1 暂不购买商业 Authenticode 代码签名。
- Mac 继续沿用当前 ad-hoc 签名方式，除非发布前另行决定。
- Release 和安装说明必须如实说明 Windows SmartScreen 或 macOS Gatekeeper 可能出现的首次启动提示。

### 2.6 发布策略

Windows v1 发布时统一重打并验证：

1. macOS DMG。
2. Android APK。
3. Windows x64 安装包。
4. Linux amd64 Miru Server Docker image archive。

四个包必须映射到同一个 Miru-Internal 源码提交和同一个发布清单。Windows v1 不维护旧客户端与新服务器的混合版本兼容承诺。验收必须使用同一轮新包。

“不兼容旧包”不等于可以静默删除用户数据。若数据 schema 发生变化，仍应提供前向迁移或明确阻断启动；只是无需保证旧 DMG/APK/Windows 客户端继续和新 Docker 协议互通。

## 3. 源码基线与 Git 管理

### 3.1 唯一事实源

- Miru-Internal 是开发、测试、构建和发布的唯一源码事实源。
- 公开 Miru 仓库只在最终开源和 Release 同步阶段使用。
- GitHub 自动生成的 Source code zip/tar.gz 不作为历史二进制的构建来源。
- 废弃 Windows 分支不得被 merge、cherry-pick 或作为架构模板使用。

Windows v1 分支从当前 Internal `origin/main` 创建。该提交相对已验证标签只包含维护文档更新，运行时代码仍与 2026-07-23 已验证基线一致。实现前应记录：

- base tag：`internal-baseline-2026-07-23-base-url-resolution`。
- tag commit：`865ee3869cb8700f6d1e5092eb971efeff5d2175`。
- runtime source commit：`922381c747bed084c020b4ce44151c4f6d39769b`。
- Windows 分支起点：`96660f0b9803857a58118ae1064d1c9d81cace7e`；它相对 tag 只增加共享文档，不包含运行时变更。
- 当前分支 commit。
- dirty diff hash。

### 3.2 分支与提交边界

建议按以下提交边界实施，保证可以独立审阅和回滚：

1. Windows 计划和共享维护文档。
2. 平台路径与 runtime adapter。
3. Windows launcher、WebView2 和 native bridge。
4. Windows 截屏与能力提示。
5. Windows Tauri 桌宠与进程生命周期。
6. Windows 首次启动、本地模式和账号切换。
7. Windows 自有服务器部署和已有账号登录。
8. Windows 打包、清状态和诊断脚本。
9. 跨平台回归和 Release 文档。

不要把大规模公共后端重构混进某个 Windows 平台提交。若确实发现共享后端 bug，使用独立提交说明影响面，并重新执行 Mac、Windows、Android 和 Docker 回归。

### 3.3 Mac 与 Windows 的开发职责

Mac 负责：

- 持有完整 Git 仓库、分支和 GitHub 凭据。
- 代码编辑、diff 审计、测试编排和 Release 清单。
- 将指定提交的干净源码同步到 Windows 专用构建目录。
- 拉回 Windows 日志、截图和安装包做审计。

Windows 负责：

- Windows 原生依赖安装。
- Windows launcher、WebView2、截图和 Tauri 真机运行。
- Windows x64 安装包构建。
- 本地模式、服务器模式和桌宠真机验收。

Windows 构建机不需要保存 Miru-Internal GitHub 凭据。源码同步必须排除 `.git`、`data/`、`logs/`、`dist/`、配置文件、密钥和本地缓存。

### 3.4 有线远控与重连

OpenSSH Server、自动启动服务和 Mac 公钥授权完成后都持久化在 Windows，本机断开网线、关机或重启不需要重新运行 bootstrap。为避免重新插线后地址漂移：

- Mac 和 Windows 的有线网卡使用固定私有 IPv4，或为该链路配置稳定 DHCP reservation。
- Mac 只通过 SSH config alias 连接；脚本读取 alias/环境变量，不保存真实 IP 和 Windows 用户名。
- `sshd` 保持自动启动，Windows Firewall 规则只放行有线私有网络 profile，不向公网开放管理端口。
- RustDesk 只承担可视化验收；访问密码/授权信息保留在各自客户端，不进入仓库或测试日志。

正常重连流程只是插线、等待网卡就绪、执行一次 SSH health check，再打开 RustDesk。只有 Windows 网络 profile 被重建、固定地址丢失或防火墙规则被系统重置时，才需要修复对应网络项，不需要重装 OpenSSH 或重新授权公钥。

## 4. 架构原则

### 4.1 平台适配而不是产品分叉

Windows 必须复用当前共享运行时：

- `app.py`
- `auth.py`
- `core.py`
- `prompt.py`
- `attention_engine.py`
- `curator.py`
- `sleep_agent.py`
- `screen_analyzer.py`
- `memory_router.py` 和完整 memory modules
- `storage.py`
- `sse.py`
- `client_provisioning.py`
- `templates/index.html`、`login.html`、`pet.html`
- `miru-mobile/` 与 Android 原生服务保持独立

Windows 新代码主要负责：

- Windows 路径解析。
- Windows WebView2 主窗口。
- 与 `window.MiruDesktop` 兼容的 native bridge。
- Windows 截屏和空闲检测。
- Windows 文件选择、外部链接和诊断入口。
- Windows 桌宠进程和窗口管理。
- Windows 退出、最小化、单实例和异常恢复。
- Windows 安装、卸载和清状态。

不要为了“统一”先重写 Mac launcher。先以最小公共接口承载 Windows，等 Windows 真机稳定后再判断是否值得抽共享 launcher 框架。

### 4.2 两种运行模式的所有权

| 能力 | Windows 本地单设备 | Windows 连接 VPS | VPS/Docker |
|---|---|---|---|
| 主 Web UI | Windows WebView2 | Windows WebView2 直连 VPS | 提供 `/app` |
| Chat agent | Windows 本机 Flask | 不在 Windows 运行 | VPS 运行 |
| 三层 API key | Windows 本地目录 | VPS owner config | VPS 持有 |
| AttentionEngine | Windows 当前本地用户 | 不在 Windows 运行 | VPS 活跃用户 |
| Curator | Windows 当前本地用户 | 不在 Windows 运行 | VPS 活跃用户 |
| Sleep/维护循环 | Windows 当前本地用户 | 不在 Windows 运行 | VPS 运行 |
| ScreenSensor | Windows 本机 | Windows 本机并上传 VPS | 不负责桌面截屏 |
| 桌宠 | Windows 本机 | Windows 本机 | 不运行桌宠 |
| SSE source of truth | Windows 本机 Flask | VPS | VPS |
| 数据 source of truth | `%LOCALAPPDATA%` | VPS volume | VPS volume |

远端模式的 Windows 进程只能拥有本机 sensor、heartbeat、WebView 和桌宠。它不得启动或停止 VPS 的 AttentionEngine、Curator、Sleep Agent、nightly maintenance 或其他服务器单例。

### 4.3 Native bridge 契约

Windows 应向前端提供与 Mac 同名的 `window.MiruDesktop` 高层契约，至少覆盖：

- `getLocalSettings()`
- `setLocalSettings(payload)`
- `openExternalUrl(url)`
- `checkScreenPermission()`，Windows 实现返回 capture capability，而不是伪造 macOS TCC。
- `notifyAppReady(payload)`
- 自有服务器镜像包文件选择所需能力。

返回结构必须包含平台标识和可诊断错误码。前端只能根据 capability 决定展示内容，不应通过 user agent 猜测权限状态。

## 5. Windows 文件与状态布局

Windows 持久化根目录使用：

```text
%LOCALAPPDATA%\Miru\
  config.json
  device.json
  data\
    _admin\
    users\<uid>\
  profiles\<account-key>\device_settings.json
  webview2\
  logs\
  diagnostics\
  runtime\
```

约束：

- 本地账号的聊天、记忆、API 配置和后台状态写入 `data/users/<uid>` 与本地 `_admin`。
- 远端账号的聊天和记忆只在 VPS；Windows 只保存登录连接信息和本机设备设置。
- `account-key` 由模式、server identity 和 user id 的非敏感稳定摘要组成，不能直接包含 token 或邀请码。
- `config.json` 不得被打进安装包。
- WebView2 profile 必须固定在 Miru 自己的目录，不得使用随机临时目录导致登录态丢失。
- 本地账号和远端账号切换时，不复用上一个账号的 WebView storage、sensor token 或设备设置内存态。

## 6. 多设备与设置隔离

### 6.1 设置分类

| 设置/数据 | 所有权 | 存储位置 | 是否跨设备同步 |
|---|---|---|---|
| timezone、个人信息 | 账号 | 本地账号目录或 VPS user settings | 服务器账号可共享 |
| chat、memory、journal、DDL | 账号 | 本地账号目录或 VPS volume | 服务器账号共享 |
| 三层 API key | runtime owner | 本地 `_admin` 或 VPS `_admin` | 不在客户端之间复制 |
| screenshot enabled | 账号 + 设备 | 本机 device settings | 否 |
| screenshot interval | 账号 + 设备 | 本机 device settings | 否 |
| capture guide/capability | 账号 + 设备 | 本机 device settings | 否 |
| pet hotkey | 账号 + 设备 | 本机 device settings | 否 |
| pet collapse/position/scale | 账号 + 设备 | 本机 device settings | 否 |
| main window geometry | Windows installation | Windows 本机 | 否 |
| auth token/server URL | 本机登录 profile | Windows 本机 | 否 |
| device id | Windows installation | `device.json` | 注册到服务器 |

### 6.2 不允许的行为

- Windows 修改快捷键后改变 Mac 快捷键。
- Windows 关闭截屏后关闭 Mac 或 Android 截屏。
- Android MediaProjection 状态覆盖 Windows 设置页开关。
- 远端账号的 VPS `user_settings.json` 被某台桌面客户端当成所有设备的截屏事实源。
- 切换账号后旧 sensor 继续使用旧 token 上传。
- 切换账号后旧 heartbeat 触发当前账号退出。
- 切换到远端账号后 Windows 本地 Curator 或 Attention 被 spawner 复活。
- 切回本地账号时重复启动两个 Curator、两个 Attention loop 或两个桌宠进程。

### 6.3 账号切换顺序

每次账号替换必须按以下顺序执行：

1. 捕获旧 session generation、mode、user id 和本机 profile。
2. 使旧 generation 失效并通知旧 deferred/heartbeat 线程退出。
3. 停止旧 Windows ScreenSensor。
4. 停止旧桌宠并清理 PID/lock。
5. 若旧账号是本地单设备，调用现有 user singleton cleanup 停 Curator、Attention、Sleep 等本机单例。
6. 保留旧账号数据目录。
7. 安装新 config 和新 generation。
8. 加载新账号对应的本机 device settings。
9. 完成 token/setup 验证后再启动新 sensor、桌宠和本地 runtime。

远端到本地、服务器先注册后本地、本地先注册后服务器、登录页退出再登录都必须走同一套会话边界。

## 7. Windows 截屏实现

### 7.1 捕获路径

首选沿用 Pillow 的 Windows capture 能力，并使用 Win32 API 获取：

- 当前鼠标位置。
- 鼠标所在 monitor。
- monitor 的物理矩形和 DPI scale。

传入对应 monitor bbox，只截该显示器。捕获后继续复用现有：

- 缩略图变化检测。
- 30% 像素变化阈值。
- 1920x1080 最大尺寸。
- JPEG quality 60。
- multipart `/api/device/screenshot` 上传。
- device id、captured_at 和 Authorization headers。

### 7.2 首次开启和失败提示

首次开启流程：

1. 用户点击开启。
2. native bridge 执行一次真实 capture probe。
3. 成功后写入当前账号 + 当前设备的 `screenshot_enabled=true`。
4. 失败则保持 false，显示 Windows 专属错误与重试按钮。
5. 成功后启动或唤醒 ScreenSensor，设置页和后台诊断必须同时显示已开启。

连续失败仍沿用受控 backoff，但 UI 必须可看到最近失败时间、错误摘要、最近成功尺寸和下一次重试状态。不得让设置页显示“已开启”而 sensor 实际处于永久失败状态。

### 7.3 Windows 特殊场景

必须测试：

- 单显示器。
- 双显示器且鼠标来回移动。
- DPI 100% 与高缩放。
- 锁屏、解锁。
- UAC secure desktop 前后。
- 睡眠、唤醒。
- RustDesk 连接与断开后。
- 主窗口最小化。
- 网络中断时不无限堆积截图。

受 DRM 保护内容、UAC 安全桌面或锁屏不可捕获属于系统限制，不应伪造成 Miru API key 或 VLM 错误。

## 8. Windows 主窗口与桌宠

### 8.1 主窗口

- 使用系统 WebView2 Runtime。
- 首次登录页由本机 Flask 提供。
- 远端 setup 完成后 WebView 直连目标 VPS `/app`。
- 本地模式 WebView 连接本机 Flask。
- WebView2 profile、缓存和 localStorage 使用 Miru 专属固定目录。
- 文件选择必须支持选择 Docker image archive。
- 外部 URL 必须交给 Windows 默认浏览器，不让 WebView 离开 Miru app。
- close event 执行完整退出；minimize event 只最小化。
- 主窗口只允许一个实例。重复启动应激活现有窗口。

### 8.2 桌宠

复用当前 Tauri + `/pet` + Live2D 资产和前端行为。Windows 必须补齐并验证：

- 透明、无边框、always-on-top、skip taskbar。
- 默认右下角位置。
- 拖动、缩放、吸附和跨显示器坐标。
- `Ctrl+Alt+M` 显隐。
- 快捷键运行时更新。
- 显隐状态与真实窗口可见性一致。
- 主窗口关闭后桌宠退出。
- 账号切换时旧桌宠退出，新桌宠只在 `/app` bootstrap 完成后出现。
- Windows pet singleton 和 stale lock 恢复。

桌宠不能在 API Key 页面、登录页或 onboarding 尚未完成时提前出现。

## 9. 首次启动和两种部署路径

### 9.1 本地单设备

必须完整复用当前 first-run gate：

1. 创建或复用 `local_single_device` 用户。
2. 新用户写 `setup_complete=false`。
3. 三层 API Key 保存并逐项测试。
4. 全部通过后才写 `setup_complete=true`。
5. 进入 onboarding。
6. `/app` bootstrap 完成后启动桌宠和本地 runtime。
7. Curator 在 fresh 用户首次 60 到 120 秒内产生 meta 运行证据。

本地模式必须运行与 Mac 本地模式完全相同的 server-side 背景服务和记忆链路。

### 9.2 从 Windows 部署 VPS

复用 `client_provisioning.py` 和 Host Manager，不重写服务器生命周期协议。Windows 适配负责：

- SSH 密码和默认 key 认证。
- Docker archive 文件选择。
- SFTP 上传进度。
- 远端 image inspect/load/pull。
- Host Manager 初始化或复用。
- instance 端口分配和创建。
- 分配端口前同时检查 Host Manager ledger 与服务器操作系统的真实 TCP 占用；自动范围跳过已占用端口，固定端口占用时明确失败。
- 复用已有 `host_home` 时沿用其原端口范围；不同范围必须使用新的隔离保存目录，避免两份配置误用同一 ledger。
- 从 Windows 本机访问公网 `/api/health`。
- 公网端口不可达时不显示“可以进入”。
- 创建成功后展示完整长邀请码并允许复制。
- 完成 API setup 后再启动 Windows sensor 和桌宠。

测试服务器只能通过本机 SSH alias 或环境变量引用。源码、文档和安装包不得写死任何真实公网地址。

### 9.3 连接已有服务器

- 输入长邀请码后按邀请码 server 段直连。
- 不走中心化 discovery。
- 保存 server URL、token、user id 和 setup 状态。
- Windows WebView、sensor、heartbeat 和桌宠使用同一 session generation。
- token 失效时停止本机 sensor/桌宠并回到登录页，不停止 VPS。

## 10. Windows 构建与安装

### 10.1 构建机依赖

通过官方来源安装并固定版本：

- Python 3.12 x64。
- Node.js LTS x64。
- Rust stable x86_64-pc-windows-msvc。
- Visual Studio Build Tools，Desktop development with C++。
- WebView2 Runtime。
- PyInstaller。
- Windows installer builder，首选 Inno Setup 或经过验证的等价方案。

Windows 当前已安装 WebView2，但其余开发工具链尚未就绪。安装脚本必须可重复执行，不得包含私有凭据。

### 10.2 包结构

首选 PyInstaller onedir 主程序 + Tauri pet sidecar，再由安装器包装。理由：

- 避免 PyInstaller onefile 每次启动解压和临时目录漂移。
- 更容易审计内嵌文件。
- 更容易定位 Defender 误报和缺失 DLL。
- 桌宠 sidecar 路径更稳定。

安装器必须：

- 安装到标准 per-user 或 Program Files 目录。
- 创建开始菜单入口。
- 不默认开机启动。
- 卸载时移除程序文件。
- 默认保留用户数据，除非用户明确选择删除。
- 不需要管理员权限即可运行 Miru；只有安装范围确实需要时才提升权限。

### 10.3 清状态脚本

新增 Windows 等价清理脚本，用于真实 fresh-user 验收。它必须：

- 停止 Miru 主进程和 pet 进程。
- 清理 `%LOCALAPPDATA%\Miru` 下的 config、data、WebView2、cache、logs 和 runtime locks。
- 清理 Miru 自己创建的临时目录。
- 验证没有残留 Miru 进程和登录态。
- 不删除源码、构建工具链或无关用户文件。
- 默认要求显式 `--yes` 或等价确认参数。

## 11. 自动化测试计划

### 11.1 现有共享测试

Windows 分支每次共享运行时代码变化后必须执行：

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
```

修改 `templates/*.html` 后必须执行 inline JavaScript 语法检查。修改 Tauri 后必须在 Mac 和 Windows 各跑 Rust tests。修改 Android 或共享前端后必须执行 APK 测试。

### 11.2 新增 Windows 单元测试

至少覆盖：

- `%LOCALAPPDATA%` 路径和 frozen bundle 资源定位。
- 固定 WebView2 profile 路径。
- `window.MiruDesktop` bridge 输入输出契约。
- Windows capture capability success/failure/error mapping。
- 鼠标所在 monitor 的 bbox 和 DPI 换算。
- `Ctrl+Alt+M` 默认值与运行时更新。
- close 与 minimize 的不同生命周期。
- 主程序和 pet 单实例。
- session generation 使旧 sensor/heartbeat/deferred setup 失效。
- 本地/远端模式 runtime ownership。
- 账号 + 设备 settings key 生成和隔离。
- Windows clean-state 脚本只删除 Miru 范围。
- 构建 manifest 中 source commit 和 artifact 元数据。

### 11.3 状态隔离组合测试

构造同一个服务器用户的 Mac、Windows、Android 三个设备，断言：

- Windows `screenshot_enabled=false` 不改变 Mac/Android。
- Windows interval 改为 45 秒不改变 Mac/Android。
- Windows hotkey 改动不改变 Mac。
- Android MediaProjection 开关不改变 Windows 设置页。
- Windows 切账号后旧 token 不再出现在 sensor request。
- Windows 远端模式不进入 `_get_background_user_ids()` 本地用户集合。
- Windows 本地模式只服务当前 active local runtime user。
- Windows 本地账号数据和 VPS 账号数据不存在 marker 串写。

## 12. Windows 真机验收

### 12.1 环境准备

- 使用专用 Windows x64 测试机。
- 通过有线 SSH 执行命令和收集日志。
- 通过 RustDesk 查看真实桌面和完成视觉交互。
- 不以 SSH session 的截图代替交互桌面截图。
- 使用 Miru 运行进程产生的 capture debug artifact 验证实际截屏内容。

### 12.2 Fresh local 模式

1. 清理 Windows Miru 状态。
2. 安装当前 Windows 安装包。
3. 首次启动选择本地模式。
4. 验证 API Key 页不能跳过。
5. 验证三层 API test、错误提示和成功持久化。
6. 验证 API setup 完成前没有 pet、sensor、Attention/Curator runtime。
7. 完成 onboarding 并进入 app。
8. 开启截屏，验证 Windows capability prompt 和真实截图。
9. 验证 ScreenObservationVLM、significance、SemanticGate 和 slot write。
10. 发送聊天，验证主 agent、SSE、chat memory、emotion 和 Attention signal。
11. 等待 Curator 首 tick，检查 `curator_meta.json`。
12. 验证 reminder/nightly/spawner 只服务当前本地用户。
13. 最小化后继续运行。
14. 关闭主窗口后所有 Windows Miru 进程退出。
15. 直接重开，恢复同一本地账号且不重复 API setup/onboarding。

### 12.3 服务器部署模式

1. 清理测试 VPS 上 Miru instance。
2. 从 Windows 首启向导选择 Docker archive。
3. 上传并创建一个新 instance。
4. 验证分配端口、公网 health、长邀请码和 restart persistence。
5. 完成三层 API setup。
6. 验证 Windows WebView 直连 VPS。
7. 验证 Windows 截图上传、VLM、Attention、memory 和 Curator 全部在 VPS 运行。
8. 关闭 Windows 主窗口，确认 VPS runtime 继续运行。
9. 重开 Windows，确认同一服务器账号恢复。

### 12.4 账号切换矩阵

必须逐项测试：

| 顺序 | 必须验证 |
|---|---|
| 本地 -> 服务器 | 本地 sensor/Curator/Attention/Sleep/pet 停；本地数据保留；VPS 启动 |
| 服务器 -> 本地 | 只停 Windows 远端 sensor/heartbeat/pet；VPS 保持；本地 runtime 唯一启动 |
| 本地 -> 登录页 -> 本地 | 旧线程停止；重进只有一组新线程；数据恢复 |
| 服务器先注册 -> 本地 -> 服务器 | 两套配置和 device settings 不串写 |
| 本地先注册 -> 服务器 -> 本地 | 不重复 API setup/onboarding；Curator 能重新启动 |
| 切换后完整退出再重开 | 恢复最后选择账号；无 stale heartbeat 踢登录 |

## 13. 三设备服务器验收

使用本轮新构建的 DMG、APK、Windows 安装包和 Docker archive：

1. 测试 VPS 部署一个干净 Miru instance。
2. Mac、Windows、Android 使用同一个长邀请码登录。
3. 三台设备分别发送聊天，其他设备实时收到 SSE。
4. Windows 与 Mac 分别上传桌面截图，Android 上传 MediaProjection 截图。
5. 服务端记录三个 device id 和正确平台。
6. 截图日志、emotion、Attention 和 memory 保留来源设备。
7. Windows 修改本机截屏、间隔、快捷键，不影响另外两台设备。
8. Windows 退出后 Mac、Android 和 VPS Curator 继续工作。
9. Mac 退出后 Windows、Android 和 VPS Curator 继续工作。
10. Android 前后台切换后 SSE/原生连接恢复。
11. VPS restart 后三台设备恢复连接，邀请码和账号数据不变。
12. 观察期内主动消息不出现单 intent 双投递回归，纯 chat 信号不额外制造相似主动消息。

不要求模拟两台 Windows 或两台 Mac 的同名设备展示优化。

## 14. 跨平台回归门槛

Windows 验收通过后仍必须重新验证：

### 14.1 Mac DMG

- 全新安装和本地模式。
- 服务器部署和已有邀请码登录。
- API setup runtime gate。
- 截屏权限引导和鼠标所在屏幕。
- 桌宠快捷键与完整退出。
- 本地/服务器来回切换。
- Curator、Attention、Memory 和 SSE。

### 14.2 Android APK

- 安装、长邀请码登录和凭据恢复。
- 原生 SSE service。
- MediaProjection 开关、默认 30 秒和截图上传。
- 前后台切换和网络恢复。
- 通知和消息同步。

### 14.3 Docker

- linux/amd64 build。
- clean data volume 初始化。
- long invitation login。
- chat、SSE、截图、VLM、Attention、SemanticGate、memory、Curator。
- restart persistence。
- Host Manager create/status/backup/reset/update/delete。
- 从 Windows 和 Mac 两条 provisioning 路径部署。

## 15. Release 与开源门槛

### 15.1 同源构建

Release manifest 必须记录：

- Internal source commit。
- base tag。
- build timestamp 和构建平台。
- DMG、APK、Windows installer、Docker archive 文件名、大小和 SHA-256。
- Docker config digest 和架构。
- 各平台验收状态。
- 是否完成真实 API、真实截图和真实 VPS 测试。

公开 Release 不额外上传独立 `SHA256SUMS` 文件；校验值保存在 Internal release manifest 和 GitHub asset digest 中。所有上传资产必须重新下载并逐字节比对。

### 15.2 隐私和秘密扫描

源码、构建目录和四个发布包必须扫描：

- API keys。
- SSH private keys。
- auth tokens。
- 完整邀请码。
- 真实用户聊天、截图、记忆和日志。
- 测试/生产服务器公网地址。
- 私有仓库 remote credential。
- macOS Keychain、Windows Credential Manager 或 Android preferences 导出。
- `.env`、`config.json`、`user_settings.json`、WebView profile 和临时上传文件。

代码中服务器目标使用环境变量、参数或本机 SSH alias。示例只能使用明确的 placeholder 或文档保留地址。

### 15.3 完整开源审计

公开完整源码前必须单独完成：

- Python、JavaScript、Rust、Java/C++ 依赖许可证清单。
- Live2D SDK、模型、贴图和角色资产的再分发权限确认。
- 字体、图片、声音、宣传素材和第三方图标版权确认。
- 私有运营脚本、服务器凭据和内部发布记录的边界审计。
- README 中 Windows 构建、Mac 构建、Android 构建和 Docker 部署说明。
- 从公开源码在一台无 Internal 权限的环境中复现构建。
- 公开仓库 secret scan 和 history scan。

“安装包能运行”不等于“完整源码可以合法开源”。许可证和资产再分发审计是独立 Release gate。

## 16. 风险清单与防护

| 风险 | 防护 |
|---|---|
| 误用旧 Windows 代码 | 分支固定在 Internal 已验证血统；禁止 old branch merge/cherry-pick |
| Windows 适配阉割后台能力 | 本地 runtime ownership 表和真实 Curator/Attention/Memory 验收 |
| 远端 per-device 设置互相覆盖 | 账号 + 设备本地 settings；禁止写 VPS shared fields |
| 设置显示开启但实际无法截屏 | 首次 capability probe、运行状态和最近错误统一展示 |
| 账号切换后旧线程复活 | session generation、cancel event、统一 teardown 顺序 |
| 主窗口退出但 pet/sensor 残留 | 主进程 child registry、Windows process-tree shutdown、进程验收 |
| WebView2 登录态丢失或串账号 | 固定 profile、账号切换清 user-scoped storage、profile key 隔离 |
| Windows 打包触发 Defender/SmartScreen | onedir、官方工具链、无壳压缩、真实下载/安装测试、如实提示未签名 |
| Windows 修改共享逻辑导致 Mac/Docker 回归 | 独立共享提交和四平台回归门槛 |
| 打包混入隐私数据 | 干净源码同步、构建白名单、source/bundle secret scan |
| 四个包版本不一致 | 单一 source commit、统一 manifest、回下载 byte compare |
| 完整开源触发资产许可问题 | 单独许可证和再分发审计 gate |

## 17. 分阶段实施顺序

### Phase 0：基线和工具链

- 固定 Internal source lineage 和 manifest。
- 安装并记录 Windows 官方工具链。
- 建立 Mac -> Windows 源码同步、命令执行、日志和截图回传。
- 建立 Windows clean-state 脚本骨架。

### Phase 1：Windows 主壳

- Windows launcher、WebView2、路径和固定 profile。
- native bridge。
- 本地 Flask 启停。
- close/minimize/singleton。
- 先跑 login/app 静态 smoke，不启动 AI 后台。

### Phase 2：Windows 截屏

- Win32 monitor selection。
- capture probe 和 Windows 专属提示。
- ScreenSensor、开关、间隔、诊断。
- 本地和远端上传 smoke。

### Phase 3：Windows 桌宠

- Tauri Windows build。
- 透明窗口、位置、拖缩、快捷键、singleton。
- app-ready gate、账号切换和完整退出。

### Phase 4：完整本地模式

- local account、三层 API gate、onboarding。
- 完整 chat、Attention、Curator、Sleep、memory、journal、DDL。
- fresh/relaunch/switch lifecycle。

### Phase 5：服务器模式

- Windows VPS provisioning。
- 现有邀请码登录。
- 远端 WebView、sensor、heartbeat、pet。
- 公网 health 和错误处理。

### Phase 6：三设备和隔离

- Mac + Windows + Android + test VPS。
- 设置隔离、SSE、截图来源和 runtime ownership。
- 网络、睡眠、重启和切换矩阵。

### Phase 7：打包和发布候选

- Windows installer。
- 重打 DMG、APK、Docker。
- 四平台真机回归。
- secret/license/source audit。
- Internal manifest 和 Claude Code review。

### Phase 8：公开发布

- 将经过验收的完整源码同步到公开仓库。
- 发布四个同源安装包。
- 回下载 byte compare。
- 检查公开 README、构建说明和 Release 页面。

## 18. 完成定义

Windows Desktop v1 只有同时满足以下条件才算完成：

- Windows 本地模式完整运行当前 Miru 全部后端链路，无功能阉割。
- Windows 能从 UI 部署 VPS，也能登录已有服务器。
- Windows 桌宠和截屏达到本文生命周期与权限提示要求。
- 本地、服务器、登录页来回切换不存在 stale runtime 或数据串写。
- 一台 Mac、一台 Windows、一台 Android 同账号真实验收通过。
- Mac DMG、Android APK、Windows installer、Docker archive 来自同一提交并全部重打。
- 四个平台的真实安装、聊天、截图、Attention、Curator、Memory、SSE 和 restart 证据完整。
- 发布包与源码均无秘密和用户数据。
- 完整开源许可证与资产审计通过。
- Claude Code 基于本文件和共享维护上下文完成独立复查，阻断问题全部关闭。

## 19. 2026-08-25 Internal RC 实施与验证记录

### 19.1 已完成实现

- 新增 `windows_launcher.py`，以 pywebview + WebView2 承载主窗口；本机 Flask、ScreenSensor、桌宠和 client session 由同一 launcher 生命周期管理。
- 新增 `desktop_paths.py` 和 `windows/platform.py`：Windows 状态固定在 `%LOCALAPPDATA%\Miru`，截图使用 Win32 枚举显示器并选择鼠标所在显示器，进程 DPI awareness 在创建 UI 前设置。
- 复用现有 Tauri 桌宠，Windows 默认快捷键改为 `Ctrl+Alt+M`，增加 Windows PID/lock 校验；关闭主窗口停止主壳、本机 runtime、sensor 和桌宠，最小化不停止。
- 本地模式完整复用现有 Flask/AttentionEngine/Curator/Sleep/Memory/Screen Analyzer 链路；远端模式只保留 Windows 拥有的 WebView、sensor、heartbeat 和桌宠，VPS 继续拥有 AI/background loops。
- 首次开启截屏先执行真实 capture probe；诊断端点增加显示器名称、尺寸和最近错误。切换账号或重新开启截屏时清空上一会话的变化检测缩略图，保证新账号第一帧不会因画面相似而被抑制。
- 自有服务器向导继续复用 `client_provisioning.py`、Host Instance Manager 和现有 Docker archive 协议，仅把 Mac 专属提示改成平台中性措辞。
- Host Manager 自动分配现在会跳过 ledger 外的真实系统端口占用，并把跳过端口返回给向导；例如 5001 被现有服务占用时，5001-5002 范围会选中 5002 并显示原因。高级设置固定 5002 时使用 5002-5002；如果该端口也被占用则创建失败。已有保存目录的端口范围不会被静默覆盖。
- 新增 PyInstaller onedir、Inno Setup per-user 安装器、Windows clean-state 脚本和真机 UI/生命周期探针。
- `user_settings.py` 的共享文件写入增加 per-path `RLock` 和唯一临时文件，修复 sensor、UI 与 heartbeat 并发更新时固定 `.tmp` 互相覆盖的问题；数据 schema 和 server 选人/AI 逻辑未改变。

### 19.2 Windows 真机已通过

- Windows 11 x64 干净状态安装、首次本地账号、三层真实 API test、onboarding、欢迎语、单次聊天/单次回复、SSE、截图上传、Attention、Screen Memory 和 Curator。
- Mac 工作树与 Windows `C:\MiruDev` 的 36 个本轮运行/测试改动文件逐文件 SHA-256 比对为 36/36 一致；最终 installer 与 Windows `dist` / 已安装主程序哈希也一致。
- 截图实际命中鼠标所在的 `DISPLAY1`，尺寸为 2560x1600；默认关闭、用户开启、30 秒默认间隔、设置页与后端状态一致。
- 最小化继续运行；`Ctrl+Alt+M` 显隐桌宠；关闭主窗口后 Miru、桌宠、本机 Flask 和 sensor 全部退出；重开恢复同一本地账号；重复启动只激活现有实例。
- 本地 -> 登录页 -> 本地、先本地后服务器、服务器 -> 本地均完成。切走本地账号后持续观察超过一个 Curator tick，本地 Attention/Curator/截图文件不再变化；切回后使用原数据并只启动一组 runtime。
- 已有服务器模式通过隔离 server 验收：邀请码登录、onboarding、SSE、截图、聊天、Curator、关闭/重开和切换均通过。
- 从 Windows 部署服务器通过真实 Windows SSH + Docker-in-Docker 隔离 host 验收：上传 archive、`docker load`、Host Manager 建实例、外部 health gate、邀请码、API setup、截图、聊天、Attention、Curator、重启和账号切换均通过。
- 修复远端 `/app` 在 pywebview bridge 注入前完成 bootstrap 时桌宠不出现的竞争：launcher 会在拥有窗口的一侧确认 `/app` bootstrap 完成，再幂等启动桌宠。最终安装包已重新构建、覆盖安装并通过真实启动。

### 19.3 自动化与跨平台回归

- macOS/Python 全量：`838 passed, 4 deselected, 5 xfailed, 71 warnings`。
- Windows/Python 全量：`820 passed, 18 skipped, 4 deselected, 5 xfailed, 71 warnings`。
- Rust：Windows `cargo test` / release build 通过；macOS `cargo test` / release build 通过。
- Android：`npx cap sync android`、`assembleDebug`、`testDebugUnitTest` 通过；生产依赖 `npm audit --omit=dev` 为 0。当前没有连接 Android 真机，未在本轮安装此 APK。
- Android `lintDebug` 仍有 22 个 error / 58 个 warning，首个错误位于本轮未修改的既有 `MainActivity.java` minSdk API 检查；作为独立发布债保留，未在 Windows PR 中顺带改动。
- macOS DMG 重打、ad-hoc `codesign --deep --strict` 通过、覆盖 `/Applications/Miru.app` 后主进程/桌宠/本地 health/Curator 启动通过。新 ad-hoc 二进制需要用户在系统设置里重新授予屏幕录制权限；系统弹窗和后端诊断均正确显示该状态。
- Linux amd64 Docker 从当前源码重建。无 API smoke 和真实三层 API smoke 均通过 long invitation、chat、SSE、截图 VLM、Attention、Screen Memory、Curator 和 restart persistence。Docker context 进一步排除维护文档、Agent 指南、桌面构建文件、真实 API 验收脚本和所有嵌套 `__pycache__`；重建后运行白名单与私密模式扫描通过，再次完成两轮 smoke。
- 前端 inline JavaScript 语法检查、`git diff --check` 和源码/安装包凭据扫描均通过。扩展路径扫描发现 macOS Tauri 二进制、Mac 工具字节码与 Android debug native library 含构建机源码路径；没有凭据或用户数据，但正式公开包仍需通过 path remap / clean build 消除，因此不能把当前 RC 标记为完整隐私审计通过。

### 19.4 本轮候选产物

| 产物 | 大小 | SHA-256 |
|---|---:|---|
| `Miru-Windows-x64-Setup.exe` | 41,676,077 | `63c5db9ebccb541384905567071148a318f5390f3a2160ee336b2ec40b9c11b5` |
| `Miru-0.2.0.dmg` | 49,603,982 | `c55136127b0eba3d7d06d3eda9b33f4d101ad92d1caf286930da521e5523c6a0` |
| `app-debug.apk` | 11,275,901 | `2741bfdb6562bf7def6d75bfcea50b26a2a903149b4ea441f0ff955989d79f4d` |
| `miru-server-windows-v1-rc-linux-amd64.tar.gz` | 95,658,431 | `1cc1d58024ae1402270f59c9d16b4cf773184b42fbbb74c65042655aca87e2f7` |

Docker image 为 `linux/amd64`，image id 为 `sha256:6d2adcad400957dcaf396d454c81bbd0ff26a5b71c4e264d59691c48ee0cc0a7`，config digest 为 `sha256:38c9ad04e7a7d08b2874f21b5fbdd6fce7409c9a72a508f22d1f3565e269d6bc`。运行代码与测试已冻结在 Internal 分支 commit `dd8e0898a74350948b56934dfe777156070ce2c9`。这些仍只是本地 Internal RC 证据，不等于已上传 Release；在完成最终发布 gate、从最终提交重打并回下载逐字节比对前，不得作为公开发布完成状态。

### 19.5 尚未通过的发布门槛

- 本机安全配置中指定的测试 VPS 在本轮测试窗口内 SSH banner 超时，因此没有对它写入任何内容；服务器部署验收使用隔离 Docker-in-Docker host，不得表述为真实公网 VPS 已通过。生产 VPS 从未连接或修改。
- 当前没有 Android ADB 设备，尚未完成同一 VPS 上 Mac + Windows + Android 三设备的本轮新包联合验收。
- 未完成 Windows 睡眠/唤醒、锁屏/UAC secure desktop、双显示器拔插和 SmartScreen 下载路径的全部人工矩阵。
- 依赖许可证与 Live2D/模型/字体/媒体资产再分发审计、无 Internal 权限环境的完整公开源码复现仍未完成。
- Mac/Android RC 二进制仍可检出非敏感的本机构建路径；公开发布前必须使用可复现的 path remap / clean build 消除并重新扫描。
- Claude Code 只读复查已按项目脚本发起，但本机 Claude OAuth 返回 401 expired token，未进入代码阅读或测试阶段；重新认证后必须重跑，不能把本次调用记为 review 通过。
- 运行代码已提交到本地 `codex/windows-desktop-v1`，验收文档将单独提交；分支尚未 push，未更新 Internal 或公开 Release。

## 20. 2026-08-25 本地模式对齐修正

本轮针对 RC 真机审查发现的 Windows 差异做了窄范围修正：桌宠尺寸/吸附统一使用逻辑像素，日记详情返回时保留列表原始父页面，本地单设备 heartbeat 可恢复被删除的当前桌面记录，客户端登录隐藏底层网络异常，并补齐打包品牌资源与 favicon 路由。服务器邀请码校验、AI/background loops、数据格式和部署协议均未改变。

Windows 11 x64、2560x1600、150% DPI 真机重新构建并覆盖安装后，验证桌宠单模型/单气泡/单输入框、工具栏展开、`Ctrl+Alt+M`、最小化、关闭完全退出、重开恢复本地账号、日记两级返回、设备删除后同 ID 自恢复、友好邀请码错误和打包资源。完整证据及本轮产物摘要见 `docs/agent_memory/2026-08-25-windows-local-parity-corrections.md`。

随后在 fresh Windows 本地账号中发现一次 transient capture 失败被旧文案误报为“权限”问题，且 probe 成功后弹窗立即消失、缺少可见反馈。修正后恢复弹窗强制使用 Windows capability-probe 路径，明确 Windows 不需要单独录屏授权，probe 成功先显示确认再关闭；ScreenSensor 重新启用会完整清理 backoff 状态并记录具体捕获错误。真实安装 WebView 已验证按钮、原生截图探测和恢复状态。

自动化基线更新为：macOS Python `848 passed, 4 deselected, 5 xfailed`；Windows Python `828 passed, 18 skipped, 4 deselected, 5 xfailed`，本轮 Windows 截屏恢复 targeted tests `20 passed`；两端 Rust test/release build、Android sync/build/unit、Docker linux/amd64 无 API 与真实三层 API smoke、Mac DMG 构建和秘密扫描均通过。Android 真机、三设备同 VPS、双屏/睡眠/UAC/SmartScreen 仍是发布门槛，不能描述为已完成。

## 21. 2026-08-26 双桌面最终候选

最终候选冻结在 `2bac7ec34e6c847d7659431508a355180b5cc5f9`。`cdbff52` 先保证 Windows 在登录和账号切换重定向中保留 `desktop=1&desktop_platform=windows`；最终自审又发现 Mac launcher 没有显式注入平台且共享前端 fallback 曾指向 Windows，`2bac7ec` 以一行 launcher 注入、一处 fallback 和定向回归测试修正。Mac 和 Windows 已分别完成本地模式、发起服务器部署、加入另一端发起的服务器、双向聊天/SSE、Windows 截图、设备设置隔离、账号切换、关闭/重开和桌宠生命周期验收。最终 DMG、Windows installer 与 exact final Docker image 还完成了同账号三层真实模型、四轮单问单答去重、Windows -> Mac 实时 SSE、Windows 截图/VLM、Mac remote -> local 后服务器继续运行及本地 Curator/Attention 重启验收。

隔离 SSH/Docker host 上分别验证了高级设置固定 5002，以及 5001 被无关监听占用时自动跳过并选择 5002；两个实例均通过外部 health gate，测试后已清理。生产 Project Page 和 VPS 没有写操作。最终四包来自同一提交并已收拢到 `artifacts/final-candidate-2bac7ec/`，完整自动化、真机证据、SHA-256 与未完成门槛见 `docs/agent_memory/2026-08-26-windows-multidevice-final-candidate.md`。由于 ad-hoc DMG 每次重打可能改变 code hash，安装最终包后必须对该 exact binary 重新授予一次 macOS 屏幕录制权限。Android 真机、公开 VPS、SmartScreen/睡眠/UAC/双屏拔插、许可证与 clean-room build 仍未验收，因此本节只表示 Internal 候选完成，不表示可以公开发布。
