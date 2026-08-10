import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One settings row with a boolean CheckBox, self-managed against the backend
// config API (same gate/read/write lifecycle as SpinRow).
RowLayout {
    id: control

    property string attr: ""
    property string base: ""
    property string label: ""
    property string tip: ""
    property bool available: false

    enabled: control.available

    CheckBox {
        id: chk
        text: control.label
        // toggled fires only on user interaction; the programmatic set in
        // sync() does not echo back into setConfig.
        onToggled: backend.setConfig(control.base, control.attr, chk.checked)
        ToolTip.visible: hovered && control.tip.length > 0
        ToolTip.text: control.tip || control.base + ".config." + control.attr
    }
    Item { Layout.fillWidth: true }

    function sync() {
        control.available = backend.hasConfig(control.base, control.attr)
        if (control.available)
            chk.checked = backend.getConfig(control.base, control.attr) !== 0
    }

    Connections {
        target: backend
        function onConnChanged() {
            if (backend.connected)
                control.sync()
        }
    }
}
