import QtQuick
import qs.Commons

Item {
  id: root
  property var row: ({ dest: "", hexcolor: "#888888", estimates: [] })
  property color foreground: Color.foreground
  property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.56)
  property string fontFamily: Style.font.family

  readonly property var estimates: row && row.estimates ? row.estimates : []
  readonly property color lineColor: row && row.hexcolor ? row.hexcolor : "#888888"

  implicitHeight: col.implicitHeight
  implicitWidth: parent ? parent.width : col.implicitWidth

  Column {
    id: col
    width: parent.width
    spacing: Style.space(6)

    Row {
      width: parent.width
      spacing: Style.space(8)

      Rectangle {
        width: Style.space(8)
        height: Style.space(8)
        radius: 1
        color: root.lineColor
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        width: parent.width - Style.space(16)
        text: root.row ? String(root.row.dest || "") : ""
        textFormat: Text.PlainText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
      }
    }

    Flow {
      width: parent.width
      spacing: Style.space(6)

      Repeater {
        model: root.estimates

        Rectangle {
          required property var modelData
          implicitWidth: pillText.implicitWidth + Style.space(16)
          implicitHeight: Style.space(26)
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.06)
          border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
          border.width: 1
          radius: Style.cornerRadius

          Text {
            id: pillText
            anchors.centerIn: parent
            text: modelData.minuteLabel || modelData.depart || ""
            textFormat: Text.PlainText
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
        }
      }
    }
  }
}
