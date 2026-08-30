# Miru Android 本地模式、三端多设备与手机自部署计划

状态：Internal 实现与三端候选验收完成；公开发布门槛仍未关闭
日期：2026-08-28
源码仓库：Miru-Internal
开发分支：`codex/android-local-multidevice`
当前运行时基线：`ff7d63a8ef0f81daf2233a2e7fe1f2602d934606`

## 1. 目标与边界

最终 Android APK 必须提供三个清楚分开的入口：

1. **只在这部手机使用**：在 Android 设备内运行完整 Miru 本地后端，数据、三层 API 配置、聊天、记忆、AttentionEngine、Curator、Sleep、日记和截图链路都只属于这部手机。
2. **连接已有 Miru**：输入长邀请码，连接 Mac、Windows 或其他设备已经创建的私有服务器账号。
3. **创建多设备 Miru**：由手机填写 Linux 服务器 SSH 信息并部署 Miru Server，完成公网 health gate 后生成长邀请码，随后 Mac 和 Windows 加入同一账号。

这三个入口不能互相冒充：

- “手机本地模式”不能实际把数据放在远端服务器。
- “连接已有 Miru”不能在手机重复运行服务器拥有的 Attention、Curator 或 Memory Writer。
- “手机创建多设备 Miru”只负责部署和连接服务器，不把服务器后台循环搬到手机。

服务器和 Docker 的现有单实例运行逻辑优先保持不变。Android 平台差异集中在 APK 壳、嵌入式本地运行时、系统权限、设备设置、SSH 部署控制面和生命周期。

## 2. 已确认基线

- 8 月 27 日实施前基线是 `2bac7ec` 和仅远端 WebView 的 Android APK；该历史基线已经被本计划的实现取代。
- 当前四包候选来自同一运行时提交 `ff7d63a`。Vivo `V2415A`、Android 16、`arm64-v8a` 真机已完成干净本地账号、远端连接、手机 SSH 部署、三端加入和生命周期验收。
- 最终 APK SHA-256 为 `626a8b8a...d4e623`；设备内 `base.apk` 已反向拉取并确认逐字节一致。APK 目前使用 debug signing identity，只能作为 Internal 候选。
- Android 入口已经提供真实 app-private 本地完整后端、连接已有长邀请码和 SSH 创建多设备 Miru。Chaquopy runtime、原生 SSE/通知、MediaProjection、profile store 和服务器部署控制面都在同一 APK 中。
- Android 单元测试已替换掉 Capacitor 示例断言，并覆盖 manifest/service 边界、embedded shell、profile isolation、activity recreation live launch URL 和截图重复重试保护；自动化通过仍不能替代真机验收，本轮两类证据均已完成。

## 3. 不可破坏的 ownership

| 场景 | Android 拥有 | 服务器拥有 |
|---|---|---|
| 手机本地模式 | 本地 Flask、用户数据、API 配置、聊天、Memory、Attention、Curator、Sleep、reminder、journal、截图采集与分析 | 无 |
| 手机连接服务器 | WebView、设备 ID、本机截图开关/间隔、MediaProjection、SSE/通知、Live2D 壁纸 | 用户数据、API 配置、聊天、Memory、Attention、Curator、Sleep、reminder、journal |
| 手机发起服务器部署 | SSH job、镜像包选择/上传、外部 health 检查、连接态 | 部署成功后拥有全部账号后台和数据 |

切换行为必须满足：

- 手机本地 -> 服务器：停止手机本地 Attention/Curator/Sleep/Flask 业务会话和本地截图目标，保留本地数据；服务器不受影响。
- 服务器 -> 手机本地：关闭旧远端 SSE、heartbeat 和截图 token，启动一组手机本地 runtime 并读取原本地数据；不得停止服务器后台。
- 服务器 A -> 服务器 B：立即关闭 A 的 SSE、截图和 token，B 使用独立设备设置；A、B 数据不串。
- 删除/切换账号：SSH 密码不落盘，旧 token 不继续上传，旧后台线程不能被 spawner 或 stale callback 复活。
- 手机被强停：Android 系统停止 APK 内所有本地线程；远端服务器继续运行。再次打开时按已保存的 active profile 恢复，并要求重新进行系统规定的 MediaProjection 同意。

## 4. 目标架构

### 4.1 Android 本地控制面

APK 增加一个仅绑定 `127.0.0.1` 的 Android client runtime。WebView 首先访问本机入口页，再根据 active profile 进入本地 `/app` 或远端服务器 `/app`。

- 使用 Android 专用入口启动同一份 `app.py`、`auth.py`、`core.py`、Memory、Attention、Curator 和 Sleep 代码。
- 不启动 Tauri 桌宠、pywebview、pynput 或桌面 ScreenSensor；Android Live2D、MediaProjection 和通知仍由原生层负责。
- `DATA_DIR` 固定为 APK 私有 `filesDir` 下的 Miru 目录，加入备份排除规则，API key/token 不进入系统云备份。
- Flask 使用可显式 shutdown 的 loopback server，不使用 debug/reloader，不监听局域网地址。
- Android 本地数据 API 即使来自 loopback 也必须验证当前 profile token；随机端口只能减少碰撞，不能作为安全边界。
- platform/device payload 显式写 `android`，不得因为嵌入 Python 而注册成 `linux`。

### 4.2 Python 运行时可行性门

首选做一个隔离的 Chaquopy `androidLocalDebug` flavor，因为它能最大程度复用已验证 Python 核心，避免把 Memory/Attention/Curator 在 Kotlin 中重写成第二套实现。

可行性 spike 必须先证明：

1. arm64 APK 能打包并启动 Python，loopback `/api/health` 可用。
2. Flask、Pillow、OpenAI/Anthropic/httpx/requests、Pydantic 和项目源文件可导入。
3. Paramiko/cryptography 能在真机完成一次隔离 SSH 握手；如果不能，只有 provisioning adapter 改用原生 SSH 库，AI/记忆核心仍不重写。
4. app 私有目录能完成 local user 创建、图片写入、slot 写入和重启持久化。
5. 真实三层模型 test、chat、截图 VLM、Attention 和 Curator 在 Android 16 真机运行。
6. 前后台、锁屏、网络切换和进程重建没有双 loop、孤儿线程或数据损坏。

如果 spike 失败，不允许把“远端单人实例”改名为手机本地模式，也不直接启动 Kotlin 重写全部后端；先记录具体不兼容依赖和成本，再做架构复审。

### 4.3 Android 前台服务

- `ScreenCaptureService` 继续使用 `mediaProjection`，每个新的 MediaProjection session 必须由用户在系统界面确认。
- `MiruConnectionService` 已移除 `dataSync` foreground-service type，并实现系统 timeout 时的自停/恢复边界。公开前仍必须对 exact APK 做至少 8 小时前后台、锁屏和断网恢复真机验收；不能把短期运行等同于长时通过。
- 手机本地运行时也需要可见、可停止的前台通知，不能隐蔽常驻。用户从通知可进入 Miru或停止本地陪伴。
- 远端 SSE、手机本地 runtime 和录屏服务的通知/生命周期要分开观测，但账号切换必须统一 teardown。

### 4.4 设备设置隔离

旧版 `miru_prefs` 的截图开关、间隔、server URL、token 和 device ID 是全局键；当前实现已经改为：

- 安装级稳定 `device_id`：由原生层生成并保存，所有服务器都识别为同一部物理手机。
- profile key：`mode + canonical_server_origin + user_id`；本地账号使用 `local + user_id`。
- 截图开关、间隔、授权引导和最近错误按 profile 保存。
- Live2D 壁纸启用状态是设备级，不属于服务器用户设置。
- SSH 密码只存在于单次 job 内存；私钥通过 Android Storage Access Framework 临时读取，不复制到普通日志或共享文档。
- active profile 变更先同步停止旧 SSE/录屏，再写新 profile，最后启动新服务，避免旧 token 窗口。

## 5. 分阶段实现

### Phase A：候选 APK 真机基线和自动化脚手架

- 核对手机、候选 APK、签名证书和 source commit。
- 同签名使用 `adb install -r` 覆盖安装；日常 fresh-user 回归使用 `adb shell pm clear com.miru.companion`，不反复卸载。
- 新增真实 Android unit/instrumentation tests，删除错误模板 package 断言。
- 新增 Mac 侧 acceptance runner：设备检查、安装、清状态、启动、logcat、UI hierarchy、截图、端口转发、服务状态和产物哈希统一采集。
- 普通 APK UI 使用 UIAutomator/ADB 操作；安装风险确认、MediaProjection 同意等系统安全对话框只检测并停在一个明确人工动作，不伪造“已自动通过”。

验收：精确候选 APK 在手机上完成已有邀请码登录、聊天、SSE、通知、回复、截图上传、30 秒设置、前后台和重启，且服务日志无旧 token。

### Phase B：三端现有服务器矩阵

先不改手机本地模式，补完现有最终候选缺失的 Android 物理验收。

1. Mac 发起部署；Windows、Android 用同一长邀请码加入。
2. Windows 发起部署；Mac、Android 加入。
3. 自动范围 5001-5010：5001 被独立现有服务占用时必须选 5002，并返回 `skipped_system_ports`。
4. 高级设置 5002-5002：5002 空闲时成功；被占用时明确失败，不顺延。

测试只使用隔离 Host Manager home、独立 compose/project 前缀和测试端口。若复用承载 Project Page 的服务器，执行前必须单独确认，并建立以下硬保护：不修改 Nginx、域名、Project Page 目录、生产容器、生产 5001 或全局 Docker；记录测试前后 `docker ps`/监听端口/Project Page health；只删除本轮 instance 和本轮目录。

### Phase C：Android 远端客户端硬化

- 把静态邀请码页升级为与桌面一致的三入口 first-run shell，但未完成的本地/创建入口只在开发 flavor 展示，Release 不放假入口。
- 实现原生稳定 device/profile store 和账号切换 teardown。
- 修复 Android 15/16 长期连接前台服务时限，补 `onTimeout`、网络恢复和服务重建测试。
- 统一用户可理解的错误：邀请码格式、服务器不可达、端口未开放、邀请码无效、服务器异常。
- 保持服务器 AI/background ownership 不变。

### Phase D：手机真实本地模式

- 通过可行性门后，把 Python runtime 和 Android platform adapter 合入 APK。
- 本地账号沿用桌面 `local_single_device` 数据模型、三层 API 必测 gate、onboarding 和完整后端。
- Android 原生 ScreenCaptureService 把图片上传到本机 loopback backend；本地 backend 继续走同一 ScreenAnalyzer、Gate、Writer 和 Attention 信号链。
- 增加 Android runtime foreground service、graceful shutdown、crash recovery 和 profile generation。
- 本地 -> 登录页 -> 本地、本地 -> 服务器 -> 本地和先服务器后本地全部验收。

### Phase E：手机发起服务器部署

- 优先复用嵌入 Python 中现有 `client_provisioning.py`、Paramiko、Host Manager bundle 和 public-health gate。
- 支持选择本机下载的 `.tar/.tar.gz/.tgz` 镜像包；在线镜像源只做高级 fallback，不硬编码私有下载地址或凭据。
- 支持 SSH password 和 Storage Access Framework 选择私钥；密码不持久化，job 终态清除临时镜像和 secret。
- 成功后先展示/复制长邀请码，再由用户进入；失败 job 可继续检查状态但不能重复创建第二个 instance。
- 由手机网络访问实际 `http://IP:PORT/api/health`，服务器内 healthy 但手机不可达时提示开放安全组端口或改端口。

### Phase F：Android 发起的三端矩阵

- Android 自动部署：5001 已占用，选择 5002；Mac、Windows 加入。
- Android 高级指定 5002：部署成功；Mac、Windows 加入。
- 三端分别发送唯一 marker，每个 marker 只能有一个 user message ID 和一个 assistant message ID，并通过 SSE 到另两端。
- 三端分别产生截图，服务端必须保留正确 `device_id/platform`，不能互相覆盖截图开关/间隔。
- 服务端 Attention 主动消息只持久化一个 ID并广播三端；Curator、Sleep 和 Memory Writer 只在服务器运行一份。

### Phase G：完整本地/远端交叉回归

- Mac 本地、Windows 本地、Android 本地分别跑真实三层模型、chat、截图、Memory、Attention、Curator、Sleep、journal、DDL、重启。
- 每个平台分别做 local -> server -> local；切走后本地 marker/meta 不再变化，远端继续；切回只启动一组本地 runtime。
- 服务器账号下 Mac、Windows、Android 同时在线 8 小时以上，覆盖 Android SSE 服务时限、网络切换、锁屏、截图暂停/恢复和主动消息。
- 删除一个设备只影响该 server account 的 device inventory，不删除账号；本地模式删除/切换不能误删远端数据。

### Phase H：四包候选与开源门槛

- 从同一 source commit 构建 DMG、Windows installer、APK 和 linux/amd64 Docker archive。
- Android 在公开前使用固定 release signing key；key 不进 Git，CI 只读 secret。当前 debug 证书只能用于内部覆盖测试。
- 对四包做 source hash、secret scan、隐私数据、构建机路径、依赖许可和资产再分发审计。
- 安装并测试“最终字节”，不能用 dev build 代替；生成 manifest 后回读每个安装包 hash。
- 未经用户 review 不发布公开 Miru Release。

## 6. 三端逐项验收矩阵

每个“发起端”场景都必须至少检查：

1. fresh first-run 入口、返回、取消、错误恢复。
2. 服务器 SSH 验证、镜像上传进度、job 去重和 secret 清理。
3. 5001 占用自动到 5002、固定 5002、5002 也占用时失败。
4. 发起端外部 health，而不是只看服务器内 health。
5. API Key 三层保存、逐项真实测试、失败停留、成功后才进入。
6. onboarding 只出现一次；加入设备不重复创建人格或欢迎语。
7. `darwin`、`win32`、`android` 三个设备记录都在线。
8. 三端各自 chat、唯一 message ID、历史一致和实时 SSE。
9. 三端截图来源、实际图片尺寸、VLM、semantic gate、slot write。
10. 每设备截图开关/间隔和桌宠/壁纸状态隔离。
11. Attention cadence、单次主动消息、单次广播和未回应节奏。
12. Curator 四 domain、Sleep、journal、commitment 和重启持久化。
13. 每端切换账号、退出、重开、网络断开/恢复。
14. 删除设备、删除测试 instance、端口释放和服务器现场恢复。
15. Project Page 和生产服务前后只读 health/hash 不变。

## 7. 手机本地模式逐项验收

- 首次创建：无邀请码、数据在 app 私有目录、API gate 不能跳过。
- 三层 API：视觉使用真实非纯色图片，chat/memory 各真实调用；失败不进入 app。
- UI：onboarding、欢迎语、对话、记忆、情绪、日记、DDL、模型设置、设备页逐页检查。
- 截图：默认关、默认 30 秒、用户开启后系统授权、图片进入本地 VLM/Attention/Memory。
- 后台：Attention/Curator/Sleep/reminder/journal 的时间点和单实例线程。
- 生命周期：前台、后台、锁屏、杀进程、重开、手机重启、低内存回收。
- 隔离：本地 marker 不进入任一服务器；服务器 marker 不进入本地；API key/token/profile 设置不串。
- 安全：loopback 数据接口无 token 被拒绝；app backup 不含 API key/token；日志不打印 secret。
- 性能：冷启动、Python 解包、内存、CPU、耗电、APK 体积、30 秒截图和 24 小时稳定性。

## 8. 自动化与人工边界

Codex 可自动完成：

- `adb install -r` 同签名覆盖、`pm clear` 模拟新用户、启动/强停、普通 UI 点击和输入。
- logcat、dumpsys、UI hierarchy、APK hash、服务/通知/网络状态、手机截图和服务器日志采集。
- 三端 marker、SSE、后台文件、容器、端口和清理审计。

Codex 不能也不应绕过：

- vivo/Android 的“继续安装/风险确认”等受保护系统确认。
- 每个新 MediaProjection session 的系统录屏同意。
- Android Keystore 生物识别或其他明确要求本人确认的安全界面。

减少人工次数的方法是：固定同一签名证书、使用覆盖安装、不为 fresh test 反复卸载、用 `pm clear` 清状态，并把所有需要人工的系统确认集中成一个短清单。若系统确认出现，自动 runner 应明确停在该步骤，用户确认一次后继续执行剩余测试，而不是每一步都询问。

## 9. 完成定义

只有同时满足以下条件，才能称为可开源最终版：

- Mac、Windows、Android 都有真实完整本地模式，Android 本地不是远端实例的改名。
- Mac、Windows、Android 都能连接已有私有服务器；三者都完成真实同账号并发验收。
- Mac、Windows、Android 都能发起服务器部署，自动 5002 与高级固定 5002 均通过。
- 本地/远端 ownership、设备设置、API key、token、截图和后台线程隔离通过。
- Android 15/16 长期后台连接和本地 runtime 经过时限、锁屏、网络和进程重建测试。
- 四包同源、最终字节真机安装通过，secret/license/source-build gate 通过。
- 生产 Project Page 和生产 Miru instance 在整个测试中未被修改。
- 用户完成最终人工体验 review 后，才进入公开仓库和 Release 发布。
