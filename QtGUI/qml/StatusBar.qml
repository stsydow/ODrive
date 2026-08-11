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
            text: statusBackend.connText
            font.bold: true
            color: statusBackend.connColor
        }

        Label {
            text: statusBackend.stateText
        }

        Label {
            text: statusBackend.errorText
            color: statusBackend.errorColor
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
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
