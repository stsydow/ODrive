import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

// Current decoded errors (Device > Errors… or the footer Err indicator).
// A real Window (native title bar, movable/resizable). Live: text binds to
// backend.errorsText, re-rendered on each error update.
Window {
    id: errorDialog
    title: "Errors"
    color: palette.window
    // Size to the content layout's preferred size (resizable afterwards).
    width: layout.implicitWidth + layout.anchors.margins * 2
    height: layout.implicitHeight + layout.anchors.margins * 2
    modality: Qt.ApplicationModal

    ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: 6
        TextArea {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 520
            Layout.preferredHeight: 300
            readOnly: true
            text: backend.errorsText
            font.family: "monospace"
        }
        RowLayout {
            Item { Layout.fillWidth: true }
            Button {
                text: "Clear Errors"
                enabled: statusBackend.connected
                onClicked: backend.clearErrors()
            }
            Button {
                text: "Close"
                onClicked: errorDialog.close()
            }
        }
    }
}
