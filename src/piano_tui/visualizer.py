"""终端频谱可视化：低音声浪动画。

- 上半部分: 对数频率分布 (20Hz–16kHz) 的多行柱状图，使用块字符 ▁▂▃▄▅▆▇█
- 下面一行 BASS 能量条: 20–150Hz 实时强度
- FFT 窗口 2048 + hanning 窗，按播放位置取音频片段，保证音画同步
- 自动增益 (峰值缓慢衰减) + 指数平滑，避免画面抖动
- 柱数量随终端宽度自适应 (SIGWINCH)
"""

from __future__ import annotations

import shutil
import signal
import sys
import unicodedata

import numpy as np

BLOCKS = " ▁▂▃▄▅▆▇█"
F_MIN, F_MAX = 20.0, 16000.0
BASS_MIN, BASS_MAX = 20.0, 150.0
FFT_WIN = 2048

# 柱状图配色: 顶部红 → 底部绿 (256 色)
_ROW_COLORS = (196, 208, 220, 118, 47)


def _disp_len(s: str) -> int:
    """考虑中日韩宽字符的显示宽度。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _truncate(s: str, width: int) -> str:
    out: list[str] = []
    w = 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw > width:
            break
        out.append(c)
        w += cw
    return "".join(out)


def _fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


class Visualizer:
    def __init__(self, samplerate: float):
        self.fs = float(samplerate)
        self._window = np.hanning(FFT_WIN)
        self._freqs = np.fft.rfftfreq(FFT_WIN, d=1.0 / self.fs)
        self._peak_gain = 1e-9  # 自动增益基准，缓慢衰减
        self._bass_gain = 1e-9
        self._resized = True
        self._cleared = False
        try:
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (AttributeError, OSError, ValueError):
            pass  # 非 Unix 或非主线程
        self._update_size()

    # -- 尺寸 ---------------------------------------------------------------

    def _on_resize(self, *_args) -> None:
        self._resized = True

    def _update_size(self) -> None:
        cols, lines = shutil.get_terminal_size(fallback=(80, 24))
        self.cols = max(20, cols)
        self.bar_rows = max(3, min(lines - 5, 20))
        self.n_bars = max(8, self.cols - 2)
        self._edges = np.geomspace(F_MIN, F_MAX, self.n_bars + 1)
        self._smooth = np.zeros(self.n_bars)
        self._bass_smooth = 0.0
        self._resized = False

    # -- 频谱 ---------------------------------------------------------------

    def _spectrum(self, mono: np.ndarray, pos: int) -> np.ndarray:
        seg = mono[pos : pos + FFT_WIN]
        if seg.size == 0:
            return np.zeros(FFT_WIN // 2 + 1)
        if seg.size < FFT_WIN:
            seg = np.pad(seg, (0, FFT_WIN - seg.size))
        return np.abs(np.fft.rfft(seg * self._window))

    # -- 绘制 ---------------------------------------------------------------

    def draw(
        self,
        mono: np.ndarray,
        pos: int,
        total_samples: int,
        title: str,
        index: int,
        count: int,
        paused: bool = False,
    ) -> None:
        if not sys.stdout.isatty():
            return
        if self._resized:
            self._update_size()
            self._cleared = False

        spec = self._spectrum(mono, min(pos, max(0, len(mono) - 1)))

        # 对数频率柱: 把 FFT 频段聚合到几何分布的频带
        bin_idx = np.searchsorted(self._edges, self._freqs, side="right") - 1
        valid = (bin_idx >= 0) & (bin_idx < self.n_bars)
        idx = bin_idx[valid]
        sums = np.bincount(idx, weights=spec[valid], minlength=self.n_bars)
        cnts = np.bincount(idx, minlength=self.n_bars)
        vals = sums / np.maximum(cnts, 1)

        # 自动增益: 峰值立即抬升、缓慢衰减
        peak = float(vals.max()) if vals.size else 0.0
        self._peak_gain = max(self._peak_gain * 0.999, peak, 1e-9)
        norm = np.clip(vals / self._peak_gain, 0.0, 1.0)
        self._smooth = self._smooth * 0.65 + norm * 0.35

        # BASS: 20–150Hz 平均强度
        bass_mask = (self._freqs >= BASS_MIN) & (self._freqs <= BASS_MAX)
        bass = float(spec[bass_mask].mean()) if bass_mask.any() else 0.0
        self._bass_gain = max(self._bass_gain * 0.999, bass, 1e-9)
        self._bass_smooth = (
            self._bass_smooth * 0.7 + min(bass / self._bass_gain, 1.0) * 0.3
        )

        cur = pos / self.fs
        tot = total_samples / self.fs
        lines = [
            self._header(cur, tot, title, index, count, paused),
            self._progress_bar(cur / tot if tot > 0 else 0.0),
            *self._bar_rows(),
            self._bass_row(),
            "空格:暂停  n:下一首  p:上一首  q:退出",
        ]

        prefix = "\x1b[2J" if not self._cleared else "\x1b[H"
        sys.stdout.write(prefix + "\n".join(lines) + "\x1b[0J\x1b[?25l")
        sys.stdout.flush()
        self._cleared = True

    def _header(
        self, cur: float, tot: float, title: str, index: int, count: int, paused: bool
    ) -> str:
        left = f"♪ {title}  [{index}/{count}]" + ("  [暂停]" if paused else "")
        right = f"{_fmt_time(cur)} / {_fmt_time(tot)}"
        left = _truncate(left, self.cols - _disp_len(right) - 2)
        pad = max(self.cols - _disp_len(left) - _disp_len(right), 1)
        return left + " " * pad + right

    def _progress_bar(self, ratio: float) -> str:
        w = self.cols - 2
        filled = round(max(0.0, min(1.0, ratio)) * w)
        return "[" + "█" * filled + "░" * (w - filled) + "]"

    def _bar_rows(self) -> list[str]:
        rows: list[str] = []
        r_total = self.bar_rows
        for r in range(r_total):
            need = r_total - r  # 填满该行所需的归一化高度
            color = _ROW_COLORS[
                min(len(_ROW_COLORS) - 1, r * len(_ROW_COLORS) // r_total)
            ]
            chars = []
            for v in self._smooth:
                h = v * r_total
                if h >= need:
                    chars.append("█")
                elif h > need - 1:
                    chars.append(BLOCKS[min(7, int((h - (need - 1)) * 8))])
                else:
                    chars.append(" ")
            rows.append(f"\x1b[38;5;{color}m" + "".join(chars) + "\x1b[0m")
        return rows

    def _bass_row(self) -> str:
        w = self.cols - 8
        filled = int(self._bass_smooth * w)
        return "BASS [" + "█" * filled + " " * (w - filled) + "]"
