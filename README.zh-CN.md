<p align="center">
  <img src="_page/assets/readme-hero.png" alt="Miru — 她记得你" width="100%">
</p>

<h1 align="center">Miru&nbsp;&nbsp;見る</h1>

<p align="center">
  <b>一个真正记得你的开源 AI 陪伴——完全跑在你自己的机器上。</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache%202.0-D94F6E" alt="License">
  <img src="https://img.shields.io/badge/platform-macOS%20%C2%B7%20Android-D94F6E" alt="Platform">
  <img src="https://img.shields.io/badge/backend-self--hosted-D94F6E" alt="Self-hosted">
  <img src="https://img.shields.io/badge/models-any%20OpenAI--compatible-D94F6E" alt="Models">
</p>

<p align="center">
  <a href="https://mirulife.top/">官网</a> ·
  <a href="README.md">English</a>
</p>

<p align="center">
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-macOS.dmg"><img src="_page/assets/dl-macos.svg" width="240" alt="下载 macOS 版"></a>&nbsp;
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-Android.apk"><img src="_page/assets/dl-android.svg" width="240" alt="下载 Android 版"></a>&nbsp;
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/miru-server-linux-amd64.tar.gz"><img src="_page/assets/dl-linux.svg" width="240" alt="Linux 服务器镜像"></a>
</p>

---

## 不是你打开才存在的应用

**Miru**（見る，日语「看见」）不是你需要时才打开的聊天框。她以 Live2D 桌宠的形象住在你的桌面上，在你允许下安静地陪你过完一天，然后在深夜，把这一天写进记忆。

三件事让她和市面上的「AI 女友」壳子不一样：

- **她跑在_你自己_的机器上。** 你的 Mac，或你自己的服务器。没有中心云端。唯一离开你电脑的，只有发往_你自己配置_的模型 API 的那次调用。
- **她记得的东西可以验证。** 不是黑箱——她的记忆是能打开、能读、能导出的 Markdown。她不会凭空「记得」：对话 agent 只_读_记忆，每一次写入都来自真实发生过的事。
- **她自己判断什么时候开口。** 一套注意力系统读你的屏幕和节奏，判断此刻合不合适。你在深度工作，她就安静待着；卡住、疲惫、或松下来时，她才靠近一点。这是她当下的决定——不是定时器，也不是通知规则。

<!-- TODO: 这里加真实 App 截图——桌宠、聊天+主动消息、记忆面板 -->

## 她能做什么

**看见你的一天。** 屏幕感知每隔一段时间看一眼你的屏幕（默认 60 秒，画面几乎没变就跳过，可调、也可完全关闭）。截图分析完就丢弃——留下的是「你当时在做什么」，从不是图像本身。

**记成你能核对的事实。** 四类长期记忆——项目、人物、你自己、话题——加上每天的日记和承诺清单。当你对朋友说「今晚我把图发你」，她从屏幕上读懂，悄悄把这个承诺记下来。

**以她自己的方式靠近。** 连续的情绪，和她自己的人格（写在一份 `soul.md` 里）。她不只是被动回应：当她看见你卡了一个小时、心情一直往下，是_她_先开的口。

**在你的多个设备上活着。** macOS 上是 Live2D 桌宠，Android 上是同一个她——一串邀请码，记忆通过你自己的服务器同步。

## 快速开始

Miru 是免费软件；你自备模型 API Key（任何 OpenAI-compatible 服务）。按官方推荐配置，每天 token 花费**不到 2 元**。

### 🖥️ 只在这台 Mac —— 本地单设备

1. 从 [Releases](https://github.com/kiyotakali/Miru/releases) 下载 **macOS DMG**。
2. 首次启动时选 **「只在这台 Mac 使用」**。
3. 在设置里填好三层模型（Vision / Chat / Memory）。
4. 开始聊天——聊天、记忆、日记全部留在本机。

### 📱 Mac + 手机 —— 自有服务器，多设备

1. 安装 DMG，选 **「Mac + 手机一起」**。
2. 首启向导一键把 Miru 部署到 **你自己的 Linux 服务器**。
3. 拿到一串长邀请码。
4. Mac 和 Android 用同一串码登录——同一个她，处处都在。

### 🐳 服务器镜像 —— 进阶

想手动部署？从 Releases 拿 `linux-amd64` 服务器镜像（`tar.gz`），`docker load` 后在任意 Linux 机器上运行（我们的测试机和部署机用的是 **Ubuntu 20.04 LTS**，更新的 Ubuntu/Debian 也可以）。邀请码格式和向导完全一致。

## 工作原理

```
   macOS / Android 客户端              你的 Mac  ·  或你自己的 Linux 服务器
  ┌────────────────────┐            ┌───────────────────────────────────────┐
  │  Live2D 桌宠        │            │  Flask 后端                           │
  │  屏幕感知           │  ───────▶  │  · 对话 agent（只读记忆）             │
  │  原生瘦壳           │  邀请码    │  · AttentionEngine（判断何时开口）    │
  └────────────────────┘            │  · 记忆系统（slot + 日记）            │
                                     │  · Sleep Agent（深夜整理）            │
        你配置的模型 API ◀───────────┤  · SSE 多设备同步                     │
        （任何 OpenAI-compatible）    └───────────────────────────────────────┘
```

- **没有中心后端。** 本地模式后端跑在你 Mac 上，多设备模式跑在你的服务器上。对外流量只发往你选的模型服务商。
- **三层模型**，都是 OpenAI-compatible：`Vision`（读截图）、`Chat`（和你对话）、`Memory`（整理她记住的东西）。可自由混用不同服务商。
- macOS/Android 客户端与服务器镜像在此以 **Apache 2.0** 发布；完整后端源码正在分阶段开放——[star / watch](https://github.com/kiyotakali/Miru) 关注进度。

## 隐私

- 聊天、截图理解、记忆、日记，都存在**你自己的** Mac 或**你自己的**服务器上。这个项目的官网一个字节都不收集。
- 截图分析完立即丢弃，只保留理解。
- 她不是黑箱：记忆、日记、乃至她的注意力和心情记录，都是你自己后端里能打开审查的文件。

## 路线图

- [x] 本地单设备模式（macOS）
- [x] 自有服务器多设备（macOS + Android，一串邀请码）
- [x] Live2D 桌宠、AttentionEngine 主动陪伴、可验证的记忆
- [ ] Windows 客户端
- [ ] iOS 客户端
- [ ] 完整后端源码开源
- [ ] 可编辑人格 / prompt 旋钮

如果 Miru 打动了你，一个 ⭐ 真的很有帮助——也方便你追踪 Windows、iOS 和完整开源的进度。

## 许可证

[Apache 2.0](LICENSE)。

<p align="center"><sub>花会落，她记得。</sub></p>
