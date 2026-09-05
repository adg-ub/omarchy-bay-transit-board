import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

BarWidget {
  id: root
  moduleName: "io.github.adg-ub.bay-transit-board"

  readonly property string home: Quickshell.env("HOME")
  readonly property string pluginDir: home + "/.config/omarchy/plugins/" + moduleName
  readonly property string origin: {
    var value = String(setting("origin", "12TH")).toUpperCase()
    return value === "-" ? "" : value
  }
  readonly property string dest: {
    var value = String(setting("dest", "EMBR")).toUpperCase()
    return value === "-" ? "" : value
  }
  readonly property bool useLocation: setting("useLocation", false) === true || setting("useLocation", false) === "true"

  property var snapshot: Model.emptySnapshot()
  property bool loading: false
  property string lastError: ""
  property bool refreshPending: false
  readonly property int maxPayloadCharacters: 262144

  readonly property string displayText: Model.barLabel(snapshot)
  readonly property string tooltip: Model.barTooltip(snapshot)
  readonly property bool trainLeaving: {
    if (!snapshot || !snapshot.soonest) return false
    return String(snapshot.soonest.minutes) === "Leaving"
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true
    : false

  function persist(key, value) {
    var entry = { id: root.moduleName }
    for (var k in root.settings) if (k !== "id") entry[k] = root.settings[k]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function persistPair(originCode, destCode) {
    var entry = { id: root.moduleName }
    for (var k in root.settings) if (k !== "id") entry[k] = root.settings[k]
    entry.origin = String(originCode || "-").toUpperCase() || "-"
    entry.dest = String(destCode || "-").toUpperCase() || "-"
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function setOrigin(abbr) {
    var code = String(abbr || "").toUpperCase()
    persist("origin", code || "-")
    refresh()
  }

  function setDest(abbr) {
    var code = String(abbr || "").toUpperCase()
    persist("dest", code || "-")
    refresh()
  }

  function setPair(originCode, destCode) {
    persistPair(originCode, destCode)
    refresh()
  }

  function setUseLocation(on) {
    persist("useLocation", on === true)
    refresh()
  }

  function swapStations() {
    persist("origin", dest || "-")
    persist("dest", origin || "-")
    refresh()
  }

  function refresh() {
    if (fetchProc.running) {
      refreshPending = true
      return
    }
    loading = true
    var cmd = ["python3", pluginDir + "/fetch.py", origin || "-", dest || "-"]
    if (useLocation) cmd.push("--locate")
    fetchProc.command = cmd
    fetchProc.running = true
  }

  function applyPayload(raw) {
    if (!raw) {
      lastError = "Transit data process returned no output"
      loading = false
      return
    }
    if (raw.length > maxPayloadCharacters) {
      lastError = "Transit response exceeded the local output safety limit"
      loading = false
      return
    }
    try {
      var parsed = JSON.parse(raw)
      snapshot = parsed
      lastError = snapshot.error || ""
    } catch (e) {
      lastError = String(e)
    }
    loading = false
  }

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onOriginChanged: refresh()
  onDestChanged: refresh()
  onUseLocationChanged: refresh()

  Timer {
    interval: 15000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: fetchProc
    onRunningChanged: {
      if (!running && root.refreshPending) {
        root.refreshPending = false
        Qt.callLater(root.refresh)
      }
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyPayload(String(text || "").trim())
    }
    stderr: StdioCollector { waitForEnd: true }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: root.moduleName

    function refresh(): void { root.refresh() }
    function swap(): void { root.swapStations() }
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function status(): string {
      return JSON.stringify({
        origin: root.origin,
        dest: root.dest,
        direction: root.snapshot ? root.snapshot.direction : "",
        board: root.snapshot ? root.snapshot.boardStation : "",
        useLocation: root.useLocation,
        loading: root.loading,
        error: root.lastError,
        soonest: root.snapshot ? root.snapshot.soonest : null
      })
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.displayText
    tooltipText: root.tooltip
    active: root.trainLeaving
    horizontalMargin: 8.75
    verticalPadding: 8.75

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else if (buttonCode === Qt.MiddleButton) root.swapStations()
      else root.toggle()
    }
  }
}
