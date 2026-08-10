import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One settings row with a numeric DoubleSpinBox, self-managed against the
// backend config API:
//   - feature gate:  available = backend.hasConfig(base, attr)
//   - read-on-bind:  on connect, value = backend.getConfig(base, attr)
//   - write-on-change: user edit  -> backend.setConfig(base, attr, value)
// A device refresh (reconnect) re-reads; the parent (Settings GroupBox)
// cascades the disconnected/disabled state, and `available` gates per-row
// firmware availability.
RowLayout {
    id: control

    property string attr: ""
    property string base: ""
    property string label: ""
    property string unit: ""
    property string tip: ""
    property real min: 0.0
    property real max: 60.0
    property int decimals: 2
    property real step: 0.1
    property bool available: false

    enabled: control.available

    Label {
        text: control.unit ? "%1 (%2):".arg(control.label).arg(control.unit)
                           : control.label + ":"
        Layout.fillWidth: true
    }
    DoubleSpinBox {
        id: spin
        from: control.min
        to: control.max
        decimals: control.decimals
        stepSize: control.step
        // Write-on-change: valueModified fires only on interactive edits, so
        // the programmatic set in sync() does not echo back into setConfig.
        onValueModified: backend.setConfig(control.base, control.attr, spin.value)
        ToolTip.visible: hovered && control.tip.length > 0
        ToolTip.text: control.tip || control.base + ".config." + control.attr
    }

    function sync() {
        control.available = backend.hasConfig(control.base, control.attr)
        if (control.available)
            spin.value = backend.getConfig(control.base, control.attr)
    }

    Connections {
        target: backend
        function onConnChanged() {
            if (backend.connected)
                control.sync()
        }
    }
}
