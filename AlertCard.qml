import QtQuick
import qs.Commons

Item {
  id: root

  property string text: ""
  property int extraCount: 0
  property color foreground: Color.foreground
  property color accent: Color.urgent
  property string fontFamily: Style.font.menuFamily

  visible: String(text || "").trim() !== ""
  implicitHeight: visible ? card.implicitHeight : 0
  implicitWidth: parent ? parent.width : 0

  Rectangle {
    id: card
    width: parent.width
    implicitHeight: body.implicitHeight + Style.space(20)
    color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.08)
    border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.28)
    border.width: 1
    radius: Style.cornerRadius

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: Style.space(3)
      color: root.accent
    }

    Column {
      id: body
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(14)
      anchors.rightMargin: Style.spacing.md
      spacing: Style.space(4)

      Text {
        text: extraCount > 0 ? "ADVISORY · " + (extraCount + 1) : "ADVISORY"
        textFormat: Text.PlainText
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
      }

      Text {
        width: parent.width
        text: root.text
        textFormat: Text.PlainText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        maximumLineCount: 4
        elide: Text.ElideRight
      }
    }
  }
}
