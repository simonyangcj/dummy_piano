"""单曲处理流水线：图片合并 → OMR → MusicXML → MIDI → 音频渲染，以及缓存管理。

每个步骤的产物保存在按内容哈希命名的工作目录中，某一步失败后重跑时
从失败的那一步继续（通过检查产物文件是否已存在实现）。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
SAMPLE_RATE = 44100

_oemer_cmd: list[str] | None | False = False  # False = 未探测


@dataclass
class Song:
    """一首曲子：目录名作为曲名，images 为自然排序后的乐谱图片。"""

    name: str
    images: list[Path]

    @property
    def directory(self) -> Path:
        """曲谱所在目录。"""
        return self.images[0].parent


def natural_key(name: str) -> list:
    """自然排序键：使 1, 2, 10 排在正确顺序。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def discover_songs(root: Path) -> list[Song]:
    """扫描播放列表根目录，每个含图片的子目录视为一首曲子。"""
    songs: list[Song] = []
    for entry in sorted(root.iterdir(), key=lambda p: natural_key(p.name)):
        if not entry.is_dir():
            continue
        imgs = sorted(
            (
                p
                for p in entry.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            ),
            key=lambda p: natural_key(p.name),
        )
        if not imgs:
            print(f"提示: 跳过目录 “{entry.name}” (没有图片)")
            continue
        songs.append(Song(name=entry.name, images=imgs))
    return songs


def compute_hash(song: Song) -> str:
    """由所有图片的 文件名 + 修改时间 + 大小 计算缓存哈希。"""
    h = hashlib.sha256()
    h.update(song.name.encode())
    for p in song.images:
        st = p.stat()
        h.update(f"{p.name}|{st.st_mtime_ns}|{st.st_size}".encode())
    return h.hexdigest()[:12]


def work_dir_for(cache_dir: Path, song: Song) -> Path:
    return cache_dir / f"{song.name}-{compute_hash(song)}"


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------


def oemer_command() -> list[str] | None:
    """返回调用 oemer 的命令行，找不到则返回 None。"""
    global _oemer_cmd
    if _oemer_cmd is False:
        exe = shutil.which("oemer")
        if exe:
            _oemer_cmd = [exe]
        else:
            try:
                from importlib.util import find_spec

                if (
                    find_spec("oemer") is not None
                    and find_spec("oemer.__main__") is not None
                ):
                    _oemer_cmd = [sys.executable, "-m", "oemer"]
                else:
                    _oemer_cmd = None
            except ImportError:
                _oemer_cmd = None
    return _oemer_cmd


def fluidsynth_available() -> bool:
    """检查 FluidSynth 库（pyfluidsynth 的底层依赖）是否可用。"""
    import ctypes.util

    return (
        ctypes.util.find_library("fluidsynth") is not None
        or shutil.which("fluidsynth") is not None
    )


# ---------------------------------------------------------------------------
# 各处理步骤
# ---------------------------------------------------------------------------


def merge_images(song: Song, out_png: Path) -> None:
    """统一宽度后纵向拼接，转灰度；宽度不足 2000px 时等比放大。"""
    from PIL import Image

    imgs = [Image.open(p).convert("RGB") for p in song.images]
    target_w = max(im.width for im in imgs)
    scaled: list = []
    total_h = 0
    for im in imgs:
        if im.width != target_w:
            im = im.resize(
                (target_w, max(1, round(im.height * target_w / im.width))),
                Image.LANCZOS,
            )
        scaled.append(im)
        total_h += im.height

    canvas = Image.new("RGB", (target_w, total_h), "white")
    y = 0
    for im in scaled:
        canvas.paste(im, (0, y))
        y += im.height

    if canvas.width < 2000:
        ratio = 2000 / canvas.width
        canvas = canvas.resize((2000, round(canvas.height * ratio)), Image.LANCZOS)

    canvas.convert("L").save(out_png)


def run_oemer(merge_png: Path, work: Path) -> Path:
    """调用 oemer 识别乐谱，输出统一重命名为 play.musicxml。"""
    cmd = oemer_command()
    if cmd is None:
        raise RuntimeError("找不到 oemer 命令")
    proc = subprocess.run(
        cmd + [str(merge_png), "-o", str(work)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"oemer 识别失败 (exit={proc.returncode}): {detail}")

    candidates = sorted(
        p for p in work.rglob("*.musicxml") if p.name != "play.musicxml"
    )
    if not candidates:
        raise RuntimeError("oemer 未输出 .musicxml 文件")
    target = work / "play.musicxml"
    if target.exists():
        target.unlink()
    shutil.move(str(candidates[0]), target)
    return target


def musicxml_to_midi(musicxml: Path, midi_path: Path) -> None:
    from music21 import converter

    score = converter.parse(str(musicxml))
    score.write("midi", fp=str(midi_path))


def midi_to_wav(midi_path: Path, wav_path: Path, sf2_path: Path) -> None:
    import pretty_midi
    import soundfile as sf

    pm = pretty_midi.PrettyMIDI(str(midi_path))
    audio = pm.fluidsynth(fs=SAMPLE_RATE, sf2_path=str(sf2_path))
    if audio.ndim == 1:
        audio = audio[:, None]
    sf.write(str(wav_path), audio, SAMPLE_RATE)


# ---------------------------------------------------------------------------
# 缓存流水线
# ---------------------------------------------------------------------------


def ensure_ready(
    song: Song,
    cache_dir: Path,
    sf2_path: Path,
    on_step: Callable[[str], None] = lambda label: None,
) -> Path:
    """确保该曲子的 play.wav 已生成，返回其路径。

    - 曲谱目录下已有 play.wav 时直接使用，跳过全部转换；
      （play.hash 记录生成时的图片哈希，图片改动后自动重新处理）
    - 每一步先检查缓存产物是否存在，存在即跳过 —— 某步失败重跑时
      从失败的那步继续。
    """
    song_wav = song.directory / "play.wav"
    song_hash = song.directory / "play.hash"
    current_hash = compute_hash(song)

    # 曲谱目录已存在 play.wav: 无哈希记录(用户自行放置)或哈希一致 → 直接播放
    if song_wav.is_file():
        stored = (
            song_hash.read_text(encoding="utf-8").strip()
            if song_hash.is_file()
            else None
        )
        if stored is None or stored == current_hash:
            return song_wav
        # 哈希不一致: 图片已改动，重新处理并覆盖

    cache_dir.mkdir(parents=True, exist_ok=True)
    work = work_dir_for(cache_dir, song)
    work.mkdir(parents=True, exist_ok=True)

    merge_png = work / "merge.png"
    musicxml = work / "play.musicxml"
    midi_path = work / "play.mid"
    wav_path = work / "play.wav"

    if wav_path.exists():
        result = wav_path
    else:
        if not merge_png.exists():
            on_step("合并乐谱图片")
            merge_images(song, merge_png)

        if not musicxml.exists():
            on_step("正在识别乐谱 (oemer，首次运行需下载模型，耗时较长)")
            run_oemer(merge_png, work)

        if not midi_path.exists():
            on_step("转换 MIDI (music21)")
            musicxml_to_midi(musicxml, midi_path)

        if not wav_path.exists():
            on_step("渲染钢琴音频 (FluidSynth)")
            midi_to_wav(midi_path, wav_path, sf2_path)

        result = wav_path

    # 把 wav 放到曲谱同层目录，下次播放免转换
    try:
        shutil.copyfile(result, song_wav)
        song_hash.write_text(current_hash, encoding="utf-8")
        return song_wav
    except OSError:
        # 曲谱目录不可写时仍使用缓存目录里的 wav
        return result
