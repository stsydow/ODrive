import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One Control Command setpoint section:
// - Row 1: [label] [spinbox] [estimate?] [Apply]
// - Row 2: [CheckBox: "Analog In"] [Min spinbox] [Max spinbox]
// Editing never writes to the device; the user confirms via Apply / Enter.
// When Analog In is enabled, this row becomes the mapping target and
// mirrors the live input value.
ColumnLayout {
    id: control

    property string label: ""
    property string estimate: ""
    property string targetName: ""
    // True while an analog or external mapping drives this setpoint: the box is
    // disabled and mirrors the live mapped value instead of the stored one.
    property bool externallyDriven: backend.analogTarget === control.targetName
    property real mappedValue: backend.analogValue
    property real pointMin: -100
    property real pointMax: 100
    property int decimals: 3
    property real step: 0.1
    // Backend property this row mirrors (device truth, re-synced on demand).
    property real backendValue: 0
    // Emitted when the user edits the value (arrows / typing + Enter).
    signal committed(real v)

    RowLayout {
        Layout.fillWidth: true
        Label { text: control.label }
        DoubleSpinBox {
            id: spin
            editable: true
            enabled: !control.externallyDriven
            from: control.pointMin
            to: control.pointMax
            stepSize: control.step
            decimals: control.decimals
            // No value binding: user edits stay local; device truth is pushed in
            // via onSetpointChanged below (re-syncs even after an edit).
            onValueModified: if (!control.externallyDriven) control.committed(spin.value)
        }
        Label {
            text: control.estimate
            color: "gray"
            visible: control.estimate.length > 0
        }
        Button {
            // Must parse the displayed text like the Enter path below: typed text
            // is not committed to spin.value until focus loss, so a bare
            // applySetpoint() would send the previously stored setpoint.
            text: "Apply"
            enabled: !control.externallyDriven
            onClicked: control.applyDisplayed()
        }
        Item { Layout.fillWidth: true }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: 6
        CheckBox {
            id: analogEnable
            text: "Analog In"
            checked: control.externallyDriven
            enabled: statusBackend.connected && backend.axisIdle
            onToggled: {
                if (checked) {
                    backend.setAnalogTarget(control.targetName)
                } else if (control.externallyDriven) {
                    backend.setAnalogTarget("Disabled")
                }
            }
        }
        Label {
            text: "Min:"
            color: control.externallyDriven ? "black" : "gray"
        }
        DoubleSpinBox {
            id: rowAnalogMin
            editable: true
            from: control.pointMin; to: control.pointMax
            decimals: control.decimals; stepSize: control.step
            value: backend.analogMin
            enabled: statusBackend.connected && backend.axisIdle && control.externallyDriven
            onValueModified: backend.setAnalogMin(value)
        }
        Label {
            text: "Max:"
            color: control.externallyDriven ? "black" : "gray"
        }
        DoubleSpinBox {
            id: rowAnalogMax
            editable: true
            from: control.pointMin; to: control.pointMax
            decimals: control.decimals; stepSize: control.step
            value: backend.analogMax
            enabled: statusBackend.connected && backend.axisIdle && control.externallyDriven
            onValueModified: backend.setAnalogMax(value)
        }
        Item { Layout.fillWidth: true }
    }

    // Re-sync the box from the device whenever the backend setpoint updates.
    // valueModified only fires on interactive edits, so the programmatic set
    // here does not loop back into committed().
    onExternallyDrivenChanged: {
        if (!externallyDriven)
            spin.value = backendValue
    }

    Connections {
        target: backend
        function onSetpointChanged() {
            if (!control.externallyDriven)
                spin.value = control.backendValue
        }
        // Analog mapping drives this setpoint: mirror the live input value.
        function onAnalogChanged() {
            if (control.externallyDriven) {
                spin.value = control.mappedValue
                rowAnalogMin.value = backend.analogMin
                rowAnalogMax.value = backend.analogMax
            }
        }
    }

    // Enter and Apply share one path: parse the displayed text (see Apply
    // comment), store it, then write it to the device.
    function applyDisplayed() {
        backend.setActiveSetpoint(
                    spin.valueFromText(spin.contentItem.text, Qt.locale()))
        backend.applySetpoint()
    }
    // Enter confirms the edit and applies it to the device. Hook the inner
    // TextInput's accepted() — it consumes Return, so Keys.* on the spinbox
    // never see it.
    Connections {
        target: spin.contentItem
        function onAccepted() {
            control.applyDisplayed()
        }
    }
}
