# 任务：开发基于终端的钢琴乐谱播放器 (piano-tui)

请用 Python 3.10+ 实现一个命令行工具，把乐谱截图自动转成钢琴音频并播放，同时在终端里实时显示低音声浪（频谱）动画。

## 1. 命令行接口

```bash
python piano_player.py --play-list /tmp/myplaylist [--sf2 piano.sf2] [--shuffle] [--cache-dir ~/.cache/piano-tui] [--no-loop]
```

| 参数 | 说明 |
|---|---|
| `--play-list` | 必填，播放列表根目录 |
| `--sf2` | 钢琴 SoundFont 路径，默认从环境变量 `PIANO_SF2` 或当前目录查找 |
| `--shuffle` | 随机播放顺序，默认按目录名排序 |
| `--cache-dir` | 缓存目录，默认 `~/.cache/piano-tui` |
| `--no-loop` | 播完一轮后退出，默认无限循环 |

## 2. 播放列表目录结构

```
/tmp/myplaylist/
├── 安静/
│   ├── 乐谱1.png
│   ├── 乐谱2.png
│   └── 乐谱3.png
├── 欢快/
│   ├── 1.png
│   └── 2.png
```

- 根目录下**每个子目录 = 一首曲子**，目录名作为曲名
- 子目录内的图片（png/jpg/jpeg）按**文件名自然排序**（1, 2, 10 而不是 1, 10, 2）后纵向拼接，从上到下对应乐谱顺序
- 播放器依次遍历所有子目录，默认循环播放
- 跳过空目录和无图片的目录，并给出提示

## 3. 单曲处理流水线

对每首曲子执行：

**图片 → 合并 → MusicXML → MIDI → 钢琴音频 → 播放**

1. **合并图片（Pillow）**
   - 统一宽度（缩放到最宽图片的宽度，LANCZOS），白底画布纵向拼接
   - 转灰度，保存为 `merge.png`
   - 如果宽度小于 2000px，等比放大到 2000px 以提高识别率
2. **OMR（oemer）**
   - 用 `subprocess` 调用 `oemer merge.png -o <work_dir>`
   - 输出重命名为 `play.musicxml`
   - 首次运行会下载模型，需要提示用户"首次识别较慢"
3. **MusicXML → MIDI（music21）**
   - `converter.parse` 后写出 `play.mid`
4. **MIDI → 音频（pretty_midi + pyfluidsynth）**
   - `pm.fluidsynth(fs=44100, sf2_path=...)` 渲染，`soundfile` 写出 `play.wav`
5. **播放**：`sounddevice` 播放 `play.wav`，同时驱动可视化

## 4. 缓存机制（重要）

OMR 很慢，必须缓存：

- 每首曲子的工作目录：`<cache-dir>/<曲名>-<hash>/`，其中 hash 由该目录下所有图片的 **文件名 + 修改时间 + 大小** 计算
- 工作目录里保存 `merge.png`、`play.musicxml`、`play.mid`、`play.wav`
- 如果 `play.wav` 已存在且 hash 未变，**直接跳过所有转换步骤**
- 图片改动后 hash 变化，自动重新处理
- 每一步独立缓存，某步失败后重跑时从失败的那步继续

## 5. 终端可视化（低音声浪）

播放时在终端实时绘制：

- 上半部分：对数频率分布的多行柱状图（20Hz–16kHz），左低右高，使用字符 ` ▁▂▃▄▅▆▇█`
- 下面一行 `BASS` 能量条：20–150Hz 的实时强度
- 使用 FFT（窗口 2048，hanning 窗），30 FPS，根据播放时间戳取对应音频片段，保证画面与声音同步
- 自动增益（峰值缓慢衰减），并做指数平滑让动画不抖动
- 柱数量根据终端宽度自适应，监听终端尺寸变化
- 画面顶部显示：当前曲名、序号（如 `2/5`）、进度条和时间 `01:23 / 03:45`
- 使用 ANSI 转义码清屏和隐藏光标，退出时必须恢复光标

## 6. 键盘控制

用非阻塞方式读取按键（Unix 用 `termios`+`select`，Windows 用 `msvcrt`）：

| 按键 | 功能 |
|---|---|
| `space` | 暂停 / 继续 |
| `n` | 下一首 |
| `p` | 上一首 |
| `q` / `Ctrl+C` | 退出 |

## 7. 后台预处理

- 当前曲子播放时，用后台线程提前处理**下一首**（OMR 慢，避免播放间隙）
- 第一首处理时显示进度提示，如 `[2/4] 正在识别乐谱 (oemer)...`，用 rich 或简单的 spinner 均可

## 8. 错误处理

- 某首曲子任一步骤失败：打印原因，跳过该曲继续下一首，不要整个程序退出
- 启动时检查依赖：`oemer` 命令是否存在、FluidSynth 是否可用、`.sf2` 文件是否存在，缺失时给出明确的安装提示
- 所有曲子都失败时给出友好退出信息

## 9. 项目结构与交付物

```
piano-tui/
├── pyproject.toml       # 项目元数据与依赖（由 uv 管理）
├── uv.lock              # 锁文件，需提交到仓库
├── .python-version      # 固定 Python 版本，如 3.11
├── src/piano_tui/
│   ├── __init__.py
│   ├── __main__.py      # 入口，参数解析与主循环
│   ├── pipeline.py      # 合并、OMR、MIDI、音频渲染、缓存
│   ├── visualizer.py    # 终端频谱可视化
│   └── controls.py      # 键盘输入
└── README.md            # 安装说明（含 uv、FluidSynth、SoundFont）、使用示例
```

### 依赖管理（uv）

- 使用 **uv** 管理依赖和虚拟环境，**不要**生成 `requirements.txt`
- `pyproject.toml` 使用 `[project]` 表，`requires-python = ">=3.10,<3.13"`（oemer / onnxruntime 对过新的 Python 版本支持可能滞后，建议固定 3.11）
- 运行时依赖：`pillow numpy sounddevice soundfile oemer music21 pretty_midi pyfluidsynth rich`
- 开发依赖放在 `[dependency-groups] dev`（如 `pytest`、`ruff`），用 `uv add --dev` 添加
- 在 `pyproject.toml` 里配置入口脚本：
  ```toml
  [project.scripts]
  piano-tui = "piano_tui.__main__:main"
  ```
- 初始化与安装流程写进 README：
  ```bash
  uv init --package piano-tui
  uv python pin 3.11
  uv add pillow numpy sounddevice soundfile oemer music21 pretty_midi pyfluidsynth rich
  uv add --dev pytest ruff
  uv sync
  ```
- 运行方式统一为：
  ```bash
  uv run piano-tui --play-list /tmp/myplaylist
  # 或不安装直接运行
  uv run python -m piano_tui --play-list /tmp/myplaylist
  ```
- 系统级依赖（FluidSynth、PortAudio）无法由 uv 安装，README 中单独说明：
  - macOS：`brew install fluid-synth portaudio`
  - Ubuntu/Debian：`sudo apt install fluidsynth libportaudio2 libsndfile1`
- oemer 依赖会拉取 onnxruntime，如果 `uv sync` 失败，README 里给出排查提示（先确认 Python 版本是 3.11）