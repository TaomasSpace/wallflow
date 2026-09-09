"""The coverflow picker (PySide6 + QML). Only imported by `wallflow ui`."""
import sys

from . import backend, config, thumbs

QML = r"""
import QtQuick
import QtQuick.Window

Window {
    id: win
    visibility: Window.FullScreen
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


def run(cfg: dict) -> int:
    from PySide6.QtCore import QObject, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    import os

    class Backend(QObject):
        @Slot(str)
        def apply(self, path: str) -> None:
            backend.apply(path, cfg)

    app = QGuiApplication(sys.argv[:1])
    files = backend.gather_wallpapers(cfg)
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
    return app.exec()
