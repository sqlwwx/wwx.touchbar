import Quickshell
import Quickshell.Io
import QtQuick

// Lifecycle wrapper for the Touch Bar renderer. The Touch Bar is a DRM device
// owned by /run/t1bridge/touchbar.sock, so the drawing lives in
// renderer/touchbar.py; this service starts it, restarts it when it dies,
// and stops it when the plugin is unloaded.
Item {
  id: root

  // Injected by omarchy-shell.
  property var shell: null

  readonly property string pluginDir: (Quickshell.env("HOME") || "") + "/.config/omarchy/plugins/wwx.touchbar"
  property bool unloading: false

  Process {
    id: renderer
    running: true
    command: [root.pluginDir + "/renderer/launch.sh"]

    onExited: {
      if (!root.unloading)
        restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 3000
    repeat: false
    onTriggered: {
      if (!root.unloading)
        renderer.running = true
    }
  }

  Component.onDestruction: {
    root.unloading = true
    restartTimer.stop()
    renderer.running = false
  }
}