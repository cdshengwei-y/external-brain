"""实时转写屏幕帧：把会话文本渲染成 240x240 RGB565 位图。

桌宠屏幕（TFT_eSPI 240x240）没有中文字库，改由中枢渲染好整帧位图、
设备端收到后直接整块贴屏（agent_gateway 侧用 PIL + 系统中文字体）。
字节序为大端（高字节在前）：ESP32 端 uint8 缓冲强转 uint16_t 后由
pushImage 再做一次字节交换，落到 LCD 正好是它要的大端 RGB565。
"""

import math
import struct
import threading
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

FRAME_WIDTH = 240
FRAME_HEIGHT = 240
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 2

# 用户目录的 Noto Sans SC 优先（现代黑体），退回系统 STHeiti
_FONT_CANDIDATES = (
    str(Path.home() / "Library/Fonts/notosanssc-regular.ttf"),
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)

_BG = (10, 16, 34)
_TITLE_COLOR = (110, 150, 230)
_TEXT_DIM = (150, 165, 200)
_TEXT_BRIGHT = (240, 246, 255)
_EXIT_COLOR = (255, 190, 90)

_font_cache: dict = {}
_lock = threading.Lock()


def _get_font(size: int):
    with _lock:
        if size in _font_cache:
            return _font_cache[size]
        for path in _FONT_CANDIDATES:
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[size] = font
                return font
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    """按像素宽度折行（中文没有空格，逐字累积）。"""
    lines: List[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if draw.textlength(candidate, font=font) > max_width and current:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render_live_frame(lines: List[str], note: Optional[str] = None) -> bytes:
    """会话文本 → 240x240 RGB565 大端字节串。

    lines: 已带时间戳的文本行（新的在尾部）；只保留最后 6 行，底部对齐。
    note:  无文本时的提示语（如「说话吧…」），显示在最后一行位置。
    """
    image = Image.new("RGB", (FRAME_WIDTH, FRAME_HEIGHT), _BG)
    draw = ImageDraw.Draw(image)
    title_font = _get_font(17)
    text_font = _get_font(24)

    draw.text((10, 6), "● 实时转写", font=title_font, fill=_TITLE_COLOR)
    draw.line((8, 30, FRAME_WIDTH - 8, 30), fill=(40, 58, 96), width=1)

    show: List[str] = []
    for line in lines[-6:]:
        show.extend(_wrap(draw, line, text_font, FRAME_WIDTH - 20))
    show = show[-6:]

    line_height = 32
    if show:
        start_y = FRAME_HEIGHT - 10 - len(show) * line_height
        for index, line in enumerate(show):
            is_last = index == len(show) - 1
            draw.text(
                (10, start_y + index * line_height),
                line,
                font=text_font,
                fill=_TEXT_BRIGHT if is_last else _TEXT_DIM,
            )
    elif note:
        draw.text((10, FRAME_HEIGHT - 44), note, font=text_font, fill=_TEXT_DIM)

    # RGB888 → RGB565 大端
    raw = image.convert("RGB").tobytes()
    out = bytearray(FRAME_BYTES)
    for index in range(FRAME_WIDTH * FRAME_HEIGHT):
        r = raw[index * 3]
        g = raw[index * 3 + 1]
        b = raw[index * 3 + 2]
        value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        struct.pack_into(">H", out, index * 2, value)
    return bytes(out)
