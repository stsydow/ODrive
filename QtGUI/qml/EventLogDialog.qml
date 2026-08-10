import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

// Chronological UI/device event log (Debug > Event Log…).
// A real Window (native title bar, movable/resizable). Non-modal + live:
// text binds to backend.logText (refreshed on logUpdated); works while
// disconnected so the run-up to a disconnect stays visible.
Window {
    id: logDialog
    title: "Event Log"
    color: palette.window
    width: layout.implicitWidth + layout.anchors.margins * 2
    height: layout.implicitHeight + layout.anchors.margins * 2
    modality: Qt.NonModal

    ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: 6
        TextArea {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 640
            Layout.preferredHeight: 400
            readOnly: true
            text: backend.logText
            font.family: "monospace"
        }
        RowLayout {
            Item { Layout.fillWidth: true }
            Button {
                text: "Export Log…"
                onClicked: backend.exportLog()
            }
            Button {
                text: "Close"
                onClicked: logDialog.close()
            }
        }
    }
}
