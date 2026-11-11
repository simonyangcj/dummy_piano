# piano-tui

终端钢琴乐谱播放器：把乐谱截图自动转成钢琴音频并播放，同时在终端里实时显示低音声浪（频谱）动画。

```
图片 → 合并 → MusicXML (oemer) → MIDI (music21) → 钢琴音频 (FluidSynth) → 播放 + 可视化
```

## 功能

- 播放列表根目录下**每个子目录 = 一首曲子**（目录名即曲名），图片按文件名自然排序后纵向拼接
- OMR 识别乐谱 → MIDI → 钢琴音色渲染 → 播放
- 终端实时频谱：20Hz–16kHz 对数柱状图 + BASS (20–150Hz) 能量条，30 FPS，音画同步
- 缓存：识别结果按图片内容哈希缓存，图片未变时秒开；修改图片自动重新处理；某步失败重跑时从失败的那步继续
- 转出的 `play.wav` 会自动放到曲谱目录下（与图片同层），下次播放直接使用、跳过全部转换；旁边的 `play.hash` 记录生成时的图片哈希，图片改动后会自动重新生成（自行放入 `play.wav` 且不带 `play.hash` 时视为手动指定，原样使用）
- 后台线程预取下一首，避免播放间隙
- 键盘控制：`空格` 暂停/继续、`n` 下一首、`p` 上一首、`q` / `Ctrl+C` 退出

## 系统依赖

无法由 uv 安装，需提前装好：

- **Ubuntu / Debian**

  ```bash
  sudo apt install fluidsynth libportaudio2 libsndfile1
  ```

- **macOS**

  ```bash
  brew install fluid-synth portaudio
  ```

另外需要一份钢琴 SoundFont (`.sf2`)，例如 [FluidR3_GM](https://sites.google.com/site/soundfonts4u/) 或 GeneralUser GS。

## 安装

```bash
# 从零初始化（本仓库已完成这些步骤，可直接 uv sync）
uv init --package piano-tui
uv python pin 3.11
uv add pillow numpy sounddevice soundfile oemer music21 pretty_midi pyfluidsynth rich
uv add --dev pytest ruff
uv sync
```

## 使用

```bash
uv run piano-tui --play-list /tmp/myplaylist
# 或不安装脚本直接运行
uv run python -m piano_tui --play-list /tmp/myplaylist
```

播放列表结构（每个子目录一首曲子，目录名作为曲名）：

```
/tmp/myplaylist/
├── 安静/
│   ├── 乐谱1.png
│   ├── 乐谱2.png
│   └── 乐谱3.png
└── 欢快/
    ├── 1.png
    └── 2.png
```

### 参数

| 参数 | 说明 |
|---|---|
| `--play-list` | 必填，播放列表根目录 |
| `--sf2` | 钢琴 SoundFont 路径，默认从环境变量 `PIANO_SF2` 或当前目录查找 |
| `--shuffle` | 随机播放顺序，默认按目录名排序 |
| `--cache-dir` | 缓存目录，默认 `~/.cache/piano-tui` |
| `--no-loop` | 播完一轮后退出，默认无限循环 |

示例：

```bash
uv run piano-tui --play-list /tmp/myplaylist --sf2 ~/soundfonts/FluidR3_GM.sf2 --shuffle --no-loop
```

## 排查

- **`uv sync` 失败**：oemer 会拉取 onnxruntime / torch 等较重的依赖，对过新的 Python 版本支持可能滞后。先确认 Python 版本是 3.11（`uv python pin 3.11`），再重试 `uv sync`。
- **首次识别很慢**：oemer 首次运行需要下载模型（约 220MB，来自 GitHub Releases），属于正常现象；之后有缓存，秒级加载。
- **依赖版本已固定**：`onnxruntime-gpu==1.19.2`（1.20+ 拒绝加载 oemer 模型中的负数 pads）和 `opencv-python-headless<5`（OpenCV 5 改变了 `HoughLinesP` 返回形状，会触发 oemer 内部 IndexError），请勿随意升级。
- **无声音**：检查 SoundFont 是否指定正确、系统音频输出；Linux 下确认 `libportaudio2` 已安装。
- **识别质量差**：尽量提供清晰、分辨率较高的乐谱截图（宽度小于 2000px 时会自动放大）。

## 项目结构

```
piano-tui/
├── pyproject.toml       # 项目元数据与依赖（由 uv 管理）
├── uv.lock              # 锁文件
├── .python-version      # 固定 Python 3.11
├── src/piano_tui/
│   ├── __init__.py
│   ├── __main__.py      # 入口，参数解析与主循环
│   ├── pipeline.py      # 合并、OMR、MIDI、音频渲染、缓存
│   ├── visualizer.py    # 终端频谱可视化
│   └── controls.py      # 键盘输入
└── README.md
```
