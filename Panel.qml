import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "Network.js" as Network

Panel {
  id: root
  moduleName: "io.github.adg-ub.bay-transit-board"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  readonly property var snapshot: hostWidget ? hostWidget.snapshot : Model.emptySnapshot()
  readonly property string origin: hostWidget ? String(hostWidget.origin) : "12TH"
  readonly property string dest: hostWidget ? String(hostWidget.dest) : "EMBR"
  readonly property bool useLocation: hostWidget ? hostWidget.useLocation === true : false
  readonly property bool loading: hostWidget ? hostWidget.loading === true : false
  readonly property var stationOptions: Model.stationOptions()
  readonly property var lines: snapshot && snapshot.lines ? snapshot.lines : []
  readonly property var trips: snapshot && snapshot.trips ? snapshot.trips : []
  readonly property var departures: snapshot && snapshot.departures ? snapshot.departures : []
  readonly property var advisories: snapshot && snapshot.advisories ? snapshot.advisories : []

  readonly property color background: Color.popups.background
  readonly property color foreground: root.barForeground
  readonly property color accent: Color.accent
  readonly property bool lightTheme:
    0.2126 * background.r + 0.7152 * background.g + 0.0722 * background.b > 0.5
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.56)
  readonly property color faint: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.12)
  readonly property color mapBackground: lightTheme ? "#e7e6e1" : "#090a0c"
  readonly property color mapWater: lightTheme ? "#b4c6d6" : "#07141c"
  readonly property color mapLand: lightTheme ? "#e6e2d8" : "#455560"
  readonly property color mapGrid: lightTheme ? "#3f454a" : "#7d8791"
  readonly property color mapStation: lightTheme ? "#202428" : "#d9dee3"

  readonly property bool originPicked: !!(origin && origin !== "-")
  readonly property bool destPicked: !!(dest && dest !== "-")
  readonly property bool pairReady: originPicked && destPicked

  readonly property int headerHeight: Style.space(62)
  readonly property int sidebarWidth: Style.space(380)

  readonly property var routed: Network.routeBetween(origin, dest)
  readonly property var displayLines: {
    if (root.lines.length > 0) return root.lines
    var segs = root.routed && root.routed.segments ? root.routed.segments : []
    var out = []
    for (var i = 0; i < segs.length; i++) {
      out.push({
        name: segs[i].name,
        hexcolor: segs[i].hexcolor,
        head: root.destPicked ? Model.stationName(root.dest) : "",
        transfer: root.routed.transfer ? "via " + Model.stationName(root.routed.transfer) : ""
      })
    }
    return out
  }
  readonly property var lineChips: {
    var rows = root.displayLines
    var out = []
    for (var i = 0; i < rows.length; i++) {
      var name = rows[i] && rows[i].name ? String(rows[i].name) : ""
      if (!name) continue
      var seen = false
      for (var j = 0; j < out.length; j++) {
        if (String(out[j].name).toLowerCase() === name.toLowerCase()) {
          seen = true
          break
        }
      }
      if (!seen) out.push(rows[i])
    }
    return out
  }
  readonly property var activeLineNames: {
    var names = []
    var rows = root.lineChips
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i].name) names.push(String(rows[i].name))
    }
    return names
  }
  readonly property string advisoryText: {
    var rows = root.advisories
    if (!rows || rows.length === 0) return ""
    return Model.advisoryLead(rows[0])
  }
  readonly property var soonest: snapshot && snapshot.soonest ? snapshot.soonest : null
  readonly property var transfer: snapshot && snapshot.transfer ? snapshot.transfer : null
  readonly property var locationState: snapshot && snapshot.location
    ? snapshot.location
    : ({ ok: false, message: "Location unavailable" })
  readonly property bool locationReady: useLocation && locationState.ok === true
  readonly property string locationStation: locationReady
    ? (snapshot.boardName || Model.stationName(locationState.nearest))
    : ""
  readonly property string locationDistance: {
    if (!root.locationReady || root.locationState.meters === undefined) return ""
    var meters = Number(root.locationState.meters)
    if (isNaN(meters)) return ""
    if (meters < 1000) return Math.max(1, Math.round(meters)) + " m away"
    return (meters / 1000).toFixed(1) + " km away"
  }

  function open() {
    if (hostWidget && hostWidget.refresh) hostWidget.refresh()
    root.controller.show()
  }

  function close() {
    root.controller.hide()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  function selectStation(abbr) {
    var code = String(abbr || "").toUpperCase()
    if (!root.hostWidget || !code) return
    if (root.originPicked && code === root.origin) {
      root.hostWidget.setOrigin("-")
      return
    }
    if (root.destPicked && code === root.dest) {
      root.hostWidget.setDest("-")
      return
    }
    if (!root.originPicked) {
      root.hostWidget.setOrigin(code)
      return
    }
    if (root.destPicked) {
      root.hostWidget.setPair(code, "-")
      return
    }
    root.hostWidget.setDest(code)
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(1040))
    contentHeight: panel.cappedContentHeight(Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: startPicker.popupOpen || destPicker.popupOpen
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (!root.hostWidget) return
        if (t === "r" || t === "R") root.hostWidget.refresh()
        else if (t === "x" || t === "X") root.hostWidget.swapStations()
        else if (t === "l" || t === "L") root.hostWidget.setUseLocation(!root.useLocation)
      }

      Item {
        id: header
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: root.headerHeight
        z: 2

        Text {
          anchors.left: parent.left
          anchors.leftMargin: Style.spacing.panelPadding
          anchors.verticalCenter: parent.verticalCenter
          text: "BAY"
          textFormat: Text.PlainText
          color: root.foreground
          font.family: Style.font.menuFamily
          font.pixelSize: Style.font.heading
          font.bold: true
        }

        Column {
          anchors.left: parent.left
          anchors.leftMargin: Style.space(110)
          anchors.right: locationSwitch.left
          anchors.rightMargin: Style.spacing.md
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(2)

          Text {
            width: parent.width
            text: (root.originPicked ? Model.stationName(root.origin) : "Start")
              + " → "
              + (root.destPicked ? Model.stationName(root.dest) : "End")
            textFormat: Text.PlainText
            color: root.foreground
            font.family: Style.font.menuFamily
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            text: root.loading
              ? "Updating"
              : (Model.directionLabel(root.snapshot)
                + (root.snapshot.updated ? " · " + Model.updatedLabel(root.snapshot.updated) : ""))
            textFormat: Text.PlainText
            color: root.dim
            font.family: Style.font.menuFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        ToggleSwitch {
          id: locationSwitch
          anchors.right: parent.right
          anchors.rightMargin: Style.spacing.panelPadding
          anchors.verticalCenter: parent.verticalCenter
          checked: root.useLocation
          foreground: root.foreground
          accent: root.accent
          onToggled: if (root.hostWidget) root.hostWidget.setUseLocation(!root.useLocation)
        }

        Text {
          anchors.right: locationSwitch.left
          anchors.rightMargin: Style.spacing.sm
          anchors.verticalCenter: parent.verticalCenter
          text: "Location"
          textFormat: Text.PlainText
          color: root.dim
          font.family: Style.font.menuFamily
          font.pixelSize: Style.font.caption
        }

        Rectangle {
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          height: 1
          color: root.faint
        }
      }

      Item {
        id: body
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: header.bottom
        anchors.bottom: parent.bottom

        Item {
          id: mapPane
          anchors.left: parent.left
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          anchors.right: sidebar.left

          NetworkMap {
            id: networkMap
            anchors.fill: parent
            anchors.margins: Style.spacing.lg
            origin: root.origin
            dest: root.dest
            activeLineNames: root.activeLineNames
            fontFamily: Style.font.menuFamily
            backgroundColor: root.mapBackground
            waterColor: root.mapWater
            landColor: root.mapLand
            gridColor: root.mapGrid
            outlineColor: root.lightTheme ? "#3c4247" : "#9099a3"
            stationColor: root.mapStation
            accentColor: root.accent
            textColor: root.foreground
            dimColor: root.dim
            lightTheme: root.lightTheme
            hint: !root.originPicked
              ? "Click a station to set start · drag to pan · wheel to zoom"
              : (!root.destPicked
                ? "Click a station to set end · click start again to clear"
                : "Click a station to start a new trip · click start or end to clear")
            onInteractionStarted: keyCatcher.forceActiveFocus()
            onStationActivated: function(abbr) { root.selectStation(abbr) }
          }
        }

        Rectangle {
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          anchors.right: sidebar.left
          width: 1
          color: root.faint
        }

        Item {
          id: sidebar
          width: root.sidebarWidth
          anchors.right: parent.right
          anchors.top: parent.top
          anchors.bottom: parent.bottom

          Flickable {
            id: sidebarScroll
            anchors.fill: parent
            anchors.margins: Style.spacing.panelPadding
            contentWidth: width
            contentHeight: sidebarContent.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            interactive: contentHeight > height

            Column {
              id: sidebarContent
              width: sidebarScroll.width
              spacing: Style.space(18)

              AlertCard {
                width: parent.width
                text: root.advisoryText
                extraCount: Math.max(0, root.advisories.length - 1)
                foreground: root.foreground
                accent: Color.urgent
                fontFamily: Style.font.menuFamily
              }

              Column {
                width: parent.width
                spacing: Style.space(8)

                SearchableDropdown {
                  id: startPicker
                  width: parent.width
                  label: "From"
                  value: root.originPicked ? root.origin : ""
                  options: root.stationOptions
                  placeholderText: "Search start station"
                  emptyText: "No stations"
                  triggerLabel: root.originPicked ? "" : "Click the map"
                  foreground: root.foreground
                  fontFamily: Style.font.menuFamily
                  onChanged: function(value) {
                    if (root.hostWidget) root.hostWidget.setOrigin(value)
                  }
                }

                SearchableDropdown {
                  id: destPicker
                  width: parent.width
                  label: "To"
                  value: root.destPicked ? root.dest : ""
                  options: root.stationOptions
                  placeholderText: "Search end station"
                  emptyText: "No stations"
                  triggerLabel: root.destPicked ? "" : "Click the map"
                  foreground: root.foreground
                  fontFamily: Style.font.menuFamily
                  onChanged: function(value) {
                    if (root.hostWidget) root.hostWidget.setDest(value)
                  }
                }

                Button {
                  text: "Swap"
                  iconText: "󰓡"
                  foreground: root.foreground
                  fontFamily: Style.font.menuFamily
                  fontSize: Style.font.caption
                  iconSize: Style.font.body
                  bordered: true
                  focusable: true
                  tooltipText: "Swap start and end"
                  onClicked: if (root.hostWidget) root.hostWidget.swapStations()
                }
              }

              Rectangle {
                width: parent.width
                implicitHeight: locationContent.implicitHeight + Style.space(24)
                visible: root.useLocation
                color: Qt.rgba(
                  root.accent.r, root.accent.g, root.accent.b,
                  root.locationReady ? 0.09 : 0.045)
                border.color: Qt.rgba(
                  root.locationReady ? root.accent.r : root.foreground.r,
                  root.locationReady ? root.accent.g : root.foreground.g,
                  root.locationReady ? root.accent.b : root.foreground.b,
                  root.locationReady ? 0.38 : 0.14)
                border.width: 1
                radius: Style.cornerRadius

                Rectangle {
                  anchors.left: parent.left
                  anchors.top: parent.top
                  anchors.bottom: parent.bottom
                  width: Style.space(3)
                  color: root.locationReady ? root.accent : root.dim
                  radius: parent.radius
                }

                Column {
                  id: locationContent
                  anchors.left: parent.left
                  anchors.leftMargin: Style.space(16)
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(12)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(4)

                  Row {
                    spacing: Style.space(7)

                    Rectangle {
                      width: Style.space(7)
                      height: width
                      radius: width / 2
                      color: root.locationReady ? root.accent : root.dim
                      anchors.verticalCenter: parent.verticalCenter
                    }

                    Text {
                      text: root.loading
                        ? "FINDING LOCATION"
                        : (root.locationReady ? "LOCATION ACTIVE" : "LOCATION UNAVAILABLE")
                      textFormat: Text.PlainText
                      color: root.locationReady ? root.accent : root.dim
                      font.family: Style.font.menuFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }
                  }

                  Text {
                    width: parent.width
                    text: root.loading
                      ? "Finding the closest selected station…"
                      : (root.locationReady
                        ? "Starting from " + root.locationStation
                        : "Could not determine your approximate location.")
                    textFormat: Text.PlainText
                    color: root.foreground
                    font.family: Style.font.menuFamily
                    font.pixelSize: Style.font.body
                    font.bold: root.locationReady
                    wrapMode: Text.WordWrap
                  }

                  Text {
                    width: parent.width
                    visible: !root.loading && root.locationReady
                    text: "Closest selected station"
                      + (root.locationDistance ? " · " + root.locationDistance : "")
                      + " · approximate IP location"
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: Style.font.menuFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                  }
                }
              }

              Column {
                width: parent.width
                spacing: Style.space(8)

                PanelSectionHeader {
                  width: parent.width
                  text: "NEXT"
                  foreground: root.foreground
                  fontFamily: Style.font.menuFamily
                }

                Text {
                  visible: !!root.soonest
                  text: Model.nextHero(root.snapshot)
                  textFormat: Text.PlainText
                  color: root.accent
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.heading
                  font.bold: true
                }

                Text {
                  width: parent.width
                  visible: !!root.soonest
                  text: {
                    var toward = Model.nextToward(root.snapshot)
                    return toward ? "toward " + toward : ""
                  }
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }

                Text {
                  width: parent.width
                  visible: !!root.transfer
                  text: root.transfer
                    ? "Transfer at " + root.transfer.stationName
                      + " · " + root.transfer.waitMinutes + " min"
                    : ""
                  textFormat: Text.PlainText
                  color: root.foreground
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  wrapMode: Text.WordWrap
                }

                Text {
                  width: parent.width
                  visible: !root.soonest && root.trips.length > 0
                  text: root.trips.length > 0 ? "First train " + root.trips[0].depart : ""
                  textFormat: Text.PlainText
                  color: root.foreground
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.heading
                  font.bold: true
                }

                Text {
                  width: parent.width
                  visible: !root.soonest && root.pairReady && root.trips.length === 0
                  text: root.loading ? "Finding trains…" : "No live trains"
                  textFormat: Text.PlainText
                  color: root.foreground
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.body
                }

                Text {
                  width: parent.width
                  visible: !root.soonest && root.pairReady && root.trips.length === 0 && !root.loading
                  text: root.advisories.length > 0
                    ? "Service on this pair is replaced or not running tonight."
                    : "Nothing is scheduled for this pair right now."
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }

                Text {
                  width: parent.width
                  visible: !root.pairReady
                  text: "Pick start and end on the map."
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.caption
                }

                Flow {
                  width: parent.width
                  spacing: Style.space(6)
                  visible: root.lineChips.length > 0

                  Repeater {
                    model: root.lineChips
                    Rectangle {
                      required property var modelData
                      implicitHeight: Style.space(24)
                      implicitWidth: chipLabel.implicitWidth + Style.space(22)
                      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.05)
                      border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
                      border.width: 1
                      radius: Style.cornerRadius

                      Row {
                        anchors.centerIn: parent
                        spacing: Style.space(6)

                        Rectangle {
                          width: Style.space(8)
                          height: Style.space(8)
                          radius: 1
                          color: modelData.hexcolor || "#888888"
                          anchors.verticalCenter: parent.verticalCenter
                        }

                        Text {
                          id: chipLabel
                          text: modelData.name || ""
                          textFormat: Text.PlainText
                          color: root.foreground
                          font.family: Style.font.menuFamily
                          font.pixelSize: Style.font.caption
                        }
                      }
                    }
                  }
                }

                Text {
                  width: parent.width
                  visible: root.displayLines.length > 0 && !root.soonest
                  text: Model.lineCaption(root.displayLines[0])
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: Style.font.menuFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
              }

              Column {
                width: parent.width
                spacing: Style.space(10)
                visible: root.departures.length > 0 || root.trips.length > 0

                PanelSectionHeader {
                  width: parent.width
                  text: "BOARD"
                  foreground: root.foreground
                  fontFamily: Style.font.menuFamily
                }

                Repeater {
                  model: root.departures
                  DestinationRow {
                    required property var modelData
                    width: sidebarContent.width
                    row: modelData
                    foreground: root.foreground
                    dim: root.dim
                    fontFamily: Style.font.menuFamily
                  }
                }

                Flow {
                  width: parent.width
                  spacing: Style.space(6)
                  visible: root.departures.length === 0 && root.trips.length > 0

                  Repeater {
                    model: root.trips
                    Rectangle {
                      required property var modelData
                      implicitWidth: tripLabel.implicitWidth + Style.space(16)
                      implicitHeight: Style.space(26)
                      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.06)
                      border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
                      border.width: 1
                      radius: Style.cornerRadius

                      Text {
                        id: tripLabel
                        anchors.centerIn: parent
                        text: (modelData.depart || "") + (modelData.arrive ? " → " + modelData.arrive : "")
                        textFormat: Text.PlainText
                        color: root.foreground
                        font.family: Style.font.menuFamily
                        font.pixelSize: Style.font.body
                      }
                    }
                  }
                }
              }

              Text {
                width: parent.width
                visible: !!(root.snapshot && root.snapshot.traincount)
                text: Model.trainCountLabel(root.snapshot.traincount)
                textFormat: Text.PlainText
                color: root.dim
                font.family: Style.font.menuFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }
      }
    }
  }
}
