// wallflow depth overlay — run by `wallflow overlay start` as `qs -p overlay.qml`.
//
// One click-through window per screen on the wlr-layer-shell `bottom` layer:
// widget addons (~/.config/wallflow/widgets/*.qml) at the back, the current
// wallpaper's subject cutout on top. Everything comes from overlay.json
// (written by Python, watched here) — nothing is hardcoded.
//
// Edit mode (state.edit = true, `wallflow overlay edit`): the window moves to the
// `top` layer, takes input, widgets get draggable; a drop calls root.save(),
// which runs `wallflow config set …` and the new state flows back in.
//
// Other programs' widgets: anything on the `background` layer (Caelestia's desktop
// clock, eww `:stacking "bg"`) is always below us. On the `bottom` layer Hyprland draws
// surfaces in the order they appeared, so a widget started after us would cover the
// cutout. Whenever a layer opens we ask `hyprctl -j layers` whether we're still last on
// `bottom`, and if not, hop to `background` and back: a layer change re-appends a
// surface at the end of its new layer, i.e. on top (overlay.raise, `wallflow overlay raise`).
import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root

    property var state: ({})
    property bool editing: state.edit === true
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

    // --- staying on top of other bottom-layer widgets -----------------------------
    readonly property string ns: "wallflow-overlay"
    property bool autoRaise: !root.state.config || root.state.config.raise !== false
    property var liftScreens: []       // screen names to lift on the next liftSeq bump
    property int liftSeq: 0
    property int lastManual: -1

    onStateChanged: {
        const n = root.state.raise || 0
        if (root.lastManual >= 0 && n !== root.lastManual)
            root.lift(["*"])                                   // `wallflow overlay raise`
        root.lastManual = n
    }

    function lift(names) {
        root.liftScreens = names
        root.liftSeq++
    }

    Connections {
        target: Hyprland
        function onRawEvent(event) {
            if (event.name === "openlayer" && event.data !== root.ns && root.autoRaise)
                check.restart()
        }
    }
    Timer { id: check; interval: 400; onTriggered: if (!layers.running) layers.running = true }
    Timer { interval: 1500; running: true; onTriggered: if (root.autoRaise) check.restart() }   // after start

    Process {
        id: layers
        command: ["hyprctl", "-j", "layers"]
        stdout: StdioCollector {
            onStreamFinished: {
                let mons = {}
                try { mons = JSON.parse(this.text) } catch (e) { return }
                const behind = []
                for (const mon in mons) {
                    const bottom = ((mons[mon].levels || {})["1"] || [])
                    const ours = bottom.findIndex(l => l.namespace === root.ns)
                    if (ours >= 0 && ours < bottom.length - 1)
                        behind.push(mon)
                }
                if (behind.length) root.lift(behind)
            }
        }
    }

    function wantsScreen(name) {
        const outs = root.state.outputs || "*"
        if (outs === "*" || outs === "") return true
        return outs.split(",").map(s => s.trim()).indexOf(name) >= 0
    }

    // --- writing back (edit mode) ---------------------------------------------
    // save({clock_x: 0.61, clock_y: 0.2}) -> wallflow config set overlay.clock_x 0.61 …
    property var pending: []
    Process { id: runner; onExited: root.flush() }

    function wallflow(args) {
        root.pending.push(args)
        root.flush()
    }
    function flush() {
        if (runner.running || root.pending.length === 0) return
        const exe = root.state.exe || ["wallflow"]
        runner.command = exe.concat(root.pending.shift())
        runner.running = true
    }
    function save(values) {
        let args = ["config", "set"]
        for (const k in values) args.push("overlay." + k, String(values[k]))
        root.wallflow(args)
    }
    function stopEditing() { root.wallflow(["overlay", "done"]) }

    Variants {
        model: Quickshell.screens

        PanelWindow {
            id: win
            property var modelData
            property Region emptyRegion: Region {}
            screen: modelData
            visible: root.wantsScreen(modelData.name)

            // lifting: one short hop to `background` and back puts us last (= on top) on `bottom`
            property bool lifting: false
            property int seenLift: 0
            WlrLayershell.layer: root.editing ? WlrLayer.Top : (lifting ? WlrLayer.Background : WlrLayer.Bottom)

            Connections {
                target: root
                function onLiftSeqChanged() {
                    if (win.seenLift === root.liftSeq || root.editing) return
                    win.seenLift = root.liftSeq
                    if (root.liftScreens.indexOf("*") >= 0 || root.liftScreens.indexOf(win.modelData.name) >= 0) {
                        win.lifting = true
                        hop.restart()
                    }
                }
            }
            Timer { id: hop; interval: 90; onTriggered: win.lifting = false }   // two separate commits
            WlrLayershell.namespace: "wallflow-overlay"
            WlrLayershell.keyboardFocus: root.editing ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None
            anchors { top: true; bottom: true; left: true; right: true }
            // ignore every other layer's reserved space (bars!) — the crop must be computed
            // on the full output, exactly like the wallpaper backend does, or the cutout shifts
            exclusionMode: ExclusionMode.Ignore
            color: root.editing ? "#20000000" : "transparent"
            mask: root.editing ? null : emptyRegion    // empty region = every click passes through

            Item {
                id: stage
                anchors.fill: parent
                focus: root.editing
                Keys.onEscapePressed: root.stopEditing()

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
                            if (item.hasOwnProperty("editing"))
                                item.editing = Qt.binding(() => root.editing)
                            if (item.hasOwnProperty("overlay"))
                                item.overlay = root
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
                    opacity: (status === Image.Ready ? 1 : 0) * (root.editing ? 0.35 : 1)
                    Behavior on opacity { NumberAnimation { duration: 250 } }
                }

                // --- edit-mode hint -----------------------------------------
                Rectangle {
                    visible: root.editing
                    z: 2000
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 40
                    width: hint.implicitWidth + 40; height: hint.implicitHeight + 20
                    radius: height / 2
                    color: "#cc101216"
                    border.color: "#40ffffff"
                    Text {
                        id: hint
                        anchors.centerIn: parent
                        color: "white"; font.pixelSize: 15
                        text: "drag widgets  \u00b7  Esc or `wallflow overlay done` to finish"
                    }
                }
            }
        }
    }
}
