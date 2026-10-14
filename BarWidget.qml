import QtQuick
import Quickshell
import QtQuick.Controls
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "icedeep.ai-usage"

  readonly property real summaryPercent: panelLoader.item ? panelLoader.item.summaryPercent : -1
  readonly property bool alarming: summaryPercent >= 0.9
  readonly property string helperPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/icedeep.ai-usage/read_usage.py"
  readonly property string collectorPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/icedeep.ai-usage/collect_codex.py"

  function injectPanel() {
    var panel = panelLoader.item
    if (!panel) return
    if ("bar" in panel) panel.bar = root.bar
    if ("settings" in panel) panel.settings = root.settings
    if ("anchorItem" in panel) panel.anchorItem = button
    if ("hostWidget" in panel) panel.hostWidget = root
    if ("helperPath" in panel) panel.helperPath = root.helperPath
    if ("collectorPath" in panel) panel.collectorPath = root.collectorPath
  }

  function togglePanel() {
    if (panelLoader.item && panelLoader.item.toggle) panelLoader.item.toggle()
  }

  function open() {
    if (panelLoader.item && panelLoader.item.openFromHotkey) panelLoader.item.openFromHotkey()
  }

  function close() {
    if (panelLoader.item && panelLoader.item.close) panelLoader.item.close()
  }

  function refreshNow() {
    if (panelLoader.item && panelLoader.item.refreshNow) panelLoader.item.refreshNow()
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function closeForPopoutSwitch() {
    if (panelLoader.item && panelLoader.item.closeForPopoutSwitch)
      panelLoader.item.closeForPopoutSwitch()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

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

  Component {
    id: gaugeIcon

    Item {
      id: gauge
      property real ratio: root.summaryPercent
      property color ink: root.bar ? root.bar.foreground : Color.foreground
      implicitWidth: 17
      implicitHeight: 17

      Canvas {
        id: ring
        anchors.fill: parent
        onPaint: {
          var ctx = getContext("2d")
          ctx.clearRect(0, 0, width, height)
          ctx.lineWidth = 2
          ctx.lineCap = "round"
          ctx.beginPath()
          ctx.strokeStyle = Qt.rgba(gauge.ink.r, gauge.ink.g, gauge.ink.b, 0.27)
          ctx.arc(width / 2, height / 2, width / 2 - 2, -Math.PI / 2, Math.PI * 1.5, false)
          ctx.stroke()
          if (gauge.ratio >= 0) {
            ctx.beginPath()
            ctx.strokeStyle = gauge.ratio >= 0.9 && root.bar ? root.bar.urgent : gauge.ink
            ctx.arc(width / 2, height / 2, width / 2 - 2, -Math.PI / 2,
              -Math.PI / 2 + Math.PI * 2 * Math.max(0.025, Math.min(1, gauge.ratio)), false)
            ctx.stroke()
          }
        }
        Component.onCompleted: requestPaint()
      }

      onRatioChanged: ring.requestPaint()
      onInkChanged: ring.requestPaint()
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    active: root.alarming
    iconComponent: gaugeIcon
    tooltipText: "AI usage dashboard"

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refreshNow()
      else if (buttonCode === Qt.MiddleButton && panelLoader.item)
        panelLoader.item.selectProvider(panelLoader.item.providerIndex + 1)
      else root.togglePanel()
    }
  }
}
