<p align="center">
  <img src="_page/assets/readme-hero.png" alt="Miru — she remembers you" width="100%">
</p>

<h1 align="center">Miru&nbsp;&nbsp;見る</h1>

<p align="center">
  <b>The AI companion who actually remembers you — and lives entirely on your own machine.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache%202.0-D94F6E" alt="License">
  <img src="https://img.shields.io/badge/platform-macOS%20%C2%B7%20Android-D94F6E" alt="Platform">
  <img src="https://img.shields.io/badge/backend-self--hosted-D94F6E" alt="Self-hosted">
  <img src="https://img.shields.io/badge/models-any%20OpenAI--compatible-D94F6E" alt="Models">
</p>

<p align="center">
  <a href="https://mirulife.top/">Website</a> ·
  <a href="README.zh-CN.md">中文说明</a>
</p>

<p align="center">
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-macOS.dmg"><img src="_page/assets/dl-macos.svg" width="240" alt="Download for macOS"></a>&nbsp;
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/Miru-Android.apk"><img src="_page/assets/dl-android.svg" width="240" alt="Download for Android"></a>&nbsp;
  <a href="https://github.com/kiyotakali/Miru/releases/latest/download/miru-server-linux-amd64.tar.gz"><img src="_page/assets/dl-linux.svg" width="240" alt="Linux server image"></a>
</p>

---

## Not a chatbot you open

**Miru** (見る, Japanese for *to see*) isn't an app you launch when you need something. She lives on your desktop as a Live2D character, quietly follows your day — with your permission — and at midnight writes it into memory.

Three things set her apart from every "AI girlfriend" wrapper:

- **She runs on _your_ machine.** Your Mac, or your own server. There is no central cloud. The only thing that ever leaves your computer is the call to the model API *you* configured.
- **What she remembers is verifiable.** Not a black box — her memory is plain Markdown you can open, read, and export. She never "remembers" out of thin air: the chat agent only *reads* memory; every write comes from something that actually happened.
- **She decides when to speak.** An attention system reads your screen and your rhythm and judges the moment. Deep in work? She stays quiet. Stuck, tired, or winding down? She comes a little closer. It's her call — not a timer, not a notification rule.

<!-- TODO: add real in-app screenshots here — desktop pet, chat + proactive message, memory panel -->

## What she can do

**Sees your day.** A screen sensor glances at your screen (default every 60s, skipped when nothing changed, adjustable, or fully off). The screenshot is analyzed and thrown away — what stays is *what you were doing*, never the image.

**Remembers it as facts you can check.** Four kinds of long-term memory — projects, people, yourself, topics — plus a daily journal and a commitment list. When you tell a friend "I'll send you the figures tonight," she reads it off the screen and quietly logs the promise.

**Comes closer on her own terms.** Continuous emotion and a personality of her own (defined in a `soul.md`). She isn't reactive-only: when she sees you've been stuck for an hour and your mood keeps sinking, *she* opens the conversation.

**Lives across your devices.** A Live2D desktop pet on macOS, and the same her on Android — one invitation code, memory synced through your own server.

## Quick start

Miru is free software; you bring your own model API keys (any OpenAI-compatible provider). A recommended setup costs **under ¥2/day** in tokens.

### 🖥️ Just this Mac — local, single-device

1. Download the **macOS DMG** from [Releases](https://github.com/kiyotakali/Miru/releases).
2. On first launch, choose **"Only on this Mac."**
3. Fill in the three model tiers (Vision / Chat / Memory) in Settings.
4. Start talking — everything (chat, memory, journal) stays on your machine.

### 📱 Mac + phone — your own server, multi-device

1. Install the DMG and pick **"Mac + phone together."**
2. The first-run wizard deploys Miru to **your own Linux server** in one step.
3. You get a long invitation code.
4. Log in on Mac and Android with the same code — the same her, everywhere.

### 🐳 Server image — advanced

Prefer to deploy by hand? Grab the `linux-amd64` server image (`tar.gz`) from Releases, `docker load`, and run it on any Linux box (we test and deploy on **Ubuntu 20.04 LTS**; newer Ubuntu/Debian works too). Same invitation-code format as the wizard.

## How it works

```
   macOS / Android app                your Mac  ·  or your own Linux server
  ┌────────────────────┐            ┌───────────────────────────────────────┐
  │  Live2D pet         │            │  Flask backend                        │
  │  screen sensor      │  ───────▶  │  · chat agent (reads memory)          │
  │  thin native shell  │  invite    │  · AttentionEngine (decides to speak) │
  └────────────────────┘   code     │  · memory system (slots + journal)    │
                                     │  · sleep agent (organizes at night)   │
        model API you configured ◀───┤  · SSE sync across devices            │
        (any OpenAI-compatible)      └───────────────────────────────────────┘
```

- **No central backend.** In local mode the backend runs on your Mac; in multi-device mode it runs on your server. Outbound traffic goes only to your chosen model provider.
- **Three model tiers**, all OpenAI-compatible: `Vision` (reads screenshots), `Chat` (talks to you), `Memory` (organizes what she keeps). Mix providers freely.
- The macOS/Android apps and the server image are released here under **Apache 2.0**. The full backend source is being opened up in stages — [star / watch](https://github.com/kiyotakali/Miru) to follow along.

## Privacy

- Chat, screen understanding, memory, and journals live on **your** Mac or **your** server. This project's website stores nothing.
- Screenshots are discarded right after analysis; only the understanding is kept.
- She's not a black box: her memory, journal, and even her attention/mood notes are files on your own backend you can open and audit.

## Roadmap

- [x] Local single-device mode (macOS)
- [x] Self-hosted multi-device (macOS + Android, one invitation code)
- [x] Live2D desktop pet, AttentionEngine proactive presence, verifiable memory
- [ ] Windows client
- [ ] iOS client
- [ ] Full backend source open-sourced
- [ ] Editable personality / prompt knobs

If Miru resonates with you, a ⭐ genuinely helps — and lets you follow the road to Windows, iOS, and full open source.

## License

[Apache 2.0](LICENSE).

<p align="center"><sub>花会落，她记得。 — Petals fall; she remembers.</sub></p>
