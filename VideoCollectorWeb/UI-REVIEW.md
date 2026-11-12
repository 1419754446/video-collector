# VideoCollectorWeb 界面评估（设计感 / 美观度）

对照对象：`pyVideo`（PySide6 版）当前 UI，以及本仓库 `ui/index.html`、`ui/style.css`、`ui/preview.html`、`ui/preview.css`、`src/main.cpp`。

**一句话结论**：底子是好的——玻璃拟态深色面板、双主题、DWM 深色标题栏、独立窗口自绘播放条都做了，比一般 Win32 工具精致。但有三处硬伤让它"看起来精致、用起来不像视频工具"：**表格没有缩略图列**、**侧栏内预览用的是 Chromium 原生控制条**、**次级文字偏小偏灰**。下面按优先级列，每条都给到能直接改的位置。

---

## 评分（5 分制，主观但可对照）

| 维度 | 分 | 依据 |
| --- | --- | --- |
| 整体气质 | 4.0 | 深色板 + 渐变强调色统一，比系统控件观的工具有明显设计意图 |
| 信息层级 | 3.0 | 标题 17px / 正文 13px / 次级 10.5px，跨度不够，主次靠"变灰"而不是"变大小" |
| 一致性 | 3.0 | 强调色混用 `#8b8bff`、`rgba(124,124,255,*)`、`#7c7cff`；圆角 7 档；两处播放控件不同风格 |
| 可读性 | 2.5 | 10.5–11.5px 小字 + 深色 `#6b7689`（3.95:1）、浅色 `#828ca1`（2.99:1）未达 4.5:1 |
| 细节完成度 | 3.5 | 空状态、Toast、模态、右键菜单都齐；缺缩略图骨架、focus 环、行内左标记 |
| 与 Python 版一致性 | 3.0 | 品牌标已同源，但行高 46 vs 68、无缩略图列、标题字号与侧栏宽度都不同 |

---

## P0：影响可用性，先改这三件

### 1. 表格缺「缩略图」列（与 Python 版差距最大的一项）

现状：`.thead/.row` 的 `grid-template-columns` 是 `38px 46px minmax(150px,1.9fr) 84px minmax(112px,.95fr) minmax(118px,1.15fr)`，`--row-h: 46px`，整行纯文字。用户在整理 20000 个视频时，真正需要的是"扫一眼就知道哪个文件是哪个"——这一步在 Python 版靠 104×68 的首帧列完成，C++ 版没有，识别成本高，也直接少了"视频工具"的观感。

改法（前端）：

```css
:root { --row-h: 58px; }   /* 46 → 58：留出首帧的呼吸感 */
.thead, .row {
  grid-template-columns: 38px 46px 92px minmax(150px, 1.75fr) 82px minmax(110px, .92fr) minmax(112px, 1.1fr);
}
.row .thumb {
  width: 92px; height: 48px; border-radius: 8px; overflow: hidden;
  background: linear-gradient(135deg, rgba(124,124,255,.18), rgba(52,214,232,.10));
  display: grid; place-items: center;   /* 出图前就是骨架位 */
}
.row .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
```

首帧来源（后端）建议用 Windows 自带缩略图，不必上 ffmpeg：

- `IShellItemImageFactory::GetImage`（Shell 缩略图），有视频解码器就能出图、命中系统缓存时几乎零成本；失败再退到占位图。
- 走现成的 `http_server.cpp` 加一个 `/thumb?path=...&v=mtime` 端点，响应带 `Cache-Control: max-age=604800` + 磁盘缓存目录（Python 版是 `~/.videocollector/thumbnails/`，两版可共用同一目录，用户换版本自带缓存）。
- 前端按可视区懒加载（`IntersectionObserver` 或滚动节流），一万行也不要一次性请求。

### 2. 侧栏内预览用的是 Chromium 原生 `<video controls>`，和整体设计割裂

`ui/index.html:291` 是 `<video id="video" controls preload="metadata">`，于是右侧面板底部是一条系统灰的播放条：圆角、按钮、进度条全是 Chromium 默认样式，和旁边自绘的深色卡片完全是两个世界。**同一个程序里，独立预览窗口（`preview.html` + `preview.css`）反而是自绘控制条**（`#btnPlay/#btnMute/#volume/#btnFull`），两处不一致这一点最伤"设计感"。

改法：把 `preview.html` 那套 `controls` 原样搬进侧栏面板（同一套 CSS 类、同一套 JS 绑定），`video` 去掉 `controls` 属性；顺带把独立窗口有的 **倍速（0.5/1/1.5/2）** 补上——Python 版侧栏是有倍速下拉的。

```html
<video id="video" preload="metadata"></video>   <!-- 去掉 controls -->
<footer class="pv-controls"> …btnPlay / 时间 / 进度 / 静音 / 音量 / 倍速 / 全屏… </footer>
```

### 3. 行内 hover 操作胶囊会盖住「来源文件夹」文字

`.row .actions` 是 `position:absolute; right:8px; top:50%`，浮在最后一列上方，`opacity:0 → 1` 只在 hover/选中时出现。问题：鼠标划过时正好挡住用户正在看的那段路径（最后一列是 `minmax(118px,1.15fr)`，本来就窄）；而且它是**浮层**，不占布局，行内右侧永远被压着看。

改法二选一：

- 稳妥：把它做成**第 7 个 grid 列**（`56px`，常态 `visibility:hidden`，hover 才显形），文字永远不会被覆盖；
- 或者干脆去掉行内浮层，只保留右键菜单（Python 版就是这么做的，行内更干净）。

---

## P1：设计感 / 一致性，改动小、观感提升明显

### 4. 小字太小、太灰（这也是 Python 版你提过的"字细"）

实测对比度（WCAG）：

| 位置 | 颜色 | 对比度 | 结论 |
| --- | --- | --- | --- |
| 深色 `--text-mute` on `--bg` | `#6b7689` / `#0a0c11` | 4.26:1 | 低于 4.5，11px 及以下不达标 |
| 深色 hint（10.5px）on 卡片 `#12161f` | `#6b7689` | 3.95:1 | 明显偏低 |
| 浅色 `--text-mute` on `#eef1f7` | `#828ca1` | 2.99:1 | 严重偏低 |
| 浅色主按钮白字 on `#4f8dff` | `#ffffff` | 3.19:1 | 偏低 |

改法：

```css
:root { --text-mute: #7d879b; }                       /* 深色提亮一档，→ 4.9:1 */
html[data-theme="light"] { --text-mute: #6d7789; }     /* 浅色压深一档，→ 4.6:1 */
```

并把字号下限从 10.5 抬到 11.5–12：`.hint`、`.switch-row small`、`.seg button small`、`.chip`、`.th`、`.pill` 统一 ≥ 11.5px；正文 13px → 13.5px；行内文件名 `font-weight: 500 → 600`（深色 + `-webkit-font-smoothing: antialiased` 会让 500 显得更细）。

### 5. 强调色散落在各处，浅色主题属于"改一处漏一处"

`--accent: #8b8bff`，但 `.seg button.active`、`.badge`、`.chip.clickable`、`.input-group:focus-within`、`.progress-fill` 等处写死 `rgba(124,124,255,…)` / `#7c7cff`；浅色主题又整段复制了一遍 `rgba(91,91,214,…)`。结果是：改主色要动十几处，深浅两套还容易错位。

改法：引入通道变量，所有半透明都用它推导：

```css
:root { --accent-rgb: 139, 139, 255; }
html[data-theme="light"] { --accent-rgb: 91, 91, 214; }
/* 用法 */
background: rgba(var(--accent-rgb), .16);
box-shadow: 0 0 0 3px rgba(var(--accent-rgb), .14);
```

### 6. 圆角档位太多（7 档）

现用 18（modal）/16（卡片）/12（seg、菜单、fab）/10（按钮、输入组）/9（行内浮层、logo）/8（icon-btn、菜单项）/7（tiny 图标）/6/5（kbd、checkbox）。视觉上差 1–2px 无人能分辨，却让"圆角节奏"失效。

收敛成 3 档：**卡片 16 / 控件 10 / 小件 8**，胶囊一律 999。

### 7. 字号阶梯混乱

现有 17 / 15.5 / 14.5 / 13 / 12.5 / 12 / 11.5 / 11 / 10.5 / 10 共 10 档。建议 6 档：`20（大标题）/ 16（区块标题）/ 14（重点）/ 13（正文）/ 12（次级）/ 11.5（最小）`。

具体：`h1` 17 → 18~19（现在比 Python 版标题小一号，主区显得没"头"）；`.modal-title` 14.5 → 15；`.brand-title` 15.5 → 17，`.logo` 42 → 46（和 Python 侧栏的品牌区同尺寸，两版截图并排时不至于一个"小一号"）。

### 8. 空状态插画太"线性"

`.empty svg` 是一组 1.4px 描边 + 0.35~0.6 透明度，配上 0.5 的整体透明度，在深色下几乎看不见，观感像"没画完"。Python 版用的是带渐变底的 EmptyArt（叠层圆角方块 + 渐变播放三角 + 柔和投影）。

改法：给 `.empty` 加一层柔光底 + 实心渐变图标：

```css
.empty svg { opacity: .9; }
.empty::before {
  content: ""; position: absolute; width: 220px; height: 220px;
  background: radial-gradient(circle, rgba(var(--accent-rgb), .16), transparent 65%);
  filter: blur(2px);
}
```

### 9. 选中行缺左侧识别标记

`.row.selected` 只有一层从 0.17 到 0.05 的横向渐变；一屏 20 行里选中 3 行，眼睛要靠底色深浅去分辨。Python 版是 3px 强调色竖条 + 浅底。

```css
.row.selected { box-shadow: inset 3px 0 0 0 var(--accent); }
```

### 10. 统计胶囊 + 进度条分成两块，白占一行高度

`<div class="stats">` 和 `<div class="progress-card">` 垂直排两行（各约 28 + 19px + gap 12）。Python 版把「状态胶囊」和「进度条」压在同一行（胶囊左、进度右），主区域顶部能省 20~30px 给表格。

改法：`.progress-card` 改成 `position:absolute` 或与 `.stats` 同 flex 行内（`justify-content: space-between`），窄窗再折行。

### 11. 深色卡片叠了两层阴影，略显"糊"

`.card/.table-card/.log-card` 同时吃 `--panel`(0.035 白)+`--border`(0.075 白)+`--card-shadow`(0 8 28 -12)。深色下三层叠加会让卡片边缘发毛，尤其相邻卡片 gap 只有 10px。

改法：深色只留 1px 边框 + 极浅阴影（或直接去掉阴影，靠 `--panel` 明度差分层）；浅色反而可以留阴影（现有 light 规则已经这么做了）。

---

## P2：打磨项（有空再做）

| # | 问题 | 改法 |
| --- | --- | --- |
| 12 | 深色行分隔线 `rgba(255,255,255,.035)` 几乎看不见 | 提到 `.06` |
| 13 | 滚动条 10px，thumb 0.12 白 | 8px + 圆头，hover 才提亮 |
| 14 | 表头 hover `0.05`、行 hover `0.035`，两个不同的"悬停强度" | 统一到 `.05` |
| 15 | 全站无 `:focus-visible` | 统一 `outline: 2px solid rgba(var(--accent-rgb), .45); outline-offset: 1px` |
| 16 | 大小列 Consolas 11.5px，正文 12.5px，基线和字重都不齐 | 数字统一 `font-variant-numeric: tabular-nums`，字号跟正文一致 |
| 17 | 首帧加载前后无过渡，行内"闪"一下 | 已有 `.thumb` 渐变骨架位即可（见 P0-1） |
| 18 | `.pv-tip` 用虚线分隔，是全站唯一一处虚线 | 改成细实线，和卡片分隔统一 |
| 19 | 快捷键提示 `kbd` 5px 圆角、10px 字 | 归入"小件 8px / 11.5px"体系 |
| 20 | 窗口标题栏 | C++ 已用 `DwmSetWindowAttribute`（20/35/36/34）做深色标题栏，这点**比 Python 版好**；Python 版深色下标题栏仍是白色，属于反向要补的项 |

---

## 两版并排对照（改完应当对齐的数值）

| 项 | Python 版（现状） | C++ 版（现状） | 建议 |
| --- | --- | --- | --- |
| 侧栏宽 | 320 | 312 | 320 |
| 表格行高 | 68 | 46 | C++ 加缩略图后 58~62 |
| 缩略图列 | 104×68，圆角 7 | 无 | 92×48 |
| 标题字号 | 19 左右 | 17 | 18~19 |
| 品牌 logo | 与 exe 同源 | 42px，同源 SVG | 46px |
| 次级文字下限 | 12px | 10.5px | 11.5~12px |
| 预览控制条 | 自绘（播放/时间/音量/倍速） | 侧栏原生 / 独立窗自绘 | 两处统一自绘 |
| 深色标题栏 | 系统白 | DWM 深色 | 保持 C++ 做法 |

---

## 建议的落地顺序

1. **P0-1 缩略图列**（含 `/thumb` 端点 + 磁盘缓存）——影响最大，也是两版观感差距的主因；
2. **P0-2 侧栏预览统一自绘控制条**（直接复用 `preview.html` 那套）；
3. **P1-4 字号/对比度**（一次性全局改，收益最直观，也是最容易被用户吐槽"字太细"的地方）；
4. **P0-3 行内浮层**、**P1-5~11**（颜色变量、圆角、字号阶梯、空状态、选中竖条、统计行、阴影）；
5. P2 打磨项按需。

> 说明：本次只做到"读代码 + 出诊断"，没有改 C++ 源码，也没有重新编译 `bin\VideoCollectorWeb.exe`（当前 exe 仍是 2026-11-10 14:24 那版）。要动手改的时候，按上面顺序来，每步都能独立验证（前端改完直接刷新 WebView2 即可看到效果）。
