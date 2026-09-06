import QtQuick
import QtQuick.Layouts
import QtQuick.Controls

Rectangle {
    height: 30
    color: palette.alternateBase
    border.color: palette.mid

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 8
        anchors.rightMargin: 8
        spacing: 14

        Label {
            text: statusBackend.statusText
            font.bold: true
            color: statusBackend.statusColor
            MouseArea {
                anchors.fill: parent
                enabled: statusBackend.hasError
                cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                onClicked: errorDialog.show()
            }
        }

        Item { Layout.fillWidth: true }

        Label {
            text: statusBackend.vbusText
        }

        Label {
            text: statusBackend.powerText
        }
    }
}
