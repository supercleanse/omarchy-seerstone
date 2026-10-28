import QtQuick
import qs.Ui

// A bar button that opens Seerstone. Its icon is the Seerstone mark (a stone
// with an eye cut out of it), drawn in the bar's own color. Before Seerstone
// is installed, the button runs install.sh in a floating terminal instead.
BarWidget {
  id: root
  moduleName: "supercleanse.seerstone"

  readonly property string pluginDir: decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "").replace(/\/$/, ""))

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: "Seerstone"
    iconComponent: Canvas {
      id: mark
      property color ink: button.foreground
      onInkChanged: requestPaint()
      onWidthChanged: requestPaint()
      onHeightChanged: requestPaint()
      onPaint: {
        var ctx = getContext("2d")
        var s = Math.min(width, height) / 256
        ctx.reset()
        ctx.save()
        ctx.translate((width - 256 * s) / 2, (height - 256 * s) / 2)
        ctx.scale(s, s)
        ctx.fillStyle = ink
        // The stone: an ellipse tilted 30 degrees.
        ctx.save()
        ctx.translate(128, 128)
        ctx.rotate(-Math.PI / 6)
        ctx.scale(114, 94)
        ctx.beginPath()
        ctx.arc(0, 0, 1, 0, 2 * Math.PI)
        ctx.restore()
        ctx.fill()
        // The eye, cut out of the stone.
        ctx.globalCompositeOperation = "destination-out"
        ctx.beginPath()
        ctx.moveTo(70, 134)
        ctx.bezierCurveTo(92, 86, 164, 86, 186, 134)
        ctx.bezierCurveTo(164, 182, 92, 182, 70, 134)
        ctx.closePath()
        ctx.fill()
        // The pupil.
        ctx.globalCompositeOperation = "source-over"
        ctx.beginPath()
        ctx.arc(128, 134, 22, 0, 2 * Math.PI)
        ctx.fill()
        ctx.restore()
      }
    }
    onPressed: function(b) {
      root.bar.run("if command -v seerstone >/dev/null; then setsid uwsm-app -- seerstone >/dev/null 2>&1; "
                   + "else omarchy-launch-floating-terminal-with-presentation "
                   + "'" + root.pluginDir + "/install.sh && (setsid uwsm-app -- seerstone >/dev/null 2>&1 &)'; fi")
    }
  }
}
