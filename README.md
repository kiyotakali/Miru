# Miru Project Page

Miru（見る）的项目主页——「花会落，她记得。」

一个纯静态、零依赖、零构建的单页站点：这个文件夹本身就是完整的网站。

## 概念

- **樱花光谱**：整站只用一个樱花色族，从清晨薄樱白到 00:00 的夜樱深紫。
- **一天 24 小时就是信息架构**：序章（《キミの記憶》歌词扉页）→ 07:00 Hero →
  09:00–20:30 横向卷轴「日常」（纵向滚动驱动横向滑动，借鉴 P3R 人物章的滚动语法）→
  21:00 人物（贴纸拼贴档案）→ 00:00 記憶（核心章）→ 03:00 隐私 → 05:30 开始（黎明）→
  08:00 下载 → FAQ。
- **签名动效**：白天花瓣缓缓下落（日常在流逝）；进入 00:00 记忆章后花瓣发光上升
  （她把这一天收进记忆）。

## 本地预览

任何静态服务器都可以：

```bash
cd miru-page
python -m http.server 8899
# 打开 http://localhost:8899/
```

或直接双击 `index.html`（file:// 协议下 sessionStorage 可能受浏览器限制，序章每次都会播放）。

## 文件结构

```
index.html      # 单页全部结构与文案
css/main.css    # 樱花色系设计令牌 + 全部样式（含响应式 / reduced-motion）
js/main.js      # 序章、花瓣引擎、横向卷轴、时刻度盘导航、滚动揭示
assets/         # 立绘 / 海报 / logo / 图标（源自 Miru 主仓库 + 定制海报）
```

## 调试参数

| 参数 | 作用 |
|---|---|
| `?prologue=1` | 强制重播歌词序章（正常每个会话只播一次） |
| `?prologue=skip` | 跳过序章 |
| `?flat=1` | 横向卷轴平铺为纵向（供截图 / 排版检查） |
| `?flat=1&probe=1` | 屏幕左上列出各章节像素偏移 |
| `?flat=1&off=<px>` | 页面整体上移 <px> 像素（headless 截图用，隐藏导航） |

## 行为细节

- 序章每会话一次（`sessionStorage.miruPrologueSeen`），可点击快进、`Esc` 跳过，
  `prefers-reduced-motion` 时静态显示。
- 横向卷轴在 ≤860px 或 reduced-motion 下降级为原生 `scroll-snap` 横向滑动 / 纵向堆叠。
- 花瓣层是单个 canvas，reduced-motion 时整层关闭。
- 序章歌词四句出自「キミの記憶」（《女神异闻录3》，ATLUS），页面内已署名；
  请勿在页面中新增受版权保护的歌词引用。

## 部署

任意静态托管（GitHub Pages / Netlify / Vercel / VPS nginx）直接指向本目录即可。
注意 `og:image` 使用相对路径，部署后如需社交分享卡生效，请把
`<meta property="og:image">` 改为线上绝对 URL。

## 素材说明

- `assets/miru-poster.png`：Miru 宣传海报（高马尾）。页面中以"电影式裁切"用于
  Hero 首屏与人物章的实体宣传单，原图带有的 "VTUBER DEBUT!" 字样已用
  「SHE REMEMBERS!」贴纸在视觉上覆盖/替换（Miru 不是 VTuber）。
- `assets/miru-hero.png`：长发安静立绘 = 深夜的她，用于 00:00 记忆章（照片化处理）。
- `assets/miru-avatar.png`：Q 版头像 = 全站功能性贴纸。
