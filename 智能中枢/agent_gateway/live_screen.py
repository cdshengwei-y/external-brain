"""实时转写屏幕帧：把会话文本渲染成 240x240 RGB565 位图。

桌宠屏幕（TFT_eSPI 240x240）没有中文字库，改由中枢渲染好整帧位图、
设备端收到后直接整块贴屏（agent_gateway 侧用 PIL + 系统中文字体）。
字节序为大端（高字节在前）：ESP32 端 uint8 缓冲强转 uint16_t 后由
pushImage 再做一次字节交换，落到 LCD 正好是它要的大端 RGB565。

密度规格（2026-11-11 用户定稿）：15px 字号 × 15px 行高，整屏 16 列 ×
16 行 ≈ 16*16 个字。第 i 个显示段固定写在 i % 16 行——写满整屏后
回卷到顶行覆盖最旧的行，即「说满了多出来的文字又从头开始」。
"""

import struct
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

FRAME_WIDTH = 240
FRAME_HEIGHT = 240
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 2

_FONT_SIZE = 15          # 240 / 15 = 16 列
_LINE_HEIGHT = 15        # 240 / 15 = 16 行
_ROWS = FRAME_HEIGHT // _LINE_HEIGHT
_MAX_WIDTH = FRAME_WIDTH  # 顶格 16 个字
# 实测（Noto Sans SC size 15，anchor=ls）：中文墨迹在基线上 13px、下 2px，
# 合计正好 15px。基线定在 行顶+13，墨迹不偏不倚落进自己的 15px 行格，
# 上下行零重叠（默认 ascender 锚点会溢出 7px，行行粘连）。
_BASELINE_OFFSET = 13
_SEGMENTS_KEEP = _ROWS * 6  # 缓冲最多存 6 屏的段

# 用户目录的 Noto Sans SC 优先（现代黑体），退回系统 STHeiti
_FONT_CANDIDATES = (
    str(Path.home() / "Library/Fonts/notosanssc-regular.ttf"),
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)

_BG = (8, 14, 32)
_TEXT_DIM = (150, 165, 200)
_TEXT_BRIGHT = (245, 248, 255)
_NOTE_COLOR = (120, 150, 220)

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


def wrap_text(text: str) -> List[str]:
    """一句转写文本 → 若干 ≤16 字的显示段（逐字累积按像素宽度折行）。"""
    font = _get_font(_FONT_SIZE)
    measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    segments: List[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and measure.textlength(candidate, font=font) > _MAX_WIDTH:
            segments.append(current)
            current = char
        else:
            current = candidate
    if current:
        segments.append(current)
    return segments


def render_live_frame(
    segments: List[Tuple[int, str]], note: Optional[str] = None
) -> bytes:
    """有序显示段 → 240x240 RGB565 大端字节串。

    segments: [(绝对序号, 文本), …]；序号 i 的段写在第 i % 16 行，
    满屏后回卷覆盖最旧的行。最新一段高亮，其余暗色。
    note: 屏幕为空时的提示语，写在顶行。
    """
    image = Image.new("RGB", (FRAME_WIDTH, FRAME_HEIGHT), _BG)
    draw = ImageDraw.Draw(image)
    font = _get_font(_FONT_SIZE)

    if segments:
        newest_index = segments[-1][0]
        # 只画「当前在屏」的最后 16 段：更早的段已被回卷覆盖，画出来会和
        # 新段叠在同一行（回卷位置一行两段 = 上下两层文字糊在一起）。
        for index, text in segments[-_ROWS:]:
            row = index % _ROWS
            color = _TEXT_BRIGHT if index == newest_index else _TEXT_DIM
            draw.text(
                (0, row * _LINE_HEIGHT + _BASELINE_OFFSET),
                text,
                font=font,
                fill=color,
                anchor="ls",
            )
    elif note:
        draw.text((0, _BASELINE_OFFSET), note, font=font, fill=_NOTE_COLOR, anchor="ls")

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
