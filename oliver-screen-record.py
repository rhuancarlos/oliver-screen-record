#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oliver Screen Record
--------------------
Gravação de tela para Ubuntu/Debian modernos (Ubuntu >= 25.04), com áudio do
sistema, microfone e câmera sobreposta (arrastável), além de pausa/continuação.

Funciona em Wayland (padrão no Ubuntu 25.10+/26.x) e X11, usando:
  * xdg-desktop-portal (ScreenCast)  -> permissão e seleção de tela/janela
  * PipeWire                         -> fluxo de vídeo da tela
  * PulseAudio API (pipewire-pulse)  -> áudio do sistema (monitor) e microfone
  * GStreamer                        -> câmera, composição, codificação e gravação
  * GTK 4                            -> interface e prévia ao vivo
"""

import json
import os
import signal
import subprocess
import sys
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk, Pango  # noqa: E402

APP_ID = "com.oliver.ScreenRecord"
APP_NAME = "Oliver Screen Record"
APP_VERSION = "1.1.0"

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IFACE_SCREENCAST = "org.freedesktop.portal.ScreenCast"
IFACE_REQUEST = "org.freedesktop.portal.Request"
IFACE_SESSION = "org.freedesktop.portal.Session"

CONFIG_FILE = os.path.join(GLib.get_user_config_dir(), "oliver-screen-record", "config.json")

# Tipos de fonte do portal ScreenCast
SOURCE_MONITOR, SOURCE_WINDOW = 1, 2
SOURCES = [
    ("Tela inteira", SOURCE_MONITOR),
    ("Janela", SOURCE_WINDOW),
    ("Tela ou janela (escolher na hora)", SOURCE_MONITOR | SOURCE_WINDOW),
]

# (rótulo, quantizer x264, cq-level vp8)
QUALITIES = [("Alta", 18, 10), ("Média", 23, 20), ("Baixa", 28, 32)]

FORMATS = [
    {"label": "MKV (H.264 + Opus) — recomendado", "ext": "mkv", "venc": "x264", "aenc": "opus",
     "mux": "matroskamux"},
    {"label": "MP4 (H.264 + AAC)", "ext": "mp4", "venc": "x264", "aenc": "aac",
     "mux": "mp4mux faststart=true"},
    {"label": "WebM (VP8 + Opus)", "ext": "webm", "venc": "vp8", "aenc": "opus",
     "mux": "webmmux"},
]

# Câmera: posições pré-definidas (fração do espaço livre em x, y) e tamanhos (fração da largura)
CAM_POSITIONS = [
    ("Inferior direito", 1.0, 1.0),
    ("Inferior esquerdo", 0.0, 1.0),
    ("Superior direito", 1.0, 0.0),
    ("Superior esquerdo", 0.0, 0.0),
    ("Personalizada (arrastada)", None, None),
]
CAM_CUSTOM = len(CAM_POSITIONS) - 1
CAM_SIZES = [("Pequena", 0.16), ("Média", 0.22), ("Grande", 0.30)]
CAM_PROCESS_WIDTH = 640     # largura em que a câmera é processada antes da composição
PREVIEW_WIDTH = 480

# elemento GStreamer -> pacote Debian/Ubuntu
REQUIRED_ELEMENTS = {
    "pipewiresrc": "gstreamer1.0-pipewire",
    "pulsesrc": "gstreamer1.0-pulseaudio",
    "videoconvert": "gstreamer1.0-plugins-base",
    "videoscale": "gstreamer1.0-plugins-base",
    "videorate": "gstreamer1.0-plugins-base",
    "compositor": "gstreamer1.0-plugins-base",
    "audiomixer": "gstreamer1.0-plugins-base",
    "opusenc": "gstreamer1.0-plugins-base",
    "x264enc": "gstreamer1.0-plugins-ugly",
    "h264parse": "gstreamer1.0-plugins-bad",
    "matroskamux": "gstreamer1.0-plugins-good",
    "mp4mux": "gstreamer1.0-plugins-good",
    "vp8enc": "gstreamer1.0-plugins-good",
    "webmmux": "gstreamer1.0-plugins-good",
    "videoflip": "gstreamer1.0-plugins-good",
    "jpegdec": "gstreamer1.0-plugins-good",
    "v4l2src": "gstreamer1.0-plugins-good",
}
OPTIONAL_ELEMENTS = {"gtk4paintablesink": "gstreamer1.0-gtk4"}  # prévia ao vivo
AAC_ENCODERS = ["fdkaacenc", "avenc_aac", "voaacenc"]


def has_element(name):
    return Gst.ElementFactory.find(name) is not None


def missing_packages(optional=False):
    table = OPTIONAL_ELEMENTS if optional else REQUIRED_ELEMENTS
    return sorted({pkg for el, pkg in table.items() if not has_element(el)})


# --------------------------------------------------------------------------- #
#  Configuração persistente
# --------------------------------------------------------------------------- #
class Config(dict):
    def __init__(self):
        super().__init__()
        videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS) or \
            os.path.join(GLib.get_home_dir(), "Videos")
        self.update({
            "output_dir": os.path.join(videos, "Oliver Screen Record"),
            "source": 0, "cursor": True, "fps": 30, "quality": 0, "format": 0,
            "sys_audio": True, "mic": True, "remember": False, "restore_token": "",
            "minimize": True, "start_paused": False,
            "camera": False, "camera_name": "", "mirror": True,
            "cam_pos": 0, "cam_size": 1, "cam_fx": 1.0, "cam_fy": 1.0,
        })
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print(f"[config] não foi possível salvar: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
#  Dispositivos de áudio (via pactl / pipewire-pulse)
# --------------------------------------------------------------------------- #
def _pactl(*args):
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def list_audio(kind):
    """kind = 'sinks' (saídas) ou 'sources' (entradas). Retorna [(nome, descrição)]."""
    out = _pactl("-f", "json", "list", kind)
    items = []
    if out:
        try:
            for d in json.loads(out):
                items.append((d["name"], d.get("description") or d["name"]))
        except (ValueError, KeyError, TypeError):
            items = []
    if not items:  # fallback para versões antigas do pactl
        for line in _pactl("list", "short", kind).splitlines():
            parts = line.split("\t")
            if len(parts) > 1:
                items.append((parts[1], parts[1]))
    if kind == "sources":
        items = [i for i in items if not i[0].endswith(".monitor")]
    return items


def default_monitor():
    sink = _pactl("get-default-sink")
    return f"{sink}.monitor" if sink else "@DEFAULT_MONITOR@"


def default_mic():
    return _pactl("get-default-source") or None


# --------------------------------------------------------------------------- #
#  Câmeras (Gst.DeviceMonitor: V4L2, PipeWire, libcamera)
# --------------------------------------------------------------------------- #
CAM_RAW_CAPS = "video/x-raw,width=[320,1280],height=[240,720],framerate=[24/1,60/1]"
CAM_JPEG_CAPS = "image/jpeg,width=[320,1280],height=[240,720],framerate=[24/1,60/1]"


def _quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def list_cameras():
    """Retorna [{'label', 'src'}], onde 'src' é o trecho de pipeline que entrega vídeo bruto."""
    mon = Gst.DeviceMonitor.new()
    mon.add_filter("Video/Source", None)
    try:
        if not mon.start():
            return []
        devices = mon.get_devices()
    finally:
        mon.stop()

    cams = []
    for dev in devices:
        try:
            el = dev.create_element(None)
        except Exception:  # noqa: BLE001
            el = None
        if el is None:
            continue
        factory = el.get_factory().get_name()
        if factory == "v4l2src":
            src, api = f"v4l2src device={_quote(el.get_property('device'))}", "V4L2"
        elif factory == "pipewiresrc":
            target = None
            for prop in ("target-object", "path"):
                try:
                    target = el.get_property(prop)
                except TypeError:
                    continue
                if target:
                    src = f"pipewiresrc {prop}={_quote(target)} do-timestamp=true"
                    break
            if not target:
                continue
            api = "PipeWire"
        elif factory == "libcamerasrc":
            src, api = f"libcamerasrc camera-name={_quote(el.get_property('camera-name'))}", \
                "libcamera"
        else:
            continue

        # Escolhe um formato que o dispositivo realmente oferece (bruto ou MJPEG)
        caps = dev.get_caps()
        if caps and caps.can_intersect(Gst.Caps.from_string(CAM_RAW_CAPS)):
            src += f" ! {CAM_RAW_CAPS}"
        elif caps and caps.can_intersect(Gst.Caps.from_string(CAM_JPEG_CAPS)):
            src += f" ! {CAM_JPEG_CAPS} ! jpegdec"
        cams.append({"label": f"{dev.get_display_name()} ({api})", "src": src})

    order = {"V4L2": 0, "libcamera": 1, "PipeWire": 2}
    cams.sort(key=lambda c: order.get(c["label"].rsplit("(", 1)[-1].rstrip(")"), 9))
    return cams


# --------------------------------------------------------------------------- #
#  xdg-desktop-portal (D-Bus)
# --------------------------------------------------------------------------- #
class Portal:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self._count = 0

    def new_token(self):
        self._count += 1
        return f"oliver_{os.getpid()}_{self._count}"

    def get_property(self, iface, name, default=None):
        try:
            res = self.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (iface, name)), GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE, 3000, None)
            return res.unpack()[0]
        except GLib.Error:
            return default

    def request(self, iface, method, build_params, callback):
        """Chama um método do portal que devolve um objeto Request e aguarda o sinal Response.
        build_params(token) -> GLib.Variant ; callback(response:int, results:dict)."""
        token = self.new_token()
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        state = {"sub": 0}

        def on_response(conn, _sender, _path, _iface, _signal, params):
            conn.signal_unsubscribe(state["sub"])
            response, results = params.unpack()
            callback(response, results)

        state["sub"] = self.bus.signal_subscribe(
            PORTAL_BUS, IFACE_REQUEST, "Response", path, None,
            Gio.DBusSignalFlags.NONE, on_response)

        def on_called(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error as e:
                conn.signal_unsubscribe(state["sub"])
                callback(2, {"error": e.message})

        self.bus.call(PORTAL_BUS, PORTAL_PATH, iface, method, build_params(token),
                      None, Gio.DBusCallFlags.NONE, -1, None, on_called)


class ScreenCastSession:
    """Fluxo: CreateSession -> SelectSources -> Start -> OpenPipeWireRemote."""

    def __init__(self, portal, source_types, cursor, persist, restore_token,
                 on_ready, on_fail, on_closed):
        self.portal = portal
        self.types = source_types
        self.cursor = cursor
        self.persist = persist
        self.restore_token = restore_token
        self.on_ready, self.on_fail, self.on_closed = on_ready, on_fail, on_closed
        self.handle = None
        self._closed_sub = 0
        self._closing = False

    def _opts(self, token, **extra):
        d = {"handle_token": GLib.Variant("s", token)}
        d.update(extra)
        return d

    def start(self):
        self.portal.request(
            IFACE_SCREENCAST, "CreateSession",
            lambda t: GLib.Variant("(a{sv})", (self._opts(
                t, session_handle_token=GLib.Variant("s", self.portal.new_token())),)),
            self._on_created)

    def _check(self, response, results):
        if response == 0:
            return True
        self.close()
        if response == 1:
            self.on_fail("Seleção cancelada pelo usuário.", True)
        else:
            self.on_fail(results.get("error", "O portal recusou a solicitação."), False)
        return False

    def _on_created(self, response, results):
        if not self._check(response, results):
            return
        self.handle = str(results["session_handle"])
        self._closed_sub = self.portal.bus.signal_subscribe(
            PORTAL_BUS, IFACE_SESSION, "Closed", self.handle, None,
            Gio.DBusSignalFlags.NONE, self._on_session_closed)

        avail_types = self.portal.get_property(IFACE_SCREENCAST, "AvailableSourceTypes", 1)
        cursor_modes = self.portal.get_property(IFACE_SCREENCAST, "AvailableCursorModes", 1)
        version = self.portal.get_property(IFACE_SCREENCAST, "version", 1)

        def params(token):
            types = (self.types & avail_types) or SOURCE_MONITOR
            extra = {"types": GLib.Variant("u", types), "multiple": GLib.Variant("b", False)}
            want = 2 if self.cursor else 1  # 2 = cursor embutido, 1 = oculto
            if cursor_modes & want:
                extra["cursor_mode"] = GLib.Variant("u", want)
            if version >= 4 and self.persist:
                extra["persist_mode"] = GLib.Variant("u", 2)
                if self.restore_token:
                    extra["restore_token"] = GLib.Variant("s", self.restore_token)
            return GLib.Variant("(oa{sv})", (self.handle, self._opts(token, **extra)))

        self.portal.request(IFACE_SCREENCAST, "SelectSources", params, self._on_selected)

    def _on_selected(self, response, results):
        if not self._check(response, results):
            return
        self.portal.request(
            IFACE_SCREENCAST, "Start",
            lambda t: GLib.Variant("(osa{sv})", (self.handle, "", self._opts(t))),
            self._on_started)

    def _on_started(self, response, results):
        if not self._check(response, results):
            return
        streams = results.get("streams") or []
        if not streams:
            self.close()
            self.on_fail("Nenhum fluxo de vídeo foi disponibilizado pelo portal.", False)
            return
        node_id, props = streams[0]
        try:
            res, fdlist = self.portal.bus.call_with_unix_fd_list_sync(
                PORTAL_BUS, PORTAL_PATH, IFACE_SCREENCAST, "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.handle, {})), GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE, -1, None, None)
            fd = fdlist.get(res.unpack()[0])
        except GLib.Error as e:
            self.close()
            self.on_fail(f"Falha ao conectar ao PipeWire: {e.message}", False)
            return
        self.on_ready(fd, int(node_id), props, results.get("restore_token", ""))

    def _on_session_closed(self, *_):
        if not self._closing:
            self._closing = True
            self._unsubscribe()
            self.on_closed()

    def _unsubscribe(self):
        if self._closed_sub:
            self.portal.bus.signal_unsubscribe(self._closed_sub)
            self._closed_sub = 0

    def close(self):
        self._closing = True
        self._unsubscribe()
        if self.handle:
            self.portal.bus.call(PORTAL_BUS, self.handle, IFACE_SESSION, "Close", None, None,
                                 Gio.DBusCallFlags.NONE, -1, None, None)
            self.handle = None


# --------------------------------------------------------------------------- #
#  Gravador (pipeline GStreamer)
# --------------------------------------------------------------------------- #
class Recorder:
    """
    Tela ─▶ videorate ─▶ I420 ─▶ escala (par) ─┐
                                               ├─▶ compositor ─▶ tee ─┬─▶ [portão] ─▶ encoder ─┐
    Câmera ─▶ videorate ─▶ espelho ─▶ escala ──┘                      └─▶ prévia (GTK)         ├─▶ mux
    Sistema/Microfone ─▶ audiomixer ─▶ [portão] ─▶ encoder de áudio ──────────────────────────┘

    A pausa NÃO pausa o pipeline: os "portões" descartam os buffers antes dos encoders e,
    ao continuar, um offset negativo é aplicado aos pads para remover o intervalo pausado
    dos timestamps. Assim a prévia segue ao vivo durante a pausa e o arquivo fica contínuo.
    """

    def __init__(self, fd, node_id, *, fps, quality, fmt, sys_dev, mic_dev, path,
                 camera=None, mirror=True, cam_layout=(1.0, 1.0, 0.22), preview=False,
                 start_paused=False, on_done=None):
        self.fd = fd
        self.path = path
        self.on_done = on_done  # on_done(ok: bool, message: str)
        self.pipeline = None
        self.preview_paintable = None
        self.video_size = None          # (W, H) da saída
        self.camera_rect = None         # (x, y, w, h) da câmera na saída
        self._cam_pad = None
        self._cam_aspect = None         # altura/largura da câmera
        self._cam_fx, self._cam_fy, self._cam_size = cam_layout
        self._finished = False
        self._eos_timeout = 0
        self._sized = False
        self._gates = []
        self._last_pts = {}
        self._paused = start_paused
        self._pause_started = 0 if start_paused else None
        self._pause_total = 0
        self._build(node_id, fps, quality, fmt, sys_dev, mic_dev, camera, mirror, preview)

    # ---------- construção ----------
    @staticmethod
    def _aac_encoder():
        for name in AAC_ENCODERS:
            if has_element(name):
                return f"{name} bitrate=192000 ! aacparse"
        return None

    def _build(self, node_id, fps, quality, fmt, sys_dev, mic_dev, camera, mirror, preview):
        _, q264, qvp8 = QUALITIES[quality]
        gop = fps * 2

        if fmt["venc"] == "x264":
            venc = (f"x264enc speed-preset=veryfast tune=zerolatency pass=quant quantizer={q264} "
                    f"key-int-max={gop} ! video/x-h264,profile=high ! h264parse")
        else:
            venc = (f"vp8enc deadline=1 cpu-used=8 threads=4 end-usage=cq cq-level={qvp8} "
                    f"target-bitrate=20000000 keyframe-max-dist={gop}")

        if fmt["aenc"] == "aac":
            aenc = self._aac_encoder() or "opusenc bitrate=160000"
        else:
            aenc = "opusenc bitrate=160000"

        big_queue = "queue max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0"
        leaky1 = "queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0"

        # Tela: fila "leaky" evita travar o PipeWire caso o encoder atrase;
        # keepalive reenvia o último quadro quando a tela fica parada.
        screen = (
            f"pipewiresrc fd={self.fd} path={node_id} do-timestamp=true keepalive-time=1000 "
            "! queue leaky=downstream max-size-buffers=5 max-size-time=0 max-size-bytes=0 "
            f"! videorate ! video/x-raw,framerate={fps}/1 "
            "! videoconvert ! video/x-raw,format=I420 "
            "! videoscale name=vscale add-borders=true ! capsfilter name=vsize ")

        desc = [f"{fmt['mux']} name=mux ! filesink name=sink"]
        preview = preview and has_element("gtk4paintablesink")

        if camera:
            flip = "! videoflip method=horizontal-flip " if mirror else ""
            desc += [
                screen + "! comp.sink_0",
                f"{camera['src']} ! queue leaky=downstream max-size-buffers=3 max-size-time=0 "
                f"max-size-bytes=0 ! videorate ! video/x-raw,framerate={fps}/1 ! videoconvert "
                f"{flip}! videoscale ! capsfilter name=camcaps "
                f"caps=video/x-raw,width={CAM_PROCESS_WIDTH},pixel-aspect-ratio=1/1 "
                "! comp.sink_1",
                # alpha=0 até sabermos o tamanho da tela e da câmera (evita "piscar")
                "compositor name=comp background=black sink_0::zorder=0 "
                "sink_1::zorder=1 sink_1::alpha=0 "
                f"! video/x-raw,framerate={fps}/1 ! videoconvert ! video/x-raw,format=I420 "
                "! tee name=vt",
                f"vt. ! {big_queue} ! queue name=vgate ! {venc} ! queue ! mux.",
            ]
            if preview:
                desc.append(
                    f"vt. ! {leaky1} ! videorate drop-only=true ! video/x-raw,framerate=15/1 "
                    f"! videoscale ! video/x-raw,width={PREVIEW_WIDTH} ! videoconvert "
                    "! gtk4paintablesink name=preview sync=false async=false")
        else:
            desc.append(screen + f"! {big_queue} ! queue name=vgate ! {venc} ! queue ! mux.")

        audio_inputs = []
        if sys_dev is not None:
            audio_inputs.append(("asys", sys_dev))
        if mic_dev is not False:
            audio_inputs.append(("amic", mic_dev))
        if audio_inputs:
            for name, _ in audio_inputs:
                desc.append(
                    f"pulsesrc name={name} provide-clock=false ! audioconvert ! audioresample "
                    "! audio/x-raw,rate=48000,channels=2 ! queue ! amix.")
            desc.append(
                "audiomixer name=amix ! audio/x-raw,rate=48000,channels=2 ! audioconvert "
                f"! queue name=agate ! {aenc} ! queue ! mux.")

        self.pipeline = Gst.parse_launch(" ".join(desc))
        self.pipeline.use_clock(Gst.SystemClock.obtain())
        self.pipeline.get_by_name("sink").set_property("location", self.path)
        for name, dev in audio_inputs:
            if dev:  # None => dispositivo padrão do pulsesrc
                self.pipeline.get_by_name(name).set_property("device", dev)

        # H.264 4:2:0 exige largura/altura pares: fixa a saída no 1º formato recebido
        # (redimensionamentos de janela posteriores são escalados com bordas).
        self.pipeline.get_by_name("vscale").get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_screen_caps)

        if camera:
            self._cam_pad = self.pipeline.get_by_name("comp").get_static_pad("sink_1")
            self.pipeline.get_by_name("camcaps").get_static_pad("src").add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_camera_caps)
        if preview:
            self.preview_paintable = self.pipeline.get_by_name("preview").get_property("paintable")

        # Portões de pausa (saída das filas antes dos encoders)
        for name in ("vgate", "agate"):
            el = self.pipeline.get_by_name(name)
            if el:
                pad = el.get_static_pad("src")
                pad.add_probe(Gst.PadProbeType.BUFFER, self._gate_probe)
                self._gates.append(pad)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    # ---------- probes ----------
    @staticmethod
    def _caps_size(info):
        event = info.get_event()
        if event.type != Gst.EventType.CAPS:
            return None
        s = event.parse_caps().get_structure(0)
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        return (w, h) if ok_w and ok_h else None

    def _on_screen_caps(self, _pad, info):
        size = self._caps_size(info)
        if size and not self._sized:
            self._sized = True
            w, h = size[0] - size[0] % 2, size[1] - size[1] % 2
            self.video_size = (w, h)
            self.pipeline.get_by_name("vsize").set_property("caps", Gst.Caps.from_string(
                f"video/x-raw,width={w},height={h},pixel-aspect-ratio=1/1"))
            GLib.idle_add(self._apply_layout)
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK

    def _on_camera_caps(self, _pad, info):
        size = self._caps_size(info)
        if size:
            self._cam_aspect = size[1] / size[0]
            GLib.idle_add(self._apply_layout)
        return Gst.PadProbeReturn.OK

    def _gate_probe(self, pad, info):
        if self._paused:
            return Gst.PadProbeReturn.DROP
        # Descarta quadros com timestamp repetido/regressivo (o compositor pode duplicar
        # o PTS 0 quando a câmera demora a iniciar; o x264 + MP4 não toleram isso).
        pts = info.get_buffer().pts
        if pts != Gst.CLOCK_TIME_NONE:
            last = self._last_pts.get(pad)
            if last is not None and pts <= last:
                return Gst.PadProbeReturn.DROP
            self._last_pts[pad] = pts
        return Gst.PadProbeReturn.OK

    # ---------- câmera ----------
    def _cam_geometry(self):
        W, H = self.video_size
        cw = int(W * self._cam_size) & ~1
        ch = min(int(cw * self._cam_aspect) & ~1, H)
        margin = int(min(W, H) * 0.025)
        return W, H, cw, ch, margin

    def _apply_layout(self):
        if not (self._cam_pad and self.video_size and self._cam_aspect):
            return False
        W, H, cw, ch, m = self._cam_geometry()
        x = m + int(self._cam_fx * max(0, W - cw - 2 * m))
        y = m + int(self._cam_fy * max(0, H - ch - 2 * m))
        for prop, val in (("xpos", x), ("ypos", y), ("width", cw), ("height", ch)):
            self._cam_pad.set_property(prop, val)
        self._cam_pad.set_property("alpha", 1.0)
        self.camera_rect = (x, y, cw, ch)
        return False

    def set_camera_layout(self, fx=None, fy=None, size=None):
        if fx is not None:
            self._cam_fx = min(max(fx, 0.0), 1.0)
        if fy is not None:
            self._cam_fy = min(max(fy, 0.0), 1.0)
        if size is not None:
            self._cam_size = size
        self._apply_layout()

    def move_camera_to(self, x, y):
        """Move o canto superior esquerdo da câmera para (x, y) em coordenadas do vídeo.
        Retorna as frações (fx, fy) resultantes."""
        if not (self.video_size and self._cam_aspect):
            return self._cam_fx, self._cam_fy
        W, H, cw, ch, m = self._cam_geometry()
        free_w, free_h = max(1, W - cw - 2 * m), max(1, H - ch - 2 * m)
        self.set_camera_layout((x - m) / free_w, (y - m) / free_h)
        return self._cam_fx, self._cam_fy

    # ---------- controle ----------
    def _running_time(self):
        clock = self.pipeline.get_clock()
        if not clock or self.pipeline.get_base_time() == 0:
            return 0
        return max(0, clock.get_time() - self.pipeline.get_base_time())

    @property
    def paused(self):
        return self._paused

    def elapsed(self):
        """Tempo gravado (ns), descontando as pausas."""
        now = self._running_time()
        paused_now = (now - self._pause_started) if self._paused else 0
        return max(0, now - self._pause_total - paused_now)

    def pause(self):
        if not self._paused and not self._finished:
            self._pause_started = self._running_time()
            self._paused = True

    def resume(self):
        if self._paused and not self._finished:
            self._pause_total += self._running_time() - self._pause_started
            for pad in self._gates:
                pad.set_offset(-self._pause_total)
            self._paused = False

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._finish(False, "Não foi possível iniciar o pipeline de gravação.")
            return False
        if self._paused:
            # "Começar pausado": a pausa começa no instante 0 do pipeline
            self._pause_started = 0
        return True

    def stop(self):
        if self._finished or not self.pipeline:
            return
        if self._paused:
            self.resume()  # fecha o intervalo pausado; EOS passa pelos portões normalmente
            self._paused = True  # continua descartando até o EOS chegar
        # EOS garante que o arquivo seja finalizado corretamente (essencial no MP4)
        self.pipeline.send_event(Gst.Event.new_eos())
        self._eos_timeout = GLib.timeout_add_seconds(
            6, lambda: self._finish(True, "Gravação finalizada (tempo de espera excedido).") or False)

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self._finish(True, "Gravação salva.")
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[gst] ERRO ({msg.src.get_name()}): {err.message}\n{dbg}", file=sys.stderr)
            self._finish(False, err.message)
        elif msg.type == Gst.MessageType.WARNING:
            w, _dbg = msg.parse_warning()
            print(f"[gst] aviso: {w.message}", file=sys.stderr)

    def _finish(self, ok, message):
        if self._finished:
            return
        self._finished = True
        if self._eos_timeout:
            GLib.source_remove(self._eos_timeout)
            self._eos_timeout = 0
        self.pipeline.get_bus().remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self.on_done:
            self.on_done(ok, message)


# --------------------------------------------------------------------------- #
#  Interface
# --------------------------------------------------------------------------- #
class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(540, -1)
        self.set_resizable(False)
        self.cfg = Config()
        self.portal = None
        self.session = None
        self.recorder = None
        self.rec_path = ""
        self.timer_id = 0
        self.quit_after_stop = False
        self.sinks, self.mics, self.cams = [], [], []
        self._drag_origin = None
        self._updating_pos = False

        try:
            self.portal = Portal()
        except GLib.Error as e:
            print(f"[portal] D-Bus indisponível: {e.message}", file=sys.stderr)

        self._build_ui()
        self._refresh_audio()
        self._refresh_cameras()
        self.connect("close-request", self._on_close_request)

        missing = missing_packages()
        if missing:
            self._status("Pacotes ausentes: " + " ".join(missing), error=True)

    # ---------- construção da UI ----------
    def _build_ui(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .section { font-weight: bold; margin-top: 8px; }
            .timer { font-family: monospace; font-size: 20px; font-weight: bold; }
            .rec { color: #e01b24; }
            .paused { color: #e5a50a; }
            .bigbtn { padding: 10px 14px; font-weight: bold; }
            .preview { background: #000; border-radius: 6px; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        self.set_child(root)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        root.append(grid)
        row = 0

        def section(text):
            nonlocal row
            lbl = Gtk.Label(label=text, xalign=0)
            lbl.add_css_class("section")
            grid.attach(lbl, 0, row, 3, 1)
            row += 1

        def attach_row(first, widget, extra=None):
            nonlocal row
            if isinstance(first, str):
                first = Gtk.Label(label=first, xalign=0)
            grid.attach(first, 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1 if extra else 2, 1)
            if extra:
                extra.set_halign(Gtk.Align.START)
                grid.attach(extra, 2, row, 1, 1)
            row += 1

        def checks(*widgets):
            nonlocal row
            box = Gtk.Box(spacing=12)
            for w in widgets:
                box.append(w)
            grid.attach(box, 0, row, 3, 1)
            row += 1

        # --- Vídeo
        section("🎬 Vídeo")
        self.dd_source = Gtk.DropDown.new_from_strings([s[0] for s in SOURCES])
        self.dd_source.set_selected(self.cfg["source"])
        attach_row("Capturar", self.dd_source)

        self.spin_fps = Gtk.SpinButton.new_with_range(10, 60, 5)
        self.spin_fps.set_value(self.cfg["fps"])
        attach_row("Quadros/s (FPS)", self.spin_fps)

        self.dd_quality = Gtk.DropDown.new_from_strings([q[0] for q in QUALITIES])
        self.dd_quality.set_selected(self.cfg["quality"])
        attach_row("Qualidade", self.dd_quality)

        self.dd_format = Gtk.DropDown.new_from_strings([f["label"] for f in FORMATS])
        self.dd_format.set_selected(self.cfg["format"])
        attach_row("Formato", self.dd_format)

        self.chk_cursor = Gtk.CheckButton(label="Mostrar cursor", active=self.cfg["cursor"])
        self.chk_minimize = Gtk.CheckButton(label="Minimizar ao gravar",
                                            active=self.cfg["minimize"])
        self.chk_remember = Gtk.CheckButton(label="Lembrar tela escolhida",
                                            active=self.cfg["remember"])
        self.chk_remember.set_tooltip_text(
            "Reutiliza a última seleção sem perguntar de novo (portal v4+).")
        checks(self.chk_cursor, self.chk_minimize, self.chk_remember)
        self.chk_start_paused = Gtk.CheckButton(
            label="Começar pausado (para ajustar a câmera antes)",
            active=self.cfg["start_paused"])
        checks(self.chk_start_paused)

        # --- Áudio
        section("🔊 Áudio")
        self.chk_sys = Gtk.CheckButton(label="Áudio do sistema", active=self.cfg["sys_audio"])
        self.dd_sys = Gtk.DropDown.new_from_strings(["Padrão"])
        btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                                 tooltip_text="Atualizar dispositivos de áudio")
        btn_refresh.connect("clicked", lambda *_: self._refresh_audio())
        attach_row(self.chk_sys, self.dd_sys, btn_refresh)

        self.chk_mic = Gtk.CheckButton(label="Microfone", active=self.cfg["mic"])
        self.dd_mic = Gtk.DropDown.new_from_strings(["Padrão"])
        attach_row(self.chk_mic, self.dd_mic)

        # --- Câmera
        section("📹 Câmera")
        self.chk_cam = Gtk.CheckButton(label="Câmera", active=self.cfg["camera"])
        self.dd_cam = Gtk.DropDown.new_from_strings(["Nenhuma câmera encontrada"])
        btn_cam_refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                                     tooltip_text="Atualizar câmeras")
        btn_cam_refresh.connect("clicked", lambda *_: self._refresh_cameras())
        attach_row(self.chk_cam, self.dd_cam, btn_cam_refresh)

        self.dd_cam_pos = Gtk.DropDown.new_from_strings([p[0] for p in CAM_POSITIONS])
        self.dd_cam_pos.set_selected(self.cfg["cam_pos"])
        self.dd_cam_pos.connect("notify::selected", self._on_cam_pos_changed)
        attach_row("Posição", self.dd_cam_pos)

        self.dd_cam_size = Gtk.DropDown.new_from_strings([s[0] for s in CAM_SIZES])
        self.dd_cam_size.set_selected(self.cfg["cam_size"])
        self.dd_cam_size.connect("notify::selected", self._on_cam_size_changed)
        attach_row("Tamanho", self.dd_cam_size)

        self.chk_mirror = Gtk.CheckButton(label="Espelhar câmera", active=self.cfg["mirror"])
        checks(self.chk_mirror)

        for chk, widgets in ((self.chk_sys, [self.dd_sys]), (self.chk_mic, [self.dd_mic]),
                             (self.chk_cam, [self.dd_cam, self.chk_mirror])):
            chk.connect("toggled", lambda c, ws=widgets: [w.set_sensitive(c.get_active())
                                                          for w in ws])

        # --- Saída
        section("📁 Saída")
        self.lbl_dir = Gtk.Label(label=self.cfg["output_dir"], xalign=0,
                                 ellipsize=Pango.EllipsizeMode.END, max_width_chars=40)
        dir_box = Gtk.Box(spacing=6)
        btn_dir = Gtk.Button(label="Alterar…")
        btn_dir.connect("clicked", self._choose_dir)
        btn_open = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Abrir pasta")
        btn_open.connect("clicked", self._open_dir)
        dir_box.append(btn_dir)
        dir_box.append(btn_open)
        attach_row("Pasta", self.lbl_dir, dir_box)

        # --- Prévia (durante a gravação com câmera)
        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                   margin_top=10, visible=False)
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        self.picture.set_size_request(PREVIEW_WIDTH, PREVIEW_WIDTH * 9 // 16)
        self.picture.add_css_class("preview")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.picture.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_preview_motion)
        self.picture.add_controller(motion)
        hint = Gtk.Label(label="Arraste a câmera na prévia para movê-la.", xalign=0)
        hint.add_css_class("dim-label")
        self.preview_box.append(self.picture)
        self.preview_box.append(hint)
        root.append(self.preview_box)

        # --- Ações
        actions = Gtk.Box(spacing=8, margin_top=12, homogeneous=True)
        self.btn_rec = Gtk.Button(label="⏺  Iniciar gravação")
        self.btn_rec.add_css_class("bigbtn")
        self.btn_rec.add_css_class("suggested-action")
        self.btn_rec.connect("clicked", self._on_record_clicked)
        self.btn_pause = Gtk.Button(label="⏸  Pausar", sensitive=False)
        self.btn_pause.add_css_class("bigbtn")
        self.btn_pause.connect("clicked", self._on_pause_clicked)
        actions.append(self.btn_rec)
        actions.append(self.btn_pause)
        root.append(actions)

        status_box = Gtk.Box(spacing=10, margin_top=6)
        self.lbl_timer = Gtk.Label(label="00:00:00")
        self.lbl_timer.add_css_class("timer")
        self.lbl_status = Gtk.Label(label="Pronto.", xalign=0, wrap=True, hexpand=True)
        status_box.append(self.lbl_timer)
        status_box.append(self.lbl_status)
        root.append(status_box)

        # Posição e tamanho da câmera continuam editáveis durante a gravação
        self.settings_widgets = [
            self.dd_source, self.spin_fps, self.dd_quality, self.dd_format, self.chk_cursor,
            self.chk_remember, self.chk_start_paused, self.chk_sys, self.dd_sys, self.chk_mic,
            self.dd_mic, self.chk_cam, self.dd_cam, self.chk_mirror, btn_refresh,
            btn_cam_refresh, btn_dir,
        ]
        self._sync_sensitivity()

    # ---------- utilidades ----------
    def _status(self, text, error=False):
        self.lbl_status.set_text(text)
        if error:
            print(f"[{APP_NAME}] {text}", file=sys.stderr)

    def _sync_sensitivity(self):
        self.dd_sys.set_sensitive(self.chk_sys.get_active())
        self.dd_mic.set_sensitive(self.chk_mic.get_active())
        self.dd_cam.set_sensitive(self.chk_cam.get_active() and bool(self.cams))
        self.chk_mirror.set_sensitive(self.chk_cam.get_active())

    def _refresh_audio(self):
        self.sinks = list_audio("sinks")
        self.mics = list_audio("sources")
        self.dd_sys.set_model(Gtk.StringList.new(
            ["Padrão (saída atual)"] + [d for _, d in self.sinks]))
        self.dd_mic.set_model(Gtk.StringList.new(
            ["Padrão (entrada atual)"] + [d for _, d in self.mics]))
        self.dd_sys.set_selected(0)
        self.dd_mic.set_selected(0)

    def _refresh_cameras(self):
        self.cams = list_cameras()
        labels = [c["label"] for c in self.cams] or ["Nenhuma câmera encontrada"]
        self.dd_cam.set_model(Gtk.StringList.new(labels))
        names = [c["label"] for c in self.cams]
        if self.cfg.get("camera_name") in names:
            self.dd_cam.set_selected(names.index(self.cfg["camera_name"]))
        self._sync_sensitivity()

    def _selected_audio(self):
        """Retorna (sys_dev, mic_dev). sys_dev None = desligado; mic_dev False = desligado,
        None = padrão do sistema."""
        sys_dev = None
        if self.chk_sys.get_active():
            i = self.dd_sys.get_selected()
            sys_dev = default_monitor() if i == 0 else f"{self.sinks[i - 1][0]}.monitor"
        mic_dev = False
        if self.chk_mic.get_active():
            i = self.dd_mic.get_selected()
            mic_dev = default_mic() if i == 0 else self.mics[i - 1][0]
        return sys_dev, mic_dev

    def _selected_camera(self):
        if not self.chk_cam.get_active() or not self.cams:
            return None
        return self.cams[min(self.dd_cam.get_selected(), len(self.cams) - 1)]

    def _cam_layout(self):
        pos = self.dd_cam_pos.get_selected()
        _, fx, fy = CAM_POSITIONS[pos]
        if fx is None:
            fx, fy = self.cfg["cam_fx"], self.cfg["cam_fy"]
        return fx, fy, CAM_SIZES[self.dd_cam_size.get_selected()][1]

    def _save_cfg(self):
        cam = self._selected_camera()
        self.cfg.update({
            "source": self.dd_source.get_selected(), "cursor": self.chk_cursor.get_active(),
            "fps": int(self.spin_fps.get_value()), "quality": self.dd_quality.get_selected(),
            "format": self.dd_format.get_selected(), "sys_audio": self.chk_sys.get_active(),
            "mic": self.chk_mic.get_active(), "remember": self.chk_remember.get_active(),
            "minimize": self.chk_minimize.get_active(),
            "start_paused": self.chk_start_paused.get_active(),
            "camera": self.chk_cam.get_active(), "mirror": self.chk_mirror.get_active(),
            "cam_pos": self.dd_cam_pos.get_selected(),
            "cam_size": self.dd_cam_size.get_selected(),
        })
        if cam:
            self.cfg["camera_name"] = cam["label"]
        if not self.cfg["remember"]:
            self.cfg["restore_token"] = ""
        self.cfg.save()

    def _output_path(self, ext):
        os.makedirs(self.cfg["output_dir"], exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return os.path.join(self.cfg["output_dir"], f"gravacao_{stamp}.{ext}")

    def _set_busy(self, busy):
        for w in self.settings_widgets:
            w.set_sensitive(not busy)
        if not busy:
            self._sync_sensitivity()

    def _choose_dir(self, _btn):
        dlg = Gtk.FileDialog(title="Pasta de saída")
        dlg.set_initial_folder(Gio.File.new_for_path(
            self.cfg["output_dir"] if os.path.isdir(self.cfg["output_dir"])
            else GLib.get_home_dir()))

        def done(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.cfg["output_dir"] = folder.get_path()
                self.lbl_dir.set_text(self.cfg["output_dir"])
                self._save_cfg()

        dlg.select_folder(self, None, done)

    def _open_dir(self, _btn):
        os.makedirs(self.cfg["output_dir"], exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(
            Gio.File.new_for_path(self.cfg["output_dir"]).get_uri(), None)

    # ---------- câmera: posição, tamanho e arraste na prévia ----------
    def _on_cam_pos_changed(self, *_):
        if self._updating_pos:
            return
        fx, fy, _ = self._cam_layout()
        if self.recorder:
            self.recorder.set_camera_layout(fx, fy)
        self._save_cfg()

    def _on_cam_size_changed(self, *_):
        if self.recorder:
            self.recorder.set_camera_layout(size=CAM_SIZES[self.dd_cam_size.get_selected()][1])
        self._save_cfg()

    def _preview_mapping(self):
        """Retorna (escala, offset_x, offset_y) entre a prévia (widget) e o vídeo."""
        if not (self.recorder and self.recorder.video_size):
            return None
        W, H = self.recorder.video_size
        pw, ph = self.picture.get_width(), self.picture.get_height()
        if pw <= 0 or ph <= 0:
            return None
        s = min(pw / W, ph / H)
        return s, (pw - W * s) / 2, (ph - H * s) / 2

    def _hit_camera(self, x, y):
        m = self._preview_mapping()
        rect = self.recorder.camera_rect if self.recorder else None
        if not (m and rect):
            return False
        s, ox, oy = m
        vx, vy = (x - ox) / s, (y - oy) / s
        cx, cy, cw, ch = rect
        return cx <= vx <= cx + cw and cy <= vy <= cy + ch

    def _on_preview_motion(self, _ctrl, x, y):
        if self._drag_origin is None:
            self.picture.set_cursor_from_name("grab" if self._hit_camera(x, y) else None)

    def _on_drag_begin(self, gesture, x, y):
        if self._hit_camera(x, y):
            self._drag_origin = self.recorder.camera_rect[:2]
            self.picture.set_cursor_from_name("grabbing")
        else:
            self._drag_origin = None
            gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _on_drag_update(self, _gesture, dx, dy):
        m = self._preview_mapping()
        if self._drag_origin is None or not m:
            return
        s = m[0]
        fx, fy = self.recorder.move_camera_to(self._drag_origin[0] + dx / s,
                                              self._drag_origin[1] + dy / s)
        self.cfg["cam_fx"], self.cfg["cam_fy"] = fx, fy
        if self.dd_cam_pos.get_selected() != CAM_CUSTOM:
            self._updating_pos = True
            self.dd_cam_pos.set_selected(CAM_CUSTOM)
            self._updating_pos = False

    def _on_drag_end(self, *_):
        if self._drag_origin is not None:
            self._drag_origin = None
            self.picture.set_cursor_from_name("grab")
            self._save_cfg()

    # ---------- gravação ----------
    def _on_record_clicked(self, _btn):
        if self.recorder:
            self._stop_recording()
        elif not self.session:
            self._start_recording()

    def _start_recording(self):
        if not self.portal:
            self._status("xdg-desktop-portal indisponível.", error=True)
            return
        missing = missing_packages()
        if missing:
            self._status("Instale os pacotes: " + " ".join(missing), error=True)
            return
        if self.chk_cam.get_active() and not self.cams:
            self._status("Nenhuma câmera encontrada. Desmarque “Câmera” ou conecte uma.",
                         error=True)
            return
        self._save_cfg()
        self._set_busy(True)
        self.btn_rec.set_sensitive(False)
        self._status("Aguardando seleção da tela/janela…")
        self.session = ScreenCastSession(
            self.portal, SOURCES[self.dd_source.get_selected()][1],
            self.chk_cursor.get_active(), self.chk_remember.get_active(),
            self.cfg.get("restore_token", ""),
            self._on_session_ready, self._on_session_failed, self._on_session_closed)
        self.session.start()

    def _on_session_failed(self, message, cancelled):
        self.session = None
        if not cancelled and self.cfg.get("restore_token"):
            self.cfg["restore_token"] = ""  # token pode ter expirado
            self.cfg.save()
        self._set_busy(False)
        self.btn_rec.set_sensitive(True)
        self._status(message, error=not cancelled)

    def _on_session_ready(self, fd, node_id, _props, restore_token):
        if self.chk_remember.get_active() and restore_token:
            self.cfg["restore_token"] = restore_token
            self.cfg.save()
        fmt = FORMATS[self.dd_format.get_selected()]
        sys_dev, mic_dev = self._selected_audio()
        camera = self._selected_camera()
        path = self._output_path(fmt["ext"])
        try:
            self.recorder = Recorder(
                fd, node_id, fps=int(self.spin_fps.get_value()),
                quality=self.dd_quality.get_selected(), fmt=fmt, sys_dev=sys_dev,
                mic_dev=mic_dev, path=path, camera=camera,
                mirror=self.chk_mirror.get_active(), cam_layout=self._cam_layout(),
                preview=camera is not None, start_paused=self.chk_start_paused.get_active(),
                on_done=self._on_recording_done)
        except GLib.Error as e:
            os.close(fd)
            self.session.close()
            self._on_session_failed(f"Erro ao montar o pipeline: {e.message}", False)
            return
        if not self.recorder.start():
            return

        self.rec_path = path
        if self.recorder.preview_paintable:
            self.picture.set_paintable(self.recorder.preview_paintable)
            self.preview_box.set_visible(True)
        elif camera and missing_packages(optional=True):
            self._status("Sem prévia: instale " + " ".join(missing_packages(optional=True)))
        self.timer_id = GLib.timeout_add(250, self._tick)
        self.btn_rec.set_label("⏹  Parar gravação")
        self.btn_rec.remove_css_class("suggested-action")
        self.btn_rec.add_css_class("destructive-action")
        self.btn_rec.set_sensitive(True)
        self.btn_pause.set_sensitive(True)
        self._update_pause_ui()

        extras = [n for n, on in (("áudio do sistema", sys_dev is not None),
                                  ("microfone", mic_dev is not False),
                                  ("câmera", camera is not None)) if on]
        self.rec_desc = (" + ".join(extras) if extras else "sem áudio")
        self._update_status_line()
        if self.chk_minimize.get_active() and not self.recorder.paused:
            self.minimize()

    def _update_status_line(self):
        if not self.recorder:
            return
        state = "⏸ Pausado" if self.recorder.paused else "Gravando"
        self._status(f"{state} ({self.rec_desc}) → {os.path.basename(self.rec_path)}")

    def _update_pause_ui(self):
        paused = self.recorder.paused if self.recorder else False
        self.btn_pause.set_label("▶  Continuar" if paused else "⏸  Pausar")
        if paused:
            self.btn_pause.add_css_class("suggested-action")
            self.lbl_timer.remove_css_class("rec")
            self.lbl_timer.add_css_class("paused")
        else:
            self.btn_pause.remove_css_class("suggested-action")
            self.lbl_timer.remove_css_class("paused")
            self.lbl_timer.add_css_class("rec")

    def _on_pause_clicked(self, _btn):
        if not self.recorder:
            return
        if self.recorder.paused:
            self.recorder.resume()
            if self.chk_minimize.get_active():
                self.minimize()
        else:
            self.recorder.pause()
        self._update_pause_ui()
        self._update_status_line()

    def _on_session_closed(self):
        # Usuário encerrou pelo indicador do sistema (ex.: ícone de gravação do GNOME)
        if self.recorder:
            self._stop_recording()

    def _tick(self):
        if self.recorder:
            secs = self.recorder.elapsed() // Gst.SECOND
            self.lbl_timer.set_text(f"{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}")
        return True

    def _stop_recording(self):
        self.btn_rec.set_sensitive(False)
        self.btn_pause.set_sensitive(False)
        self._status("Finalizando arquivo…")
        self.recorder.stop()

    def _on_recording_done(self, ok, message):
        if self.timer_id:
            GLib.source_remove(self.timer_id)
            self.timer_id = 0
        self.recorder = None
        if self.session:
            self.session.close()
            self.session = None
        self.picture.set_paintable(None)
        self.preview_box.set_visible(False)
        self.lbl_timer.remove_css_class("rec")
        self.lbl_timer.remove_css_class("paused")
        self.btn_rec.set_label("⏺  Iniciar gravação")
        self.btn_rec.remove_css_class("destructive-action")
        self.btn_rec.add_css_class("suggested-action")
        self.btn_rec.set_sensitive(True)
        self.btn_pause.set_label("⏸  Pausar")
        self.btn_pause.remove_css_class("suggested-action")
        self.btn_pause.set_sensitive(False)
        self._set_busy(False)
        if ok:
            self._status(f"{message} {os.path.basename(self.rec_path)}")
        else:
            self._status(f"Erro na gravação: {message}", error=True)
        self.present()
        if self.quit_after_stop:
            self.get_application().quit()

    def _on_close_request(self, _win):
        self._save_cfg()
        if self.recorder:
            self.quit_after_stop = True
            self._stop_recording()
            return True  # aguarda finalizar o arquivo
        if self.session:
            self.session.close()
        return False

    def request_quit(self):
        if not self._on_close_request(self):
            self.get_application().quit()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_activate(self):
        if not self.win:
            self.win = MainWindow(self)
        self.win.present()


def main():
    Gst.init(None)
    GLib.set_application_name(APP_NAME)
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if "--check" in sys.argv:
        missing, optional = missing_packages(), missing_packages(optional=True)
        print("OK: todas as dependências GStreamer encontradas." if not missing
              else "Faltando: sudo apt install " + " ".join(missing))
        if optional:
            print("Opcional (prévia da câmera): sudo apt install " + " ".join(optional))
        cams = list_cameras()
        print("Câmeras: " + (", ".join(c["label"] for c in cams) if cams else "nenhuma"))
        return 1 if missing else 0
    app = App()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,
                         lambda: (app.win.request_quit() if app.win else app.quit()) or True)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
