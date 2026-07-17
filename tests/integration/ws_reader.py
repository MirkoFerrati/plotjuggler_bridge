#!/usr/bin/env python3
"""
WebSocket reader for the pj_bridge smoke test.

Connects to a running pj-bridge snap on ws://localhost:9090, subscribes to
/hello_world, receives binary frames from the bridge, ZSTD-decompresses them,
parses the CDR-serialized std_msgs/String payload, and asserts the data
contains "hello".

Protocol reference: plotjuggler_bridge/docs/API.md

Binary frame layout (16-byte header, little-endian):
    [0:4]  magic  = 0x42524A50  ("PJRB")
    [4:8]  message_count
    [8:12] uncompressed_size
    [12:16] flags (reserved, 0)
Followed by a ZSTD-compressed payload.

Payload — for each message:
    uint16  topic_name_len
    bytes   topic_name  (UTF-8)
    uint64  timestamp   (nanoseconds)
    uint32  msg_data_len
    bytes   msg_data    (CDR-serialized)

std_msgs/String CDR layout (little-endian, eProsima ROS2 convention):
    [0:2]  0x00 0x01  (CDR representation ID — little-endian)
    [2:4]  0x00 0x00  (padding)
    [4:8]  uint32 string_len  (includes null terminator)
    [8:]   string bytes + null terminator

Usage:
    pip install websockets zstandard
    python3 ws_reader.py [--url ws://localhost:9090] [--topic /hello_world] [--timeout 20]
"""

import argparse
import asyncio
import json
import struct
import sys
import time

try:
    import websockets
except ImportError:
    sys.exit("ERROR: 'websockets' not installed — run: pip install websockets")

try:
    import zstandard as zstd
except ImportError:
    sys.exit("ERROR: 'zstandard' not installed — run: pip install zstandard")


# Binary frame magic ("PJRB" in little-endian uint32)
_MAGIC = 0x42524A50


def _parse_header(data: bytes) -> tuple[int, int, int] | None:
    """Return (message_count, uncompressed_size, flags) or None if invalid."""
    if len(data) < 16:
        return None
    magic, msg_count, uncompressed_size, flags = struct.unpack_from("<IIII", data, 0)
    if magic != _MAGIC:
        return None
    return msg_count, uncompressed_size, flags


def _decompress(data: bytes, max_output: int) -> bytes:
    dctx = zstd.ZstdDecompressor()
    # max_output hint is optional; zstandard infers size from the ZSTD frame header
    return dctx.decompress(data, max(max_output, 1))


def _parse_payload(payload: bytes, msg_count: int, target_topic: str) -> str | None:
    """
    Walk the payload and return the decoded string for target_topic, or None.
    """
    offset = 0
    for _ in range(msg_count):
        if offset + 2 > len(payload):
            break
        name_len = struct.unpack_from("<H", payload, offset)[0]
        offset += 2

        if offset + name_len > len(payload):
            break
        topic_name = payload[offset : offset + name_len].decode("utf-8", errors="replace")
        offset += name_len

        if offset + 8 > len(payload):
            break
        # timestamp (uint64, nanoseconds) — skip
        offset += 8

        if offset + 4 > len(payload):
            break
        msg_len = struct.unpack_from("<I", payload, offset)[0]
        offset += 4

        if offset + msg_len > len(payload):
            break
        msg_data = payload[offset : offset + msg_len]
        offset += msg_len

        if topic_name == target_topic:
            return _decode_ros2_string(msg_data)

    return None


def _decode_ros2_string(data: bytes) -> str | None:
    """
    Decode a CDR-serialized std_msgs/String.

    Layout (little-endian ROS 2 CDR):
        [0:4]  CDR header (0x00 0x01 0x00 0x00 for little-endian)
        [4:8]  uint32 length (includes null terminator)
        [8..]  utf-8 bytes + null terminator
    """
    if len(data) < 8:
        return None
    # Bytes 0-3: CDR encapsulation header — skip
    str_len = struct.unpack_from("<I", data, 4)[0]
    if str_len == 0:
        return ""
    end = 8 + str_len
    if len(data) < end:
        return None
    raw = data[8:end]
    # Strip null terminator if present
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


async def run(ws_url: str, topic: str, timeout: float) -> bool:
    """
    Connect to ws_url, subscribe to topic, wait up to timeout seconds for a
    message containing "hello".  Returns True on success.
    """
    print(f"Connecting to {ws_url} …")
    async with websockets.connect(ws_url, open_timeout=10) as ws:
        print("Connected.")

        # Background heartbeat — required every 1 s or server times out in 10 s.
        # Started AFTER the initial handshake to avoid heartbeat ACKs racing
        # with get_topics / subscribe responses on ws.recv().
        async def _heartbeat():
            seq = 0
            while True:
                await ws.send(json.dumps({"command": "heartbeat", "id": f"hb{seq}"}))
                seq += 1
                await asyncio.sleep(1.0)

        hb_task: asyncio.Task | None = None

        try:
            # 1. Discover available topics
            await ws.send(json.dumps({"command": "get_topics", "id": "gt1"}))
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            resp = json.loads(raw)
            available = [t["name"] for t in resp.get("topics", [])]
            print(f"Available topics ({len(available)}): {available}")

            if topic not in available:
                print(f"  WARN: {topic!r} not yet in topic list — will subscribe anyway "
                      f"(bridge may forward it once the publisher appears)")

            # 2. Subscribe
            await ws.send(json.dumps({"command": "subscribe", "topics": [topic], "id": "s1"}))
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            resp = json.loads(raw)
            status = resp.get("status", "")
            if status not in ("success", "partial_success"):
                # If topic not yet visible, that is ok — keep waiting for binary
                print(f"  Subscription response: {resp}")
            else:
                schemas = resp.get("schemas", {})
                print(f"  Subscribed. Schema keys: {list(schemas.keys())}")

            # Start heartbeat only after handshake is complete
            hb_task = asyncio.create_task(_heartbeat())

            # 3. Wait for a binary frame containing our topic
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=min(2.0, remaining))
                except asyncio.TimeoutError:
                    continue

                if isinstance(frame, str):
                    # Heartbeat ack or other JSON — ignore
                    continue

                # Binary frame
                header = _parse_header(frame)
                if header is None:
                    print(f"  WARN: received binary frame with invalid header "
                          f"({len(frame)} bytes), skipping")
                    continue

                msg_count, uncompressed_size, _ = header
                try:
                    payload = _decompress(frame[16:], uncompressed_size)
                except Exception as exc:
                    print(f"  WARN: ZSTD decompress failed: {exc}")
                    continue

                value = _parse_payload(payload, msg_count, topic)
                if value is not None:
                    print(f"  Decoded message on {topic!r}: {value!r}")
                    if "hello" in value.lower():
                        print("PASS: 'hello world' received and decoded through the bridge!")
                        return True
                    else:
                        print(f"  WARN: message received but does not contain 'hello': {value!r}")

            print(f"FAIL: timed out after {timeout:.0f}s waiting for a message on {topic!r}")
            return False

        finally:
            if hb_task is not None:
                hb_task.cancel()
                await asyncio.gather(hb_task, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="pj_bridge smoke-test WebSocket reader")
    parser.add_argument("--url", default="ws://localhost:9090",
                        help="WebSocket URL of the running pj_bridge (default: ws://localhost:9090)")
    parser.add_argument("--topic", default="/hello_world",
                        help="ROS 2 topic to verify (default: /hello_world)")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Seconds to wait for a message before failing (default: 20)")
    args = parser.parse_args()

    ok = asyncio.run(run(args.url, args.topic, args.timeout))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
