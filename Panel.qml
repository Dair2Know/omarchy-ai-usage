import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "icedeep.ai-usage"
  ipcTarget: "icedeep.ai-usage"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property string helperPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/icedeep.ai-usage/read_usage.py"
  property string collectorPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/icedeep.ai-usage/collect_codex.py"
  property var providers: []
  property string selectedProviderId: ""
  property string readError: ""
  property bool collectorRunning: false
  property bool pendingForcedRefresh: false
  property double nowMs: Date.now()

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property color surface: Color.popups.background
  readonly property color borderColor: Color.popups.border
  readonly property color trackColor: Style.selectedFillFor(foreground, Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var selectedProvider: providers.length > 0 ? providers[providerIndex] : null
  readonly property int providerIndex: {
    for (var i = 0; i < providers.length; i++)
      if (providers[i].providerId === selectedProviderId) return i
    return 0
  }
  readonly property real summaryPercent: {
    var highest = -1
    for (var i = 0; i < providers.length; i++) {
      var limits = providers[i].limits || []
      for (var j = 0; j < limits.length; j++) {
        if (!limits[j].resetExpired && isFinite(Number(limits[j].percent)))
          highest = Math.max(highest, Number(limits[j].percent))
      }
    }
    return highest
  }

  function selectProvider(index) {
    if (providers.length === 0) return
    var wrapped = ((index % providers.length) + providers.length) % providers.length
    selectedProviderId = providers[wrapped].providerId
  }

  function setProviders(value) {
    try {
      var parsed = JSON.parse(value)
      if (!parsed || !Array.isArray(parsed.providers)) throw new Error("Invalid provider snapshot")
      providers = parsed.providers
      readError = String(parsed.error || "")
      if (providers.length > 0 && providerIndex === 0)
        selectedProviderId = providers[0].providerId
    } catch (error) {
      readError = "Could not read local usage records"
    }
  }

  function readSnapshot() {
    if (snapshotProcess.running) return
    snapshotProcess.running = true
  }

  function refreshCodex(force) {
    if (collectorProcess.running) {
      if (force) pendingForcedRefresh = true
      return
    }
    collectorProcess.force = force === true
    collectorProcess.running = true
  }

  function refreshNow() {
    readSnapshot()
    refreshCodex(true)
  }

  function openFromHotkey() {
    controller.show()
    readSnapshot()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function closeForPopoutSwitch() {
    popoutSwitchClosing = true
    close()
    Qt.callLater(function() { popoutSwitchClosing = false })
  }

  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function")
      return bar.switchPanelFrom(hostWidget || root, direction)
    return false
  }

  function clamp(value, minimum, maximum) { return Math.max(minimum, Math.min(maximum, value)) }
  function percentText(value) { return value === null || value === undefined ? "—" : Math.round(Number(value) * 100) + "%" }

  function formatTimestamp(value) {
    if (!value) return "Time unavailable"
    var stamp = new Date(value)
    if (isNaN(stamp.getTime())) return "Time unavailable"
    return Qt.formatDateTime(stamp, "ddd, MMM d · h:mm AP")
  }

  function formatSubscriptionDate(value) {
    if (!value) return "Time unavailable"
    var stamp = new Date(value)
    if (isNaN(stamp.getTime())) return "Time unavailable"
    return Qt.formatDateTime(stamp, "ddd, MMM d, yyyy · h:mm AP")
  }

  function countdown(value) {
    if (!value) return "Reset time not reported"
    var remaining = new Date(value).getTime() - nowMs
    if (!isFinite(remaining)) return "Reset time unavailable"
    if (remaining <= 0) return "Reset passed · awaiting fresh data"
    var minutes = Math.floor(remaining / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return "in " + days + "d " + (hours % 24) + "h " + (minutes % 60) + "m"
    if (hours > 0) return "in " + hours + "h " + (minutes % 60) + "m"
    return "in " + Math.max(1, minutes) + "m"
  }

  function ageText(value) {
    if (value === null || value === undefined) return "source time unavailable"
    var seconds = Math.max(0, Number(value))
    if (seconds < 60) return "checked just now"
    if (seconds < 3600) return "checked " + Math.floor(seconds / 60) + "m ago"
    return "checked " + Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m ago"
  }

  function usageColor(value) {
    if (Number(value) >= 0.9) return bar ? bar.urgent : Color.urgent
    if (Number(value) >= 0.7) return Color.accent
    return foreground
  }

  function bankedSummary(data) {
    if (!data || typeof data !== "object") return "Not reported by provider"
    var count = data.availableCount
    if (count === null || count === undefined) return "Availability not reported"
    var text = Number(count) + " reported available reset " + (Number(count) === 1 ? "credit" : "credits")
    if (data.sourceAt) text += " · as of " + root.formatTimestamp(data.sourceAt)
    if (data.detailsAvailable !== true) text += " · details unavailable"
    else if (Array.isArray(data.details) && data.details.length < Number(count))
      text += " · " + data.details.length + " expiry detail" + (data.details.length === 1 ? "" : "s") + " returned"
    return text
  }

  function grantExpiryText(grant) {
    if (grant.expiresAtKnown !== true) return " · expiry not reported"
    if (!grant.expiresAt) return " · no expiry"
    var expiry = new Date(grant.expiresAt).getTime()
    if (!isFinite(expiry)) return " · expiry unavailable"
    return expiry <= nowMs ? " · expired " + root.formatTimestamp(grant.expiresAt)
      : " · expires " + root.formatTimestamp(grant.expiresAt)
  }

  function nextFreeText(value) {
    if (value === null || value === undefined) return "Not reported by provider"
    if (typeof value === "string") return formatTimestamp(value)
    if (typeof value === "object") {
      if (value.at) return formatTimestamp(value.at)
      if (value.status === "not_reported") return "Not reported by provider"
    }
    return "Not reported by provider"
  }

  function openProviderUsage(providerId) {
    var urls = {
      "claude": "https://claude.ai/settings/usage",
      "codex": "https://chatgpt.com/codex/settings/usage"
    }
    if (urls[providerId]) Qt.openUrlExternally(urls[providerId])
  }

  function subscriptionCountdown(value) {
    if (!value) return ""
    var remaining = new Date(value).getTime() - nowMs
    if (!isFinite(remaining)) return ""
    if (remaining <= 0) return " · recorded date passed"
    var minutes = Math.floor(remaining / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return " · in " + days + "d " + (hours % 24) + "h"
    if (hours > 0) return " · in " + hours + "h " + (minutes % 60) + "m"
    return " · in " + Math.max(1, minutes) + "m"
  }

  function subscriptionText(provider) {
    if (!provider) return ""
    var data = provider.subscription || {}
    var lines = []
    var plan = data.plan || provider.tierLabel
    if (plan) lines.push("Plan · " + plan + " (reported)")
    if (data.product) lines.push("Product · " + data.product)
    if (data.status) lines.push("Status · " + data.status + " (reported)")
    var explicitEnd = data.endsAt
    var periodEnd = data.periodEndsAt
    var shownEnd = explicitEnd || periodEnd
    lines.push(shownEnd
      ? (explicitEnd ? "Subscription ends · " : (data.periodSource ? "Saved paid period ends · " : "Paid period ends · "))
        + formatSubscriptionDate(shownEnd) + subscriptionCountdown(shownEnd)
      : "Subscription end date · Not reported by provider")
    var hasExplicitDetails = data.status || data.product || data.periodEndsAt || data.endsAt
      || data.nextBillingAt || data.renewsAt || data.source
    var sourceAt = explicitEnd
      ? (data.sourceAt || provider.subscriptionUpdatedAt)
      : (data.periodSourceAt || data.sourceAt || (hasExplicitDetails ? provider.subscriptionUpdatedAt : null))
    if (sourceAt) {
      var checked = new Date(sourceAt).getTime()
      var age = isFinite(checked) ? Math.max(0, nowMs - checked) : 0
      var label = data.periodSource ? "Saved period metadata checked · " : "Subscription details checked · "
      lines.push(label + formatSubscriptionDate(sourceAt) + (age > 7 * 86400000 ? " · saved dates may be outdated" : ""))
    }
    if (!explicitEnd && periodEnd) lines.push("A paid-period end does not confirm cancellation or auto-renewal.")
    return lines.join("\n")
  }

  function freshnessText(provider) {
    if (!provider) return ""
    var text = ageText(provider.ageSeconds)
    if (provider.stale === true) text = "STALE · " + text
    return text
  }

  readonly property var summaryStatus: {
    var hasLimits = false
    for (var i = 0; i < providers.length; i++)
      if ((providers[i].limits || []).length > 0) hasLimits = true
    return hasLimits ? "" : "No provider limits available"
  }

  Component.onCompleted: {
    readSnapshot()
    refreshCodex(false)
  }

  onOpenedChanged: if (opened) {
    nowMs = Date.now()
    readSnapshot()
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Process {
    id: snapshotProcess
    command: ["python3", root.helperPath]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.setProviders(text)
    }
    onExited: function(exitCode) {
      if (exitCode !== 0 && root.readError === "") root.readError = "Local usage read failed"
    }
  }

  Process {
    id: collectorProcess
    property bool force: false
    property bool forceQueued: false
    command: ["python3", root.collectorPath].concat(force ? ["--force"] : [])
    stdout: StdioCollector { waitForEnd: true }
    onRunningChanged: root.collectorRunning = running
    onExited: function(exitCode) {
      root.readSnapshot()
      if (root.pendingForcedRefresh) {
        root.pendingForcedRefresh = false
        Qt.callLater(function() { root.refreshCodex(true) })
      }
      if (exitCode !== 0) root.readError = "Codex reset-credit refresh failed; showing the last saved response"
    }
  }

  Timer {
    interval: Math.max(30, Number(root.setting("refreshIntervalSec", 60)) || 60) * 1000
    running: true
    repeat: true
    onTriggered: {
      root.nowMs = Date.now()
      root.readSnapshot()
    }
  }

  Timer {
    interval: 15 * 60 * 1000
    running: true
    repeat: true
    onTriggered: root.refreshCodex(false)
  }

  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.openFromHotkey() }
    function close(): void { root.close() }
    function show(): void { root.openFromHotkey() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refreshNow(); return "ok" }
    function next(): string { root.selectProvider(root.providerIndex + 1); return "ok" }
  }

  KeyboardPanel {
    id: popup
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(420))
    contentHeight: popup.fittedContentHeight(contentColumn.implicitHeight, Style.space(690))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onActivateRequested: root.refreshNow()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) { if (text === "r" || text === "R") root.refreshNow() }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: contentColumn
          width: panelFlick.width
          spacing: Style.space(14)

          Row {
            width: parent.width
            spacing: Style.space(10)
            Column {
              width: parent.width - refreshButton.width - Style.space(10)
              spacing: Style.space(3)
              Text {
                width: parent.width
                text: "AI usage"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: root.selectedProvider
                  ? root.selectedProvider.providerName + (root.selectedProvider.tierLabel ? " · " + root.selectedProvider.tierLabel : "")
                  : "Provider pacing and reset dashboard"
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
            Rectangle {
              id: refreshButton
              width: refreshLabel.implicitWidth + Style.space(20)
              height: refreshLabel.implicitHeight + Style.space(12)
              radius: Style.cornerRadius
              color: refreshMouse.containsMouse ? Style.hoverFillFor(root.foreground, Color.accent) : "transparent"
              border.width: 1
              border.color: root.borderColor
              anchors.verticalCenter: parent.verticalCenter
              Text {
                id: refreshLabel
                anchors.centerIn: parent
                text: root.collectorRunning ? "Refreshing…" : "Refresh"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
              MouseArea {
                id: refreshMouse
                anchors.fill: parent
                hoverEnabled: true
                onClicked: root.refreshNow()
              }
            }
          }

          Rectangle {
            width: parent.width
            height: 1
            color: root.borderColor
          }

          Column {
            width: parent.width
            spacing: Style.space(8)
            visible: root.providers.length > 1
            PanelSectionHeader {
              text: "Provider"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }
            Row {
              width: parent.width
              spacing: Style.space(6)
              Repeater {
                model: root.providers
                delegate: Rectangle {
                  required property var modelData
                  width: Math.min(tabLabel.implicitWidth + Style.space(22), contentColumn.width * 0.47)
                  height: tabLabel.implicitHeight + Style.space(12)
                  radius: Style.cornerRadius
                  color: modelData.providerId === root.selectedProviderId
                    ? Style.selectedFillFor(root.foreground, Color.accent) : "transparent"
                  border.width: 1
                  border.color: modelData.providerId === root.selectedProviderId ? Color.accent : root.borderColor
                  Text {
                    id: tabLabel
                    anchors.centerIn: parent
                    width: parent.width - Style.space(12)
                    text: modelData.providerName
                    textFormat: Text.PlainText
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    horizontalAlignment: Text.AlignHCenter
                    elide: Text.ElideRight
                  }
                  MouseArea {
                    anchors.fill: parent
                    onClicked: root.selectedProviderId = modelData.providerId
                  }
                }
              }
            }
          }

          Text {
            visible: root.providers.length === 0
            width: parent.width
            text: root.readError || "No provider records found yet. The dashboard only reads local usage data."
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Column {
            width: parent.width
            spacing: Style.space(12)
            visible: !!root.selectedProvider

            Column {
              width: parent.width
              spacing: Style.space(4)
              Row {
                width: parent.width
                Text {
                  width: parent.width - staleLabel.implicitWidth - Style.space(8)
                  text: root.selectedProvider ? root.selectedProvider.source : ""
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideMiddle
                }
                Text {
                  id: staleLabel
                  text: root.freshnessText(root.selectedProvider)
                  textFormat: Text.PlainText
                  color: root.selectedProvider && root.selectedProvider.stale ? Color.urgent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
              Text {
                width: parent.width
                visible: !!root.selectedProvider && (!!root.selectedProvider.statusText || !!root.selectedProvider.latestErrorText)
                text: root.selectedProvider
                  ? (root.selectedProvider.statusText || (root.selectedProvider.latestErrorText
                    ? "Latest scan: " + root.selectedProvider.latestErrorText : "")) : ""
                textFormat: Text.PlainText
                color: root.selectedProvider && (root.selectedProvider.isError || root.selectedProvider.latestErrorText) ? Color.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Column {
              width: parent.width
              spacing: Style.space(6)
              PanelSectionHeader {
                text: "Subscription"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Text {
                width: parent.width
                text: root.subscriptionText(root.selectedProvider)
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                lineHeight: 1.25
                wrapMode: Text.WordWrap
              }
            }

            Row {
              width: parent.width
              spacing: Style.space(8)
              PanelSectionHeader {
                width: parent.width - providerUsageLink.implicitWidth - parent.spacing
                text: "Usage windows"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Text {
                id: providerUsageLink
                visible: root.selectedProvider && (root.selectedProvider.providerId === "claude" || root.selectedProvider.providerId === "codex")
                text: "Open provider usage"
                textFormat: Text.PlainText
                color: linkMouse.containsMouse ? root.foreground : Color.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                MouseArea {
                  id: linkMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.openProviderUsage(root.selectedProvider.providerId)
                }
              }
            }

            Text {
              visible: !!root.selectedProvider && (!root.selectedProvider.limits || root.selectedProvider.limits.length === 0)
              width: parent.width
              text: "Usage limits are unavailable in this provider snapshot. No percentage or reset is inferred."
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Repeater {
              model: root.selectedProvider ? root.selectedProvider.limits : []
              delegate: Item {
                id: limitCard
                required property var modelData
                readonly property real used: Number(modelData.percent)
                readonly property real expected: modelData.expectedPercent === null || modelData.expectedPercent === undefined
                  ? -1 : Number(modelData.expectedPercent)
                readonly property bool expired: modelData.resetExpired === true
                width: parent.width
                implicitHeight: limitContent.implicitHeight + Style.space(22)

                Rectangle {
                  anchors.fill: parent
                  radius: Style.cornerRadius
                  color: root.surface
                  border.width: 1
                  border.color: root.borderColor
                }

                Column {
                  id: limitContent
                  anchors.fill: parent
                  anchors.margins: Style.space(11)
                  spacing: Style.space(7)

                  Row {
                    width: parent.width
                    spacing: Style.space(8)
                    Text {
                      width: parent.width - limitPercent.implicitWidth - Style.space(8)
                      text: modelData.label
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                      elide: Text.ElideRight
                    }
                    Text {
                      id: limitPercent
                      text: root.percentText(modelData.percent)
                      textFormat: Text.PlainText
                      color: root.usageColor(limitCard.used)
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                    }
                  }

                  Item {
                    width: parent.width
                    height: Style.space(11)
                    Rectangle {
                      anchors.verticalCenter: parent.verticalCenter
                      width: parent.width
                      height: Style.space(5)
                      radius: height / 2
                      color: root.trackColor
                      Rectangle {
                        width: parent.width * Math.max(0, Math.min(1, limitCard.used))
                        height: parent.height
                        radius: parent.radius
                        color: root.usageColor(limitCard.used)
                      }
                    }
                    Rectangle {
                      visible: limitCard.expected >= 0 && !limitCard.expired
                      x: Math.max(0, Math.min(parent.width - width, parent.width * limitCard.expected - width / 2))
                      anchors.verticalCenter: parent.verticalCenter
                      width: 2
                      height: Style.space(11)
                      radius: 1
                      color: root.dim
                    }
                  }

                  Text {
                    width: parent.width
                    text: "Used " + root.percentText(modelData.percent)
                      + "  ·  " + root.percentText(modelData.remainingPercent) + " remaining"
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    width: parent.width
                    visible: limitCard.expected >= 0 && !limitCard.expired
                    text: limitCard.expected >= 0
                      ? "Expected by now " + root.percentText(limitCard.expected)
                        + "  ·  " + (modelData.pace === "ahead"
                          ? Number(modelData.paceGapPp).toFixed(1) + " pp ahead of pace"
                          : Number(modelData.paceGapPp).toFixed(1) + " pp within pace")
                      : ""
                    textFormat: Text.PlainText
                    color: modelData.pace === "ahead" ? Color.accent : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                  }

                  Text {
                    width: parent.width
                    visible: !limitCard.expired && limitCard.expected < 0
                    text: modelData.durationSeconds === null || modelData.durationSeconds === undefined
                      ? "Pacing unavailable · window duration not reported"
                      : (!modelData.resetsAt ? "Pacing unavailable · reset time not reported" : "Pacing unavailable")
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                  }

                  Text {
                    width: parent.width
                    visible: limitCard.expired
                    text: "Reset passed at " + root.formatTimestamp(modelData.resetsAt)
                      + " · awaiting fresh data; previous usage retained"
                    textFormat: Text.PlainText
                    color: Color.urgent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                  }

                  Text {
                    width: parent.width
                    visible: !limitCard.expired && modelData.projectionPercent !== null
                      && modelData.projectionPercent !== undefined
                    text: modelData.projectionPercent !== null && Number(modelData.projectionPercent) > 1
                      ? "Linear projection at current window average: " + Math.round(Number(modelData.projectionPercent) * 100)
                        + "% by reset · above allowance"
                      : "Linear projection at current window average: " + Math.round(Number(modelData.projectionPercent) * 100) + "% by reset"
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                  }

                  Row {
                    width: parent.width
                    spacing: Style.space(6)
                    Text {
                      width: parent.width - countdownLabel.implicitWidth - Style.space(6)
                      text: modelData.resetsAt ? "Reset " + root.formatTimestamp(modelData.resetsAt) : "Reset time not reported"
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideRight
                    }
                    Text {
                      id: countdownLabel
                      text: root.countdown(modelData.resetsAt)
                      textFormat: Text.PlainText
                      color: limitCard.expired ? Color.urgent : root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
              }
            }

            PanelSectionHeader {
              text: "Reset credits and free resets"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Rectangle {
              width: parent.width
              implicitHeight: grantContent.implicitHeight + Style.space(22)
              radius: Style.cornerRadius
              color: root.surface
              border.width: 1
              border.color: root.borderColor
              Column {
                id: grantContent
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.space(11)
                spacing: Style.space(7)

                Text {
                  width: parent.width
                  text: "Banked reset credits  ·  " + root.bankedSummary(root.selectedProvider
                    ? root.selectedProvider.supplemental.bankedResets : null)
                  textFormat: Text.PlainText
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  wrapMode: Text.WordWrap
                }

                Repeater {
                  model: root.selectedProvider && root.selectedProvider.supplemental
                    && root.selectedProvider.supplemental.bankedResets
                    && Array.isArray(root.selectedProvider.supplemental.bankedResets.details)
                    ? root.selectedProvider.supplemental.bankedResets.details : []
                  delegate: Column {
                    required property var modelData
                    width: grantContent.width
                    spacing: Style.space(2)
                    Text {
                      width: parent.width
                      text: modelData.title || "Reset credit"
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                      wrapMode: Text.WordWrap
                    }
                    Text {
                      width: parent.width
                      text: (modelData.description || (modelData.grantedAt ? "Granted " + root.formatTimestamp(modelData.grantedAt) : "Grant date not reported"))
                        + root.grantExpiryText(modelData)
                      textFormat: Text.PlainText
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      wrapMode: Text.WordWrap
                    }
                  }
                }

                Text {
                  width: parent.width
                  text: "Next free reset  ·  " + root.nextFreeText(root.selectedProvider
                    ? root.selectedProvider.supplemental.nextFreeResetAt : null)
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }

                Text {
                  width: parent.width
                  visible: !!root.selectedProvider && !!root.selectedProvider.supplemental
                    && !!root.selectedProvider.supplemental.bankedResets
                    && !!root.selectedProvider.supplemental.bankedResets.sourceAt
                  text: root.selectedProvider && root.selectedProvider.supplemental
                    && root.selectedProvider.supplemental.bankedResets
                    ? "Grant data source · " + (root.selectedProvider.supplemental.bankedResets.source || "provider")
                      + " · read " + root.formatTimestamp(root.selectedProvider.supplemental.bankedResets.sourceAt) : ""
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
              }
            }

            Text {
              width: parent.width
              text: "Pace uses elapsed wall-clock time from each reported reset window. Provider reset credits are separate from prepaid billing credits."
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }
        }
      }
    }
  }
}
