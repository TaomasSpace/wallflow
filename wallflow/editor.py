"""`wallflow depth edit [file]` - decide what sits on the 3D layer, by pointing.

Click a thing and SAM 2.1 segments exactly that thing; the mouse wheel then steps
between a smaller part and the whole object. A loose lasso keeps the main object
inside it; a right-drag along a ridge, ledge or floor line takes everything below
it, snapped to the image's edges. No precise outlines, no word lists.

The mask lives in one worker process in the depth venv (pick_worker.py); after
every change it writes an 8-bit palette PNG at display size (0 behind, 1 in front,
2 outline) which Qt loads as an indexed image - so dimming, outlines and the
preview are colour-table swaps. No numpy in wallflow's own interpreter.

Lit = on the 3D layer (drawn above your widgets), dimmed = stays behind.
"""
import json
import os
import sys

from . import config, depth, objects

HINTS = ("click  object   ·   wheel  smaller / bigger   ·   Shift+click / Shift+right-click  include / exclude   "
         "·   drag  lasso   ·   right-drag  everything below   ·   Ctrl  remove instead   ·   "
         "Alt+drag  exact shape")
KEYS = ("Space  preview with clock   ·   middle-click  move preview clock   ·   A  automatic   ·   "
        "C  clear   ·   Ctrl+Z  undo   ·   Enter  apply   ·   Del  back to automatic   ·   Esc  cancel")
START = {"saved": "your saved mask", "manual": "your hand-picked layer", "auto": "the automatic cutout",
         "subject": "the character", "empty": "nothing"}


def _argb(a: int, r: int, g: int, b: int) -> int:
    return ((a & 255) << 24) | ((r & 255) << 16) | ((g & 255) << 8) | (b & 255)


def _parse_color(s: str) -> tuple[int, int, int]:
    s = (s or "").lstrip("#")
    if len(s) == 8:          # #AARRGGBB (the picker's backdrop format)
        s = s[2:]
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except (ValueError, IndexError):
        return 16, 18, 22


def build(src: str, cfg: dict):
    """The editor widget (separate from run() so it can be driven in tests)."""
    from PySide6.QtCore import QDate, QPointF, QProcess, QRectF, QSize, Qt, QTime, QTimer
    from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter, QPainterPath, QPen
    from PySide6.QtWidgets import QApplication, QWidget

    bg = _parse_color(cfg["ui"].get("backdrop", "#e6101216"))
    ov = cfg["overlay"]

    class Editor(QWidget):
        def __init__(self):
            super().__init__()
            self.src, self.cfg = os.path.abspath(src), cfg
            self.setWindowTitle("wallflow — 3D layer")
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setCursor(Qt.CrossCursor)
            self.state = "loading"          # loading | ready | busy | error
            self.status = "preparing …"
            self.error = ""
            self.note = ""
            self.photo = self.labels = self.dim_img = self.front_img = None
            self.preview = False
            self.view_px = QSize(0, 0)
            self.screen_h = 1440
            self.clock = (float(ov.get("clock_x", 0.5)), float(ov.get("clock_y", 0.12)))
            self.front = 0.0
            self.levels = (0, 0)
            self.undo_n = 0
            self.dirty = False
            self.proc = None
            self.buf = ""
            self.queue: list[tuple[str, dict, object]] = []
            self.inflight = None
            self.req = 0
            self.log_tail: list[str] = []
            self.result = None
            self.closing = False
            self.dots = 0
            self.stroke: list[tuple[float, float]] = []
            self.button = None
            self.mods = Qt.NoModifier
            self.press = None
            self.tick = QTimer(self)
            self.tick.timeout.connect(self._tick)
            self.tick.start(400)
            self.note_timer = QTimer(self)
            self.note_timer.setSingleShot(True)
            self.note_timer.timeout.connect(self._clear_note)
            self.clock_timer = QTimer(self)          # keep the preview clock's minute current
            self.clock_timer.timeout.connect(lambda: self.preview and self.update())
            self.clock_timer.start(15000)

        # -- layout ---------------------------------------------------------------
        def image_rect(self) -> QRectF:
            W, H = self.img_size
            aw, ah = max(1, self.width() - 64), max(1, self.height() - 176)
            s = min(aw / W, ah / H)
            w, h = W * s, H * s
            return QRectF((self.width() - w) / 2, (self.height() - h) / 2 - 18, w, h)

        def frac(self, pos) -> tuple[float, float]:
            rc = self.image_rect()
            return ((pos.x() - rc.x()) / rc.width(), (pos.y() - rc.y()) / rc.height())

        def prepare(self, screen_size: QSize, dpr: float) -> None:
            """Kick off: photo at display size, worker, open the image."""
            if config.is_video(self.cfg, self.src):
                return self._fail("videos and GIFs have no 3D layer (only still images)")
            reader = QImageReader(self.src)
            reader.setAutoTransform(False)           # PIL (worker) ignores EXIF rotation too
            sz = reader.size()
            if not sz.isValid():
                return self._fail(f"cannot read {self.src}")
            self.img_size = (sz.width(), sz.height())
            self.screen_h = max(1, screen_size.height())
            W, H = self.img_size
            aw, ah = max(1, screen_size.width() - 64), max(1, screen_size.height() - 176)
            s = min(aw / W, ah / H) * dpr
            self.view_px = QSize(max(1, min(W, round(W * s))), max(1, min(H, round(H * s))))
            reader.setScaledSize(self.view_px)
            self.photo = reader.read().convertToFormat(QImage.Format_ARGB32_Premultiplied)
            if not depth.ready():
                return self._fail("depth is not set up — run `wallflow depth setup` first")
            if not objects.editor_ready():
                return self._fail("the editor's packages (torch + SAM 2.1) are missing — "
                                  "run `wallflow depth setup` once more")
            self._start_worker()
            self.status = "starting SAM 2.1"
            self._send("open", objects.open_args(self.src, self.cfg, self.view_px.width(),
                                                 self.view_px.height()), self._opened)

        # -- worker plumbing (one process, JSON requests on stdin, one at a time) -----
        def _start_worker(self) -> None:
            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._read)
            self.proc.finished.connect(self._worker_exit)
            self.proc.errorOccurred.connect(self._proc_error)
            cmd = objects.serve_cmd(self.cfg)
            self.proc.start(cmd[0], cmd[1:])

        def _proc_error(self, err) -> None:
            if err == QProcess.FailedToStart:
                self._fail(f"could not start the depth venv python ({depth.venv_python()}) "
                           "— `wallflow depth setup`")

        def _send(self, cmd: str, args: dict, on_done=None) -> None:
            self.queue.append((cmd, args, on_done))
            self._pump()

        def _pump(self) -> None:
            if self.inflight is not None or not self.queue or self.proc is None or self.state == "error":
                return
            cmd, args, cb = self.queue.pop(0)
            self.req += 1
            self.inflight = (self.req, cmd, cb)
            if self.state == "ready":
                self.state = "busy"
                QApplication.setOverrideCursor(Qt.BusyCursor)
            self.proc.write((json.dumps({"id": self.req, "cmd": cmd, "args": args}) + "\n").encode())

        def _read(self) -> None:
            self.buf += bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
            *lines, self.buf = self.buf.split("\n")
            for line in lines:
                line = line.rstrip()
                if line.startswith("status: "):
                    self.status = line[8:]
                elif line.startswith("done: "):
                    try:
                        payload = json.loads(line[6:])
                    except ValueError:
                        payload = {}
                    self._finish(payload, None)
                elif line.startswith("error: "):
                    self._finish(None, line[7:])
                elif line:
                    self.log_tail = (self.log_tail + [line])[-12:]
            self.update()

        def _finish(self, payload, err) -> None:
            if self.inflight is None:
                return
            _rid, cmd, cb = self.inflight
            self.inflight = None
            if self.state == "busy":
                self.state = "ready"
                QApplication.restoreOverrideCursor()
            if err is not None:
                if cmd in ("open", "apply"):
                    self._fail(f"{cmd} failed: {err}")
                    return
                self._say(f"{cmd} failed: {err}")
            elif cb is not None:
                cb(payload)
            self._pump()

        def _worker_exit(self, code, _status) -> None:
            if self.closing:
                return
            if self.inflight is not None or self.state == "loading":
                msg = self.log_tail[-1] if self.log_tail else f"exit {code}"
                self._fail(f"the editor's worker stopped: {msg}")
            self.proc = None

        def _fail(self, msg: str) -> None:
            if self.state == "busy":
                QApplication.restoreOverrideCursor()
            self.state, self.error = "error", msg
            self.result = ("error", msg)
            self.update()

        def _tick(self) -> None:
            if self.state in ("loading", "busy"):
                self.dots = (self.dots + 1) % 4
                self.update()

        def _say(self, text: str, secs: float = 6.0) -> None:
            self.note = text
            self.note_timer.start(int(secs * 1000))
            self.update()

        def _clear_note(self) -> None:
            self.note = ""
            self.update()

        # -- results ----------------------------------------------------------------------
        def _opened(self, p: dict) -> None:
            self.state, self.status = "ready", ""
            self._show(p)
            self.dirty = False
            self._say(f"starting from {START.get(p.get('start'), p.get('start'))}"
                      f" — opened in {p.get('seconds', '?')} s", 5)

        def _edited(self, p: dict) -> None:
            self.dirty = True
            self._show(p)
            if p.get("note"):
                self._say(p["note"])
            if p.get("start"):
                self._say(f"reset to {START.get(p['start'], p['start'])}", 4)

        def _show(self, p: dict) -> None:
            img = QImage(p["view"])
            if img.format() != QImage.Format_Indexed8:
                return self._fail("mask view did not load as an indexed image (Qt image plugins?)")
            self.labels = img
            self.front = float(p.get("front", 0))
            self.levels = (int(p.get("level", 0)), int(p.get("levels", 0)))
            self.undo_n = int(p.get("undo", 0))
            self._retable()

        def _retable(self) -> None:
            if self.labels is None:
                return
            r, g, b = bg
            table = [0] * 256
            if self.preview:
                table[1] = table[2] = _argb(255, 0, 0, 0)          # mask for the cutout
                self.labels.setColorTable(table)
                m = self.labels.convertToFormat(QImage.Format_ARGB32_Premultiplied)
                if m.size() != self.photo.size():
                    m = m.scaled(self.photo.size())
                front = self.photo.copy()
                p = QPainter(front)
                p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                p.drawImage(0, 0, m)
                p.end()
                self.front_img = front
            else:
                table[0] = _argb(160, r, g, b)                      # behind: dimmed
                table[1] = 0                                        # in front: as is
                table[2] = _argb(235, 120, 215, 255)                # its outline
                self.labels.setColorTable(table)
                self.dim_img = self.labels.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            self.update()

        # -- actions --------------------------------------------------------------------
        def edit(self, cmd: str, args: dict) -> None:
            if self.state == "ready":
                self._send(cmd, args, self._edited)

        def apply(self) -> None:
            if self.state != "ready":
                return
            self.status = "rendering the 3D layer"
            out = objects.new_cutout_path(self.src)
            self._send("apply", objects.apply_args(self.src, out), lambda p: self._saved(p, out))

        def _saved(self, p: dict, out) -> None:
            empty = bool(p.get("empty"))
            if empty:
                out.unlink(missing_ok=True)
            objects.save_manual(self.src, None if empty else str(out), p.get("coverage", 0.0))
            depth.set_enabled(self.src, True)
            objects.refresh_overlay(self.src, self.cfg)
            self.result = ("saved", "nothing in front — 3D layer empty for this image" if empty else
                           f"{p.get('coverage', 0) * 100:.0f}% of the image on the 3D layer → {out.name}")
            self.close()

        def back_to_auto(self) -> None:
            objects.reset(self.src)
            objects.refresh_overlay(self.src, self.cfg)
            if depth.wanted(self.src, self.cfg):
                from . import backend
                if backend.read_current() == self.src:
                    backend._spawn_background_cutout(self.src)
            self.result = ("reset", "back to the automatic cutout")
            self.close()

        # -- events ---------------------------------------------------------------------
        def mousePressEvent(self, e) -> None:
            if self.state != "ready":
                return
            pos = e.position()
            if e.button() == Qt.MiddleButton:
                self.clock = self.frac(pos)
                self.preview = True
                self._retable()
                self._say("preview clock moved (your real clock: `wallflow overlay edit`) — Space hides it", 4)
                return
            if e.modifiers() & Qt.ShiftModifier and e.button() in (Qt.LeftButton, Qt.RightButton):
                x, y = self.frac(pos)
                self.edit("refine", {"x": x, "y": y, "include": e.button() == Qt.LeftButton})
                return
            self.button, self.mods, self.press = e.button(), e.modifiers(), pos
            self.stroke = [(pos.x(), pos.y())]

        def mouseMoveEvent(self, e) -> None:
            if self.button is not None:
                self.stroke.append((e.position().x(), e.position().y()))
                self.update()

        def mouseReleaseEvent(self, e) -> None:
            if self.button is None:
                return
            btn, self.button = self.button, None
            moved = (e.position() - self.press).manhattanLength() > 6
            add = not (self.mods & Qt.ControlModifier)
            pts = [self.frac(QPointF(x, y)) for x, y in self.stroke]
            self.stroke = []
            if btn == Qt.LeftButton and not moved:
                x, y = self.frac(e.position())
                if 0 <= x <= 1 and 0 <= y <= 1:
                    self.edit("pick", {"x": x, "y": y})
            elif btn == Qt.LeftButton:
                self.edit("lasso", {"pts": pts, "add": add, "exact": bool(self.mods & Qt.AltModifier)})
            elif btn == Qt.RightButton and moved:
                self.edit("below", {"pts": pts, "add": add})
            self.update()

        def wheelEvent(self, e) -> None:
            if self.levels[1] > 1 and self.state == "ready":
                self.edit("cycle", {"step": 1 if e.angleDelta().y() > 0 else -1})

        def keyPressEvent(self, e) -> None:
            k, mods = e.key(), e.modifiers()
            if k == Qt.Key_Escape:
                self.result = self.result or ("cancel", "nothing changed")
                self.close()
            elif k in (Qt.Key_Return, Qt.Key_Enter):
                self.apply()
            elif k == Qt.Key_Space and not e.isAutoRepeat():
                self.preview = not self.preview
                self._retable()
            elif k == Qt.Key_Z and mods & Qt.ControlModifier:
                self.edit("undo", {})
            elif k == Qt.Key_A:
                self.edit("set", {"what": "auto"})
            elif k == Qt.Key_C:
                self.edit("set", {"what": "clear"})
            elif k in (Qt.Key_Delete, Qt.Key_Backspace) and self.state in ("ready", "error"):
                self.back_to_auto()

        def closeEvent(self, e) -> None:
            self.closing = True
            if self.state == "busy":
                QApplication.restoreOverrideCursor()
            if self.proc is not None and self.proc.state() != QProcess.NotRunning:
                self.proc.write(b'{"cmd": "quit"}\n')
                if not self.proc.waitForFinished(1500):
                    self.proc.kill()
                    self.proc.waitForFinished(2000)
            e.accept()

        # -- painting ---------------------------------------------------------------------
        def paintEvent(self, _e) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.fillRect(self.rect(), QColor(*bg))
            if self.photo is not None and not self.photo.isNull():
                rc = self.image_rect()
                p.drawImage(rc, self.photo)
                if self.state in ("ready", "busy") and self.labels is not None:
                    if self.preview and self.front_img is not None:
                        self._paint_clock(p, rc)
                        p.drawImage(rc, self.front_img)
                    elif self.dim_img is not None:
                        p.setRenderHint(QPainter.SmoothPixmapTransform, False)   # crisp outline
                        p.drawImage(rc, self.dim_img)
                        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                if self.state in ("loading", "error"):
                    p.fillRect(rc, QColor(bg[0], bg[1], bg[2], 150))
                if len(self.stroke) > 1:
                    path = QPainterPath(QPointF(*self.stroke[0]))
                    for pt in self.stroke[1:]:
                        path.lineTo(QPointF(*pt))
                    col = QColor(255, 110, 110) if self.mods & Qt.ControlModifier else QColor(120, 255, 160)
                    p.setPen(QPen(col, 2, Qt.DashLine if self.button == Qt.RightButton else Qt.SolidLine))
                    p.drawPath(path)
            self._paint_text(p)
            p.end()

        def _paint_clock(self, p, rc: QRectF) -> None:
            """Your [overlay] clock, scaled to the preview (approximate: the real one is
            placed on the whole screen)."""
            scale = rc.height() / self.screen_h
            size = max(8, int(float(ov.get("clock_size", 140)) * scale))
            weights = {"thin": QFont.Thin, "light": QFont.Light, "normal": QFont.Normal,
                       "bold": QFont.Bold, "black": QFont.Black}
            f = QFont(ov.get("clock_font") or self.font().family())
            f.setPixelSize(size)
            f.setWeight(weights.get(str(ov.get("clock_weight", "light")), QFont.Light))
            col = QColor(str(ov.get("clock_color", "#ffffff")))
            col.setAlphaF(float(ov.get("clock_opacity", 0.92)))
            cx, cy = rc.x() + self.clock[0] * rc.width(), rc.y() + self.clock[1] * rc.height()
            lines = [(QTime.currentTime().toString(str(ov.get("clock_format", "HH:mm"))), f)]
            dfmt = str(ov.get("clock_date_format", ""))
            if dfmt:
                f2 = QFont(f)
                f2.setPixelSize(max(6, int(size * 0.2)))
                lines.append((QDate.currentDate().toString(dfmt), f2))
            y = cy - size * 0.6
            for text, font in lines:
                p.setFont(font)
                h = font.pixelSize() * 1.25
                box = QRectF(cx - rc.width(), y, rc.width() * 2, h)
                if ov.get("clock_shadow", True):
                    p.setPen(QColor(0, 0, 0, 110))
                    p.drawText(box.translated(0, max(1, size / 40)), Qt.AlignHCenter | Qt.AlignVCenter, text)
                p.setPen(col)
                p.drawText(box, Qt.AlignHCenter | Qt.AlignVCenter, text)
                y += h

        def _paint_text(self, p) -> None:
            W = self.width()
            f = QFont(self.font())
            f.setPixelSize(18)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 1)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255))
            name = os.path.basename(self.src)
            title = name
            if self.state in ("ready", "busy"):
                title = f"{name}   ·   {self.front * 100:.0f}% in front"
                if self.levels[1] > 1:
                    title += f"   ·   pick size {self.levels[0] + 1}/{self.levels[1]} (wheel)"
                if self.preview:
                    title += "   ·   preview"
            p.drawText(QRectF(0, 22, W, 30), Qt.AlignHCenter | Qt.AlignVCenter, title)
            if self.state == "loading":
                f.setPixelSize(17)
                p.setFont(f)
                p.drawText(QRectF(0, self.height() / 2 - 20, W, 40), Qt.AlignCenter,
                           (self.status or "working") + "." * self.dots)
            elif self.state == "error":
                f.setPixelSize(16)
                p.setFont(f)
                p.setPen(QColor(255, 190, 190))
                p.drawText(QRectF(80, self.height() / 2 - 90, W - 160, 180),
                           Qt.AlignCenter | Qt.TextWordWrap, self.error + "\n\nEsc  close   ·   Del  back to automatic")
            f.setPixelSize(13)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 0)
            p.setFont(f)
            if self.state in ("ready", "busy"):
                if self.note:
                    ctx, col = self.note, QColor(170, 225, 255, 235)
                elif self.state == "busy":
                    ctx, col = (self.status or "working") + "." * self.dots, QColor(255, 255, 255, 200)
                else:
                    ctx = "lit = on the 3D layer (above your widgets)   ·   dimmed = stays behind"
                    col = QColor(255, 255, 255, 200)
                p.setPen(col)
                p.drawText(QRectF(0, self.height() - 84, W, 20), Qt.AlignHCenter | Qt.AlignVCenter, ctx)
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(0, self.height() - 58, W, 20), Qt.AlignHCenter | Qt.AlignVCenter, HINTS)
            p.drawText(QRectF(0, self.height() - 36, W, 20), Qt.AlignHCenter | Qt.AlignVCenter, KEYS)

    return Editor()


def run(src: str, cfg: dict) -> int:
    """Open the editor fullscreen on the focused monitor (same dance as the picker)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .ui import APP_ID, _focused_monitor, _relocate_when_mapped, _screen_for

    app = QApplication(sys.argv[:1])
    app.setDesktopFileName(APP_ID)
    mon = _focused_monitor()
    target = _screen_for(app, mon)
    w = build(src, cfg)
    w.winId()                                   # creates the QWindow so the screen can be set pre-map
    if w.windowHandle() is not None:
        w.windowHandle().setScreen(target)
    w.prepare(target.size(), target.devicePixelRatio())
    done = [False]

    def go_fullscreen():
        if done[0]:
            return
        done[0] = True
        w.showFullScreen()
        _relocate_when_mapped(app, mon)

    def on_screen(scr):
        if scr is not None and scr.name() == target.name():
            go_fullscreen()

    if w.windowHandle() is not None:
        w.windowHandle().screenChanged.connect(on_screen)
    w.resize(target.size())
    w.show()
    if w.windowHandle() is not None and w.windowHandle().screen() is not None \
            and w.windowHandle().screen().name() == target.name():
        QTimer.singleShot(30, go_fullscreen)
    QTimer.singleShot(200, go_fullscreen)
    rc = app.exec()
    if w.result:
        kind, msg = w.result
        print(msg, file=sys.stderr if kind == "error" else sys.stdout)
        return 1 if kind == "error" else 0
    return rc
