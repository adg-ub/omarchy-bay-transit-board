import QtQuick
import QtQuick.Window
import qs.Commons
import "Network.js" as Network

Item {
  id: root

  property string origin: "12TH"
  property string dest: "EMBR"
  property var activeLineNames: []
  property string pickFocus: "origin"
  property string hint: "Drag to pan · wheel to zoom · click a station"
  property string fontFamily: Style.font.menuFamily

  property color backgroundColor: "#090a0c"
  property color waterColor: "#07141c"
  property color landColor: "#455560"
  property color gridColor: "#7d8791"
  property color outlineColor: "#8eb4c2"
  property color stationColor: "#e6ecef"
  property color accentColor: "#ff8a3d"
  property color textColor: "#f3f4f5"
  property color dimColor: "#9aa3ab"
  property bool lightTheme: false
  readonly property real renderScale: Math.max(1, Screen.devicePixelRatio)

  property real zoom: 1
  property real panX: 0
  property real panY: 0
  property real minimumZoom: 0.78
  property real maximumZoom: 6.5

  property var hoveredStation: null
  property real hoverX: 0
  property real hoverY: 0
  property var preparedStations: []
  property var trackCache: []
  property var route: ({ segments: [], transfer: "" })

  signal stationActivated(string abbr)
  signal interactionStarted()

  Accessible.name: "Bay Area rapid-transit network map"
  Accessible.description: "Drag to pan, use the mouse wheel to zoom, and click a station to set start or end"
  Accessible.role: Accessible.Pane

  readonly property var legend: [
    { name: "Yellow", hexcolor: "#FFFF33" },
    { name: "Orange", hexcolor: "#FF9933" },
    { name: "Green", hexcolor: "#339933" },
    { name: "Red", hexcolor: "#FF0000" },
    { name: "Blue", hexcolor: "#0099CC" },
    { name: "OAK", hexcolor: "#B0BEC7" }
  ]

  function withAlpha(color, alpha) {
    return Qt.rgba(color.r, color.g, color.b, alpha)
  }

  function parseHex(hex) {
    var raw = String(hex || "#888888").replace("#", "")
    if (raw.length === 3)
      raw = raw[0] + raw[0] + raw[1] + raw[1] + raw[2] + raw[2]
    if (raw.length < 6) raw = "888888"
    return Qt.rgba(
      parseInt(raw.substring(0, 2), 16) / 255,
      parseInt(raw.substring(2, 4), 16) / 255,
      parseInt(raw.substring(4, 6), 16) / 255,
      1)
  }

  function lineColor(name, hex) {
    var color = parseHex(hex)
    if (!lightTheme) return color
    if (name === "Yellow") return parseHex("#b89f00")
    return Qt.darker(color, 1.15)
  }

  function lineActive(name) {
    if (!activeLineNames || activeLineNames.length === 0) return false
    for (var i = 0; i < activeLineNames.length; i++) {
      if (String(activeLineNames[i]).toLowerCase() === String(name).toLowerCase())
        return true
    }
    return false
  }

  function projectSchematic(x, y) {
    var fit = Math.min(width * 0.94 / 100, height * 0.94 / 100)
    var scale = fit * zoom
    return {
      x: width / 2 + panX + (x - 50) * scale,
      y: height / 2 + panY + (y - 52) * scale
    }
  }

  function stationPoint(abbr) {
    var point = Network.schematicPoint(abbr)
    return point ? projectSchematic(point.x, point.y) : { x: 0, y: 0 }
  }

  function schematicStops(stops) {
    var pts = []
    for (var i = 0; i < stops.length; i++) {
      var point = Network.schematicPoint(stops[i].abbr)
      if (!point) continue
      var p = projectSchematic(point.x, point.y)
      pts.push({ x: p.x, y: p.y, abbr: stops[i].abbr })
    }
    return pts
  }

  function strokeTrack(ctx, pts) {
    if (!pts || pts.length < 2) return
    ctx.beginPath()
    ctx.moveTo(pts[0].x, pts[0].y)
    for (var i = 1; i < pts.length; i++)
      ctx.lineTo(pts[i].x, pts[i].y)
    ctx.stroke()
  }

  function laneScale() {
    return trackWidth() * 0.9
  }

  function trackWidth() {
    return Math.max(3.1, 3.55 * zoom)
  }

  function glowWidth() {
    return trackWidth()
  }

  function trackPoints(line) {
    return Network.offsetPolyline(
      schematicStops(line.stops),
      Network.lineLane(line.name) * laneScale())
  }

  function collectTracks() {
    var source = Network.NETWORK.lines
    var rows = []
    for (var i = 0; i < source.length; i++)
      rows.push({ name: source[i].name, line: source[i], pts: trackPoints(source[i]) })
    rows.sort(function(a, b) {
      return Network.lineLane(a.name) - Network.lineLane(b.name)
    })
    return rows
  }

  function trackByName(tracks, name) {
    for (var i = 0; i < tracks.length; i++)
      if (tracks[i].name === name) return tracks[i].pts
    return []
  }

  function pointOnTrack(pts, abbr) {
    var code = String(abbr || "").toUpperCase()
    for (var i = 0; i < pts.length; i++)
      if (pts[i].abbr === code) return pts[i]
    return null
  }

  function glowSegments() {
    var segs = route && route.segments ? route.segments : []
    if (!segs.length) return []
    if (!activeLineNames || activeLineNames.length === 0) return segs
    var filtered = []
    for (var i = 0; i < segs.length; i++) {
      if (lineActive(segs[i].name)) filtered.push(segs[i])
    }
    return filtered.length ? filtered : segs
  }

  function stationAnchor(abbr, tracks) {
    return stationPoint(abbr)
  }

  function haloText(ctx, text, x, y, fill) {
    ctx.save()
    ctx.lineJoin = "round"
    ctx.miterLimit = 2
    ctx.strokeStyle = withAlpha(backgroundColor, 0.96)
    ctx.lineWidth = 2.8
    ctx.strokeText(text, x, y)
    ctx.fillStyle = fill
    ctx.fillText(text, x, y)
    ctx.restore()
  }

  function paintLines(ctx, tracks) {
    var hasRoute = !!(route && route.segments && route.segments.length)
    ctx.lineCap = "round"
    ctx.lineJoin = "round"
    var core = trackWidth()
    for (var i = 0; i < tracks.length; i++) {
      var line = tracks[i].line
      var color = lineColor(line.name, line.hexcolor)
      var pts = tracks[i].pts
      var alpha = !hasRoute ? 0.96 : 0.58
      ctx.strokeStyle = withAlpha(color, alpha)
      ctx.lineWidth = core
      strokeTrack(ctx, pts)
    }
  }

  function glowSliceEnds(index, count) {
    var hub = route && route.transfer ? String(route.transfer).toUpperCase() : ""
    if (hub && count >= 2) {
      if (index === 0) return [String(origin).toUpperCase(), hub]
      return [hub, String(dest).toUpperCase()]
    }
    return [String(origin).toUpperCase(), String(dest).toUpperCase()]
  }

  function preparedGlow(tracks) {
    var segs = glowSegments()
    var out = []
    for (var i = 0; i < segs.length; i++) {
      var ends = glowSliceEnds(i, segs.length)
      var pts = Network.sliceProjected(trackByName(tracks, segs[i].name), ends[0], ends[1])
      if (pts.length < 2 && segs[i].stops && segs[i].stops.length >= 2) {
        pts = Network.sliceProjected(
          trackByName(tracks, segs[i].name),
          segs[i].stops[0].abbr,
          segs[i].stops[segs[i].stops.length - 1].abbr)
      }
      if (pts.length < 2) continue
      out.push({
        name: segs[i].name,
        color: lineColor(segs[i].name, segs[i].hexcolor),
        pts: pts
      })
    }
    return out
  }

  function paintRoute(ctx, tracks) {
    var rows = preparedGlow(tracks)
    if (rows.length === 0) return
    var core = glowWidth()
    ctx.lineCap = "round"
    ctx.lineJoin = "round"
    for (var i = 0; i < rows.length; i++) {
      ctx.strokeStyle = rows[i].color
      ctx.lineWidth = core + 0.45
      strokeTrack(ctx, rows[i].pts)
    }
  }

  function trackNormalAt(pts, abbr) {
    var code = String(abbr || "").toUpperCase()
    for (var i = 0; i < pts.length; i++) {
      if (pts[i].abbr !== code) continue
      var prev = pts[Math.max(0, i - 1)]
      var next = pts[Math.min(pts.length - 1, i + 1)]
      var dx = next.x - prev.x
      var dy = next.y - prev.y
      var len = Math.hypot(dx, dy) || 1
      return { x: -dy / len, y: dx / len }
    }
    return { x: 1, y: 0 }
  }

  function stationTrackHits(abbr, tracks) {
    var hits = []
    for (var i = 0; i < tracks.length; i++) {
      var point = pointOnTrack(tracks[i].pts, abbr)
      if (point) hits.push({ point: point, pts: tracks[i].pts })
    }
    return hits
  }

  function paintStationTick(ctx, abbr, tracks) {
    var hits = stationTrackHits(abbr, tracks)
    if (hits.length === 0) return
    var a = hits[0].point
    var b = hits[0].point
    var farthest = 0
    for (var i = 0; i < hits.length; i++) {
      for (var j = i + 1; j < hits.length; j++) {
        var distance = Math.hypot(
          hits[j].point.x - hits[i].point.x,
          hits[j].point.y - hits[i].point.y)
        if (distance <= farthest) continue
        farthest = distance
        a = hits[i].point
        b = hits[j].point
      }
    }

    var nx, ny
    if (farthest > 0.5) {
      nx = (b.x - a.x) / farthest
      ny = (b.y - a.y) / farthest
    } else {
      var normal = trackNormalAt(hits[0].pts, abbr)
      nx = normal.x
      ny = normal.y
    }
    var pad = hits.length > 1 ? 2.2 : 3.1
    var x1 = a.x - nx * pad
    var y1 = a.y - ny * pad
    var x2 = b.x + nx * pad
    var y2 = b.y + ny * pad

    ctx.lineCap = "round"
    ctx.beginPath()
    ctx.moveTo(x1, y1)
    ctx.lineTo(x2, y2)
    ctx.strokeStyle = withAlpha(backgroundColor, 0.9)
    ctx.lineWidth = hits.length >= 3 ? 4.2 : 3.5
    ctx.stroke()
    ctx.beginPath()
    ctx.moveTo(x1, y1)
    ctx.lineTo(x2, y2)
    ctx.strokeStyle = withAlpha(stationColor, 0.96)
    ctx.lineWidth = hits.length >= 3 ? 2.1 : 1.55
    ctx.stroke()
  }

  function paintStations(ctx, tracks) {
    var rows = Network.NETWORK.stations
    var prepared = []
    var originCode = Network.isPicked(origin) ? String(origin).toUpperCase() : ""
    var destCode = Network.isPicked(dest) ? String(dest).toUpperCase() : ""
    var transfer = route && route.transfer ? String(route.transfer).toUpperCase() : ""
    var hoverAbbr = hoveredStation ? hoveredStation.abbr : ""

    for (var i = 0; i < rows.length; i++) {
      var station = rows[i]
      var p = stationAnchor(station.abbr, tracks)
      var isOrigin = station.abbr === originCode
      var isDest = station.abbr === destCode
      var isTransfer = station.abbr === transfer
      var isHover = station.abbr === hoverAbbr
      paintStationTick(ctx, station.abbr, tracks)
      prepared.push({
        station: station,
        screenX: p.x,
        screenY: p.y,
        isOrigin: isOrigin,
        isDest: isDest
      })

      if (isOrigin || isDest || isTransfer || isHover) {
        var radius = isOrigin || isDest ? 6.5 : 5.2
        ctx.beginPath()
        ctx.arc(p.x, p.y, radius, 0, Math.PI * 2)
        ctx.fillStyle = backgroundColor
        ctx.fill()
        ctx.beginPath()
        ctx.arc(p.x, p.y, radius, 0, Math.PI * 2)
        ctx.strokeStyle = isOrigin || isDest ? accentColor : stationColor
        ctx.lineWidth = isOrigin || isDest ? 2.1 : 1.5
        ctx.stroke()
        ctx.beginPath()
        ctx.arc(p.x, p.y, 1.8, 0, Math.PI * 2)
        ctx.fillStyle = isOrigin || isDest ? accentColor : stationColor
        ctx.fill()
      }
    }
    preparedStations = prepared
    paintLabels(ctx, prepared, originCode, destCode, transfer, hoverAbbr)
  }

  function stationOnGlow(abbr) {
    var code = String(abbr || "").toUpperCase()
    var segs = glowSegments()
    if (!segs.length) return false
    for (var i = 0; i < segs.length; i++) {
      var line = Network.lineByName(segs[i].name)
      if (!line) continue
      var ends = glowSliceEnds(i, segs.length)
      var a = Network.indexOnLine(line, ends[0])
      var b = Network.indexOnLine(line, ends[1])
      var c = Network.indexOnLine(line, code)
      if (a < 0 || b < 0 || c < 0) continue
      if ((a <= c && c <= b) || (b <= c && c <= a)) return true
    }
    return false
  }

  function labelLayout(abbr) {
    if (abbr === "MCAR") return { dx: 10, dy: -5, align: "left" }
    if (abbr === "ASHB") return { dx: -9, dy: -5, align: "right" }
    if (abbr === "19TH") return { dx: 10, dy: 2, align: "left" }
    if (abbr === "12TH") return { dx: 10, dy: 7, align: "left" }
    if (abbr === "WOAK") return { dx: -10, dy: -8, align: "right" }
    if (abbr === "LAKE") return { dx: 10, dy: 4, align: "left" }
    if (abbr === "DBRK") return { dx: -9, dy: -5, align: "right" }
    if (abbr === "NBRK") return { dx: -10, dy: -8, align: "right" }
    if (abbr === "ROCK") return { dx: 10, dy: -9, align: "left" }
    if (abbr === "FTVL") return { dx: 10, dy: 3, align: "left" }
    if (abbr === "EMBR") return { dx: -10, dy: -6, align: "right" }
    if (abbr === "COLS") return { dx: 10, dy: -8, align: "left" }
    if (abbr === "OAKL") return { dx: -9, dy: 14, align: "right" }
    if (abbr === "16TH" || abbr === "24TH" || abbr === "GLEN"
        || abbr === "BALB" || abbr === "DALY" || abbr === "COLM"
        || abbr === "SSAN" || abbr === "SBRN")
      return { dx: -10, dy: 3, align: "right" }
    if (abbr === "MONT" || abbr === "POWL" || abbr === "CIVC")
      return { dx: -10, dy: -2, align: "right" }
    if (abbr === "SFIA") return { dx: -10, dy: -11, align: "right" }
    if (abbr === "MLBR") return { dx: 10, dy: -2, align: "left" }
    if (abbr === "RICH") return { dx: -9, dy: -5, align: "right" }
    if (abbr === "ANTC")
      return { dx: 10, dy: 3, align: "left" }
    if (abbr === "BERY") return { dx: -10, dy: -18, align: "right" }
    if (abbr === "DUBL") return { dx: -10, dy: -7, align: "right" }
    if (abbr === "BAYF") return { dx: 10, dy: -9, align: "left" }
    if (abbr === "WDUB") return { dx: 9, dy: -10, align: "left" }
    if (abbr === "CAST") return { dx: -9, dy: -10, align: "right" }
    if (abbr === "NCON" || abbr === "PITT" || abbr === "PCTR")
      return { dx: 0, dy: -10, align: "right" }
    return { dx: 9, dy: -7, align: "left" }
  }

  function schematicMajor(abbr) {
    return Network.isTerminal(abbr)
      || abbr === "MCAR" || abbr === "WOAK" || abbr === "LAKE"
      || abbr === "COLS" || abbr === "BAYF" || abbr === "BALB"
  }

  function paintLabels(ctx, prepared, originCode, destCode, transfer, hoverAbbr) {
    ctx.font = Style.font.caption + "px \"" + fontFamily + "\""
    ctx.textBaseline = "bottom"
    var hasRoute = glowSegments().length > 0
    for (var i = 0; i < prepared.length; i++) {
      var row = prepared[i]
      var station = row.station
      var abbr = station.abbr
      var show = abbr === originCode
        || abbr === destCode
        || abbr === transfer
        || abbr === hoverAbbr
      if (!show && zoom >= 0.95) show = schematicMajor(abbr)
      if (!show && zoom >= 1.45) show = !hasRoute || stationOnGlow(abbr)
      if (!show && zoom >= 1.9) show = true
      if (!show) continue

      var label = Network.stationLabel(station, true)
      var layout = labelLayout(abbr)
      var metrics = ctx.measureText(label)
      var x = layout.align === "right"
        ? row.screenX + layout.dx - metrics.width
        : row.screenX + layout.dx
      var y = row.screenY + layout.dy
      if (x < 8) x = row.screenX + 9
      if (x + metrics.width > width - 10) x = row.screenX - metrics.width - 9
      var fill = abbr === originCode || abbr === destCode ? accentColor : textColor
      haloText(ctx, label, x, y, fill)
    }
  }

  function paintMap(ctx) {
    if (width < 8 || height < 8) return
    ctx.reset()
    ctx.scale(renderScale, renderScale)
    ctx.fillStyle = backgroundColor
    ctx.fillRect(0, 0, width, height)

    var vignette = ctx.createRadialGradient(
      width * 0.48, height * 0.46, Math.min(width, height) * 0.05,
      width * 0.5, height * 0.5, Math.max(width, height) * 0.75)
    vignette.addColorStop(0, withAlpha(landColor, lightTheme ? 0.18 : 0.12))
    vignette.addColorStop(0.68, withAlpha(landColor, lightTheme ? 0.08 : 0.04))
    vignette.addColorStop(1, withAlpha(backgroundColor, 0))
    ctx.fillStyle = vignette
    ctx.fillRect(0, 0, width, height)

    var tracks = collectTracks()
    trackCache = tracks
    paintLines(ctx, tracks)
    paintRoute(ctx, tracks)
    paintStations(ctx, tracks)
  }

  function stationUnderPointer(x, y) {
    var nearest = null
    var nearestDistance = 196
    for (var i = 0; i < preparedStations.length; i++) {
      var row = preparedStations[i]
      var dx = row.screenX - x
      var dy = row.screenY - y
      var distance = dx * dx + dy * dy
      if (distance > nearestDistance) continue
      nearest = row.station
      nearestDistance = distance
    }
    return nearest
  }

  function refreshRoute() {
    route = Network.routeBetween(origin, dest)
    mapCanvas.requestPaint()
  }

  onOriginChanged: refreshRoute()
  onDestChanged: refreshRoute()
  onActiveLineNamesChanged: mapCanvas.requestPaint()
  onZoomChanged: mapCanvas.requestPaint()
  onPanXChanged: mapCanvas.requestPaint()
  onPanYChanged: mapCanvas.requestPaint()
  onWidthChanged: mapCanvas.requestPaint()
  onHeightChanged: mapCanvas.requestPaint()
  onHoveredStationChanged: mapCanvas.requestPaint()
  onBackgroundColorChanged: mapCanvas.requestPaint()
  onWaterColorChanged: mapCanvas.requestPaint()
  onLandColorChanged: mapCanvas.requestPaint()
  onLightThemeChanged: mapCanvas.requestPaint()

  Component.onCompleted: refreshRoute()

  Canvas {
    id: mapCanvas
    x: 0
    y: 0
    width: Math.ceil(root.width * root.renderScale)
    height: Math.ceil(root.height * root.renderScale)
    scale: 1 / root.renderScale
    transformOrigin: Item.TopLeft
    antialiasing: true
    renderStrategy: Canvas.Cooperative
    onPaint: {
      var ctx = getContext("2d")
      if (ctx) root.paintMap(ctx)
    }
  }

  MouseArea {
    id: pointer
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.LeftButton
    cursorShape: pressed
      ? Qt.ClosedHandCursor
      : (root.hoveredStation ? Qt.PointingHandCursor : Qt.OpenHandCursor)

    property real lastX: 0
    property real lastY: 0
    property real totalMovement: 0

    onPressed: function(mouse) {
      root.interactionStarted()
      lastX = mouse.x
      lastY = mouse.y
      totalMovement = 0
      root.hoveredStation = null
    }

    onPositionChanged: function(mouse) {
      root.hoverX = mouse.x
      root.hoverY = mouse.y
      if (!(pressedButtons & Qt.LeftButton)) {
        root.hoveredStation = root.stationUnderPointer(mouse.x, mouse.y)
        return
      }
      var dx = mouse.x - lastX
      var dy = mouse.y - lastY
      root.panX += dx
      root.panY += dy
      totalMovement += Math.abs(dx) + Math.abs(dy)
      lastX = mouse.x
      lastY = mouse.y
    }

    onReleased: function(mouse) {
      if (totalMovement < 7) {
        var station = root.stationUnderPointer(mouse.x, mouse.y)
        if (station) root.stationActivated(station.abbr)
        root.hoveredStation = root.stationUnderPointer(mouse.x, mouse.y)
      }
    }

    onExited: if (!(pressedButtons & Qt.LeftButton)) root.hoveredStation = null

    onWheel: function(wheel) {
      root.interactionStarted()
      var oldZoom = root.zoom
      var factor = Math.exp(wheel.angleDelta.y / 720)
      var next = Network.clamp(oldZoom * factor, root.minimumZoom, root.maximumZoom)
      if (next === oldZoom) {
        wheel.accepted = true
        return
      }
      var mx = wheel.x - root.width / 2
      var my = wheel.y - root.height / 2
      root.panX = mx - (mx - root.panX) * (next / oldZoom)
      root.panY = my - (my - root.panY) * (next / oldZoom)
      root.zoom = next
      wheel.accepted = true
    }
  }

  Rectangle {
    id: tooltip
    visible: !!root.hoveredStation && !pointer.pressed
    x: Math.min(root.width - width - 8, Math.max(8, root.hoverX + 14))
    y: Math.min(root.height - height - 8, Math.max(8, root.hoverY + 14))
    width: Math.min(260, tooltipText.implicitWidth + 20)
    height: tooltipText.implicitHeight + 14
    color: Qt.rgba(root.backgroundColor.r, root.backgroundColor.g, root.backgroundColor.b, 0.94)
    border.color: root.withAlpha(root.outlineColor, 0.5)
    border.width: 1
    radius: 2
    z: 3

    Text {
      id: tooltipText
      anchors.centerIn: parent
      width: Math.min(240, implicitWidth)
      text: {
        if (!root.hoveredStation) return ""
        var role = ""
        if (root.hoveredStation.abbr === String(root.origin).toUpperCase()) role = " · start"
        else if (root.hoveredStation.abbr === String(root.dest).toUpperCase()) role = " · end"
        return (root.hoveredStation.label || root.hoveredStation.name) + role
      }
      textFormat: Text.PlainText
      color: root.textColor
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
  }

  Row {
    id: legendRow
    anchors.left: parent.left
    anchors.leftMargin: Style.spacing.md
    anchors.bottom: parent.bottom
    anchors.bottomMargin: Style.space(28)
    spacing: Style.space(10)
    z: 2

    Repeater {
      model: root.legend
      Row {
        required property var modelData
        spacing: Style.space(4)

        Rectangle {
          width: Style.space(10)
          height: Style.space(4)
          radius: 1
          color: root.lineColor(modelData.name, modelData.hexcolor)
          anchors.verticalCenter: parent.verticalCenter
        }

        Text {
          text: modelData.name
          textFormat: Text.PlainText
          color: root.dimColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }

  Text {
    anchors.left: parent.left
    anchors.leftMargin: Style.spacing.md
    anchors.right: parent.right
    anchors.rightMargin: Style.spacing.md
    anchors.bottom: parent.bottom
    anchors.bottomMargin: Style.spacing.sm
    z: 2
    text: root.hint
    textFormat: Text.PlainText
    color: root.dimColor
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    elide: Text.ElideRight
  }
}
