#!/usr/bin/env python3
"""
ODrive QML GUI - Velocity control focused interface for ODrive Axis 0.

The UI is declared in QML (qml/main.qml); all device logic lives in the
GuiBackend QObject (backend.py), exposed to QML as the context property
`backend`. This module only bootstraps the engine and the backend.
"""

import argparse
import logging
import os
import signal
import sys

# Make the ODrive tools package (tools/odrive) importable when running
# directly from the QtGUI directory without prior installation.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools", "odrive", "pyfibre"))

import odrive  # noqa: F401  (import side effects: register types / discovery)
from PySide6.QtCore import QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from backend import GuiBackend


def main():
    parser = argparse.ArgumentParser(description="ODrive QML GUI - Axis 0")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG-level logging at startup")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    QQuickStyle.setStyle("Fusion")  # desktop Qt Quick Controls style (matches the widget Fusion look)

    backend = GuiBackend(verbose=args.verbose)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    qml_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "qml")
    engine.load(QUrl.fromLocalFile(os.path.join(qml_dir, "main.qml")))
    if not engine.rootObjects():
        sys.exit(1)

    # Ctrl+C should terminate the UI reliably (see ARCHITECTURE.md).
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
