import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Config Browser (Plan §2.4): read/write tree over the live ODrive object
// graph. Values load on subtree expansion; a manual Refresh re-reads.
// Right-click an editable leaf (.config bool/int/float) to edit; commits are
// IDLE-gated with commit-time re-check in backend.writeBrowserValue().
Window {
    id: browserDialog
    title: "Config Browser"
    color: palette.window
    width: 720
    height: 540

    function openBrowser() {
        backend.browserModel.reset()  // re-root on the current device state
        browserDialog.showNormal()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 6
        spacing: 6

        RowLayout {
            Layout.fillWidth: true
            Label { text: "Filter:" }
            TextField {
                id: filterBox
                Layout.fillWidth: true
                placeholderText: "path substring (Enter applies)"
                onEditingFinished: backend.browserModel.set_filter(text)
            }
            Button { text: "Refresh"; onClicked: backend.browserModel.reset() }
            Button { text: "Close"; onClicked: browserDialog.close() }
        }
        Label {
            Layout.fillWidth: true
            text: "read-only except .config scalars — use the edit button on writable rows"
            font.pointSize: 8
            opacity: 0.6
        }

        TreeView {
            id: tree
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: backend.browserModel
            boundsBehavior: Flickable.StopAtBounds
            // per-level indent; our delegate lays rows out itself
            property int indentStep: 24

            delegate: Item {
                id: treeNode
                implicitWidth: 40
                implicitHeight: Math.max(rowLabel.implicitHeight + 6, 24)

                required property int depth
                required property int row
                required property bool expanded
                required property bool hasChildren
                required property string name
                required property string value
                required property string path
                required property bool editable

                // Expand/collapse affordance: a real button (+/−), not a glyph.
                Rectangle {
                    id: expander
                    x: 4 + treeNode.depth * tree.indentStep
                    anchors.verticalCenter: parent.verticalCenter
                    width: 16; height: 16; radius: 3
                    visible: treeNode.hasChildren
                    color: expanderMouse.containsMouse ? palette.mid : palette.button
                    border.color: palette.placeholderText
                    border.width: 1

                    Label {
                        anchors.centerIn: parent
                        text: treeNode.expanded ? "−" : "+"
                        font.pixelSize: 11
                        font.bold: true
                        color: palette.buttonText
                    }
                    MouseArea {
                        id: expanderMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: tree.toggleExpanded(treeNode.row)
                    }
                }
                Label {
                    id: rowLabel
                    x: 28 + treeNode.depth * tree.indentStep
                    anchors.verticalCenter: parent.verticalCenter
                    font.family: "monospace"
                    text: treeNode.value !== ""
                          ? treeNode.name + ": " + treeNode.value
                          : treeNode.name
                    color: treeNode.editable ? palette.text : palette.placeholderText
                }
                // Explicit affordance beats hit-testing subtleties: one click
                // opens the editor for this leaf.
                Button {
                    id: editBtn
                    x: rowLabel.x + rowLabel.implicitWidth + 8
                    anchors.verticalCenter: parent.verticalCenter
                    visible: treeNode.editable
                    hoverEnabled: true
                    width: 20; height: 20
                    ToolTip.visible: hovered
                    ToolTip.text: "Edit value"
                    background: Rectangle {
                        radius: 3
                        color: editBtn.hovered ? palette.mid : palette.button
                        border.color: palette.placeholderText
                        border.width: 1
                    }
                    // I-beam text-cursor glyph, drawn (font-independent).
                    contentItem: Item {
                        Rectangle { x: parent.width / 2 - 0.5; y: 4; width: 1; height: parent.height - 8; color: palette.buttonText }
                        Rectangle { x: parent.width / 2 - 3.5; y: 3; width: 7; height: 1; color: palette.buttonText }
                        Rectangle { x: parent.width / 2 - 3.5; y: parent.height - 4; width: 7; height: 1; color: palette.buttonText }
                    }
                    onClicked: {
                        editor.editorPath = treeNode.path
                        valueInput.text = treeNode.value
                        editor.open()
                    }
                }
            }
        }
    }

    // Small OK/cancel editor for one leaf value (§2.4 edit UX).
    Dialog {
        id: editor
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        title: "Edit " + editorPath
        property string editorPath: ""

        ColumnLayout {
            TextField {
                id: valueInput
                Layout.preferredWidth: 240
                font.family: "monospace"
                placeholderText: "bool: true/false · int (hex ok) · float"
                onAccepted: editor.accept()
            }
            Label {
                font.pointSize: 8
                opacity: 0.6
                text: "Committed only when the axis is IDLE at OK time."
            }
        }
        onAccepted: backend.writeBrowserValue(editor.editorPath, valueInput.text)
    }
}
