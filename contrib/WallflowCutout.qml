// WallflowCutout.qml - wallflow's 3D cutout, for your own quickshell config.
//
// Put it ABOVE your widgets, inside the same full-screen window, and they sit behind the
// wallpaper's subject - no matter which layer that window is on:
//
//     PanelWindow {                       // your desktop / clock window
//         anchors { top: true; bottom: true; left: true; right: true }
//         exclusionMode: ExclusionMode.Ignore      // measure the full screen, like the wallpaper
//         color: "transparent"
//         MyClock { … }
//         WallflowCutout { anchors.fill: parent }  // last = on top
//     }
//
// It follows ~/.cache/wallflow/cutout.json (rewritten on every wallpaper change) and fits
// the image the way wallflow does (overlay.fill). With nothing in front it draws nothing.
// If your window covers the whole screen you may not need wallflow's own overlay at all:
// `wallflow config set overlay.enabled false`.
import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root

    property string infoPath: (Quickshell.env("XDG_CACHE_HOME") || (Quickshell.env("HOME") + "/.cache"))
                              + "/wallflow/cutout.json"
    property var info: ({})
    property real fadeMs: 250

    FileView {
        id: file
        path: root.infoPath
        watchChanges: true
        onFileChanged: reload()
        onLoaded: {
            try { root.info = JSON.parse(text()) } catch (e) { retry.start() }
        }
        onLoadFailed: root.info = ({})
    }
    Timer { id: retry; interval: 300; onTriggered: file.reload() }

    Image {
        anchors.fill: parent
        // the real file, not the link: its name changes per cutout, so Qt never shows a stale one
        source: root.info.cutout ? "file://" + root.info.cutout : ""
        fillMode: root.info.fill === "fit" ? Image.PreserveAspectFit : Image.PreserveAspectCrop
        asynchronous: true
        cache: false
        smooth: true
        mipmap: true
        opacity: status === Image.Ready ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: root.fadeMs } }
    }
}
