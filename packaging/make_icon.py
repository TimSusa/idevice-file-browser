"""Render the app icon (blue rounded square with 'iD') as a 1024x1024 PNG.

Usage:  QT_QPA_PLATFORM=offscreen python packaging/make_icon.py [out.png]
Convert to .icns afterwards with sips + iconutil (see build_app.sh).
"""
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QGuiApplication, QImage,
                           QLinearGradient, QPainter)

app = QGuiApplication(sys.argv[:1])
S = 1024
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)

p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)

grad = QLinearGradient(0, 0, 0, S)
grad.setColorAt(0, QColor("#4a86ff"))
grad.setColorAt(1, QColor("#1c46b8"))
p.setBrush(QBrush(grad))
p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(56, 56, S - 112, S - 112), 220, 220)

p.setPen(QColor("white"))
font = QFont(".AppleSystemUIFont", 430)
font.setWeight(QFont.Bold)
p.setFont(font)
p.drawText(QRectF(0, 24, S, S), Qt.AlignCenter, "iD")
p.end()

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/idevice_icon_1024.png"
assert img.save(out), "failed to save icon"
print("saved", out)
