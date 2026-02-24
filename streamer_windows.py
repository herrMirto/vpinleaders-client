"""
VPinLeaders Client Streamer for Windows
Windows screen streamer using mss and WebSocket.
"""

import argparse
import asyncio
import time

import cv2
import numpy as np
import websockets
from mss import mss


###############################################################
# Helpers
###############################################################

def build_ws_url(api_base: str, room: str, player: str) -> str:
    """
    Converts http://host:8000 -> ws://host:8000
    """
    api_base = api_base.rstrip("/")

    if api_base.startswith("https://"):
        ws_base = "wss://" + api_base[len("https://"):]
    elif api_base.startswith("http://"):
        ws_base = "ws://" + api_base[len("http://"):]
    elif api_base.startswith("ws://") or api_base.startswith("wss://"):
        ws_base = api_base
    else:
        ws_base = "ws://" + api_base

    return f"{ws_base}/ingest?room={room}&player={player}"


def resize_to_max(frame_bgr: np.ndarray, maxw: int, maxh: int) -> np.ndarray:
    """
    Downscale frame if needed (bandwidth saver).
    """
    h, w = frame_bgr.shape[:2]
    scale = min(maxw / w, maxh / h, 1.0)

    if scale >= 1.0:
        return frame_bgr

    new_w = int(w * scale)
    new_h = int(h * scale)

    return cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)


###############################################################
# Main streamer
###############################################################

async def stream_windows(api_base: str, room: str, player: str,
                         fps: int, quality: int,
                         maxw: int, maxh: int):

    ws_url = build_ws_url(api_base, room, player)

    print("\n===============================================")
    print("VPin Streamer (Windows Mode)")
    print("===============================================")
    print(f"Room:   {room}")
    print(f"Player: {player}")
    print(f"WS URL: {ws_url}")
    print("===============================================\n")

    # Connect websocket
    print("Connecting to ingest websocket...")
    async with websockets.connect(ws_url, max_size=None) as ws:
        print("Connected! Streaming LIVE...\n")

        sct = mss()

        # Monitor 1 = full primary screen
        monitor = sct.monitors[1]

        frame_interval = 1.0 / fps
        sent_frames = 0
        last_stats = time.time()

        while True:
            start_time = time.time()

            # Capture screen (BGRA)
            img = np.array(sct.grab(monitor))

            # Convert BGRA to BGR
            frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

            # Resize if needed
            frame = resize_to_max(frame, maxw=maxw, maxh=maxh)

            # Encode JPEG
            ok, enc = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
            )

            if ok:
                await ws.send(enc.tobytes())
                sent_frames += 1

            # Stats every 5s
            now = time.time()
            if now - last_stats > 5:
                last_stats = now
                h, w = frame.shape[:2]
                print(f"Sent frames={sent_frames} | Frame={w}x{h} | FPS={fps} | Q={quality}")

            # FPS limiter
            elapsed = time.time() - start_time
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)


###############################################################
# CLI
###############################################################

async def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--api", required=True, help="Backend URL, ex: https://www.vpinleaders.com")
    ap.add_argument("--room", required=True, help="Room code, ex: ROOMCODE")
    ap.add_argument("--player", required=True, choices=["p1", "p2"])

    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--quality", type=int, default=65)

    ap.add_argument("--maxw", type=int, default=1280)
    ap.add_argument("--maxh", type=int, default=720)

    args = ap.parse_args()

    await stream_windows(
        args.api,
        args.room,
        args.player,
        args.fps,
        args.quality,
        args.maxw,
        args.maxh,
    )


if __name__ == "__main__":
    asyncio.run(main())
