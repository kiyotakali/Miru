<p align="center">
  <img src="_page/assets/readme-hero.png" alt="Miru — she remembers you" width="100%">
</p>

<h1 align="center">Miru&nbsp;&nbsp;見る</h1>

<p align="center">
  <b>The AI companion who actually remembers you — and lives entirely on your own machine.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache%202.0-C64D70" alt="License">
  <img src="https://img.shields.io/badge/platform-macOS%20%C2%B7%20Windows%20%C2%B7%20Android-3F74B5" alt="Platform">
  <img src="https://img.shields.io/badge/backend-self--hosted-318B67" alt="Self-hosted">
  <img src="https://img.shields.io/badge/models-any%20OpenAI--compatible-75618A" alt="Models">
</p>

<p align="center">
  <a href="https://mirulife.top/">Website</a> ·
  <a href="README.zh-CN.md">中文说明</a> ·
  <a href="docs/wechat-group.md">WeChat Group</a>
</p>

<p align="center">
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-macOS.dmg"><img src="docs/readme-assets/dl-macos.svg" width="300" alt="Download Miru for macOS"></a>
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-Windows-x64-Setup.exe"><img src="docs/readme-assets/dl-windows.svg" width="300" alt="Download Miru for Windows x64"></a>
  <br><br>
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-Android.apk"><img src="docs/readme-assets/dl-android.svg" width="300" alt="Download Miru for Android"></a>
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-Server-linux-amd64.tar.gz"><img src="docs/readme-assets/dl-linux.svg" width="300" alt="Download the Miru Linux server image"></a>
</p>

---

## Not a chatbot you open

**Miru** is 見る — Japanese for *to see*. Spell it out and it says something else: **M**emory, **I** **R**emember **U**. She isn't an app you launch when you need something; she lives on your desktop as a Live2D character, quietly follows your day — with your permission — and at midnight writes it into memory.

Three things set her apart from every "AI girlfriend" wrapper:

- **She runs on _your_ device.** Your Mac, Windows PC, Android phone, or your own server. There is no central Miru cloud. The only application traffic sent to a third party is the model API call *you* configured.
- **What she remembers is verifiable.** Not a black box — her memory is plain Markdown you can open, read, and export. She never "remembers" out of thin air: the chat agent only *reads* memory; every write comes from something that actually happened.
- **She decides when to speak.** An attention system reads your screen and your rhythm and judges the moment. Deep in work? She stays quiet. Stuck, tired, or winding down? She comes a little closer. It's her call — not a timer, not a notification rule.

<!-- TODO: add real in-app screenshots here — desktop pet, chat + proactive message, memory panel -->

## What she can do

**Sees your day.** A screen sensor glances at your screen (default every 30s, skipped when nothing changed, adjustable, or fully off). The screenshot is analyzed and thrown away — what stays is *what you were doing*, never the image.

**Remembers it as facts you can check.** Four kinds of long-term memory — projects, people, yourself, topics — plus a daily journal and a commitment list. When you tell a friend "I'll send you the figures tonight," she reads it off the screen and quietly logs the promise.

**Comes closer on her own terms.** Continuous emotion and a personality of her own (defined in a `soul.md`). She isn't reactive-only: when she sees you've been stuck for an hour and your mood keeps sinking, *she* opens the conversation.

**Lives across your devices.** macOS, Windows, and Android can each run Miru locally, or connect through one invitation code to the same Miru on your own server.

## Quick start

Miru is free software; you bring your own model API keys (any OpenAI-compatible provider). A recommended setup costs **under ¥2/day** in tokens.

### Local, single-device

1. Download the installer for macOS, Windows, or Android from [Releases](https://github.com/kiyotakali/Miru/releases).
2. On first launch, choose local single-device mode.
3. Fill in the three model tiers (Vision / Chat / Memory) in Settings.
4. Start talking — chat, memory, journal, AttentionEngine, Curator, and SleepAgent run on that device.

### Your own server, multi-device

1. On macOS, Windows, or Android, choose multi-device mode.
2. Deploy Miru to **your own Linux server** from the first-run wizard, or connect to an existing Miru instance.
3. You get a long invitation code.
4. Log in on Mac, Windows, and Android with the same code — the same account and memory on every connected device.

### Ubuntu desktop preview

Ubuntu 22.04 / X11 users can run the desktop client from source. See the
[Ubuntu setup guide](docs/UBUNTU_DESKTOP.md) for dependencies and limitations.

### 🐳 Server image — advanced

Prefer to deploy by hand? Grab `Miru-Server-linux-amd64.tar.gz` from Releases, run `docker load`, and start it on an x86-64 Linux server. See [the deployment guide](deploy/README.md).

## How it works

```
 macOS / Windows / Android       current device  ·  or your own Linux server
  ┌─────────────────────┐           ┌────────────────────────────────────────┐
  │  Live2D pet         │           │  Flask backend                         │
  │  screen sensor      │  ───────> │  · chat agent (reads memory)           │
  │  thin native shell  │  invite   │  · AttentionEngine (decides to speak)  │
  └─────────────────────┘   code    │  · memory system (slots + journal)     │
                                    │  · sleep agent (organizes at night)    │
        model API you configured <──┤  · SSE sync across devices             │
        (any OpenAI-compatible)     └────────────────────────────────────────┘
```

- **No central Miru backend.** In local mode the backend runs on the current device; in multi-device mode it runs on your server. Model traffic goes to the provider you configure.
- **Three model tiers**, all OpenAI-compatible: `Vision` (reads screenshots), `Chat` (talks to you), `Memory` (organizes what she keeps). Mix providers freely.
- Miru's application source is available in this repository under **Apache 2.0**. Live2D Cubism components and the Hiyori sample model keep their separate upstream terms and are not included in the source archive; see [Third-Party Notices](THIRD_PARTY_NOTICES.md).

## Privacy

- Chat, screen understanding, memory, and journals live on **your** device or **your** server. This project's website stores nothing.
- Screenshots are discarded right after analysis; only the understanding is kept.
- She's not a black box: her memory, journal, and even her attention/mood notes are files on your own backend you can open and audit.

## Roadmap

- [x] Local single-device mode (macOS, Windows, Android)
- [x] Self-hosted multi-device (macOS, Windows, Android, one invitation code)
- [x] Live2D desktop pet, AttentionEngine proactive presence, verifiable memory
- [x] Full Miru application source published
- [ ] iOS client
- [ ] Editable personality / prompt knobs

If Miru resonates with you, a ⭐ genuinely helps — and lets you follow the road to Windows, iOS, and full open source.

## Acknowledgements

Heartfelt thanks to [Jiazhe Wei](https://jiazhewei.github.io/), [Hongzhe Chen](https://github.com/Chenhzjs), [Jingkang Yang](https://jingkangyang.com/), [Haofan Wang](https://haofanwang.github.io/), and [Chenyang Si](https://chenyangsi.top/) for their help along the way.

## Contact

For questions and collaborations, please contact:

- **Ken Li**: [kiyotakali075@gmail.com](mailto:kiyotakali075@gmail.com)
- **Chenyang Si**: [chenyangsi@smail.nju.edu.cn](mailto:chenyangsi@smail.nju.edu.cn)

## License

Miru is released under the [Apache License 2.0](https://github.com/kiyotakali/Miru/blob/main/LICENSE).

Third-party dependencies and assets remain under their respective licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### Commercial Deployment Notification

If you deploy Miru or a modified version as part of a commercial product or service, we kindly ask you to notify us at [kiyotakali075@gmail.com](mailto:kiyotakali075@gmail.com).

Please include the name of your organization, the product or service name, and a brief description of how Miru is being used. No confidential information is required.

This notification is requested for project tracking and community outreach purposes only. It does not require approval, impose a license fee, or modify any rights granted under the Apache License 2.0.

Developer setup and all four build targets are documented in [docs/BUILDING.md](docs/BUILDING.md). Contributions are described in [CONTRIBUTING.md](CONTRIBUTING.md).

<p align="center"><sub>花会落，她记得。 — Petals fall; she remembers.</sub></p>
