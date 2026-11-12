"""独立转换命令：把目录下的乐谱图片预先转换为 play.wav，播放时免等待。

用法:
    piano-convert <目录> [--sf2 路径] [--cache-dir 缓存目录]

目录可以是单曲目录（内含图片），也可以是播放列表根目录（每个含图片的
子目录一首曲子），两种情况都会被处理。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import pipeline
from .pipeline import IMAGE_EXTS, Song, natural_key


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="piano-convert",
        description="把目录下的乐谱图片预先转换为 play.wav (播放前批量转换，免等待)",
    )
    parser.add_argument("directory", help="单曲目录或播放列表根目录")
    parser.add_argument(
        "--sf2",
        default=None,
        help="钢琴 SoundFont 路径 (默认取环境变量 PIANO_SF2 或当前目录)",
    )
    parser.add_argument("--cache-dir", default="~/.cache/piano-tui", help="缓存目录")
    return parser.parse_args(argv)


def collect_songs(root: Path) -> list[Song]:
    """收集要转换的曲子：根目录自身的图片 + 各含图片的子目录。"""
    songs: list[Song] = []

    own_imgs = sorted(
        (p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=lambda p: natural_key(p.name),
    )
    if own_imgs:
        songs.append(Song(name=root.name, images=own_imgs))

    songs.extend(pipeline.discover_songs(root))
    return songs


def main(argv: list[str] | None = None) -> int:
    from .__main__ import check_environment

    args = parse_args(argv)
    root = Path(args.directory).expanduser()
    if not root.is_dir():
        sys.exit(f"目录不存在: {root}")

    sf2_path = check_environment(args)
    cache_dir = Path(args.cache_dir).expanduser()

    songs = collect_songs(root)
    if not songs:
        sys.exit(f"目录中没有找到 png/jpg 图片: {root}")

    n = len(songs)
    print(f"共 {n} 首曲子待转换: " + "、".join(s.name for s in songs))

    failed: list[str] = []
    for i, song in enumerate(songs, 1):
        def on_step(label: str, _i=i, _name=song.name) -> None:
            print(f"[{_i}/{n}] 《{_name}》 {label} ...")

        try:
            wav = pipeline.ensure_ready(song, cache_dir, sf2_path, on_step)
            print(f"[{i}/{n}] 《{song.name}》 完成 → {wav}")
        except Exception as e:
            failed.append(song.name)
            print(f"[{i}/{n}] 《{song.name}》 转换失败: {e}", file=sys.stderr)

    if failed:
        print(f"\n{len(failed)} 首转换失败: " + "、".join(failed), file=sys.stderr)
        return 1
    print("\n全部转换完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
