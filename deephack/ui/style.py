"""Colours and the application style sheet."""

BG = "#12131a"
SIDEBAR = "#181a23"
CARD = "#1d1f2b"
CARD_HOVER = "#232636"
BORDER = "#2a2d3d"
TEXT = "#eceef5"
MUTED = "#8d91a6"
ACCENT = "#7c6cff"
ACCENT_SOFT = "#2a2750"
OK = "#3ecf8e"
WARN = "#f5b84a"
ERR = "#f0616d"

ICON_FONT = "Segoe Fluent Icons"

QSS = f"""
* {{ font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 10pt; color: {TEXT}; }}
QMainWindow, #content {{ background: {BG}; }}
#sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
#brand {{ font-family: "Segoe UI Variable Display", "Segoe UI"; font-size: 17pt; font-weight: 700; }}
#brandSub, #muted {{ color: {MUTED}; }}
#section {{ color: {MUTED}; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }}
#h1 {{ font-family: "Segoe UI Variable Display", "Segoe UI"; font-size: 15pt; font-weight: 600; }}
#hint {{ color: {MUTED}; font-size: 9pt; }}
#caption {{ color: {MUTED}; font-size: 9pt; }}
#nowLine {{ font-size: 10.5pt; font-weight: 600; }}

QPushButton#modeButton {{
    background: transparent; border: 1px solid transparent; border-radius: 10px; text-align: left; padding: 0;
}}
QPushButton#modeButton:hover {{ background: {CARD_HOVER}; }}
QPushButton#modeButton:checked {{ background: {ACCENT_SOFT}; border: 1px solid {ACCENT}; }}
#modeGlyph {{ font-family: "{ICON_FONT}", "Segoe MDL2 Assets"; font-size: 15pt; color: {TEXT}; }}
#modeTitle {{ font-weight: 600; }}
#modeDesc {{ color: {MUTED}; font-size: 8.5pt; }}

#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
#banner {{ background: #3a2c12; border: 1px solid #6b5220; border-radius: 10px; }}
#banner QLabel {{ color: #ffdf9e; }}

QPushButton {{
    background: {CARD_HOVER}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {ACCENT_SOFT}; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #8e80ff; }}

QLineEdit, QComboBox {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 10px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox::down-arrow {{ image: url("@ARROW@"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_SOFT}; outline: none; padding: 4px;
}}
QMenu {{ background: {CARD}; border: 1px solid {BORDER}; padding: 6px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QToolTip {{ background: {CARD}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}
"""
