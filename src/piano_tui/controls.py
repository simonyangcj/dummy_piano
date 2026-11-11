"""非阻塞键盘输入: Unix 用 termios + select, Windows 用 msvcrt。"""

from __future__ import annotations

import sys

try:  # Unix
    import select
    import termios
    import tty

    _UNIX = True
except ImportError:  # Windows
    import msvcrt  # type: ignore

    _UNIX = False


class KeyListener:
    """以 cbreak 模式读取单个按键，poll() 非阻塞 (或带超时) 返回。"""

    def __init__(self) -> None:
        self._fd: int | None = None
        self._orig: list | None = None

    def start(self) -> None:
        if _UNIX and sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._orig = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)

    def stop(self) -> None:
        if self._orig is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._orig)
            self._orig = None
            self._fd = None

    def poll(self, timeout: float = 0.0) -> str | None:
        """返回一个按键字符；无输入时返回 None。timeout 单位为秒。"""
        if _UNIX:
            if not sys.stdin.isatty():
                return None
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
            ch = sys.stdin.read(1)
            return ch if ch else None
        # Windows
        if timeout > 0:
            import time

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.01)
            return None
        if msvcrt.kbhit():
            return msvcrt.getwch()
        return None
