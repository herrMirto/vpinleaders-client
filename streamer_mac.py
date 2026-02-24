"""
VPinLeaders Client Streamer for MacOS
This script captures the screen on MacOS using mss and streams it to a WebSocket server.
"""
import argparse
import asyncio
import time

import cv2
import numpy as np
from mss import mss
import websockets

def resize_if_needed(img_bgr: np.ndarray, max_width: int, max_height: int) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    scale = min(max_width / w, max_height / h, 1.0)
    if scale >= 1.0:
        return img_bgr
    new_w = int(w * scale)
    new_h = int(h * scale)
    return cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)

async def stream_screen(ws_url: str, fps: int, jpeg_quality: int, max_width: int, max_height: int):
    frame_interval = 1.0 / max(1, fps)
    sct = mss()

    # Full screen: primary monitor is sct.monitors[1] (mss convention)
    monitor = sct.monitors[1]

    while True:
        try:
            async with websockets.connect(ws_url, max_size=None) as ws:
                print(f"✅ Connected to ingest: {ws_url}")
                last = time.perf_counter()

                while True:
                    start = time.perf_counter()

                    sct_img = sct.grab(monitor)
                    img = np.array(sct_img)  # BGRA
                    img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

                    # Reduce bandwidth/CPU (recommended)
                    img_bgr = resize_if_needed(img_bgr, max_width, max_height)

                    ok, enc = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
                    if not ok:
                        continue

                    await ws.send(enc.tobytes())

                    # Sleep to maintain fps
                    elapsed = time.perf_counter() - start
                    sleep_for = frame_interval - elapsed
                    if sleep_for > 0:
                        await asyncio.sleep(sleep_for)

                    # small log every ~5s
                    if time.perf_counter() - last > 5:
                        last = time.perf_counter()
                        print(f"📡 streaming... ({fps} fps target, q={jpeg_quality}, max={max_width}x{max_height})")

        except Exception as e:
            print(f"❌ Connection error: {e}")
            print("⏳ Reconnecting in 2s...")
            await asyncio.sleep(2)

def build_ws_url(api_base: str, room: str, player: str) -> str:
    api_base = api_base.rstrip("/")
    # If api_base is http(s), convert to ws(s)
    if api_base.startswith("https://"):
        ws_base = "wss://" + api_base[len("https://"):]
    elif api_base.startswith("http://"):
        ws_base = "ws://" + api_base[len("http://"):]
    elif api_base.startswith("ws://") or api_base.startswith("wss://"):
        ws_base = api_base
    else:
        ws_base = "ws://" + api_base
    return f"{ws_base}/ingest?room={room}&player={player}"

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="https://www.vpinleaders.com", help="Server base URL (http://... or ws://...)")
    p.add_argument("--room", required=True, help="Challenge code / room id")
    p.add_argument("--player", required=True, choices=["p1", "p2"], help="Player side: p1 or p2")
    p.add_argument("--fps", type=int, default=60, help="Target FPS (recommend 10-15 for MVP)")
    p.add_argument("--quality", type=int, default=80, help="JPEG quality 1-100 (recommend 60-75)")
    p.add_argument("--maxw", type=int, default=1280, help="Max output width to reduce bandwidth")
    p.add_argument("--maxh", type=int, default=720, help="Max output height to reduce bandwidth")
    args = p.parse_args()

    ws_url = build_ws_url(args.api, args.room, args.player)
    asyncio.run(stream_screen(ws_url, args.fps, args.quality, args.maxw, args.maxh))
