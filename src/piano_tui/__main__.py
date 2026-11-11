"""入口：参数解析、环境检查、播放主循环与后台预取。"""

from __future__ import annotations

import argparse
import os
import queue
import random
import sys
import threading
import time
from pathlib import Path

from . import pipeline
from .controls import KeyListener
from .pipeline import Song

FRAME_INTERVAL = 1.0 / 30.0  # 可视化 30 FPS


# ---------------------------------------------------------------------------
# 参数与环境
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="piano-tui",
        description="把乐谱截图自动转成钢琴音频并播放，同时显示终端低音声浪动画",
    )
    parser.add_argument("--play-list", required=True, help="播放列表根目录")
    parser.add_argument(
        "--sf2",
        default=None,
        help="钢琴 SoundFont 路径 (默认取环境变量 PIANO_SF2 或当前目录)",
    )
    parser.add_argument(
        "--shuffle", action="store_true", help="随机播放顺序 (默认按目录名排序)"
    )
    parser.add_argument("--cache-dir", default="~/.cache/piano-tui", help="缓存目录")
    parser.add_argument(
        "--no-loop", action="store_true", help="播完一轮后退出 (默认无限循环)"
    )
    return parser.parse_args(argv)


def resolve_sf2(explicit: str | None) -> Path:
    if explicit:
        candidates = [Path(explicit).expanduser()]
    elif os.environ.get("PIANO_SF2"):
        candidates = [Path(os.environ["PIANO_SF2"]).expanduser()]
    else:
        candidates = sorted(Path.cwd().glob("*.sf2"))
    for c in candidates:
        if c.is_file():
            return c.resolve()
    sys.exit(
        "未找到钢琴 SoundFont (.sf2) 文件。\n"
        "  请用 --sf2 指定路径，或设置环境变量 PIANO_SF2，或把 .sf2 放到当前目录。\n"
        "  例如下载 FluidR3_GM / GeneralUser GS 后: piano-tui --sf2 ~/soundfonts/FluidR3_GM.sf2 ..."
    )


def check_environment(args: argparse.Namespace) -> Path:
    problems: list[str] = []
    if pipeline.oemer_command() is None:
        problems.append("未找到 oemer 命令，请先安装 Python 依赖: uv sync")
    if not pipeline.fluidsynth_available():
        problems.append(
            "未找到 FluidSynth，请安装系统依赖 "
            "(Ubuntu/Debian: sudo apt install fluidsynth; macOS: brew install fluid-synth)"
        )
    if problems:
        sys.exit("环境检查未通过:\n  " + "\n  ".join(problems))
    return resolve_sf2(args.sf2)


# ---------------------------------------------------------------------------
# 播放
# ---------------------------------------------------------------------------


class AudioPlayer:
    """基于 sounddevice 回调的 WAV 播放器，支持暂停并暴露播放进度。"""

    def __init__(self, data, samplerate: float):
        import sounddevice as sd

        self.data = data
        self.fs = samplerate
        self.pos = 0
        self.paused = False
        self._lock = threading.Lock()
        self.stream = sd.OutputStream(
            samplerate=samplerate,
            channels=data.shape[1],
            dtype="float32",
            callback=self._callback,
        )

    def _callback(self, outdata, frames, _time_info, _status):
        with self._lock:
            if self.paused:
                outdata.fill(0)
                return
            n = min(frames, len(self.data) - self.pos)
            if n > 0:
                outdata[:n] = self.data[self.pos : self.pos + n]
            if n < frames:
                outdata[n:] = 0
            self.pos += n

    def start(self) -> None:
        self.stream.start()

    def stop(self) -> None:
        self.stream.stop()
        self.stream.close()

    @property
    def position(self) -> int:
        return self.pos

    @property
    def finished(self) -> bool:
        return self.pos >= len(self.data)


def _handle_key(key: str, player: AudioPlayer) -> str | None:
    """处理按键，返回 'next' / 'prev' / 'quit' 或 None (继续播放)。"""
    if key == " ":
        player.paused = not player.paused
        return None
    if key in ("n", "N"):
        return "next"
    if key in ("p", "P"):
        return "prev"
    if key in ("q", "Q", "\x03", "\x04"):  # q / Ctrl+C / Ctrl+D
        return "quit"
    return None


def play_song(wav_path: Path, title: str, index: int, total: int) -> str:
    """播放一首曲子并驱动可视化，返回 'next' / 'prev' / 'quit'。"""
    import soundfile as sf

    from .visualizer import Visualizer

    data, fs = sf.read(str(wav_path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)

    player = AudioPlayer(data, fs)
    vis = Visualizer(fs)
    listener = KeyListener()

    player.start()
    listener.start()
    try:
        while True:
            t0 = time.monotonic()
            vis.draw(
                mono, player.position, len(mono), title, index, total, player.paused
            )
            if player.finished and not player.paused:
                return "next"
            # 排空当前积压按键
            while True:
                key = listener.poll(0)
                if key is None:
                    break
                action = _handle_key(key, player)
                if action:
                    return action
            # 睡满剩余帧时间，有按键则提前唤醒
            remaining = FRAME_INTERVAL - (time.monotonic() - t0)
            if remaining > 0:
                key = listener.poll(remaining)
                if key:
                    action = _handle_key(key, player)
                    if action:
                        return action
    finally:
        player.stop()
        listener.stop()
        sys.stdout.write("\x1b[?25h\x1b[0m\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# 后台预取
# ---------------------------------------------------------------------------


class Prefetcher:
    """单工作线程，提前处理下一首曲子；结果按曲子索引缓存。"""

    def __init__(self, songs: list[Song], cache_dir: Path, sf2_path: Path):
        self.songs = songs
        self.cache_dir = cache_dir
        self.sf2_path = sf2_path
        self.results: dict[int, Path] = {}
        self.errors: dict[int, BaseException] = {}
        self._lock = threading.Lock()
        self._inflight: set[int] = set()
        self._queue: queue.Queue[int] = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name="prefetch").start()

    def request(self, song_idx: int) -> None:
        with self._lock:
            if (
                song_idx in self.results
                or song_idx in self.errors
                or song_idx in self._inflight
            ):
                return
            self._inflight.add(song_idx)
        self._queue.put(song_idx)

    def get(self, song_idx: int, on_step=lambda label: None) -> Path:
        """取曲子的 WAV；命中缓存直接返回，否则前台处理。"""
        while True:
            with self._lock:
                if song_idx in self.results:
                    return self.results[song_idx]
                if song_idx in self.errors:
                    raise self.errors[song_idx]
                if song_idx not in self._inflight:
                    break
            time.sleep(0.1)  # 后台正在处理，稍等
        with self._lock:
            if song_idx in self.results or song_idx in self.errors:
                return self.get(song_idx, on_step)
            self._inflight.add(song_idx)
        try:
            wav = pipeline.ensure_ready(
                self.songs[song_idx], self.cache_dir, self.sf2_path, on_step
            )
            with self._lock:
                self.results[song_idx] = wav
            return wav
        except Exception as e:
            with self._lock:
                self.errors[song_idx] = e
            raise
        finally:
            with self._lock:
                self._inflight.discard(song_idx)

    def _worker(self) -> None:
        while True:
            song_idx = self._queue.get()
            try:
                wav = pipeline.ensure_ready(
                    self.songs[song_idx],
                    self.cache_dir,
                    self.sf2_path,
                    lambda label: None,
                )
                with self._lock:
                    self.results[song_idx] = wav
            except Exception as e:
                with self._lock:
                    self.errors[song_idx] = e
            finally:
                with self._lock:
                    self._inflight.discard(song_idx)
                self._queue.task_done()


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def _make_step_printer(pos: int, total: int, name: str):
    def on_step(label: str) -> None:
        print(f"[{pos}/{total}] 《{name}》 {label} ...")

    return on_step


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.play_list).expanduser()
    if not root.is_dir():
        sys.exit(f"播放列表目录不存在: {root}")

    sf2_path = check_environment(args)
    cache_dir = Path(args.cache_dir).expanduser()

    songs = pipeline.discover_songs(root)
    if not songs:
        sys.exit(
            "播放列表中没有可播放的曲子 (需要根目录下的每个子目录里包含 png/jpg 图片)"
        )

    n = len(songs)
    order = list(range(n))
    if args.shuffle:
        random.shuffle(order)
    print(f"共发现 {n} 首曲子: " + "、".join(songs[i].name for i in order))

    prefetcher = Prefetcher(songs, cache_dir, sf2_path)
    failures: set[int] = set()
    idx = 0

    try:
        while True:
            song_idx = order[idx]
            song = songs[song_idx]
            try:
                wav = prefetcher.get(
                    song_idx, on_step=_make_step_printer(idx + 1, n, song.name)
                )
            except Exception as e:
                print(
                    f"[{idx + 1}/{n}] 《{song.name}》 处理失败，跳过: {e}",
                    file=sys.stderr,
                )
                failures.add(song_idx)
                if len(failures) == n:
                    print("所有曲子均处理失败，退出。", file=sys.stderr)
                    return 1
                idx = _advance(idx, +1, n, args.no_loop)
                if idx is None:
                    return 0
                continue
            failures.discard(song_idx)

            # 播放当前曲子时，后台预取下一首（no-loop 的最后一首无需预取）
            if not (args.no_loop and idx == n - 1):
                nxt = _advance(idx, +1, n, no_loop=False)
                prefetcher.request(order[nxt])

            try:
                action = play_song(wav, song.name, idx + 1, n)
            except Exception as e:
                print(
                    f"[{idx + 1}/{n}] 《{song.name}》 播放失败，跳过: {e}",
                    file=sys.stderr,
                )
                failures.add(song_idx)
                if len(failures) == n:
                    print("所有曲子均无法播放，退出。", file=sys.stderr)
                    return 1
                action = "next"

            if action == "quit":
                return 0
            if action == "prev":
                idx = (idx - 1) % n  # 上一首总是回绕
            else:  # next
                idx = _advance(idx, +1, n, args.no_loop)
                if idx is None:
                    print("播放完毕。")
                    return 0
    except KeyboardInterrupt:
        return 0


def _advance(idx: int, delta: int, n: int, no_loop: bool) -> int | None:
    """前进一步；no_loop 时越界返回 None 表示应退出。"""
    ni = idx + delta
    if ni >= n:
        return None if no_loop else 0
    if ni < 0:
        return n - 1
    return ni


if __name__ == "__main__":
    sys.exit(main())
