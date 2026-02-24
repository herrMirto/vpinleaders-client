"""
VPinLeaders Client Streamer for Linux (Wayland)
Linux screen streamer using PipeWire screencast portal and GStreamer appsink callback.
"""

import argparse
import asyncio
import os
import random
import string
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import websockets

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, MessageType
from dbus_next.message import Message
from dbus_next import Variant

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp, GLib


# ============================================================
# Helpers
# ============================================================

def unwrap(v):
    if isinstance(v, Variant):
        return unwrap(v.value)
    if isinstance(v, dict):
        return {unwrap(k): unwrap(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [unwrap(x) for x in v]
    return v


def rand_token(n: int = 14) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def is_wayland() -> bool:
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def build_ws_url(api_base: str, room: str, player: str) -> str:
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
    h, w = frame_bgr.shape[:2]
    scale = min(maxw / w, maxh / h, 1.0)
    if scale >= 1.0:
        return frame_bgr
    new_w = max(2, int(w * scale))
    new_h = max(2, int(h * scale))
    return cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)


# ============================================================
# Portal
# ============================================================

@dataclass
class PortalStreamInfo:
    node_id: int
    width: int
    height: int
    framerate: Optional[Tuple[int, int]] = None


class RawPortalScreencast:
    DEST = "org.freedesktop.portal.Desktop"
    DESKTOP_PATH = "/org/freedesktop/portal/desktop"

    IFACE_SCREENCAST = "org.freedesktop.portal.ScreenCast"
    IFACE_REQUEST = "org.freedesktop.portal.Request"
    IFACE_SESSION = "org.freedesktop.portal.Session"

    def __init__(self):
        self.bus: Optional[MessageBus] = None
        self.fd_passing_enabled = False

    async def connect(self):
        # dbus-next varies: negotiate_unix_fd is typically a ctor arg
        try:
            self.bus = MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True)
            await self.bus.connect()
            self.fd_passing_enabled = True
        except TypeError:
            self.bus = MessageBus(bus_type=BusType.SESSION)
            await self.bus.connect()
            self.fd_passing_enabled = False

    async def call_with_reply(self, path: str, interface: str, member: str, signature: str, body: list):
        assert self.bus is not None
        msg = Message(
            destination=self.DEST,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body,
        )
        reply = await self.bus.call(msg)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"D-Bus error calling {interface}.{member}: {reply.body}")
        return reply

    async def call(self, path: str, interface: str, member: str, signature: str, body: list):
        reply = await self.call_with_reply(path, interface, member, signature, body)
        return unwrap(reply.body)

    async def wait_request_response(self, request_path: str, timeout_s: int) -> Dict[str, Any]:
        assert self.bus is not None
        expected = unwrap(request_path)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        def handler(msg: Message):
            if (
                msg.message_type == MessageType.SIGNAL
                and msg.interface == self.IFACE_REQUEST
                and msg.member == "Response"
            ):
                response_code = int(msg.body[0])
                results_dict = msg.body[1] or {}
                decoded = {k: unwrap(v) for k, v in results_dict.items()}
                if not fut.done():
                    fut.set_result({"response": response_code, "results": decoded, "path": msg.path, "expected": expected})
            return True

        self.bus.add_message_handler(handler)
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        finally:
            try:
                self.bus.remove_message_handler(handler)
            except Exception:
                pass

    async def create_session(self) -> str:
        options = {
            "session_handle_token": Variant("s", rand_token()),
            "handle_token": Variant("s", rand_token()),
        }
        body = await self.call(self.DESKTOP_PATH, self.IFACE_SCREENCAST, "CreateSession", "a{sv}", [options])
        resp = await self.wait_request_response(body[0], timeout_s=30)
        if resp["response"] != 0:
            raise RuntimeError(f"CreateSession failed (response={resp['response']})")
        session_handle = unwrap(resp["results"].get("session_handle"))
        if not session_handle:
            raise RuntimeError("Portal did not return session_handle")
        return session_handle

    async def select_sources(self, session_handle: str):
        options = {
            "types": Variant("u", 1),          # monitor
            "multiple": Variant("b", False),
            "handle_token": Variant("s", rand_token()),
        }
        body = await self.call(self.DESKTOP_PATH, self.IFACE_SCREENCAST, "SelectSources", "oa{sv}", [session_handle, options])
        resp = await self.wait_request_response(body[0], timeout_s=60)
        if resp["response"] != 0:
            raise RuntimeError(f"SelectSources failed (response={resp['response']})")

    async def start(self, session_handle: str) -> List[PortalStreamInfo]:
        options = {"handle_token": Variant("s", rand_token())}
        body = await self.call(self.DESKTOP_PATH, self.IFACE_SCREENCAST, "Start", "osa{sv}", [session_handle, "", options])
        resp = await self.wait_request_response(body[0], timeout_s=90)
        if resp["response"] != 0:
            raise RuntimeError(f"Start failed (response={resp['response']})")

        streams = unwrap(resp["results"].get("streams"))
        if not streams:
            raise RuntimeError("No streams returned by portal")

        out: List[PortalStreamInfo] = []
        for entry in streams:
            node_id = int(entry[0])
            props = entry[1] or {}
            size = unwrap(props.get("size", (0, 0)))
            fr = unwrap(props.get("framerate", None))

            w, h = 0, 0
            if isinstance(size, list) and len(size) >= 2:
                w, h = int(size[0]), int(size[1])

            fr_t = None
            if isinstance(fr, list) and len(fr) == 2:
                fr_t = (int(fr[0]), int(fr[1]))

            out.append(PortalStreamInfo(node_id=node_id, width=w, height=h, framerate=fr_t))
        return out

    async def open_pipewire_remote(self, session_handle: str) -> int:
        if not self.fd_passing_enabled:
            raise RuntimeError(
                "Your dbus-next doesn't support unix fd passing in this environment.\n"
                "Run: pip install -U dbus-next\n"
            )

        reply = await asyncio.wait_for(
            self.call_with_reply(self.DESKTOP_PATH, self.IFACE_SCREENCAST, "OpenPipeWireRemote", "oa{sv}", [session_handle, {}]),
            timeout=45,
        )
        unix_fds = getattr(reply, "unix_fds", None)
        if not unix_fds:
            raise RuntimeError("OpenPipeWireRemote returned no unix_fds (FD passing failed).")

        fd_handle = reply.body[0]
        if isinstance(fd_handle, Variant):
            fd_handle = unwrap(fd_handle)
        fd_index = int(fd_handle)

        fd = int(unix_fds[fd_index])
        return fd

    async def close_session(self, session_handle: str):
        try:
            await self.call(session_handle, self.IFACE_SESSION, "Close", "", [])
        except Exception:
            pass


# ============================================================
# GStreamer capture via appsink callback (robust across GI)
# ============================================================

class GstPipeWireCallbackGrabber:
    """
    Runs a GStreamer pipeline and pushes frames into an asyncio.Queue via appsink new-sample callback.

    This avoids try_pull_sample() which is not available in all GI builds.

    NOTE: GStreamer needs a GLib context; we run a GLib.MainLoop in a dedicated thread.
    """

    def __init__(self, portal_fd: int, node_id: int, fps: int, loop: asyncio.AbstractEventLoop):
        self.portal_fd = portal_fd
        self.node_id = node_id
        self.fps = fps
        self.loop = loop

        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=2)

        self.pipeline = None
        self.appsink: Optional[GstApp.AppSink] = None
        self.bus = None

        self._glib_loop = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        Gst.init(None)

        pipeline_desc = (
            f"pipewiresrc fd={self.portal_fd} path={self.node_id} do-timestamp=true ! "
            f"queue leaky=downstream max-size-buffers=1 ! "
            f"videoconvert ! "
            f"videorate ! "
            f"video/x-raw,format=BGR,framerate={self.fps}/1 ! "
            f"appsink name=sink emit-signals=true sync=false max-buffers=1 drop=true"
        )

        self.pipeline = Gst.parse_launch(pipeline_desc)
        self.appsink = self.pipeline.get_by_name("sink")
        if self.appsink is None:
            raise RuntimeError("appsink not found in pipeline")

        # Connect callback
        self.appsink.connect("new-sample", self._on_new_sample)

        self.bus = self.pipeline.get_bus()

        self.pipeline.set_state(Gst.State.PLAYING)

        # Start GLib loop in background thread
        self._glib_loop = GLib.MainLoop()
        self._running = True
        self._thread = threading.Thread(target=self._run_glib, daemon=True)
        self._thread.start()

    def _run_glib(self):
        assert self._glib_loop is not None
        try:
            self._glib_loop.run()
        except Exception:
            pass

    def stop(self):
        self._running = False
        try:
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass
        try:
            if self._glib_loop is not None:
                self._glib_loop.quit()
        except Exception:
            pass

    def _on_new_sample(self, sink):
        """
        Called from the GLib thread context.
        Must be fast; we push to asyncio queue using call_soon_threadsafe.
        """
        if not self._running:
            return Gst.FlowReturn.EOS

        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        caps = sample.get_caps()
        s = caps.get_structure(0)
        width = int(s.get_value("width"))
        height = int(s.get_value("height"))

        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK

        try:
            data = mapinfo.data
            frame = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 3)).copy()
        finally:
            buf.unmap(mapinfo)

        def push():
            # keep newest only
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                except Exception:
                    pass
            self.queue.put_nowait(frame)

        self.loop.call_soon_threadsafe(push)
        return Gst.FlowReturn.OK

    async def get_frame(self, timeout_s: float = 2.0) -> Optional[np.ndarray]:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None


# ============================================================
# Streaming main
# ============================================================

async def stream_wayland(api_base: str, room: str, player: str, fps: int, quality: int, maxw: int, maxh: int):
    ws_url = build_ws_url(api_base, room, player)

    print("\n===============================================")
    print("\U0001f3a5 VPin Streamer (Linux Wayland Mode)")
    print("===============================================")
    print(f"Room:   {room}")
    print(f"Player: {player}")
    print(f"WS URL: {ws_url}")
    print("===============================================\n")

    if not is_wayland():
        print("\u274c This streamer is for Wayland sessions.")
        print("   XDG_SESSION_TYPE =", os.environ.get("XDG_SESSION_TYPE"))
        return

    print("\U0001f310 Connecting to ingest websocket...")
    async with websockets.connect(ws_url, max_size=None) as ws:
        print("\U0001f680 Connected to ingest websocket!\n")

        portal = RawPortalScreencast()
        await portal.connect()

        print("\U0001f7e6 Requesting screen share permission...")
        session = await portal.create_session()

        grabber: Optional[GstPipeWireCallbackGrabber] = None
        try:
            await portal.select_sources(session)
            print("\U0001f449 Select your monitor in the popup and click Share...")

            streams = await portal.start(session)
            s = streams[0]
            print(f"\u2705 Portal stream node={s.node_id} (reported size={s.width}x{s.height})")

            print("\U0001f9e9 Calling OpenPipeWireRemote...")
            pw_fd = await portal.open_pipewire_remote(session)
            print(f"\u2705 Got PipeWire FD: {pw_fd}")

            loop = asyncio.get_event_loop()
            grabber = GstPipeWireCallbackGrabber(portal_fd=pw_fd, node_id=s.node_id, fps=fps, loop=loop)
            grabber.start()
            print("\u2705 GStreamer pipeline started. Streaming frames...\n")

            sent = 0
            last_stats = time.time()

            while True:
                frame = await grabber.get_frame(timeout_s=3.0)
                if frame is None:
                    print("\u23f3 No frames yet (waiting...)")
                    continue

                frame = resize_to_max(frame, maxw=maxw, maxh=maxh)

                ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
                if ok:
                    await ws.send(enc.tobytes())
                    sent += 1

                now = time.time()
                if now - last_stats > 5:
                    last_stats = now
                    h, w = frame.shape[:2]
                    print(f"\U0001f4c8 sent={sent} frame={w}x{h} fps_target={fps} q={quality}")

        finally:
            if grabber is not None:
                try:
                    grabber.stop()
                except Exception:
                    pass
            await portal.close_session(session)


# ============================================================
# CLI
# ============================================================

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True, help="Backend URL ex: https://www.vpinleaders.com")
    ap.add_argument("--room", required=True)
    ap.add_argument("--player", required=True, choices=["p1", "p2"])
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--quality", type=int, default=65)
    ap.add_argument("--maxw", type=int, default=1280)
    ap.add_argument("--maxh", type=int, default=720)
    args = ap.parse_args()

    await stream_wayland(args.api, args.room, args.player, args.fps, args.quality, args.maxw, args.maxh)


if __name__ == "__main__":
    asyncio.run(main())