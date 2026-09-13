// wallflow widget: clock. Contract for widgets — a root Item with
//   property var cfg          (the [overlay] section of config.toml, live)
//   property string screenName (the output this instance is on)
// filling the screen; the overlay loads it below the subject cutout.
import QtQuick
import Quickshell

Item {
    id: clock
    property var cfg: ({})
    property string screenName: ""
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

    Column {
        x: clock.width  * (cfg.clock_x ?? 0.5) - width  / 2
        y: clock.height * (cfg.clock_y ?? 0.12) - height / 2
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
}
