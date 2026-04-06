import glob
import json
import os
import re
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _psutil = None  # type: ignore
    _PSUTIL_OK = False

try:
    from pinmame_live import PinMameLiveSession
except Exception:
    PinMameLiveSession = None  # type: ignore


def _parse_int(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v.startswith('0x'):
            try:
                return int(v, 16)
            except Exception:
                return default
        try:
            return int(v)
        except Exception:
            return default
    return default


@dataclass
class NVRAMSegment:
    address: int
    size: int
    file_base: int
    nibble: str


class MapRepository:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.index_path = os.path.join(root_dir, 'index.json')
        self.maps_dir = os.path.join(root_dir, 'maps')
        self.platform_dir = os.path.join(root_dir, 'platforms')
        self._index = {}
        self._map_cache: Dict[str, dict] = {}
        self._platform_cache: Dict[str, dict] = {}
        self._load_index()

    def _load_index(self):
        with open(self.index_path, 'r', encoding='utf-8') as f:
            self._index = json.load(f)

    def _resolve_map_rel(self, rom: str) -> Optional[str]:
        rel = self._index.get(rom)
        if isinstance(rel, str) and rel.endswith('.map.json'):
            return rel
        return None

    def has_rom(self, rom: str) -> bool:
        return self._resolve_map_rel(rom) is not None

    def map_for_rom(self, rom: str) -> Optional[dict]:
        rel = self._resolve_map_rel(rom)
        if not rel:
            return None
        if rel not in self._map_cache:
            p = os.path.join(self.root_dir, rel)
            with open(p, 'r', encoding='utf-8') as f:
                self._map_cache[rel] = json.load(f)
        return self._map_cache[rel]

    def platform_for_map(self, map_data: dict) -> Optional[dict]:
        meta = map_data.get('_metadata', {})
        platform = meta.get('platform')
        if not platform:
            return None
        if platform not in self._platform_cache:
            p = os.path.join(self.platform_dir, f'{platform}.json')
            with open(p, 'r', encoding='utf-8') as f:
                self._platform_cache[platform] = json.load(f)
        return self._platform_cache[platform]


class DescriptorDecoder:
    def __init__(self, map_data: dict, platform_data: Optional[dict], direct_mode: bool = False):
        self.map_data = map_data or {}
        self.platform_data = platform_data or {}
        self.char_map = self.map_data.get('_metadata', {}).get('char_map')
        self.shared_values = self.map_data.get('_metadata', {}).get('values', {})
        self.default_endian = self.platform_data.get('endian', 'big').lower()
        self.segments = self._build_segments(self.platform_data)
        self.low_ram_mirror_limit = self._infer_low_ram_mirror_limit(self.platform_data)
        self.default_nibble = self.segments[0].nibble if self.segments else 'both'
        self.direct_mode = direct_mode

    @staticmethod
    def _build_segments(platform_data: dict) -> List[NVRAMSegment]:
        layout = platform_data.get('memory_layout', []) if isinstance(platform_data, dict) else []
        nv = []
        for entry in layout:
            if entry.get('type') != 'nvram':
                continue
            addr = _parse_int(entry.get('address'))
            size = _parse_int(entry.get('size'))
            if addr is None or size is None or size <= 0:
                continue
            nv.append((addr, size, (entry.get('nibble') or 'both').lower()))

        nv.sort(key=lambda x: x[0])
        out: List[NVRAMSegment] = []
        file_base = 0
        for addr, size, nibble in nv:
            out.append(NVRAMSegment(address=addr, size=size, file_base=file_base, nibble=nibble))
            file_base += size
        return out

    @staticmethod
    def _infer_low_ram_mirror_limit(platform_data: dict) -> int:
        layout = platform_data.get('memory_layout', []) if isinstance(platform_data, dict) else []
        nvram_addrs: List[int] = []
        ram_zero_size = 0
        for entry in layout:
            addr = _parse_int(entry.get('address'))
            size = _parse_int(entry.get('size'))
            if addr is None or size is None or size <= 0:
                continue
            typ = (entry.get('type') or '').lower()
            if typ == 'nvram':
                nvram_addrs.append(addr)
            elif typ == 'ram' and addr == 0:
                ram_zero_size = max(ram_zero_size, size)

        if not nvram_addrs or ram_zero_size <= 0:
            return 0
        first_nvram = min(nvram_addrs)
        # Some platforms (notably Williams System4/6/7) mirror low banked RAM
        # into the beginning of the persisted NVRAM blob.
        if first_nvram > 0 and ram_zero_size >= first_nvram:
            return first_nvram
        return 0

    def _segment_for_addr(self, addr: int) -> Optional[NVRAMSegment]:
        for seg in self.segments:
            if seg.address <= addr < seg.address + seg.size:
                return seg
        return None

    def _resolve_addresses(self, desc: dict) -> List[int]:
        if 'offsets' in desc and isinstance(desc['offsets'], list):
            return [a for a in (_parse_int(v) for v in desc['offsets']) if a is not None]

        start = _parse_int(desc.get('start'))
        if start is None:
            return []

        length = _parse_int(desc.get('length'))
        if length is not None:
            if length < 1:
                return []
            return list(range(start, start + length))

        end = _parse_int(desc.get('end'))
        if end is not None and end >= start:
            return list(range(start, end + 1))

        return [start]

    def _resolve_values(self, desc: dict) -> List[Any]:
        values = desc.get('values', [])
        if isinstance(values, str):
            shared = self.shared_values.get(values)
            if isinstance(shared, list):
                return shared
            return []
        if isinstance(values, list):
            return values
        return []

    def _apply_mask(self, b: int, desc: dict) -> int:
        mask = desc.get('mask')
        if mask is None:
            return b
        m = _parse_int(mask)
        if m is None:
            return b
        return b & m

    def _read_units(self, data: bytes, desc: dict) -> Optional[Tuple[List[int], str]]:
        addrs = self._resolve_addresses(desc)
        if not addrs:
            return None

        units: List[int] = []
        desc_nibble = (desc.get('nibble') or '').lower()

        for addr in addrs:
            if self.direct_mode:
                file_off = addr
                nibble_mode = desc_nibble or 'both'
            # Preferred path: platform-aware address mapping.
            elif self.segments:
                seg = self._segment_for_addr(addr)
                if seg is None:
                    if self.low_ram_mirror_limit > 0 and 0 <= addr < self.low_ram_mirror_limit:
                        file_off = addr
                        # Mirrored low RAM is stored as raw bytes at the beginning
                        # of the NVRAM file, independent of NVRAM nibble packing.
                        nibble_mode = desc_nibble or 'both'
                    else:
                        return None
                else:
                    file_off = seg.file_base + (addr - seg.address)
                    nibble_mode = desc_nibble or seg.nibble
            else:
                # Compatibility path for maps without platform metadata.
                file_off = addr
                nibble_mode = desc_nibble or 'both'

            if file_off < 0 or file_off >= len(data):
                return None

            raw_b = data[file_off]
            if nibble_mode == 'low':
                # For nibble-based platforms, apply mask to the selected nibble unit.
                # Maps use nibble masks (0x0..0xF), not byte masks (0x00..0xFF).
                unit = raw_b & 0x0F
                units.append(self._apply_mask(unit, desc))
            elif nibble_mode == 'high':
                unit = (raw_b >> 4) & 0x0F
                units.append(self._apply_mask(unit, desc))
            else:
                units.append(self._apply_mask(raw_b, desc))

        return units, (desc_nibble or (nibble_mode if 'nibble_mode' in locals() else 'both'))

    def _decode_int(self, units: List[int], endian: str) -> int:
        ordered = list(reversed(units)) if endian == 'little' else units
        value = 0
        for b in ordered:
            value = (value << 8) | (b & 0xFF)
        return value

    def _decode_bcd(self, units: List[int], nibble_mode: str, endian: str) -> int:
        ordered = list(reversed(units)) if endian == 'little' else units

        digits = []
        if nibble_mode in ('low', 'high'):
            for n in ordered:
                digits.append(n if 0 <= n <= 9 else 0)
        else:
            for b in ordered:
                hi = (b >> 4) & 0x0F
                lo = b & 0x0F
                digits.append(hi if hi <= 9 else 0)
                digits.append(lo if lo <= 9 else 0)

        value = 0
        for d in digits:
            value = (value * 10) + d
        return value

    def _decode_ch(self, units: List[int], nibble_mode: str, desc: dict) -> str:
        if nibble_mode in ('low', 'high'):
            bytes_out = []
            i = 0
            while i + 1 < len(units):
                bytes_out.append(((units[i] & 0x0F) << 4) | (units[i + 1] & 0x0F))
                i += 2
        else:
            bytes_out = [u & 0xFF for u in units]

        chars = []
        null_mode = (desc.get('null') or 'ignore').lower()

        for b in bytes_out:
            if self.char_map:
                if b < len(self.char_map):
                    ch = self.char_map[b]
                else:
                    ch = ' '
            else:
                if b == 0:
                    ch = '\x00'
                elif 32 <= b <= 126:
                    ch = chr(b)
                else:
                    ch = ' '

            if ch == '\x00':
                if null_mode in ('truncate', 'terminate'):
                    break
                if null_mode == 'ignore':
                    continue

            chars.append(ch)

        return ''.join(chars)

    def _decode_dipsw(self, data: bytes, desc: dict):
        # PinMAME DIP switches are stored in the last 6 bytes of the .nv file.
        # SW1 is LSB of the first of those six bytes.
        offsets = desc.get('offsets')
        if not isinstance(offsets, list) or not offsets:
            return None

        if len(data) < 6:
            return None

        index = 0
        dip_base = len(data) - 6
        for sw in offsets:
            sw_num = _parse_int(sw)
            if sw_num is None or sw_num < 1:
                return None
            pos = sw_num - 1
            byte_off = dip_base + (pos // 8)
            bit_off = pos % 8
            if byte_off < dip_base or byte_off >= len(data):
                return None
            bit = 1 if (data[byte_off] & (1 << bit_off)) else 0
            index = (index << 1) | bit

        values = self._resolve_values(desc)
        if values and 0 <= index < len(values):
            return values[index]
        return index

    def _decode_bits(self, units: List[int], endian: str, desc: dict):
        raw = self._decode_int(units, endian)
        values = self._resolve_values(desc)
        if not values:
            return raw

        # Spec expects numeric values that are summed for set bits.
        if all(isinstance(v, (int, float)) for v in values):
            out = 0
            for i, v in enumerate(values):
                if raw & (1 << i):
                    out += int(v)
            return out

        # Fallback for non-numeric maps: return selected labels.
        selected = []
        for i, v in enumerate(values):
            if raw & (1 << i):
                selected.append(v)
        return selected

    def _decode_enum(self, units: List[int], endian: str, desc: dict):
        idx = self._decode_int(units, endian)
        values = self._resolve_values(desc)
        if values and 0 <= idx < len(values):
            return values[idx]
        return idx

    def _decode_wpc_rtc(self, units: List[int], endian: str):
        ordered = list(reversed(units)) if endian == 'little' else units
        if len(ordered) < 7:
            return None

        year = ((ordered[0] & 0xFF) << 8) | (ordered[1] & 0xFF)
        month = ordered[2] & 0xFF
        day = ordered[3] & 0xFF
        dow = ordered[4] & 0xFF
        hour = ordered[5] & 0xFF
        minute = ordered[6] & 0xFF

        return {
            'year': year,
            'month': month,
            'day': day,
            'day_of_week': dow,
            'hour': hour,
            'minute': minute,
        }

    def decode(self, data: bytes, desc: dict):
        encoding = (desc.get('encoding') or '').lower()
        if not encoding:
            return None

        if encoding == 'dipsw':
            return self._decode_dipsw(data, desc)

        ru = self._read_units(data, desc)
        if ru is None:
            return None
        units, nibble_mode = ru

        endian = (desc.get('endian') or self.default_endian or 'big').lower()

        if encoding == 'int':
            value = self._decode_int(units, endian)
        elif encoding == 'bcd':
            value = self._decode_bcd(units, nibble_mode, endian)
        elif encoding == 'bool':
            raw = self._decode_int(units, endian)
            value = raw != 0
            if bool(desc.get('invert', False)):
                value = not value
            return value
        elif encoding == 'ch':
            return self._decode_ch(units, nibble_mode, desc)
        elif encoding == 'raw':
            return bytes(units)
        elif encoding == 'enum':
            return self._decode_enum(units, endian, desc)
        elif encoding == 'bits':
            value = self._decode_bits(units, endian, desc)
        elif encoding == 'wpc_rtc':
            return self._decode_wpc_rtc(units, endian)
        else:
            return None

        if isinstance(value, int):
            scale = desc.get('scale')
            if scale is not None:
                try:
                    value = int(value * float(scale))
                except Exception:
                    pass
            off = desc.get('offset')
            if off is not None:
                try:
                    value = int(value + float(off))
                except Exception:
                    pass

        return value


@dataclass
class RomState:
    active: bool = False
    session_best: int = 0
    last_best: int = 0
    last_scores: Tuple[int, ...] = tuple()
    last_change_ts: float = 0.0
    last_update_ts: float = 0.0
    session_start_ts: float = 0.0
    baseline_match_counter: Optional[int] = None
    last_match_counter: Optional[int] = None
    last_game_over: Optional[bool] = None
    last_current_ball: Optional[int] = None
    last_sent_score: int = 0
    last_sent_ts: float = 0.0
    has_nonstart_update: bool = False
    warned_no_disk_updates: bool = False
    last_short_end_warn_ts: float = 0.0
    last_progress_ts: float = 0.0
    attract_pattern_ts: float = 0.0


class NVRAMMonitor:
    def __init__(
        self,
        maps_root: str,
        nvram_dir: str,
        nvram_scan_pattern: str,
        logger: Callable[[str, str], None],
        on_current_scores: Callable[[str, List[int], Optional[int]], None],
        on_game_end: Callable[[str, List[int], str, Optional[int]], None],
        on_game_start: Optional[Callable[[str], None]] = None,
        on_status_message: Optional[Callable[[str, str], None]] = None,
        use_live_pinmame: bool = True,
        poll_interval_sec: float = 1.0,
        game_end_stable_sec: float = 8.0,
        idle_end_sec: float = 45.0,
        min_game_duration_sec: float = 60.0,
    ):
        self.repo = MapRepository(maps_root)
        self.nvram_dir = nvram_dir
        self.nvram_scan_pattern = (nvram_scan_pattern or '*.nv').strip() or '*.nv'
        self._log = logger
        self.on_current_scores = on_current_scores
        self.on_game_end = on_game_end
        self.on_game_start = on_game_start
        self.on_status_message = on_status_message
        self.use_live_pinmame = bool(use_live_pinmame)
        self.poll_interval_sec = max(0.2, poll_interval_sec)
        self.game_end_stable_sec = max(3.0, game_end_stable_sec)
        self.idle_end_sec = max(self.game_end_stable_sec, idle_end_sec)
        self.min_game_duration_sec = max(0.0, min_game_duration_sec)

        self._file_mtime: Dict[str, float] = {}
        self._file_crc32: Dict[str, int] = {}
        self._state: Dict[str, RomState] = {}
        self.active_rom: Optional[str] = None
        self.active_path: Optional[str] = None
        self._live_session = PinMameLiveSession() if PinMameLiveSession is not None else None
        self._last_live_crc_by_rom: Dict[str, int] = {}
        self._last_live_diag_ts = 0.0
        self._last_live_unchanged_log_ts = 0.0
        self._last_live_snapshot_err_ts = 0.0
        self._last_live_warn_ts = 0.0
        self._last_live_attach_attempt_log_ts = 0.0
        self._last_live_snapshot_attempt_log_ts = 0.0
        self._last_wait_log_ts = 0.0
        self._last_vpx_probe_ts = 0.0
        self._last_vpx_diag_ts = 0.0
        self._last_vpx_presence_diag_ts = 0.0
        self._last_any_vpx_seen_ts = 0.0
        self._last_attach_filter_diag_ts = 0.0
        self._vpx_proc_cache: Optional[List[Tuple[int, str, str]]] = None
        self._vpx_proc_cache_ts: float = 0.0
        self._last_unsupported_warn_by_rom: Dict[str, float] = {}
        self._rom_live_support_cache: Dict[str, Tuple[bool, str]] = {}
        self._unsupported_active_rom: Optional[str] = None
        self._unsupported_active_path: Optional[str] = None
        self._unsupported_active_since: float = 0.0
        self._live_unsupported_by_rom: Dict[str, str] = {}
        self._last_live_unsupported_log_by_rom: Dict[str, float] = {}
        # Limit attach targets to known VPX renderer executables.
        self._allowed_attach_exec_basenames = {
            'vpinballx_bgfx',
            'vpinballx_bgfx.exe',
            'vpinball_bgfx',
            'vpinball_bgfx.exe',
            'vpinballx_bgfx64',
            'vpinballx_bgfx64.exe',
            'vpinball_bgfx64',
            'vpinball_bgfx64.exe',
            'vpinballx_gl',
            'vpinballx_gl.exe',
            'vpinball_gl',
            'vpinball_gl.exe',
            'vpinballx_gl64',
            'vpinballx_gl64.exe',
            'vpinball_gl64',
            'vpinball_gl64.exe',
        }

    @staticmethod
    def _looks_nonplay_score_pattern(scores: Tuple[int, ...]) -> bool:
        if not scores:
            return True
        non_zero = [v for v in scores if isinstance(v, int) and v > 0]
        if not non_zero:
            return True
        # Attract/high-score cycle pattern seen in several families.
        if len(non_zero) >= 3 and len(set(non_zero)) == 1:
            return True
        # In single-player games, P1=0 while other players show values is usually
        # not an in-play score state.
        if scores[0] == 0 and any(v > 0 for v in scores[1:]):
            return True
        return False

    def _canon_path(self, path: str) -> str:
        p = os.path.expanduser(path or '')
        p = os.path.normpath(p)
        p = os.path.realpath(p)
        if os.name == 'nt':
            p = os.path.normcase(p)
        return p

    def _read_file(self, path: str) -> Optional[bytes]:
        try:
            with open(path, 'rb') as f:
                return f.read()
        except Exception:
            return None

    def _extract_game_state(self, rom: str, data: bytes, direct_mode: bool = False):
        map_data = self.repo.map_for_rom(rom)
        if not map_data:
            return None

        platform_data = self.repo.platform_for_map(map_data) or {}
        decoder = DescriptorDecoder(map_data, platform_data, direct_mode=direct_mode)

        gs = map_data.get('game_state')
        if not isinstance(gs, dict):
            return None

        def decode_desc(d):
            if not isinstance(d, dict):
                return None
            return decoder.decode(data, d)

        scores = []
        raw_scores = gs.get('scores')
        if isinstance(raw_scores, list):
            for s in raw_scores:
                v = decode_desc(s)
                try:
                    iv = int(v)
                except Exception:
                    iv = 0
                scores.append(max(0, iv))

        game_over = decode_desc(gs.get('game_over')) if isinstance(gs.get('game_over'), dict) else None

        current_ball = decode_desc(gs.get('current_ball')) if isinstance(gs.get('current_ball'), dict) else None
        try:
            current_ball = int(current_ball) if current_ball is not None else None
        except Exception:
            current_ball = None

        match_counter = decode_desc(gs.get('match_counter')) if isinstance(gs.get('match_counter'), dict) else None
        try:
            match_counter = int(match_counter) if match_counter is not None else None
        except Exception:
            match_counter = None

        player_count = decode_desc(gs.get('player_count')) if isinstance(gs.get('player_count'), dict) else None
        try:
            player_count = int(player_count) if player_count is not None else None
        except Exception:
            player_count = None

        current_player = decode_desc(gs.get('current_player')) if isinstance(gs.get('current_player'), dict) else None
        try:
            current_player = int(current_player) if current_player is not None else None
        except Exception:
            current_player = None

        ball_count = decode_desc(gs.get('ball_count')) if isinstance(gs.get('ball_count'), dict) else None
        try:
            ball_count = int(ball_count) if ball_count is not None else None
        except Exception:
            ball_count = None

        final_scores: List[int] = []
        if isinstance(gs.get('final_scores'), list):
            for s in gs.get('final_scores', []):
                v = decode_desc(s)
                try:
                    iv = int(v)
                except Exception:
                    iv = 0
                final_scores.append(max(0, iv))

        return {
            'scores': scores,
            'best': max(scores) if scores else 0,
            'final_scores': final_scores,
            'game_over': bool(game_over) if isinstance(game_over, bool) else None,
            'current_ball': current_ball,
            'match_counter': match_counter,
            'player_count': player_count,
            'current_player': current_player,
            'ball_count': ball_count,
        }

    def _should_start(self, st: RomState, parsed: dict, prev_best: int, prev_scores: Tuple[int, ...]) -> bool:
        best = parsed['best']
        game_over = parsed['game_over']
        current_ball = parsed['current_ball']
        ball_count = parsed.get('ball_count')
        player_count = parsed['player_count']
        current_player = parsed.get('current_player')
        prev_game_over = st.last_game_over

        valid_ball = False
        if current_ball is not None and current_ball > 0:
            if ball_count is None or ball_count <= 0:
                valid_ball = True
            else:
                valid_ball = current_ball <= ball_count

        valid_player = False
        if player_count is not None and player_count > 0:
            if current_player is None:
                valid_player = True
            else:
                valid_player = 1 <= current_player <= player_count

        # Strongest start signal: known game-over flag transitions true -> false.
        if prev_game_over is True and game_over is False:
            return True
        # A positively asserted game-over state should not arm a new session.
        if game_over is True:
            return False
        # Active play indicators from map (ball and/or player state).
        if valid_ball and (valid_player or player_count is None):
            return True
        # Use game_over=false only when corroborated by valid player context.
        if game_over is False and valid_player and (valid_ball or best > 0):
            return True
        # Last-resort fallback when maps only expose scores:
        # require at least one prior sample to avoid stale attract-mode starts.
        if prev_scores and prev_best > 0 and best > prev_best and best > 0 and game_over is not True:
            return True
        return False

    def _clear_active_monitoring(self, rom: Optional[str] = None, force_wait_log: bool = False):
        if rom is not None and self.active_rom != rom:
            return
        self.active_rom = None
        self.active_path = None
        if self._live_session is not None:
            self._live_session.detach()
        self._last_live_crc_by_rom.clear()
        if force_wait_log:
            self._log_waiting(force=True)

    def _mark_live_unsupported(self, rom: str, reason: str):
        self._live_unsupported_by_rom[rom] = reason
        now = time.time()
        last = self._last_live_unsupported_log_by_rom.get(rom, 0.0)
        if (now - last) >= 30.0:
            if os.name == 'nt' and reason == 'pinmame_exports_not_found':
                self._log(
                    'WARN',
                    (
                        f'Windows VPX build for ROM "{rom}" does not expose the live PinMAME API; '
                        'using NVRAM file fallback only'
                    ),
                )
            else:
                self._log('WARN', f'Live PinMAME disabled for ROM "{rom}": {reason}')
            self._last_live_unsupported_log_by_rom[rom] = now

    def _maybe_emit_game_end(self, rom: str, st: RomState, parsed: dict, reason: str):
        now = time.time()
        duration = now - st.session_start_ts if st.session_start_ts else 0
        if duration < self.min_game_duration_sec:
            if (now - st.last_short_end_warn_ts) >= 5.0:
                self._log(
                    'WARN',
                    (
                        f'Ignoring game_end for {rom}: reason={reason}, '
                        f'duration too short ({duration:.1f}s < {self.min_game_duration_sec:.0f}s)'
                    ),
                )
                st.last_short_end_warn_ts = now
            # Keep active sessions alive for in-play false positives (for example,
            # transient game_over/ball signals between balls). Only clear state when
            # the table has actually exited.
            if reason in ('vpx_play_exit', 'vpx_exit_unsupported_nvram'):
                st.active = False
                st.session_best = 0
                st.session_start_ts = 0.0
                st.has_nonstart_update = False
                st.warned_no_disk_updates = False
                st.last_progress_ts = 0.0
                st.attract_pattern_ts = 0.0
            else:
                # Rearm stable-window checks to avoid repeated end attempts every tick.
                st.last_change_ts = now
            return

        best = max(st.session_best, parsed.get('best', 0))
        if best <= 0:
            st.active = False
            st.session_best = 0
            st.session_start_ts = 0.0
            st.has_nonstart_update = False
            st.warned_no_disk_updates = False
            st.last_progress_ts = 0.0
            st.attract_pattern_ts = 0.0
            self._clear_active_monitoring(rom, force_wait_log=True)
            return

        if st.last_sent_score == best and (now - st.last_sent_ts) < 15:
            st.active = False
            st.session_best = 0
            st.session_start_ts = 0.0
            st.has_nonstart_update = False
            st.warned_no_disk_updates = False
            st.last_progress_ts = 0.0
            st.attract_pattern_ts = 0.0
            self._clear_active_monitoring(rom, force_wait_log=True)
            return

        scores = parsed.get('scores') or [best]
        final_scores = parsed.get('final_scores') or []
        if isinstance(final_scores, list) and final_scores:
            final_norm: List[int] = []
            for v in final_scores:
                try:
                    iv = int(v)
                except Exception:
                    iv = 0
                final_norm.append(max(0, iv))
            if parsed.get('game_over') is True:
                session_ref = max(st.session_best, parsed.get('best', 0))
                final_best = max(final_norm, default=0)
                # Use final_scores only when it is plausible for the active session.
                if final_best > 0 and (session_ref <= 0 or final_best <= int(session_ref * 1.2) + 100000):
                    scores = final_norm
                    best = max(best, final_best)

        self.on_game_end(rom, scores, reason, int(duration))
        st.last_sent_score = best
        st.last_sent_ts = now
        st.active = False
        st.session_best = 0
        st.session_start_ts = 0.0
        st.has_nonstart_update = False
        st.warned_no_disk_updates = False
        st.last_short_end_warn_ts = 0.0
        st.last_progress_ts = 0.0
        st.attract_pattern_ts = 0.0
        self._clear_active_monitoring(rom, force_wait_log=True)

    def _handle_update(self, rom: str, parsed: dict, force_start: bool = False):
        now = time.time()
        st = self._state.setdefault(rom, RomState())
        started_now = False

        scores = tuple(parsed.get('scores') or [])
        best = parsed.get('best', 0)
        prev_scores = st.last_scores
        prev_game_over = st.last_game_over
        prev_current_ball = st.last_current_ball

        st.last_update_ts = now
        if scores != st.last_scores:
            st.last_scores = scores
            st.last_change_ts = now

        prev_best = st.last_best

        if scores:
            self.on_current_scores(rom, list(scores), parsed.get('current_ball'))

        if not st.active and (force_start or self._should_start(st, parsed, prev_best, prev_scores)):
            if force_start or not self._looks_nonplay_score_pattern(scores):
                st.active = True
                st.session_start_ts = now
                st.session_best = best
                st.last_progress_ts = now
                st.last_change_ts = now
                st.baseline_match_counter = parsed.get('match_counter')
                # A start signal can come from stale state in builds that only flush .nv on exit.
                # Require at least one subsequent update before allowing idle/game-over heuristics.
                st.has_nonstart_update = False
                st.warned_no_disk_updates = False
                st.attract_pattern_ts = 0.0
                started_now = True
                self._log('INFO', f'Game started (NVRAM): {rom}')
                if self.on_game_start:
                    self.on_game_start(rom)

        if st.active:
            if not started_now and not force_start:
                st.has_nonstart_update = True
                st.warned_no_disk_updates = False

            effective_best = best
            looks_nonplay = self._looks_nonplay_score_pattern(scores)
            non_zero_scores = [v for v in scores if isinstance(v, int) and v > 0]
            is_mirrored_attract = len(non_zero_scores) >= 3 and len(set(non_zero_scores)) == 1
            if is_mirrored_attract and st.session_best > 0:
                st.attract_pattern_ts = now
            if looks_nonplay and st.session_best > 0:
                # Do not treat attract/high-score cycles as in-play score progress.
                effective_best = st.session_best

            if effective_best > st.session_best:
                st.last_progress_ts = now
            st.session_best = max(st.session_best, effective_best)

            match_counter = parsed.get('match_counter')
            game_over = parsed.get('game_over')
            current_ball = parsed.get('current_ball')

            if st.baseline_match_counter is not None and match_counter is not None and match_counter != st.baseline_match_counter:
                self._maybe_emit_game_end(rom, st, parsed, 'match_counter_changed')
                st.baseline_match_counter = match_counter
                st.last_match_counter = match_counter
                st.last_game_over = game_over
                st.last_current_ball = current_ball
                return

            stable = (now - st.last_change_ts) >= self.game_end_stable_sec
            ball_count = parsed.get('ball_count')
            final_scores = parsed.get('final_scores') or []
            final_best = 0
            if isinstance(final_scores, list):
                for v in final_scores:
                    try:
                        iv = int(v)
                    except Exception:
                        iv = 0
                    if iv > final_best:
                        final_best = iv

            def _is_in_play_ball(ball_value: Optional[int], count_value: Optional[int]) -> bool:
                if ball_value is None:
                    return False
                if ball_value <= 0:
                    return False
                if count_value is None or count_value <= 0:
                    return True
                return ball_value <= count_value

            current_in_play_ball = _is_in_play_ball(current_ball, ball_count)
            prev_in_play_ball = _is_in_play_ball(prev_current_ball, ball_count)
            progress_idle = (now - st.last_progress_ts) if st.last_progress_ts else 0.0

            if game_over is True and prev_game_over is not True and stable and st.has_nonstart_update:
                self._maybe_emit_game_end(rom, st, parsed, 'game_over')
            elif (not current_in_play_ball) and prev_in_play_ball and stable and st.has_nonstart_update:
                # Generic fallback from map spec: current_ball values of 0 or
                # values larger than ball_count indicate "no game in progress".
                self._maybe_emit_game_end(rom, st, parsed, 'ball_out_of_play_stable')
            elif (
                game_over is True
                and stable
                and st.has_nonstart_update
                and st.session_best > 0
                and progress_idle >= self.game_end_stable_sec
            ):
                # Generic fallback when edge transitions were missed but end-of-game
                # state is latched and score progression has stopped.
                self._maybe_emit_game_end(rom, st, parsed, 'game_over_latched')
            elif (
                st.has_nonstart_update
                and st.session_best > 0
                and progress_idle >= self.game_end_stable_sec
                and final_best > 0
                and final_best >= int(st.session_best * 0.80)
                and best <= int(st.session_best * 0.25)
            ):
                # Fallback for families where end-of-game DMD cycles keep changing
                # "scores" rapidly (preventing score-stability checks), but final_scores
                # already contain the just-finished game result.
                self._maybe_emit_game_end(rom, st, parsed, 'final_scores_no_progress')
            elif (
                st.has_nonstart_update
                and st.session_best > 0
                and st.attract_pattern_ts > 0
                and (now - st.attract_pattern_ts) >= 2.0
                and progress_idle >= self.game_end_stable_sec
                and looks_nonplay
            ):
                # Generic fallback when maps do not expose reliable game_over/current_ball:
                # if we have seen attract-style mirrored scores and no in-game score progress,
                # treat it as game end without waiting for VPX process exit.
                self._maybe_emit_game_end(rom, st, parsed, 'attract_pattern_no_progress')

        st.last_best = best
        st.last_match_counter = parsed.get('match_counter')
        st.last_game_over = parsed.get('game_over')
        st.last_current_ball = parsed.get('current_ball')

    def _check_idle_ends(self):
        now = time.time()
        for rom, st in self._state.items():
            if not st.active:
                continue
            if not st.has_nonstart_update:
                # Session was force-started (e.g. file handle detected) but no
                # real NVRAM content updates were observed yet.
                continue
            idle = now - st.last_update_ts
            stable = now - st.last_change_ts
            if idle >= self.idle_end_sec and stable >= self.game_end_stable_sec:
                parsed = {
                    'scores': list(st.last_scores) if st.last_scores else [st.session_best],
                    'best': max(st.session_best, st.last_best),
                }
                self._maybe_emit_game_end(rom, st, parsed, 'idle_timeout')
                if self.active_rom == rom:
                    self.active_rom = None
                    self._log_waiting(force=True)

    def _check_stale_disk_updates(self):
        now = time.time()
        for rom, st in self._state.items():
            if not st.active:
                continue
            if st.has_nonstart_update:
                continue
            if st.warned_no_disk_updates:
                continue
            if not st.session_start_ts:
                continue
            if (now - st.session_start_ts) < 20.0:
                continue
            st.warned_no_disk_updates = True
            self._log(
                'WARN',
                (
                    f'No NVRAM disk updates observed for active ROM "{rom}" after '
                    f'{now - st.session_start_ts:.0f}s; this VPX build appears to flush NVRAM on exit'
                ),
            )

    def _check_vpx_play_exit(self):
        if self.active_rom is None:
            return
        st = self._state.get(self.active_rom)
        if st is None:
            self.active_rom = None
            self.active_path = None
            if self._live_session is not None:
                self._live_session.detach()
            self._last_live_crc_by_rom.clear()
            self._log_waiting(force=True)
            return

        # Grace period avoids false exits while VPX is still starting/relaunching.
        if st.session_start_ts and (time.time() - st.session_start_ts) < 5.0:
            return

        if self._has_vpx_play_process():
            return

        if os.name == 'nt' and self._last_any_vpx_seen_ts > 0:
            if (time.time() - self._last_any_vpx_seen_ts) < 20.0:
                return

        if not st.active:
            # ROM was monitored but never reached in-play state.
            self.active_rom = None
            self.active_path = None
            if self._live_session is not None:
                self._live_session.detach()
            self._last_live_crc_by_rom.clear()
            self._log_waiting(force=True)
            return

        parsed = None
        if self.active_path:
            raw = self._read_file(self.active_path)
            if raw is not None:
                parsed = self._extract_game_state(self.active_rom, raw)

        if parsed is None:
            parsed = {
                'scores': list(st.last_scores) if st.last_scores else [st.session_best],
                'best': max(st.session_best, st.last_best),
            }
        self._maybe_emit_game_end(self.active_rom, st, parsed, 'vpx_play_exit')
        self.active_rom = None
        self.active_path = None
        if self._live_session is not None:
            self._live_session.detach()
        self._last_live_crc_by_rom.clear()
        self._log_waiting(force=True)

    def _iter_nvram_paths(self):
        # Support multiple glob patterns separated by ';' or ','.
        raw = self.nvram_scan_pattern or '*.nv'
        parts = [p.strip() for p in re.split(r'[;,]', raw) if p.strip()]
        if not parts:
            parts = ['*.nv']

        seen = set()
        for pattern in parts:
            if os.path.isabs(pattern):
                full_pattern = pattern
            else:
                full_pattern = os.path.join(self.nvram_dir, pattern)

            recursive = '**' in full_pattern
            for path in glob.iglob(full_pattern, recursive=recursive):
                if not path.lower().endswith('.nv'):
                    continue
                if not os.path.isfile(path):
                    continue
                cp = self._canon_path(path)
                if cp in seen:
                    continue
                seen.add(cp)
                yield path

    def _log_waiting(self, force: bool = False):
        now = time.time()
        if force or (now - self._last_wait_log_ts) >= 15:
            self._log('INFO', 'Waiting for game to start')
            self._last_wait_log_ts = now

    def _emit_status_message(self, title: str, message: str):
        cb = self.on_status_message
        if cb is None:
            return
        try:
            cb(title, message)
        except Exception:
            pass

    def _log_unsupported_rom(self, rom: str, context: str = ''):
        now = time.time()
        last = self._last_unsupported_warn_by_rom.get(rom, 0.0)
        if (now - last) < 30.0:
            return
        is_ram_only = 'reads RAM' in (context or '')
        if is_ram_only:
            self._log('WARN', f'ROM "{rom}" doesn\'t support NVRAM reading.')
            self._log('INFO', 'Close the table to send your scores.')
            self._emit_status_message(
                'ROM Not Supported',
                f'ROM {rom} doesn\'t support NVRAM reading.\nClose the table to send your scores.',
            )
        else:
            msg = f'ROM "{rom}" not supported'
            if context:
                msg = f'{msg} ({context})'
            self._log('WARN', msg)
            self._emit_status_message('ROM Not Supported', f'Rom {rom} not Support')
        self._last_unsupported_warn_by_rom[rom] = now

    def _clear_unsupported_active(self):
        self._unsupported_active_rom = None
        self._unsupported_active_path = None
        self._unsupported_active_since = 0.0

    def _mark_unsupported_active(self, rom: str, path: str, reason: str = ''):
        if self._unsupported_active_rom == rom and self._unsupported_active_path == path:
            return
        self._unsupported_active_rom = rom
        self._unsupported_active_path = path
        self._unsupported_active_since = time.time()
        is_ram_only = 'reads RAM' in (reason or '')
        if is_ram_only:
            self._log('WARN', f'ROM "{rom}" doesn\'t support NVRAM reading.')
            self._log('INFO', 'Close the table to send your scores.')
            self._emit_status_message(
                'ROM Not Supported',
                f'ROM {rom} doesn\'t support NVRAM reading.\nClose the table to send your scores.',
            )
        else:
            self._log('WARN', f'ROM "{rom}" not supported via NVRAM reading')
            self._log('INFO', 'Score will be sent after closing VPX')
            self._emit_status_message('ROM Not Supported', f'Rom {rom} not Support')

    def _try_emit_unsupported_post_exit(self) -> bool:
        rom = self._unsupported_active_rom
        path = self._unsupported_active_path
        if not rom or not path:
            return False
        if self._has_vpx_play_process():
            return False

        raw = self._read_file(path)
        parsed = self._extract_game_state(rom, raw) if raw is not None else None
        if not parsed:
            self._log('WARN', f'Post-exit NVRAM parse failed for ROM "{rom}"')
            self._clear_unsupported_active()
            self._log_waiting(force=True)
            return True

        scores = parsed.get('scores') or []
        best = parsed.get('best', 0)
        if best <= 0 and scores:
            norm_scores: List[int] = []
            for v in scores:
                try:
                    iv = int(v)
                except Exception:
                    iv = 0
                norm_scores.append(max(0, iv))
            scores = norm_scores
            best = max(norm_scores, default=0)
        if best <= 0:
            final_scores = parsed.get('final_scores') or []
            if isinstance(final_scores, list) and final_scores:
                norm_final: List[int] = []
                for v in final_scores:
                    try:
                        iv = int(v)
                    except Exception:
                        iv = 0
                    norm_final.append(max(0, iv))
                if norm_final:
                    scores = norm_final
                    best = max(norm_final, default=0)

        if best > 0:
            payload = list(scores) if scores else [best]
            self.on_game_end(rom, payload, 'vpx_exit_unsupported_nvram', None)
            self._log('INFO', f'Post-exit score read from NVRAM for ROM "{rom}"')
        else:
            self._log('WARN', f'Post-exit NVRAM read for ROM "{rom}" did not contain a valid score')
            self._log('WARN', f'ROM "{rom}" appears to keep player score in volatile RAM; no recoverable score in .nv after VPX exit')

        self._clear_unsupported_active()
        self._log_waiting(force=True)
        return True

    @staticmethod
    def _desc_addresses(desc: dict) -> List[int]:
        if not isinstance(desc, dict):
            return []
        if isinstance(desc.get('offsets'), list):
            out: List[int] = []
            for v in desc.get('offsets', []):
                iv = _parse_int(v)
                if iv is not None:
                    out.append(iv)
            return out

        start = _parse_int(desc.get('start'))
        if start is None:
            return []
        length = _parse_int(desc.get('length'))
        if length is not None:
            if length < 1:
                return []
            if length > 1024:
                return [start]
            return list(range(start, start + length))
        end = _parse_int(desc.get('end'))
        if end is not None and end >= start:
            if (end - start) > 1024:
                return [start, end]
            return list(range(start, end + 1))
        return [start]

    @staticmethod
    def _addr_region_type(addr: int, layout: List[dict]) -> Optional[str]:
        for e in layout:
            a = _parse_int(e.get('address'))
            s = _parse_int(e.get('size'))
            if a is None or s is None or s <= 0:
                continue
            if a <= addr < (a + s):
                t = (e.get('type') or '').lower()
                return t or None
        return None

    def _supports_live_game_state(self, rom: str) -> Tuple[bool, str]:
        cached = self._rom_live_support_cache.get(rom)
        if cached is not None:
            return cached

        map_data = self.repo.map_for_rom(rom)
        if not isinstance(map_data, dict):
            out = (False, 'map missing')
            self._rom_live_support_cache[rom] = out
            return out

        gs = map_data.get('game_state')
        if not isinstance(gs, dict):
            out = (False, 'missing game_state')
            self._rom_live_support_cache[rom] = out
            return out

        platform_data = self.repo.platform_for_map(map_data) or {}
        layout = platform_data.get('memory_layout')
        if not isinstance(layout, list):
            out = (False, 'missing platform memory_layout')
            self._rom_live_support_cache[rom] = out
            return out

        # Support gate intentionally focuses on what we actually need:
        # - live player scores from NVRAM
        # - game_over from NVRAM when mapped
        # Some families keep current_ball/current_player in RAM, and those should
        # not make the whole ROM unsupported.
        scores_desc = gs.get('scores')
        score_entries = scores_desc if isinstance(scores_desc, list) else [scores_desc]
        score_checked = 0
        for d in score_entries:
            if not isinstance(d, dict):
                continue
            addrs = self._desc_addresses(d)
            if not addrs:
                continue
            score_checked += 1
            for a in addrs:
                typ = self._addr_region_type(a, layout)
                if typ == 'ram':
                    out = (False, f'game_state.scores reads RAM (0x{a:X})')
                    self._rom_live_support_cache[rom] = out
                    return out

        if score_checked == 0:
            out = (False, 'missing game_state.scores descriptors')
            self._rom_live_support_cache[rom] = out
            return out

        game_over_desc = gs.get('game_over')
        if isinstance(game_over_desc, dict):
            addrs = self._desc_addresses(game_over_desc)
            for a in addrs:
                typ = self._addr_region_type(a, layout)
                if typ == 'ram':
                    out = (False, f'game_state.game_over reads RAM (0x{a:X})')
                    self._rom_live_support_cache[rom] = out
                    return out

        out = (True, '')
        self._rom_live_support_cache[rom] = out
        return out

    def _prime_baseline(self):
        count = 0
        for path in self._iter_nvram_paths():
            try:
                self._file_mtime[path] = os.path.getmtime(path)
                raw = self._read_file(path)
                if raw is not None:
                    self._file_crc32[path] = zlib.crc32(raw) & 0xFFFFFFFF
                count += 1
            except Exception:
                continue
        self._log('INFO', f'Baseline loaded for {count} nvram file(s)')

    def _process_nvram_update(self, rom: str, path: str, raw: bytes, force_start: bool = False):
        parsed = self._extract_game_state(rom, raw)
        if not parsed:
            return

        self._handle_update(rom, parsed, force_start=force_start)
        # Do not clear active_rom here if game has not started yet. Keeping the
        # monitored ROM active allows live PinMAME polling to continue.

    def _detect_open_nvram_paths(self, paths: List[str]) -> List[str]:
        if not paths:
            return []
        if os.name == 'nt':
            return []

        try:
            # Query which monitored .nv files are currently opened by any process.
            proc = subprocess.run(
                ['lsof', '-Fn', '--', *paths],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
        except Exception:
            return []

        known_map: Dict[str, str] = {}
        by_name: Dict[str, str] = {}
        for p in paths:
            cp = self._canon_path(p)
            known_map.setdefault(cp, p)
            by_name.setdefault(os.path.basename(p).lower(), p)

        open_paths: List[str] = []
        for line in proc.stdout.splitlines():
            if not line.startswith('n'):
                continue
            p = line[1:]
            if not p.lower().endswith('.nv'):
                continue
            cp = self._canon_path(p)
            mapped = known_map.get(cp)
            if mapped:
                open_paths.append(mapped)
                continue
            mapped = by_name.get(os.path.basename(p).lower())
            if mapped:
                open_paths.append(mapped)
        return open_paths

    def _list_vpx_processes(self) -> List[Tuple[int, str, str]]:
        # Cache result for 2 seconds to avoid spawning PowerShell (Windows) or
        # ps (Linux) on every sub-call within the same poll tick.
        now = time.time()
        if self._vpx_proc_cache is not None and (now - self._vpx_proc_cache_ts) < 2.0:
            return self._vpx_proc_cache
        result = self._scan_vpx_processes()
        self._vpx_proc_cache = result
        self._vpx_proc_cache_ts = now
        return result

    def _scan_vpx_processes(self) -> List[Tuple[int, str, str]]:
        procs: List[Tuple[int, str, str]] = []
        if os.name == 'nt':
            try:
                task = subprocess.run(
                    ['tasklist', '/FO', 'CSV', '/NH'],
                    capture_output=True,
                    text=True,
                    timeout=2.5,
                    check=False,
                )
            except Exception:
                task = None

            if task is not None:
                import csv

                for row in csv.reader(task.stdout.splitlines()):
                    if len(row) < 2:
                        continue
                    comm = (row[0] or '').strip()
                    if not re.match(r'(?i)^vpinball.*\.exe$', comm):
                        continue
                    try:
                        pid = int((row[1] or '').replace(',', '').strip())
                    except Exception:
                        continue
                    procs.append((pid, comm, ''))

            if not procs:
                now = time.time()
                if (now - self._last_vpx_presence_diag_ts) >= 15.0:
                    self._log('WARN', 'Windows VPX process scan found no matching process')
                    self._last_vpx_presence_diag_ts = now
            else:
                self._last_any_vpx_seen_ts = time.time()
            return procs

        try:
            proc = subprocess.run(
                ['ps', '-axww', '-o', 'pid=,comm=,command='],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
        except Exception:
            return procs

        for line in proc.stdout.splitlines():
            s = line.strip()
            if not s:
                continue
            parts = s.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
            except Exception:
                continue
            comm = parts[1].strip()
            cmdline = parts[2].strip()
            low = f'{comm} {cmdline}'.lower()
            if 'vpinball' not in low:
                continue
            procs.append((pid, comm, cmdline))
        if procs:
            self._last_any_vpx_seen_ts = time.time()
        return procs

    @staticmethod
    def _read_linux_proc_cmdline(pid: int) -> str:
        if pid <= 0 or not sys.platform.startswith('linux'):
            return ''
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                raw = f.read()
        except Exception:
            return ''
        if not raw:
            return ''
        return raw.replace(b'\x00', b' ').decode('utf-8', errors='replace').strip()

    @staticmethod
    def _read_linux_proc_comm(pid: int) -> str:
        if pid <= 0 or not sys.platform.startswith('linux'):
            return ''
        try:
            with open(f'/proc/{pid}/comm', 'r', encoding='utf-8', errors='replace') as f:
                return f.read().strip()
        except Exception:
            return ''

    @staticmethod
    def _is_live_pid(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == 'nt':
            # PIDs returned by tasklist are current by definition. Avoid os.kill(pid, 0)
            # on Windows here; it is not a reliable liveness probe for this use.
            return True
        if sys.platform.startswith('linux'):
            try:
                with open(f'/proc/{pid}/stat', 'r', encoding='utf-8', errors='replace') as f:
                    stat = f.read().strip()
                parts = stat.split()
                if len(parts) >= 3 and parts[2] == 'Z':
                    return False
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _normalize_cmd_path(self, raw_path: str) -> str:
        p = raw_path.strip().strip('"').strip("'")
        if not p:
            return ''
        if os.name != 'nt' and re.match(r'^[a-zA-Z]:\\', p):
            drive = p[0].upper()
            rest = p[2:].replace('\\', '/')
            if drive == 'Z':
                p = '/' + rest.lstrip('/')
        p = os.path.expanduser(p)
        return os.path.normpath(p)

    @staticmethod
    def _name_key(name: str) -> str:
        return re.sub(r'[^a-z0-9]+', '', (name or '').lower())

    @staticmethod
    def _name_tokens(name: str) -> List[str]:
        raw_tokens = re.findall(r'[a-z0-9]+', (name or '').lower())
        stop = {
            'the',
            'and',
            'edition',
            'ed',
            'pup',
            'vr',
            'fs',
            'mod',
            'mods',
            'version',
            'ver',
            'rev',
            'special',
            'ultimate',
            'table',
            'pinball',
        }
        out: List[str] = []
        for tok in raw_tokens:
            if tok in stop:
                continue
            if len(tok) <= 1:
                continue
            if re.fullmatch(r'v?[0-9]+[a-z]*', tok):
                continue
            out.append(tok)
        return out

    def _has_vpx_play_process(self) -> bool:
        procs = self._list_vpx_processes()
        if os.name == 'nt':
            if procs:
                now = time.time()
                if (now - self._last_vpx_presence_diag_ts) >= 15.0:
                    sample_pid, sample_comm, sample_cmd = procs[0]
                    self._log(
                        'INFO',
                        (
                            f'VPX process detected on Windows '
                            f'(pid={sample_pid} bin="{sample_comm}" cmd="{sample_cmd or "<empty>"}"); '
                            'using process-presence detection'
                        ),
                    )
                    self._last_vpx_presence_diag_ts = now
                return True
            return False
        for _, _, cmd in procs:
            if re.search(r'(?i)(?:^|\s)-play(?:\s|$)', cmd):
                return True
        # Fallback: any VPX binary running is treated as playing.
        # On Linux, wrappers/launchers may not forward -play; on Windows,
        # Win32_Process sometimes returns an empty CommandLine.
        if procs:
            now = time.time()
            if (now - self._last_vpx_presence_diag_ts) >= 15.0:
                sample_pid, sample_comm, sample_cmd = procs[0]
                self._log(
                    'INFO',
                    (
                        f'VPX process detected without -play in command line '
                        f'(pid={sample_pid} bin="{sample_comm}" cmd="{sample_cmd or "<empty>"}"); '
                        'using process-presence detection'
                    ),
                )
                self._last_vpx_presence_diag_ts = now
            return True
        return False

    def _extract_table_path_from_cmdline(self, cmdline: str) -> Optional[str]:
        # Common VPX form: -play <table>.vpx, often unquoted even with spaces.
        m = re.search(r'(?i)(?:^|\s)-play\s+(.+?\.vpx)(?:\s+-[a-zA-Z0-9_]+\b.*|$)', cmdline)
        if m:
            p = self._normalize_cmd_path(m.group(1))
            if p:
                return p

        # Fallback: explicit .vpx path anywhere in command line.
        matches = re.findall(r'"([^"]+\.vpx)"|\'([^\']+\.vpx)\'|(\S+\.vpx)', cmdline, flags=re.IGNORECASE)
        if not matches:
            return None
        for m in reversed(matches):
            raw = m[0] or m[1] or m[2]
            p = self._normalize_cmd_path(raw)
            if p:
                return p
        return None

    def _resolve_table_dir_from_arg(self, table_arg: Optional[str]) -> Optional[str]:
        if not table_arg:
            return None
        p = self._normalize_cmd_path(table_arg)
        if not p:
            return None

        def _has_nvram_subdir(d: Optional[str]) -> bool:
            if not d or not os.path.isdir(d):
                return False
            return os.path.isdir(os.path.join(d, 'pinmame', 'nvram'))

        if os.path.isabs(p):
            d = os.path.dirname(self._canon_path(p))
            if _has_nvram_subdir(d):
                return d

        if '/' in p or '\\' in p:
            joined = self._canon_path(os.path.join(self.nvram_dir, p))
            d = os.path.dirname(joined)
            if _has_nvram_subdir(d):
                return d

        base = os.path.basename(p)
        if not base or not os.path.isdir(self.nvram_dir):
            return None

        # Prefer resolving the actual VPX file under the configured tables root.
        # This is robust even when the launch filename contains extra tags like
        # VR/Pup/Version while the folder name stays concise.
        vpx_matches: List[str] = []
        seen = set()
        try:
            pattern = os.path.join(self.nvram_dir, '**', base)
            for match in glob.iglob(pattern, recursive=True):
                if not match.lower().endswith('.vpx'):
                    continue
                cp = self._canon_path(match)
                if cp in seen or not os.path.isfile(cp):
                    continue
                seen.add(cp)
                vpx_matches.append(cp)
        except Exception:
            vpx_matches = []
        if len(vpx_matches) == 1:
            d = os.path.dirname(vpx_matches[0])
            if _has_nvram_subdir(d):
                return d

        stem = os.path.splitext(base)[0]
        if not stem:
            return None

        stem_l = stem.lower()
        stem_k = self._name_key(stem)
        stem_tokens = set(self._name_tokens(stem))
        first_norm: Optional[str] = None
        first_fuzzy: Optional[str] = None
        best_token_match: Optional[str] = None
        best_token_score = 0.0
        try:
            for ent in os.scandir(self.nvram_dir):
                if not ent.is_dir():
                    continue
                nm = ent.name
                nm_l = nm.lower()
                nm_k = self._name_key(nm)
                nm_tokens = set(self._name_tokens(nm))
                if nm_l == stem_l:
                    return ent.path
                if first_norm is None and nm_k == stem_k:
                    first_norm = ent.path
                if first_fuzzy is None and nm_k and stem_k and (nm_k in stem_k or stem_k in nm_k):
                    first_fuzzy = ent.path
                if stem_tokens and nm_tokens:
                    overlap = stem_tokens & nm_tokens
                    if overlap:
                        coverage = len(overlap) / max(1, len(nm_tokens))
                        strength = len(overlap) + coverage
                        if len(overlap) >= 2 and strength > best_token_score:
                            best_token_score = strength
                            best_token_match = ent.path
        except Exception:
            return None
        return first_norm or first_fuzzy or best_token_match

    @staticmethod
    def _extract_exec_from_cmdline(cmdline: str) -> str:
        s = (cmdline or '').strip()
        if not s:
            return ''
        if s[0] in ('"', "'"):
            q = s[0]
            end = s.find(q, 1)
            if end > 1:
                return s[1:end]
        return s.split(None, 1)[0]

    def _is_allowed_attach_target(self, comm: str, cmdline: str) -> Tuple[bool, str]:
        # Binary-name check is sufficient to gate Frida attachment.
        # The -play flag is intentionally not required: on Linux, wrappers and
        # launchers may not forward it, and processes can rewrite argv after exec.
        # The -play flag is still used as a soft preference in _candidate_vpx_pids().
        exe = self._extract_exec_from_cmdline(cmdline) or (comm or '')
        base = os.path.basename(exe.strip().strip('"').strip("'")).lower()
        if base not in self._allowed_attach_exec_basenames:
            return False, f'non-bgfx executable ({base or "unknown"})'
        return True, ''

    def _inspect_vpx_open_files(self, pid: int, known_map: Dict[str, str], by_name: Dict[str, str]) -> Tuple[List[str], Optional[str]]:
        if pid <= 0:
            return [], None

        # Shared extractor: given a list of file paths, build the nv/table result.
        def _extract(paths):
            found_nv: List[str] = []
            table_path: Optional[str] = None
            for raw in paths:
                p = self._canon_path(raw)
                lp = p.lower()
                if lp.endswith('.vpx') and table_path is None:
                    table_path = p
                if lp.endswith('.nv'):
                    mapped = known_map.get(p)
                    if mapped:
                        found_nv.append(mapped)
                        continue
                    mapped = by_name.get(os.path.basename(p).lower())
                    if mapped:
                        found_nv.append(mapped)
            return found_nv, table_path

        if os.name == 'nt':
            # Windows uses tasklist-based process presence only. Avoid probing live
            # file handles here; that path is not required for flat NVRAM layouts
            # and is the riskiest pre-attach interaction with the VPX process.
            return [], None

        # Linux / macOS: use lsof.
        try:
            proc = subprocess.run(
                ['lsof', '-Fn', '-p', str(pid)],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
        except Exception:
            return [], None

        return _extract(line[1:] for line in proc.stdout.splitlines() if line.startswith('n'))

    def _detect_vpx_nvram_paths(self, candidate_paths: List[str]) -> List[str]:
        now = time.time()
        if (now - self._last_vpx_probe_ts) < 3.0:
            return []
        self._last_vpx_probe_ts = now

        if not candidate_paths:
            return []
        known_map: Dict[str, str] = {}
        by_name: Dict[str, str] = {}
        for p in candidate_paths:
            cp = self._canon_path(p)
            known_map.setdefault(cp, p)
            by_name.setdefault(os.path.basename(p).lower(), p)
        out: List[str] = []
        proc_count = 0
        sample_proc: Optional[Tuple[int, str, str]] = None

        for pid, comm, cmdline in self._list_vpx_processes():
            proc_count += 1
            if sample_proc is None:
                sample_proc = (pid, comm, cmdline)
            table_path = self._extract_table_path_from_cmdline(cmdline)
            pid_nv, pid_table = self._inspect_vpx_open_files(pid, known_map, by_name)
            if pid_nv:
                out.extend(pid_nv)
            if table_path is None and pid_table:
                table_path = pid_table
            if not table_path:
                continue
            table_dir = self._resolve_table_dir_from_arg(table_path)
            if not table_dir:
                continue

            nv_dir = os.path.join(table_dir, 'pinmame', 'nvram')
            if not os.path.isdir(nv_dir):
                continue
            for p in glob.glob(os.path.join(nv_dir, '*.nv')):
                cp = self._canon_path(p)
                mapped = known_map.get(cp)
                if mapped:
                    out.append(mapped)

        if proc_count > 0 and not out and (now - self._last_vpx_diag_ts) >= 15.0:
            self._log('WARN', 'VPX process detected but no mapped NVRAM path was inferred from command line/open files')
            if sample_proc is not None:
                pid, comm, cmdline = sample_proc
                cmd_snip = cmdline if len(cmdline) <= 220 else (cmdline[:220] + '...')
                exe = self._extract_exec_from_cmdline(cmdline) or comm
                self._log('INFO', f'VPX sample: pid={pid} bin="{exe}" cmd="{cmd_snip}"')
            self._last_vpx_diag_ts = now

        if proc_count > 0 and not out and os.name == 'nt':
            # Windows fallback: VPinMAME loads the .nv file into RAM then closes
            # the handle, so open-file enumeration (psutil) and the cmdline path
            # extraction both come up empty.  Monitor every supported .nv file in
            # the base NVRAM directory; the first file-system change — either a
            # mid-game flush by PinMAME or the final write on VPX exit — will
            # identify the active ROM via the normal file-change pipeline.
            flat_supported: List[str] = []
            root_cp = self._canon_path(self.nvram_dir)
            for p in candidate_paths:
                rom = os.path.splitext(os.path.basename(p))[0]
                if not self.repo.has_rom(rom):
                    continue
                if self._canon_path(os.path.dirname(p)) != root_cp:
                    continue
                flat_supported.append(p)
            if flat_supported:
                if (now - self._last_vpx_diag_ts) >= 15.0:
                    if len(flat_supported) == 1:
                        self._log(
                            'INFO',
                            f'Windows NVRAM fallback: monitoring "{os.path.basename(flat_supported[0])}"',
                        )
                    else:
                        names = ', '.join(os.path.basename(p) for p in flat_supported[:5])
                        more = f' (+{len(flat_supported) - 5} more)' if len(flat_supported) > 5 else ''
                        self._log(
                            'INFO',
                            (
                                f'Windows NVRAM fallback: monitoring {len(flat_supported)} supported ROMs '
                                f'({names}{more}); first file change will identify the active table'
                            ),
                        )
                    self._last_vpx_diag_ts = now
                out.extend(flat_supported)

        deduped: List[str] = []
        seen = set()
        for p in out:
            if p in seen:
                continue
            seen.add(p)
            deduped.append(p)
        return deduped

    def _windows_flat_supported_paths(self, candidate_paths: List[str]) -> List[str]:
        if os.name != 'nt':
            return []
        out: List[str] = []
        for p in candidate_paths:
            rom = os.path.splitext(os.path.basename(p))[0]
            if not self.repo.has_rom(rom):
                continue
            out.append(p)
        return out

    def _scan_once(self):
        if not os.path.isdir(self.nvram_dir):
            return

        if self._try_emit_unsupported_post_exit():
            return

        candidate_paths = list(self._iter_nvram_paths())
        if not candidate_paths:
            return

        if self._try_live_pinmame_update(candidate_paths):
            return

        changed: List[Tuple[str, float, str, bytes]] = []
        for path in candidate_paths:
            rom = os.path.splitext(os.path.basename(path))[0]
            if not self.repo.has_rom(rom):
                continue

            try:
                mtime = os.path.getmtime(path)
            except Exception:
                continue

            raw = self._read_file(path)
            if raw is None:
                continue

            crc = zlib.crc32(raw) & 0xFFFFFFFF

            prev_crc = self._file_crc32.get(path)
            self._file_mtime[path] = mtime
            self._file_crc32[path] = crc

            if prev_crc is None:
                continue

            if crc == prev_crc:
                continue

            changed.append((path, mtime, rom, raw))

        if not changed:
            if self.active_rom is None:
                open_paths = self._detect_open_nvram_paths(candidate_paths)
                inferred_paths = open_paths
                if os.name != 'nt':
                    vpx_paths = self._detect_vpx_nvram_paths(candidate_paths)
                    inferred_paths = inferred_paths or vpx_paths
                elif self._has_vpx_play_process():
                    inferred_paths = self._windows_flat_supported_paths(candidate_paths)
                if inferred_paths:
                    inferred_paths = sorted(inferred_paths, key=lambda p: self._file_mtime.get(p, 0), reverse=True)
                    for active_path in inferred_paths:
                        rom = os.path.splitext(os.path.basename(active_path))[0]
                        if not self.repo.has_rom(rom):
                            self._log_unsupported_rom(rom, 'detected as active NVRAM')
                            continue
                        supported, reason = self._supports_live_game_state(rom)
                        if not supported:
                            self._mark_unsupported_active(rom, active_path, reason)
                            continue
                        raw = self._read_file(active_path)
                        if raw is not None:
                            self._clear_unsupported_active()
                            self.active_rom = rom
                            self.active_path = active_path
                            if open_paths:
                                source_reason = 'open handle'
                            elif os.name == 'nt':
                                source_reason = 'Windows flat fallback'
                            else:
                                source_reason = 'VPX process'
                            self._log(f'INFO', f'NV "{os.path.basename(active_path)}" is active ({source_reason}), monitoring ROM "{rom}"')
                            self._process_nvram_update(
                                rom,
                                active_path,
                                raw,
                                force_start=(os.name == 'nt' and not open_paths),
                            )
                            break
            return

        changed.sort(key=lambda x: x[1], reverse=True)

        if self.active_rom:
            active_changes = [c for c in changed if c[2] == self.active_rom]
            if active_changes:
                for path, _, rom, raw in sorted(active_changes, key=lambda x: x[1]):
                    self._process_nvram_update(rom, path, raw, force_start=False)
                return

            st = self._state.get(self.active_rom)
            if st is not None and st.active:
                return
            self.active_rom = None
            self.active_path = None

        if not self._has_vpx_play_process():
            # Ignore standalone disk flushes when no active VPX play process is running.
            return
        selected_rom: Optional[str] = None
        selected_path: Optional[str] = None
        for path, _, rom, raw in changed:
            supported, reason = self._supports_live_game_state(rom)
            if not supported:
                self._mark_unsupported_active(rom, path, reason)
                continue
            self._clear_unsupported_active()
            self.active_rom = rom
            self.active_path = path
            self._log('INFO', f'NV "{os.path.basename(path)}" is active, monitoring ROM "{rom}"')
            self._process_nvram_update(rom, path, raw, force_start=False)
            selected_rom = rom
            selected_path = path
            break
        if selected_rom is None:
            return
        for extra_path, _, extra_rom, extra_raw in changed:
            if selected_path is not None and extra_path == selected_path:
                continue
            if extra_rom == selected_rom:
                self._process_nvram_update(extra_rom, extra_path, extra_raw, force_start=False)

    def _candidate_vpx_pids(self) -> List[int]:
        procs = self._list_vpx_processes()
        if not procs:
            return []
        preferred: List[int] = []
        fallback: List[int] = []
        filtered: List[Tuple[int, str, str, str]] = []
        for pid, comm, cmd in procs:
            if not self._is_live_pid(pid):
                continue
            live_cmd = self._read_linux_proc_cmdline(pid) or cmd
            live_comm = self._read_linux_proc_comm(pid) or comm
            ok, reason = self._is_allowed_attach_target(live_comm, live_cmd)
            if not ok:
                filtered.append((pid, live_comm, live_cmd, reason))
                continue
            if re.search(r'(?i)(?:^|\s)-play(?:\s|$)', live_cmd):
                preferred.append(pid)
            else:
                fallback.append(pid)

        # Keep only unique PIDs; on Linux prefer the newest process first.
        preferred = list(dict.fromkeys(preferred))
        fallback = list(dict.fromkeys(fallback))
        if sys.platform.startswith('linux'):
            preferred.sort(reverse=True)
            fallback.sort(reverse=True)

        now = time.time()
        diag_item = filtered[0] if filtered else None
        if diag_item and (now - self._last_attach_filter_diag_ts) >= 15.0:
            pid, comm, cmd, reason = diag_item
            exe = self._extract_exec_from_cmdline(cmd) or comm
            self._log(
                'INFO',
                (
                    f'Frida attach filter skipped pid={pid} bin="{exe}" '
                    f'reason={reason} (BGFX-only policy)'
                ),
            )
            self._last_attach_filter_diag_ts = now
        if os.name == 'nt' and not (preferred or fallback) and procs and (now - self._last_attach_filter_diag_ts) >= 15.0:
            sample_pid, sample_comm, sample_cmd = procs[0]
            exe = self._extract_exec_from_cmdline(sample_cmd) or sample_comm
            self._log(
                'WARN',
                (
                    f'Windows VPX PID selection produced no attach candidates '
                    f'(sample pid={sample_pid} bin="{exe}" cmd="{sample_cmd or "<empty>"}")'
                ),
            )
            self._last_attach_filter_diag_ts = now
        return preferred + fallback

    def _try_live_pinmame_update(self, candidate_paths: List[str]) -> bool:
        if not self.use_live_pinmame:
            return False
        if self._live_session is None:
            return False
        if self.active_rom is None:
            return False

        rom = self.active_rom
        if not self.repo.has_rom(rom):
            return False
        if rom in self._live_unsupported_by_rom:
            return False
        if os.name == 'nt':
            st = self._state.get(rom)
            if st is not None and st.session_start_ts:
                attach_age = time.time() - st.session_start_ts
                if attach_age < 8.0:
                    return False
        supported, reason = self._supports_live_game_state(rom)
        if not supported:
            self._log_unsupported_rom(rom, reason)
            return False

        pids = self._candidate_vpx_pids()
        if not pids:
            return False

        def _ordered_attach_targets(raw_pids: List[int], preferred_pid: Optional[int]) -> List[int]:
            ordered: List[int] = []
            if preferred_pid is not None and preferred_pid in raw_pids:
                ordered.append(preferred_pid)
            for candidate_pid in raw_pids:
                if candidate_pid not in ordered:
                    ordered.append(candidate_pid)
            return ordered

        def _attach_first(target_pids: List[int]) -> Tuple[bool, Optional[int]]:
            for candidate_pid in target_pids:
                now = time.time()
                if (now - self._last_live_attach_attempt_log_ts) >= 10.0:
                    self._log('INFO', f'Attempting Live PinMAME attach to pid={candidate_pid} for ROM "{rom}"')
                    self._last_live_attach_attempt_log_ts = now
                if self._live_session.attach(candidate_pid):
                    return True, candidate_pid
            return False, None

        attached_pid = self._live_session.attached_pid
        ordered_pids = _ordered_attach_targets(pids, attached_pid)
        attach_ok, active_pid = _attach_first(ordered_pids)

        # Linux can race on VPX restart PID churn; rescan once on process-not-found.
        if not attach_ok:
            first_error = self._live_session.last_error or ''
            if 'ProcessNotFoundError' in first_error:
                retry_pids = self._candidate_vpx_pids()
                if retry_pids:
                    retry_attached_pid = self._live_session.attached_pid
                    retry_ordered = _ordered_attach_targets(retry_pids, retry_attached_pid)
                    attach_ok, active_pid = _attach_first(retry_ordered)

        if not attach_ok:
            now = time.time()
            if (now - self._last_live_diag_ts) >= 15.0:
                reason = self._live_session.last_error or 'unknown_attach_error'
                self._log('WARN', f'Live PinMAME attach failed ({reason}), using .nv file polling only')
                if 'ProcessNotFoundError' in reason:
                    self._log('INFO', f'Live PinMAME candidate PIDs at failure: {ordered_pids}')
                    live_candidates = [pid for pid in ordered_pids if self._is_live_pid(pid)]
                    if live_candidates:
                        self._log(
                            'WARN',
                            (
                                'Frida reported ProcessNotFoundError for running VPX PID(s) '
                                f'{live_candidates}; likely ptrace/namespace restriction. '
                                'Using .nv file polling only'
                            ),
                        )
                self._last_live_diag_ts = now
            return False
        if active_pid is not None and active_pid != attached_pid:
            self._log('INFO', f'Live PinMAME attached to VPX pid={active_pid} for ROM "{rom}"')

        now = time.time()
        if (now - self._last_live_snapshot_attempt_log_ts) >= 10.0:
            pid_txt = str(active_pid) if active_pid is not None else 'n/a'
            self._log('INFO', f'Attempting Live PinMAME snapshot on pid={pid_txt} for ROM "{rom}"')
            self._last_live_snapshot_attempt_log_ts = now
        raw = self._live_session.snapshot()
        if raw is None:
            now = time.time()
            reason = self._live_session.last_error or 'unknown_snapshot_error'
            if reason.startswith('pinmame_exports_not_found'):
                self._mark_live_unsupported(rom, 'pinmame_exports_not_found')
                return False
            if (now - self._last_live_snapshot_err_ts) >= 10.0:
                pid_txt = str(active_pid) if active_pid is not None else 'n/a'
                self._log('WARN', f'Live PinMAME snapshot failed on pid {pid_txt}: {reason}')
                self._last_live_snapshot_err_ts = now
            return False
        now = time.time()
        session_warn = self._live_session.last_error
        if session_warn.startswith('snapshot_warn:') and (now - self._last_live_warn_ts) >= 10.0:
            pid_txt = str(active_pid) if active_pid is not None else 'n/a'
            mode = getattr(self._live_session, 'last_mode', '') or 'unknown'
            self._log('WARN', f'Live PinMAME snapshot warning on pid {pid_txt} (mode={mode}): {session_warn}')
            self._last_live_warn_ts = now

        crc = zlib.crc32(raw) & 0xFFFFFFFF
        prev_crc = self._last_live_crc_by_rom.get(rom)
        if prev_crc == crc:
            changed_count = getattr(self._live_session, 'last_changed_count', 0)
            if changed_count > 0:
                # Changed events happened but converged to same CRC; still decode once.
                path = self.active_path
                if not path or os.path.splitext(os.path.basename(path))[0] != rom:
                    for p in candidate_paths:
                        if os.path.splitext(os.path.basename(p))[0] == rom:
                            path = p
                            break
                    if not path:
                        path = f'<live-pinmame>/{rom}.nv'
                    self.active_path = path
                self._process_nvram_update(rom, path, raw, force_start=False)
                return True
            if (now - self._last_live_unchanged_log_ts) >= 20.0:
                self._log(
                    'INFO',
                    (
                        f'Live PinMAME active for {rom} (pid={active_pid}), '
                        f'no byte changes yet (bytes={self._live_session.last_count})'
                    ),
                )
                self._last_live_unchanged_log_ts = now
            # Live snapshot succeeded; keep live source authoritative and skip
            # disk-based fallback on this tick to avoid source mixing.
            return True
        self._last_live_crc_by_rom[rom] = crc

        path = self.active_path
        if not path or os.path.splitext(os.path.basename(path))[0] != rom:
            for p in candidate_paths:
                if os.path.splitext(os.path.basename(p))[0] == rom:
                    path = p
                    break
            if not path:
                path = f'<live-pinmame>/{rom}.nv'
            self.active_path = path

        self._process_nvram_update(rom, path, raw, force_start=False)
        return True

    def run_forever(self):
        self._log(
            'INFO',
            (
                f'NVRAM monitor running: root={self.nvram_dir}, '
                f'pattern={self.nvram_scan_pattern}, poll={self.poll_interval_sec:.2f}s, '
                f'live_pinmame={"on" if self.use_live_pinmame else "off"}'
            ),
        )
        if self.use_live_pinmame and self._live_session is not None and not self._live_session.has_frida:
            self._log('WARN', 'Python package "frida" not installed; live PinMAME mode disabled until installed')
        self._prime_baseline()
        self._log_waiting(force=True)
        while True:
            try:
                self._scan_once()
                self._check_idle_ends()
                self._check_stale_disk_updates()
                self._check_vpx_play_exit()
                if self.active_rom is None and self._unsupported_active_rom is None:
                    self._log_waiting(force=False)
            except Exception as e:
                self._log('ERROR', f'NVRAM monitor loop error: {e}')
            time.sleep(self.poll_interval_sec)
