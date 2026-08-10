import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

// Device info (Device > Device Info): serial, firmware, hardware.
// A real Window (native title bar, movable/resizable). Content is static and
// fetched on open (backend.deviceInfoText()).
Window {
    id: infoDialog
    title: "Device Info"
    color: palette.window
    width: layout.implicitWidth + layout.anchors.margins * 2
    height: layout.implicitHeight + layout.anchors.margins * 2
    modality: Qt.ApplicationModal

    ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: 6
        Label {
            Layout.fillWidth: true
            Layout.preferredWidth: 380
            Layout.preferredHeight: 80
            text: backend.deviceInfoText()
            wrapMode: Text.Wrap
        }
        RowLayout {
            Item { Layout.fillWidth: true }
            Button {
                text: "Close"
                onClicked: infoDialog.close()
            }
        }
    }
}
