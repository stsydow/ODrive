import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One Control Command row: [label] [spinbox] [estimate?] [stretch].
// Editing never writes to the device; the user confirms via the Apply
// button / Enter, and the active setpoint reaches the device only on an
// explicit apply (monitor-only principle, see ARCHITECTURE.md).
RowLayout {
    id: control

    property string label: ""
    property string estimate: ""
    property real pointMin: -100
    property real pointMax: 100
    property int decimals: 3
    property real step: 0.1
    // Backend property this row mirrors (device truth, re-synced on demand).
    property real backendValue: 0
    // Emitted when the user edits the value (arrows / typing + Enter).
    signal committed(real v)

    Label { text: control.label }
    DoubleSpinBox {
        id: spin
        editable: true
        from: control.pointMin
        to: control.pointMax
        stepSize: control.step
        decimals: control.decimals
        // No value binding: user edits stay local; device truth is pushed in
        // via onSetpointChanged below (re-syncs even after an edit).
        onValueModified: control.committed(spin.value)
    }
    Label {
        text: control.estimate
        color: "gray"
        visible: control.estimate.length > 0
    }
    Item { Layout.fillWidth: true }

    // Re-sync the box from the device whenever the backend setpoint updates.
    // valueModified only fires on interactive edits, so the programmatic set
    // here does not loop back into committed().
    Connections {
        target: backend
        function onSetpointChanged() {
            spin.value = control.backendValue
        }
    }

    // Enter confirms the edit and applies it to the device (Apply button
    // equivalent). Hook the inner TextInput's accepted() — it consumes Return,
    // so Keys.* on the spinbox never see it. Parse the displayed text here:
    // relying on SpinBox's internal commit order made the first Enter apply
    // the previously stored value.
    Connections {
        target: spin.contentItem
        function onAccepted() {
            // locale arg required: without it valueFromText's default impl
            // calls Number.fromLocaleString(undefined, ...) -> hard error.
            backend.setActiveSetpoint(spin.valueFromText(spin.contentItem.text, Qt.locale()))
            backend.applySetpoint()
        }
    }
}
