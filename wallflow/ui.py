"""The coverflow picker (PySide6 + QML). Only imported by `wallflow ui`."""
import sys

from . import backend, config, thumbs

QML = r"""
import QtQuick
import QtQuick.Window

Window {
    id: win
    flags: Qt.FramelessWindowHint
    color: backdrop

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: 28
        color: "white"
        font.pixelSize: 18
        font.letterSpacing: 1
        text: view.count > 0
              ? "Wallpapers  " + (view.currentIndex + 1) + " / " + view.count
              : "No wallpapers found"
    }

    PathView {
        id: view
        anchors.fill: parent
        focus: true
        model: wallpapers
        currentIndex: startIndex
        pathItemCount: 5
        preferredHighlightBegin: 0.5
        preferredHighlightEnd: 0.5
        highlightRangeMode: PathView.StrictlyEnforceRange
        highlightMoveDuration: 220

        Keys.onLeftPressed:   view.decrementCurrentIndex()
        Keys.onRightPressed:  view.incrementCurrentIndex()
        Keys.onReturnPressed: applyCurrent()
        Keys.onEnterPressed:  applyCurrent()
        Keys.onEscapePressed: win.close()
        Keys.onPressed: (event) => {
            if (event.key === Qt.Key_Home) view.currentIndex = 0
            else if (event.key === Qt.Key_End) view.currentIndex = view.count - 1
            else if (event.key === Qt.Key_R && view.count > 0)
                view.currentIndex = Math.floor(Math.random() * view.count)
        }

        function applyCurrent() {
            if (view.count > 0) {
                backend.apply(wallpapers[view.currentIndex].full)
                win.close()
            }
        }

        WheelHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: (event) => {
                if (event.angleDelta.y < 0) view.incrementCurrentIndex()
                else if (event.angleDelta.y > 0) view.decrementCurrentIndex()
            }
        }

        path: Path {
            startX: 0
            startY: win.height / 2
            PathAttribute { name: "z";     value: 0 }
            PathAttribute { name: "scale"; value: 0.55 }
            PathAttribute { name: "angle"; value: 55 }
            PathLine { x: win.width / 2; y: win.height / 2 }
            PathAttribute { name: "z";     value: 100 }
            PathAttribute { name: "scale"; value: 1.0 }
            PathAttribute { name: "angle"; value: 0 }
            PathLine { x: win.width; y: win.height / 2 }
            PathAttribute { name: "z";     value: 0 }
            PathAttribute { name: "scale"; value: 0.55 }
            PathAttribute { name: "angle"; value: -55 }
        }

        delegate: Item {
            id: card
            property real cardScale: PathView.scale
            property real cardAngle: PathView.angle
            property real cardZ:     PathView.z

            width: win.width * 0.40
            height: win.height * 0.58
            scale: cardScale
            z: cardZ

            transform: Rotation {
                origin.x: card.width / 2
                origin.y: card.height / 2
                axis { x: 0; y: 1; z: 0 }
                angle: card.cardAngle
            }

            Image {
                anchors.fill: parent
                source: "file://" + modelData.thumb
                fillMode: Image.PreserveAspectCrop
                sourceSize.width: thumbWidth
                asynchronous: true
                cache: true
                opacity: status === Image.Ready ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: 180 } }
                Rectangle {
                    anchors.fill: parent
                    color: "transparent"
                    border.color: "#40ffffff"
                    border.width: 1
                }
            }

            Rectangle {                       // "animated" badge
                visible: modelData.video
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 14
                width: 36; height: 36; radius: 18
                color: "#cc000000"
                border.color: "#40ffffff"
                border.width: 1
                Text { anchors.centerIn: parent; text: "\u25B6"; color: "white"; font.pixelSize: 15 }
            }

            Text {                            // filename under the centre card
                visible: view.currentIndex === index
                anchors.top: parent.bottom
                anchors.topMargin: 14
                anchors.horizontalCenter: parent.horizontalCenter
                color: "#ccffffff"
                font.pixelSize: 14
                text: modelData.name
            }

            MouseArea {
                anchors.fill: parent
                onClicked: {
                    if (view.currentIndex === index) {
                        backend.apply(modelData.full)
                        win.close()
                    } else {
                        view.currentIndex = index
                    }
                }
            }
        }
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 24
        color: "#aaffffff"
        font.pixelSize: 13
        text: "\u2190 / \u2192  browse   \u00b7   Enter  apply   \u00b7   R  random   \u00b7   Esc  cancel"
    }
}
"""


APP_ID = "wallflow"


def _hyprctl_json(*args):
    import json
    import subprocess
    out = subprocess.run(["hyprctl", *args, "-j"], capture_output=True, text=True, timeout=2).stdout
    return json.loads(out)


def _focused_monitor() -> dict | None:
    try:
        return next(m for m in _hyprctl_json("monitors") if m.get("focused"))
    except Exception:
        return None


def _screen_for(app, mon: dict | None):
    """QScreen matching the Hyprland monitor (Wayland output names match)."""
    screens = app.screens()
    if mon:
        for s in screens:
            if s.name() == mon["name"]:
                return s
    return app.primaryScreen()


def _relocate_when_mapped(app, mon: dict | None) -> None:
    """Hyprland ignores the output a client asks to go fullscreen on and puts the
    window on whatever workspace it likes. Once our window shows up in
    `hyprctl clients`, move it to the focused monitor's active workspace."""
    import os
    import subprocess
    from PySide6.QtCore import QTimer
    if not mon:
        return
    debug = os.environ.get("WALLFLOW_DEBUG")
    target_ws = mon["activeWorkspace"]["id"]
    tries = [0]
    timer = QTimer(app)

    def poll():
        tries[0] += 1
        try:
            win = next(c for c in _hyprctl_json("clients") if c.get("class") == APP_ID)
        except Exception:
            win = None
        if win is None:
            if tries[0] >= 40:          # ~2 s, give up quietly
                timer.stop()
            return
        timer.stop()
        if win.get("monitor") != mon["id"]:
            if debug:
                print(f"[wallflow] landed on monitor {win.get('monitor')}, moving to "
                      f"{mon['name']} (ws {target_ws})", file=sys.stderr)
            r = subprocess.run(["hyprctl", "--batch",
                                f"dispatch movetoworkspace {target_ws},class:^({APP_ID})$; "
                                f"dispatch focuswindow class:^({APP_ID})$"],
                               capture_output=True, text=True, timeout=2)
            if debug:
                print(f"[wallflow] hyprctl: {(r.stdout + r.stderr).strip()}", file=sys.stderr)
        elif debug:
            print(f"[wallflow] mapped on {mon['name']} as requested", file=sys.stderr)

    timer.timeout.connect(poll)
    timer.start(50)


def run(cfg: dict, include_hidden: bool = False) -> int:
    from PySide6.QtCore import QObject, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    import os

    class Backend(QObject):
        @Slot(str)
        def apply(self, path: str) -> None:
            backend.apply(path, cfg)

    app = QGuiApplication(sys.argv[:1])
    app.setDesktopFileName(APP_ID)          # -> Wayland app_id / Hyprland class
    files = backend.gather_wallpapers(cfg, include_hidden=include_hidden)
    if not files:
        print(f"No wallpapers found in {config.wallpaper_dir(cfg)}", file=sys.stderr)
    items = [{"full": f, "thumb": thumbs.thumb_for(f, cfg),
              "video": config.is_video(cfg, f), "name": os.path.basename(f)} for f in files]

    current = backend.read_current()
    start = next((i for i, it in enumerate(items) if it["full"] == current), 0)

    be = Backend()
    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", be)
    ctx.setContextProperty("wallpapers", items)
    ctx.setContextProperty("startIndex", start)
    ctx.setContextProperty("thumbWidth", int(cfg["ui"]["thumb_width"]))
    ctx.setContextProperty("backdrop", cfg["ui"]["backdrop"])
    engine.loadData(QML.encode("utf-8"))
    if not engine.rootObjects():
        print("Failed to load QML.", file=sys.stderr)
        return 1
    # Screen must be set on the QWindow *before* the first show: the Wayland
    # backend sends xdg_toplevel.set_fullscreen(output) at map time, and Hyprland
    # honours that output. Doing it via a QML binding raced with `visibility`.
    from PySide6.QtCore import QTimer
    mon = _focused_monitor()
    win = engine.rootObjects()[0]
    target = _screen_for(app, mon)
    win.setScreen(target)

    # Hyprland honours the output a client asks to go fullscreen on, but Qt only
    # learns a window's real output (wl_surface.enter) after it is mapped — a
    # fullscreen request before the first map carries a stale default output.
    # So: map as a normal window (lands on the focused monitor), then go
    # fullscreen once Qt reports the right screen.
    done = [False]

    def go_fullscreen():
        if done[0]:
            return
        done[0] = True
        if os.environ.get("WALLFLOW_DEBUG"):
            print(f"[wallflow] fullscreen on {win.screen().name()}", file=sys.stderr)
        win.showFullScreen()
        _relocate_when_mapped(app, mon)

    def on_screen(scr):
        if scr is not None and scr.name() == target.name():
            go_fullscreen()

    win.screenChanged.connect(on_screen)
    win.show()
    if win.screen() is not None and win.screen().name() == target.name():
        QTimer.singleShot(30, go_fullscreen)     # let the map + enter happen first
    QTimer.singleShot(200, go_fullscreen)        # fallback, never stay windowed
    return app.exec()
