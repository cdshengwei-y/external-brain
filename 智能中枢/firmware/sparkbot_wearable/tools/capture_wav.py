#!/usr/bin/env python3

import argparse
import re
import time
from pathlib import Path

import serial


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture a WAV file from ESP-SparkBot")
    parser.add_argument("--port", default="/dev/cu.usbmodem13101")
    parser.add_argument("--output", default="sparkbot_capture.wav")
    args = parser.parse_args()

    with serial.Serial(args.port, 115200, timeout=2) as device:
        time.sleep(0.2)
        device.reset_input_buffer()
        device.write(b"w\n")
        device.flush()

        header_line = b""
        while True:
            line = device.readline()
            if not line:
                raise TimeoutError("没有收到 WAV_BEGIN，检查设备是否仍连接并运行 audio_capture 固件")
            if line.startswith(b"WAV_BEGIN "):
                header_line = line
                break

        match = re.search(rb"bytes=(\d+)", header_line)
        if match is None:
            raise RuntimeError(f"WAV_BEGIN 格式异常: {header_line!r}")
        expected_bytes = int(match.group(1))
        data = bytearray()
        while len(data) < expected_bytes:
            chunk = device.read(expected_bytes - len(data))
            if not chunk:
                raise TimeoutError(f"WAV 数据不完整: {len(data)}/{expected_bytes} 字节")
            data.extend(chunk)

        while True:
            end_line = device.readline()
            if not end_line:
                raise TimeoutError("WAV 数据后未收到 WAV_END")
            if b"WAV_END" in end_line:
                break

    output = Path(args.output)
    output.write_bytes(data)
    print(f"已保存 {output} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
