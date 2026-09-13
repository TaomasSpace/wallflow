// wallflow widget: clock. Contract for widgets — a root Item filling the screen with
//   property var cfg            the [overlay] section of config.toml, live
//   property string screenName  the output this instance is on
//   property bool editing       true while `wallflow overlay edit` is on (optional)
//   property var overlay        the overlay root; overlay.save({key: value}) writes
//                               overlay.<key> back to config.toml (optional)
// The overlay loads it below the subject cutout.
import QtQuick
import Quickshell

Item {
    id: clock
    property var cfg: ({})
    property string screenName: ""
    property bool editing: false
    property var overlay: null
    anchors.fill: parent

    SystemClock { id: sys; precision: SystemClock.Seconds }

    readonly property string fmt:     cfg.clock_format      || "HH:mm"
    readonly property string dateFmt: cfg.clock_date_format ?? "dddd, d MMMM"
    readonly property real   size:    cfg.clock_size        || 140
    readonly property string family:  cfg.clock_font        || "sans-serif"
    readonly property color  colour:  cfg.clock_color       || "#ffffff"
    readonly property int    weight:  ({ thin: Font.Thin, light: Font.Light, bold: Font.Bold,
                                         black: Font.Black })[cfg.clock_weight] ?? Font.Normal
    readonly property bool   shadow:  cfg.clock_shadow ?? true

    // position = fraction of the screen (centre of the widget). While the user drags,
    // `pos` overrides cfg; it's dropped again as soon as the saved cfg arrives.
    property var pos: null
    readonly property real fx: pos ? pos.x : (cfg.clock_x ?? 0.5)
    readonly property real fy: pos ? pos.y : (cfg.clock_y ?? 0.12)
    onCfgChanged: pos = null

    Item {
        id: box
        width: col.width + 40; height: col.height + 24
        x: clock.width  * clock.fx - width  / 2
        y: clock.height * clock.fy - height / 2

        Rectangle {                                   // edit-mode frame
            anchors.fill: parent
            visible: clock.editing
            color: drag.active ? "#30ffffff" : "#18ffffff"
            border.color: "#a0ffffff"; border.width: 1; radius: 8
        }

        Column {
            id: col
            anchors.centerIn: parent
            spacing: Math.round(clock.size * 0.04)
            opacity: cfg.clock_opacity ?? 0.92

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: Qt.formatDateTime(sys.date, clock.fmt)
                color: clock.colour
                font { family: clock.family; pixelSize: clock.size; weight: clock.weight
                       letterSpacing: clock.size * 0.02 }
                style: clock.shadow ? Text.Raised : Text.Normal
                styleColor: "#66000000"
                renderType: Text.NativeRendering
            }
            Text {
                visible: clock.dateFmt !== ""
                anchors.horizontalCenter: parent.horizontalCenter
                text: Qt.formatDateTime(sys.date, clock.dateFmt)
                color: clock.colour
                font { family: clock.family; pixelSize: Math.round(clock.size * 0.22)
                       weight: Font.Normal; letterSpacing: 1 }
                style: clock.shadow ? Text.Raised : Text.Normal
                styleColor: "#66000000"
                renderType: Text.NativeRendering
            }
        }

        DragHandler {
            id: drag
            enabled: clock.editing
            target: null                              // we move via fx/fy, not the item directly
            property real sx: 0
            property real sy: 0
            onActiveChanged: {
                if (active) { sx = clock.fx; sy = clock.fy; return }
                if (clock.overlay)
                    clock.overlay.save({ clock_x: clock.fx.toFixed(4), clock_y: clock.fy.toFixed(4) })
            }
            onTranslationChanged: if (active) clock.pos = {
                x: Math.min(1, Math.max(0, sx + translation.x / clock.width)),
                y: Math.min(1, Math.max(0, sy + translation.y / clock.height))
            }
        }
    }
}
