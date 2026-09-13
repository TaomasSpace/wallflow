// wallflow depth overlay — run by `wallflow overlay start` as `qs -p overlay.qml`.
//
// One click-through window per screen on the wlr-layer-shell `bottom` layer:
// widget addons (~/.config/wallflow/widgets/*.qml) at the back, the current
// wallpaper's subject cutout on top. Everything comes from overlay.json
// (written by Python, watched here) — nothing is hardcoded.
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root

    property var state: ({})
    property string statePath: Quickshell.env("WALLFLOW_OVERLAY_STATE")
                               || (Quickshell.env("HOME") + "/.cache/wallflow/overlay.json")

    FileView {
        id: stateFile
        path: root.statePath
        watchChanges: true
        onFileChanged: reload()
        onLoaded: root.parse()
        onLoadFailed: retry.start()
    }

    // a write may be caught half-done — just try again shortly
    Timer { id: retry; interval: 300; repeat: false; onTriggered: stateFile.reload() }

    function parse() {
        try {
            root.state = JSON.parse(stateFile.text())
        } catch (e) {
            retry.start()
        }
    }

    function wantsScreen(name) {
        const outs = root.state.outputs || "*"
        if (outs === "*" || outs === "") return true
        return outs.split(",").map(s => s.trim()).indexOf(name) >= 0
    }

    Variants {
        model: Quickshell.screens

        PanelWindow {
            id: win
            property var modelData
            screen: modelData
            visible: root.wantsScreen(modelData.name)

            WlrLayershell.layer: WlrLayer.Bottom
            WlrLayershell.namespace: "wallflow-overlay"
            WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
            anchors { top: true; bottom: true; left: true; right: true }
            exclusiveZone: 0
            color: "transparent"
            mask: Region {}                 // empty region = every click passes through

            Item {
                id: stage
                anchors.fill: parent

                // --- widget layer -------------------------------------------
                Repeater {
                    model: root.state.widgets || []
                    Loader {
                        anchors.fill: parent
                        source: modelData.qml
                        z: index
                        onLoaded: {
                            if (item.hasOwnProperty("cfg"))
                                item.cfg = Qt.binding(() => root.state.config || ({}))
                            if (item.hasOwnProperty("screenName"))
                                item.screenName = win.modelData.name
                        }
                    }
                }

                // --- subject layer ------------------------------------------
                Image {
                    id: cutout
                    anchors.fill: parent
                    z: 1000
                    source: root.state.cutout ? "file://" + root.state.cutout : ""
                    // must match how the backend fits the wallpaper: cover (centred crop) by default
                    fillMode: root.state.fill === "fit" ? Image.PreserveAspectFit : Image.PreserveAspectCrop
                    asynchronous: true
                    cache: false
                    smooth: true
                    mipmap: true
                    opacity: status === Image.Ready ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: 250 } }
                }
            }
        }
    }
}
