import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    width: 760
    height: 560
    title: "ODrive QML GUI - Axis 0"
    visible: true
    color: palette.window

    menuBar: MenuBar {
        Menu {
            title: "&Device"
            MenuItem { text: "&Save Config"; onTriggered: backend.saveConfig() }
            MenuItem { text: "&Export Config…"; onTriggered: backend.exportConfig() }
            MenuItem { text: "&Import Config…"; onTriggered: backend.importConfig() }
            MenuSeparator {}
            MenuItem { text: "Re&boot"; onTriggered: backend.reboot() }
            MenuSeparator {}
            MenuItem { text: "Errors"; onTriggered: errorDialog.show() }
            MenuSeparator {}
            MenuItem { text: "Device Info"; onTriggered: deviceInfoDialog.show() }
        }
        Menu {
            title: "&Debug"
            MenuItem {
                text: "Verbose Logging"
                checkable: true
                checked: backend.verbose
                onTriggered: backend.setVerbose(checked)
            }
            MenuItem { text: "Event Log…"; onTriggered: eventLogDialog.show() }
            MenuSeparator {}
            MenuItem { text: "Force Reconnect"; onTriggered: backend.forceReconnect() }
        }
    }

    // Status footer pinned to the bottom of the window (like QStatusBar).
    footer: Rectangle {
        height: 30
        color: palette.alternateBase
        border.color: palette.mid
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 8
            anchors.rightMargin: 8
            spacing: 14
            Label { text: backend.connText; font.bold: true; color: backend.connColor }
            Label { text: backend.stateText }
            Label {
                text: backend.errorText
                color: backend.errorColor
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: errorDialog.show()
                }
            }
            Item { Layout.fillWidth: true }
            Label { text: backend.vbusText }
            Label { text: backend.powerText }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6

        // ── Control bar: Run / Stop / Program ─────────────────────────
        RowLayout {
            Button {
                text: "▶ Run (Closed Loop)"
                enabled: backend.connected
                onClicked: backend.run()
            }
            Button {
                text: "■ Stop (Idle)"
                enabled: backend.connected
                onClicked: backend.stop()
            }
            Item { Layout.fillWidth: true }
            Label { text: "Programm:" }
            ComboBox {
                id: stateCombo
                model: backend.stateNames
                enabled: backend.connected
            }
            Button {
                text: "Start"
                enabled: backend.connected
                onClicked: backend.startState(stateCombo.currentText)
            }
        }

        // ── Control Command ───────────────────────────────────────────
        GroupBox {
            Layout.fillWidth: true
            title: "Control Command"
            // Disable the whole subtree (mode/input combos, setpoint rows,
            // Apply) when there is no device; setpoint rows add a finer
            // closed-loop gate via backend.closedLoop below.
            enabled: backend.connected
            ColumnLayout {
                anchors.fill: parent
                spacing: 4

                RowLayout {
                    Label { text: "Control Mode:" }
                    ComboBox {
                        id: modeCombo
                        objectName: "modeCombo"
                        model: backend.modeNames
                        currentIndex: backend.currentMode
                        onActivated: backend.setMode(currentText)
                    }
                    Label { text: "Input Mode:" }
                    ComboBox {
                        id: inputCombo
                        objectName: "inputCombo"
                        model: backend.inputModes
                        currentIndex: backend.currentInputMode
                        onActivated: backend.setInputMode(currentIndex)
                    }
                    Item { Layout.fillWidth: true }
                }

                // Velocity / Torque / Position rows — only active visible
                SetpointRow {
                    label: "Velocity Setpoint (rps):"
                    backendValue: backend.velSetpoint
                    estimate: backend.velEstimateText
                    pointMin: -100; pointMax: 100; decimals: 3; step: 0.1
                    enabled: backend.closedLoop
                    visible: backend.currentMode === 0
                    onCommitted: function(v) { backend.setActiveSetpoint(v) }
                }
                SetpointRow {
                    label: "Torque Setpoint (A):"
                    backendValue: backend.torqueSetpoint
                    pointMin: -10; pointMax: 10; decimals: 3; step: 0.1
                    enabled: backend.closedLoop
                    visible: backend.currentMode === 2
                    onCommitted: function(v) { backend.setActiveSetpoint(v) }
                }
                SetpointRow {
                    label: "Position Setpoint (rev):"
                    backendValue: backend.posSetpoint
                    estimate: backend.posEstimateText
                    pointMin: -1e6; pointMax: 1e6; decimals: 4; step: 0.01
                    enabled: backend.closedLoop
                    visible: backend.currentMode === 1
                    onCommitted: function(v) { backend.setActiveSetpoint(v) }
                }

                RowLayout {
                    Button {
                        text: "Apply Setpoint"
                        enabled: backend.closedLoop
                        onClicked: backend.applySetpoint()
                    }
                    Item { Layout.fillWidth: true }
                }
            }
        }

        // ── Control Settings (2.5.3) ──────────────────────────────────
        GroupBox {
            Layout.fillWidth: true
            Layout.fillHeight: true
            title: "Settings"
            enabled: backend.connected

            ColumnLayout {
                anchors.fill: parent
                TabBar {
                    id: settingsTab
                    TabButton { text: "Electrical Limits" }
                    TabButton { text: "Mechanical Limits" }
                    TabButton { text: "Control Parameters" }
                }
                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: settingsTab.currentIndex

                    // Electrical Limits
                    GridLayout {
                        columns: 2
                        SpinRow { attr: "current_lim"; base: "motor"; label: "Current limit"; unit: "A"; min: 0; max: 60; decimals: 2; step: 0.1; Layout.fillWidth: true }
                        SpinRow { attr: "current_lim_margin"; base: "motor"; label: "Current limit margin"; unit: "A"; min: 0; max: 60; decimals: 2; step: 0.1; Layout.fillWidth: true }
                        SpinRow { attr: "requested_current_range"; base: "motor"; label: "Requested current range"; unit: "A"; min: 0; max: 60; decimals: 1; step: 0.5; Layout.fillWidth: true
                            tip: "Max 60 A on this controller. Should be > current_lim + current_lim_margin, but as low as possible for best resolution." }
                        SpinRow { attr: "dc_bus_overvoltage_trip_level"; base: "odrive"; label: "DC overvoltage trip"; unit: "V"; min: 0; max: 60; decimals: 1; step: 0.5; Layout.fillWidth: true }
                        SpinRow { attr: "dc_max_positive_current"; base: "odrive"; label: "DC +ve current limit (PSU)"; unit: "A"; min: 0; max: 60; decimals: 1; step: 0.5; Layout.fillWidth: true }
                        SpinRow { attr: "dc_max_negative_current"; base: "odrive"; label: "DC -ve current limit (regen)"; unit: "A"; min: -60; max: 0; decimals: 2; step: 0.1; Layout.fillWidth: true }
                    }

                    // Mechanical Limits
                    GridLayout {
                        columns: 2
                        SpinRow { attr: "vel_limit"; base: "controller"; label: "Velocity limit"; unit: "turn/s"; min: 0; max: 200; decimals: 1; step: 0.5; Layout.fillWidth: true }
                        SpinRow { attr: "torque_lim"; base: "motor"; label: "Torque limit"; unit: "N·m"; min: 0; max: 50; decimals: 3; step: 0.1; Layout.fillWidth: true }
                        CheckRow { attr: "enable_vel_limit"; base: "controller"; label: "Enable velocity limit"; Layout.fillWidth: true }
                        CheckRow { attr: "enable_torque_mode_vel_limit"; base: "controller"; label: "Torque-mode velocity limit"; Layout.fillWidth: true }
                        CheckRow { attr: "enable_overspeed_error"; base: "controller"; label: "Overspeed error"; Layout.fillWidth: true }
                    }

                    // Control Parameters
                    GridLayout {
                        columns: 2
                        SpinRow { attr: "vel_gain"; base: "controller"; label: "Velocity gain"; unit: "N·m/(turn/s)"; min: 0; max: 10; decimals: 4; step: 0.001; Layout.fillWidth: true }
                        SpinRow { attr: "vel_integrator_gain"; base: "controller"; label: "Vel. integrator gain"; unit: "N·m/turn"; min: 0; max: 10; decimals: 4; step: 0.001; Layout.fillWidth: true }
                        SpinRow { attr: "vel_integrator_limit"; base: "controller"; label: "Vel. integrator limit"; unit: "N·m"; min: 0; max: 50; decimals: 3; step: 0.1; Layout.fillWidth: true }
                        SpinRow { attr: "pos_gain"; base: "controller"; label: "Position gain"; unit: "(turn/s)/turn"; min: 0; max: 100; decimals: 3; step: 0.1; Layout.fillWidth: true }
                        SpinRow { attr: "inertia"; base: "controller"; label: "Inertia (feed-forward)"; unit: "N·m/(turn/s²)"; min: -50; max: 50; decimals: 4; step: 0.001; Layout.fillWidth: true }
                        CheckRow { attr: "enable_gain_scheduling"; base: "controller"; label: "Gain scheduling"; Layout.fillWidth: true }
                    }
                }
            }
        }
    }

    // ── Dialogs (2.5.4) ────────────────────────────────────────
    ErrorDialog { id: errorDialog; objectName: "errorDialog" }
    EventLogDialog { id: eventLogDialog; objectName: "eventLogDialog" }
    DeviceInfoDialog { id: deviceInfoDialog; objectName: "deviceInfoDialog" }
}
