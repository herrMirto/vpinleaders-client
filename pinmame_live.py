import threading
from typing import Optional


FRIDA_SCRIPT = r"""
var fnIsRunning = null;
var fnGetMax = null;
var fnGetNVRAM = null;
var fnGetChangedNVRAM = null;
var nvramCache = null;
var ensureError = null;

function _alloc(size) {
  if (typeof Memory !== "undefined" && Memory !== null && typeof Memory.alloc === "function") {
    return Memory.alloc(size);
  }
  throw new Error("memory_alloc_missing");
}

function _ptrAdd(p, off) {
  if (p && typeof p.add === "function") {
    return p.add(off);
  }
  throw new Error("ptr_add_missing");
}

function _readU8(p) {
  if (typeof Memory !== "undefined" && Memory !== null && typeof Memory.readU8 === "function") {
    return Memory.readU8(p);
  }
  if (p && typeof p.readU8 === "function") {
    return p.readU8();
  }
  throw new Error("read_u8_missing");
}

function _findExport(name) {
  var variants = [name];
  if (name.indexOf("Pinmame") === 0) {
    variants.push(name.replace("Pinmame", "PinMame"));
    variants.push(name.replace("Pinmame", "PinMAME"));
  }
  var candidates = [];
  for (var vi = 0; vi < variants.length; vi++) {
    var candidate = variants[vi];
    if (candidates.indexOf(candidate) === -1) candidates.push(candidate);
    if (candidates.indexOf("_" + candidate) === -1) candidates.push("_" + candidate);
  }

  // Frida API variant 1: Module.findExportByName
  if (typeof Module !== "undefined" && Module !== null && typeof Module.findExportByName === "function") {
    for (var i = 0; i < candidates.length; i++) {
      try {
        var p1 = Module.findExportByName(null, candidates[i]);
        if (p1) return p1;
      } catch (_) {}
    }
  }

  // Frida API variant 2: Module.getExportByName
  if (typeof Module !== "undefined" && Module !== null && typeof Module.getExportByName === "function") {
    for (var j = 0; j < candidates.length; j++) {
      try {
        var p2 = Module.getExportByName(null, candidates[j]);
        if (p2) return p2;
      } catch (_) {}
    }
  }

  // Frida API variant 3: Module.findGlobalExportByName
  if (typeof Module !== "undefined" && Module !== null && typeof Module.findGlobalExportByName === "function") {
    for (var k = 0; k < candidates.length; k++) {
      try {
        var p3 = Module.findGlobalExportByName(candidates[k]);
        if (p3) return p3;
      } catch (_) {}
    }
  }

  // Fallback: module instance lookup on common PinMAME module names
  if (typeof Process !== "undefined" && Process !== null && typeof Process.findModuleByName === "function") {
    var moduleNames = [
      "libpinmame.dylib",
      "libpinmame.so",
      "pinmame.dll",
      "libpinmame.dll"
    ];
    for (var mIdx = 0; mIdx < moduleNames.length; mIdx++) {
      var m = null;
      try { m = Process.findModuleByName(moduleNames[mIdx]); } catch (_) {}
      if (m && typeof m.findExportByName === "function") {
        for (var n = 0; n < candidates.length; n++) {
          try {
            var p4 = m.findExportByName(candidates[n]);
            if (p4) return p4;
          } catch (_) {}
        }
      }
    }
  }

  // Last-resort: scan loaded modules for names containing "pinmame"
  // (important on Linux where the loaded soname can be versioned, e.g. .so.3.7.0).
  if (typeof Process !== "undefined" && Process !== null && typeof Process.enumerateModules === "function") {
    try {
      var mods = Process.enumerateModules();
      for (var mi = 0; mi < mods.length; mi++) {
        var mod = mods[mi];
        var modName = "";
        try { modName = String(mod.name || "").toLowerCase(); } catch (_) {}
        if (modName.indexOf("pinmame") === -1) continue;
        if (typeof mod.findExportByName === "function") {
          for (var ci = 0; ci < candidates.length; ci++) {
            try {
              var p5 = mod.findExportByName(candidates[ci]);
              if (p5) return p5;
            } catch (_) {}
          }
        }
      }
    } catch (_) {}
  }

  return null;
}

function _moduleDebugSummary() {
  if (typeof Process === "undefined" || Process === null || typeof Process.enumerateModules !== "function") {
    return "modules_unavailable";
  }
  try {
    var mods = Process.enumerateModules();
    var rows = [];
    for (var i = 0; i < mods.length; i++) {
      var mod = mods[i];
      var modName = "";
      try { modName = String(mod.name || ""); } catch (_) {}
      var low = modName.toLowerCase();
      if (low.indexOf("pinmame") === -1 && low.indexOf("vpinball") === -1) continue;
      var names = [];
      if (typeof mod.enumerateExports === "function") {
        try {
          var ex = mod.enumerateExports();
          for (var j = 0; j < ex.length; j++) {
            var expName = String(ex[j].name || "");
            var expLow = expName.toLowerCase();
            if (expLow.indexOf("pinmame") === -1 && expLow.indexOf("nvram") === -1) continue;
            names.push(expName);
            if (names.length >= 6) break;
          }
        } catch (_) {}
      }
      rows.push(modName + (names.length ? "[" + names.join(",") + "]" : ""));
      if (rows.length >= 4) break;
    }
    if (!rows.length) return "no_pinmame_related_modules";
    return rows.join(" | ");
  } catch (e) {
    return "module_enum_failed:" + String(e);
  }
}

function _ensureFns() {
  ensureError = null;
  if (fnIsRunning && fnGetMax && fnGetNVRAM && fnGetChangedNVRAM) return true;
  if (typeof NativeFunction !== "function") {
    ensureError = "native_function_missing";
    return false;
  }
  var p1 = _findExport("PinmameIsRunning");
  var p2 = _findExport("PinmameGetMaxNVRAM");
  var p3 = _findExport("PinmameGetNVRAM");
  var p4 = _findExport("PinmameGetChangedNVRAM");
  if (!p1 || !p2 || !p3 || !p4) {
    ensureError = (
      "exports_missing:" + (!!p1) + "," + (!!p2) + "," + (!!p3) + "," + (!!p4) +
      ":mods=" + _moduleDebugSummary()
    );
    return false;
  }
  fnIsRunning = new NativeFunction(p1, "int", []);
  fnGetMax = new NativeFunction(p2, "int", []);
  fnGetNVRAM = new NativeFunction(p3, "int", ["pointer"]);
  fnGetChangedNVRAM = new NativeFunction(p4, "int", ["pointer"]);
  return true;
}

function _refreshFromFullDump(maxCount, stride) {
  var sub = "alloc";
  try {
    var buf = _alloc(maxCount * stride);
    sub = "get_nvram_call";
    var count = fnGetNVRAM(buf);
    if (count <= 0) {
      return { ok: false, error: "get_nvram_failed", count: count };
    }

    sub = "decode_curr_stat";
    nvramCache = new Array(count);
    for (var i = 0; i < count; i++) {
      var b = _ptrAdd(buf, (i * stride) + 5);
      nvramCache[i] = _readU8(b);
    }
    return { ok: true, count: count };
  } catch (e) {
    return { ok: false, error: "get_nvram_exception", detail: String(e), phase: sub };
  }
}

function _toHex(data) {
  var hex = "";
  for (var i = 0; i < data.length; i++) {
    var h = data[i].toString(16);
    if (h.length < 2) h = "0" + h;
    hex += h;
  }
  return hex;
}

rpc.exports = {
  snapshot: function () {
    var phase = "start";
    try {
      phase = "ensure_fns";
      if (!_ensureFns()) {
        return { ok: false, error: "pinmame_exports_not_found", detail: ensureError };
      }

      phase = "is_running";
      var running = fnIsRunning();
      if (!running) {
        return { ok: false, error: "pinmame_not_running" };
      }

      phase = "get_max_nvram";
      var maxCount = fnGetMax();
      if (maxCount <= 0 || maxCount > 1000000) {
        return { ok: false, error: "invalid_max_nvram", maxCount: maxCount };
      }

      var stride = 8;
      phase = "full_dump";
      var fullRefresh = _refreshFromFullDump(maxCount, stride);
      if (!fullRefresh.ok) {
        return fullRefresh;
      }

      phase = "to_hex";
      return {
        ok: true,
        count: nvramCache.length,
        changed_count: -1,
        mode: "full",
        hex: _toHex(nvramCache)
      };
    } catch (e) {
      return { ok: false, error: "snapshot_exception", detail: String(e), phase: phase };
    }
  }
};
"""


class PinMameLiveSession:
    def __init__(self):
        self._frida = None
        self._session = None
        self._script = None
        self._pid = None
        self.last_error = ""
        self.last_count = 0
        self.last_changed_count = 0
        self.last_mode = ""
        self._lock = threading.RLock()

    def _load_frida(self):
        if self._frida is not None:
            return self._frida
        try:
            import frida  # type: ignore
        except Exception:
            return None
        self._frida = frida
        return frida

    def attach(self, pid: int) -> bool:
        if pid <= 0:
            self.last_error = "invalid_pid"
            return False
        with self._lock:
            if self._pid == pid and self._session is not None and self._script is not None:
                return True
            self._detach_unlocked()

            frida = self._load_frida()
            if frida is None:
                self.last_error = "frida_not_installed"
                return False
            try:
                # Force local device attach so we do not accidentally resolve to a
                # remote/default Frida device context where host PIDs are not visible.
                device = None
                try:
                    if hasattr(frida, 'get_local_device'):
                        device = frida.get_local_device()
                except Exception:
                    device = None
                if device is not None and hasattr(device, 'attach'):
                    session = device.attach(pid)
                else:
                    session = frida.attach(pid)
                script = session.create_script(FRIDA_SCRIPT)
                script.load()
            except Exception as e:
                try:
                    session.detach()
                except Exception:
                    pass
                detail = str(e).strip()
                if detail:
                    self.last_error = f"frida_attach_failed:{e.__class__.__name__}:{detail}"
                else:
                    self.last_error = f"frida_attach_failed:{e.__class__.__name__}"
                return False

            self._session = session
            self._script = script
            self._pid = pid
            self.last_error = ""
            self.last_changed_count = 0
            self.last_mode = ""
            return True

    def _detach_unlocked(self):
        if self._script is not None:
            try:
                self._script.unload()
            except Exception:
                pass
        self._script = None
        if self._session is not None:
            try:
                self._session.detach()
            except Exception:
                pass
        self._session = None
        self._pid = None
        self.last_count = 0
        self.last_changed_count = 0
        self.last_mode = ""

    def detach(self):
        with self._lock:
            self._detach_unlocked()

    def snapshot(self) -> Optional[bytes]:
        with self._lock:
            script = self._script
            if script is None:
                self.last_error = "not_attached"
                return None
            try:
                out = script.exports_sync.snapshot()
            except Exception as e:
                self.last_error = f"rpc_snapshot_failed:{e.__class__.__name__}:{e}"
                return None
            if not isinstance(out, dict):
                self.last_error = "invalid_rpc_response"
                return None
            if not out.get("ok"):
                err = str(out.get("error") or "snapshot_not_ok")
                detail = out.get("detail")
                if detail:
                    err = f"{err}:{detail}"
                phase = out.get("phase")
                if phase:
                    err = f"{err}:phase={phase}"
                self.last_error = err
                return None
            hex_data = out.get("hex")
            if not isinstance(hex_data, str) or not hex_data:
                self.last_error = "missing_hex"
                return None
            try:
                data = bytes.fromhex(hex_data)
            except Exception:
                self.last_error = "invalid_hex"
                return None
            self.last_error = ""
            try:
                self.last_count = int(out.get("count") or 0)
            except Exception:
                self.last_count = 0
            try:
                self.last_changed_count = int(out.get("changed_count") or 0)
            except Exception:
                self.last_changed_count = 0
            mode = out.get("mode")
            self.last_mode = str(mode) if isinstance(mode, str) else ""
            warn = out.get("warn")
            if warn and not self.last_error:
                self.last_error = f"snapshot_warn:{warn}"
            return data

    @property
    def has_frida(self) -> bool:
        return self._load_frida() is not None

    @property
    def attached_pid(self) -> Optional[int]:
        with self._lock:
            return self._pid
