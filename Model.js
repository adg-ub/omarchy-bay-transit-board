var STATIONS = [
  { abbr: "12TH", name: "12th St/Oakland City Center" },
  { abbr: "16TH", name: "16th St/Mission" },
  { abbr: "19TH", name: "19th St/Oakland" },
  { abbr: "24TH", name: "24th St/Mission" },
  { abbr: "ANTC", name: "Antioch" },
  { abbr: "ASHB", name: "Ashby" },
  { abbr: "BALB", name: "Balboa Park" },
  { abbr: "BAYF", name: "Bay Fair" },
  { abbr: "BERY", name: "Berryessa/North San José" },
  { abbr: "CAST", name: "Castro Valley" },
  { abbr: "CIVC", name: "Civic Center/UN Plaza" },
  { abbr: "COLS", name: "Coliseum" },
  { abbr: "COLM", name: "Colma" },
  { abbr: "CONC", name: "Concord" },
  { abbr: "DALY", name: "Daly City" },
  { abbr: "DBRK", name: "Downtown Berkeley" },
  { abbr: "DUBL", name: "Dublin/Pleasanton" },
  { abbr: "DELN", name: "El Cerrito del Norte" },
  { abbr: "PLZA", name: "El Cerrito Plaza" },
  { abbr: "EMBR", name: "Embarcadero" },
  { abbr: "FRMT", name: "Fremont" },
  { abbr: "FTVL", name: "Fruitvale" },
  { abbr: "GLEN", name: "Glen Park" },
  { abbr: "HAYW", name: "Hayward" },
  { abbr: "LAFY", name: "Lafayette" },
  { abbr: "LAKE", name: "Lake Merritt" },
  { abbr: "MCAR", name: "MacArthur" },
  { abbr: "MLBR", name: "Millbrae" },
  { abbr: "MLPT", name: "Milpitas" },
  { abbr: "MONT", name: "Montgomery St" },
  { abbr: "NBRK", name: "North Berkeley" },
  { abbr: "NCON", name: "North Concord/Martinez" },
  { abbr: "OAKL", name: "Oakland Int'l Airport" },
  { abbr: "ORIN", name: "Orinda" },
  { abbr: "PITT", name: "Pittsburg/Bay Point" },
  { abbr: "PCTR", name: "Pittsburg Center" },
  { abbr: "PHIL", name: "Pleasant Hill/Contra Costa Centre" },
  { abbr: "POWL", name: "Powell St" },
  { abbr: "RICH", name: "Richmond" },
  { abbr: "ROCK", name: "Rockridge" },
  { abbr: "SBRN", name: "San Bruno" },
  { abbr: "SFIA", name: "SFO Int'l Airport" },
  { abbr: "SANL", name: "San Leandro" },
  { abbr: "SHAY", name: "South Hayward" },
  { abbr: "SSAN", name: "South San Francisco" },
  { abbr: "UCTY", name: "Union City" },
  { abbr: "WCRK", name: "Walnut Creek" },
  { abbr: "WARM", name: "Warm Springs/South Fremont" },
  { abbr: "WDUB", name: "West Dublin/Pleasanton" },
  { abbr: "WOAK", name: "West Oakland" }
]

function stationOptions() {
  var out = []
  for (var i = 0; i < STATIONS.length; i++)
    out.push({ value: STATIONS[i].abbr, label: STATIONS[i].name })
  return out
}

function stationName(abbr) {
  var code = String(abbr || "").toUpperCase()
  if (!code || code === "-") return ""
  for (var i = 0; i < STATIONS.length; i++)
    if (STATIONS[i].abbr === code) return STATIONS[i].name
  return code
}

function barMinute(minutes) {
  var raw = String(minutes || "").trim()
  if (!raw) return "—"
  if (raw.toLowerCase() === "leaving") return "Now"
  var n = parseInt(raw, 10)
  if (isNaN(n)) return raw
  return n + "m"
}

function barDeparture(snapshot) {
  if (!snapshot) return "—"
  if (snapshot.soonest) return barMinute(snapshot.soonest.minutes)
  var trips = snapshot.trips || []
  if (trips.length > 0 && trips[0] && trips[0].depart)
    return String(trips[0].depart)
  return "—"
}

function emptySnapshot() {
  return {
    ok: false,
    origin: "12TH",
    dest: "EMBR",
    originName: "12th St. Oakland City Center",
    destName: "Embarcadero",
    direction: "outbound",
    boardStation: "12TH",
    boardName: "12th St. Oakland City Center",
    headingName: "Embarcadero",
    locationEnabled: false,
    location: { ok: false, message: "Location off" },
    lines: [],
    trips: [],
    departures: [],
    soonest: null,
    transfer: null,
    advisories: [],
    traincount: 0,
    updated: "",
    error: ""
  }
}

function barLabel(snapshot) {
  if (!snapshot) return "󰍸 BAY"
  var from = snapshot.boardStation || snapshot.origin || ""
  var to = snapshot.dest || ""
  if (snapshot.direction === "inbound")
    to = snapshot.origin
  else
    to = snapshot.dest
  if (!from || from === "-" || !to || to === "-") return "󰍸 BAY"
  var time = barDeparture(snapshot)
  return "󰍸 " + from + "→" + to + " " + time
}

function barTooltip(snapshot) {
  if (!snapshot)
    return "Bay Transit Board"
  var from = snapshot.boardName || stationName(snapshot.boardStation)
  var to = snapshot.headingName || ""
  if (!snapshot.soonest) {
    var trips = snapshot.trips || []
    if (trips.length > 0 && trips[0] && trips[0].depart)
      return from + " → " + to + " · First train " + trips[0].depart
    return from + " → " + to
  }
  var via = snapshot.transfer && snapshot.transfer.stationName
    ? " · via " + snapshot.transfer.stationName
    : ""
  return from + " → " + to + via + " · " + snapshot.soonest.dest + " · " + barMinute(snapshot.soonest.minutes)
}

function updatedLabel(updated) {
  var raw = String(updated || "").trim()
  if (!raw) return ""
  return "Updated " + raw.replace(/\s+PDT$|\s+PST$/i, "")
}

function trainCountLabel(count) {
  var n = parseInt(count, 10) || 0
  if (n === 1) return "1 train currently in service"
  return n + " trains currently in service"
}

function directionLabel(snapshot) {
  if (!snapshot) return "Outbound"
  return snapshot.direction === "inbound" ? "Inbound" : "Outbound"
}

function tripSummary(trip) {
  if (!trip) return ""
  var bits = [trip.depart + " → " + trip.arrive]
  if (trip.minutes) bits.push(trip.minutes + " min")
  if (trip.fare) bits.push("$" + trip.fare)
  return bits.join(" · ")
}

function lineCaption(line) {
  if (!line) return ""
  var head = String(line.head || "").trim()
  var text = line.name || ""
  if (head) text += " toward " + head
  if (line.platform) text += " · platform " + line.platform
  if (line.transfer) text += " · " + line.transfer
  return text
}

function advisoryLead(text) {
  var t = String(text || "").replace(/\s+/g, " ").trim()
  if (!t) return ""
  var cut = t.search(/[.!?](\s|$)/)
  if (cut >= 40 && cut <= 180) return t.slice(0, cut + 1)
  if (t.length > 168) return t.slice(0, 165).replace(/\s+\S*$/, "") + "…"
  return t
}

function nextHero(snapshot) {
  if (!snapshot || !snapshot.soonest) return ""
  return barMinute(snapshot.soonest.minutes)
}

function nextToward(snapshot) {
  if (!snapshot || !snapshot.soonest) return ""
  return String(snapshot.soonest.dest || snapshot.headingName || "")
}
