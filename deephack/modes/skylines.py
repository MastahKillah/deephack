"""The cities the Scene mode can show: a made-up one, and a few real ones by their landmarks.

A city is what stands still in the scene: far buildings in the haze, near ones with windows, and
**landmarks**. A landmark is a small picture in *materials*, not colours (stone, shade, roof, glass, a
lamp ...): the scene gives every material its colour for the light of the moment, so the same cathedral
is pale by day, a floodlit shape at night and grey under clouds. Windows on a landmark come on with the
city's other windows, and a mast's light blinks with the others.

Everything is on the scene's small canvas, 120 x 160, four real pixels to one. At that size a landmark
is its outline and two or three things everybody knows it by. They are drawn from memory of what they
look like, not from plans: recognisable is the aim, not right.

To add a city: a function that returns a `City`, and its name in `CITIES`.
"""
from __future__ import annotations

import random
import unicodedata
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageOps

LW, LH = 120, 160
STREET = 142                                   # top of the pavement
FAR_BASE = 132                                 # where the far buildings stand
WARM = (255, 212, 120)

# What a material looks like by day. A capital and its small letter are the same thing in light and in shade.
DAY = {
    "S": (214, 208, 192), "s": (166, 160, 150),          # pale stone
    "H": (198, 170, 118), "h": (150, 124, 86),           # honey-coloured stone
    "W": (236, 236, 232), "w": (186, 190, 198),          # white paint, steel
    "D": (72, 78, 94), "Z": (122, 134, 152),             # a dark roof, a zinc one
    "B": (140, 86, 68), "b": (104, 66, 56),              # brick
    "E": (112, 92, 78), "e": (84, 68, 60),               # iron
    "g": (104, 142, 178), "q": (154, 186, 214), "k": (52, 62, 82),      # glass: plain, catching the light, dark
    "R": (212, 74, 52), "G": (226, 184, 84),             # red paint, gold
    "T": (62, 112, 74), "t": (44, 86, 60),               # trees
    "M": (112, 132, 172), "F": (240, 244, 250),          # a mountain far off, and the snow on it
    "K": (34, 36, 50),                                   # an opening: a door, a bell chamber
    "o": (60, 68, 90),                                   # a window (lit ones are drawn over it)
    "L": (236, 232, 214),                                # a lamp or a clock face: glows at night
    "N": (206, 100, 150), "C": (96, 186, 204),           # signs: glow at night
}
GLOW = {"L": (255, 236, 170), "N": (255, 112, 184), "C": (112, 232, 242)}      # at night, whatever else is dark
GILT = {"E": (236, 176, 80), "e": (188, 130, 58), "R": (255, 132, 60), "W": (250, 240, 214)}    # under floodlights: iron is
                                                         # gold, a red tower orange, white paint bright
UNLIT = frozenset("TtMFKok")                             # what no floodlight is pointed at


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Art:
    """A landmark being drawn: a little canvas whose pixels are materials."""

    def __init__(self, width: int, height: int):
        self.img = Image.new("L", (width, height), 0)
        self.d = ImageDraw.Draw(self.img)
        self.beacons: list[tuple[int, int]] = []

    def box(self, x0, y0, x1, y1, material: str) -> None:
        self.d.rectangle([x0, y0, x1, y1], fill=ord(material))

    def dot(self, x, y, material: str) -> None:
        self.d.point((x, y), fill=ord(material))

    def line(self, points, material: str) -> None:
        self.d.line(points, fill=ord(material))

    def poly(self, points, material: str) -> None:
        self.d.polygon(points, fill=ord(material))

    def dome(self, x0, top, x1, base, material: str) -> None:
        """The upper half of an ellipse, standing on row `base`."""
        self.d.chord([x0, top, x1, base + (base - top)], 180, 360, fill=ord(material))

    def ring(self, box, material: str) -> None:
        self.d.ellipse(box, outline=ord(material))

    def hole(self, x0, y0, x1, y1) -> None:
        self.d.rectangle([x0, y0, x1, y1], fill=0)

    def windows(self, x0, y0, x1, y1, dx: int = 2, dy: int = 3, tall: int = 1) -> None:
        """Windows in rows and columns, wherever there is a wall for one: none hang in the air beside a
        tower that narrows."""
        for y in range(y0, y1 + 1, dy):
            for x in range(x0, x1 + 1, dx):
                if all(0 <= x < self.img.width and 0 <= y + i < self.img.height and self.img.getpixel((x, y + i))
                       and 0 < x < self.img.width - 1 and self.img.getpixel((x - 1, y + i)) and self.img.getpixel((x + 1, y + i))
                       for i in range(tall)):
                    self.box(x, y, x, y + tall - 1, "o")

    def swap(self, y0, y1, old: str, new: str) -> None:
        """In those rows, one material for another: bands of paint."""
        for y in range(y0, y1 + 1):
            for x in range(self.img.width):
                if self.img.getpixel((x, y)) == ord(old):
                    self.img.putpixel((x, y), ord(new))

    def mirrored(self) -> "Art":
        other = Art(*self.img.size)
        other.img = ImageOps.mirror(self.img)
        other.d = ImageDraw.Draw(other.img)
        other.beacons = [(self.img.width - 1 - x, y) for x, y in self.beacons]
        return other


@dataclass(frozen=True)
class Mark:
    """A landmark where it stands: its left column `x`, and `base`, the row its bottom row is on."""
    x: int
    base: int
    layer: str                                   # "back": behind the far buildings. "far": in front of them.
                                                 # "near": in front of the near ones, on the street
    depth: float                                 # how much of the haze is over it, 0 to 1
    flood: bool                                  # lit at night
    masks: tuple                                 # (material, a mask of where it is) for each it is made of
    dots: tuple                                  # its windows: (x, y, how likely it is lit), on the canvas
    beacons: tuple                               # lights on its masts: (x, y), on the canvas
    tops: tuple                                  # where snow lies on it: (x, y), on the canvas
    size: tuple


def place(art: Art, x: int, base: int = STREET - 1, layer: str = "near", depth: float = 0.0, flood: bool = True) -> Mark:
    img = art.img
    top = base - img.height + 1
    data = img.load()
    used = sorted({data[i, j] for i in range(img.width) for j in range(img.height)} - {0})
    masks = tuple((chr(code), img.point(lambda v, code=code: 255 if v == code else 0).convert("1")) for code in used)
    dots = tuple((x + i, top + j, random.Random((x + i) * 991 + (top + j) * 7).random())
                 for j in range(img.height) for i in range(img.width) if data[i, j] == ord("o"))
    tops = []
    for i in range(img.width):
        j = next((j for j in range(img.height) if data[i, j]), None)
        if j is not None and chr(data[i, j]) not in "TtMF":
            tops.append((x + i, top + j))
    return Mark(x, base, layer, depth, flood, masks, dots, tuple((x + i, top + j) for i, j in art.beacons),
                tuple(tops), img.size)


def tones(mark: Mark, dark: float, over: float, silhouette: tuple, haze: tuple, grey: tuple) -> dict[str, tuple]:
    """Every material's colour on this landmark right now. `dark`: 0 by day to 1 at night. `over`: how
    overcast. `silhouette`: what the scene's own buildings look like. `haze`: the colour of the distance."""
    out, night = {}, min(1.0, dark * 1.2)
    for material, day in DAY.items():
        lit = _mix(_mix(day, grey, over * 0.45), silhouette, 0.16)           # by day, a little of the scene's dusk in it
        if material in GLOW:
            after = GLOW[material]
        elif mark.flood and material in GILT:
            after = GILT[material]
        elif mark.flood and material not in UNLIT:
            after = _mix(tuple(round(c * 0.62) for c in _mix(day, WARM, 0.22)), silhouette, 0.1)
        else:
            after = _mix(day, silhouette, 0.86)
        colour = _mix(lit, after, night)
        haze_now = mark.depth * (0.55 if material in GLOW or (mark.flood and material in GILT and night > 0.5) else 1.0)
        out[material] = _mix(colour, haze, haze_now)
    return out


def paint(img: Image.Image, mark: Mark, colours: dict[str, tuple]) -> None:
    corner = (mark.x, mark.base - mark.size[1] + 1)
    for material, mask in mark.masks:
        img.paste(colours[material], corner, mask)


def blocks(seed: int, base: int, low: int, high: int, narrow: int, wide: int, gaps=()) -> list[tuple[int, int, int, int]]:
    """A row of plain buildings from the left edge to the right, the same for the same `seed`. None of
    them stands in a gap ((first column, last column)): that is where a landmark is."""
    rng, x, out = random.Random(seed), -random.Random(seed).randrange(0, 6), []
    while x < LW:
        w, h = rng.randrange(narrow, wide), rng.randrange(low, high)
        if not any(x <= last and x + w >= first for first, last in gaps):
            out.append((x, base - h, x + w, base))
        x += w + rng.randrange(0, 3)
    return out


def windows_of(near) -> list[tuple[int, int, float]]:
    return [(x, y, random.Random(x * 1000 + y).random())              # (x, y, how likely it is lit)
            for x0, y0, x1, y1 in near for x in range(x0 + 2, x1 - 2, 4) for y in range(y0 + 4, y1 - 4, 5)]


def lights_of(far) -> list[tuple[int, int, float]]:
    return [(x, y, random.Random(x * 77 + y).random())
            for x0, y0, x1, y1 in far for x in range(x0 + 2, x1 - 1, 3) for y in range(y0 + 3, y1 - 2, 4)]


@dataclass(frozen=True)
class City:
    id: str
    name: str                                    # as the settings show it
    known_as: tuple                              # names the Weather page's city may have, without accents, small
    far: tuple                                   # plain buildings in the haze: (left, top, right, bottom)
    near: tuple                                  # plain buildings with windows
    marks: tuple = ()
    masts: tuple = ()                            # on plain buildings: (x, top, bottom), a red light on top
    lamps: tuple = (14, 56, 98)                  # where the street lamps stand
    windows: tuple = field(default=())
    far_lights: tuple = field(default=())

    @property
    def dots(self) -> tuple:
        return tuple(dot for mark in self.marks for dot in mark.dots)


def city(id: str, name: str, known_as, far, near, marks=(), masts=(), lamps=(14, 56, 98)) -> City:
    return City(id, name, tuple(known_as), tuple(far), tuple(near), tuple(marks), tuple(masts), tuple(lamps),
                tuple(windows_of(near)), tuple(lights_of(far)))


# ---------------------------------------------------------------- the made-up city (the scene's first)
def anywhere() -> City:
    far, near = blocks(4, FAR_BASE, 26, 66, 8, 15), blocks(11, STREET, 16, 50, 11, 21)
    tallest = min(near, key=lambda b: b[1])
    x = (tallest[0] + tallest[2]) // 2
    return city("anywhere", "A city of its own", (), far, near, masts=((x, tallest[1] - 8, tallest[1]),))


# ---------------------------------------------------------------- shared pieces
def tree(width: int = 9, height: int = 12) -> Art:
    a = Art(width, height)
    a.box(width // 2, height - 4, width // 2, height - 1, "b")
    a.d.ellipse([0, 0, width - 1, height - 4], fill=ord("T"))
    a.d.ellipse([width // 3, (height - 4) // 3, width - 1, height - 4], fill=ord("t"))
    return a


def slab(width: int, height: int, material: str = "k", glint: str = "g") -> Art:
    """A plain modern tower: dark glass with a lighter edge."""
    a = Art(width, height)
    a.box(0, 0, width - 1, height - 1, material)
    a.box(0, 0, 0, height - 1, glint)
    a.windows(2, 2, width - 2, height - 3, 2, 4)
    return a


# ---------------------------------------------------------------- Chișinău
def _arch() -> Art:
    """The Triumphal Arch: a white cube with a passage through it, columns at its corners, and a clock
    on the block that sits on top."""
    a = Art(21, 24)
    a.box(1, 9, 19, 23, "S")
    a.box(0, 8, 20, 9, "s")                      # the cornice
    for x in (3, 4, 16, 17):
        a.box(x, 10, x, 21, "s")
    for x in (2, 5, 15, 18):                     # columns, in pairs
        a.box(x, 10, x, 21, "W")
    a.box(0, 22, 20, 23, "s")
    a.box(5, 2, 15, 7, "S")                      # the upper block
    a.box(4, 1, 16, 1, "s")
    a.box(6, 0, 14, 0, "S")
    a.box(9, 3, 11, 5, "L")                      # the clock
    a.dot(10, 4, "K")
    a.box(7, 13, 13, 23, "s")                    # the passage: one can see through it
    a.hole(8, 13, 12, 23)
    a.hole(9, 12, 11, 12)
    a.box(8, 12, 8, 12, "s")
    a.box(12, 12, 12, 12, "s")
    return a


def _bell_tower() -> Art:
    """The cathedral's bell tower: four tiers, each narrower, a small dome and a cross."""
    a = Art(11, 44)
    a.box(1, 30, 9, 43, "S")
    a.box(0, 29, 10, 30, "s")
    a.box(4, 36, 6, 43, "K")
    a.dot(5, 35, "K")
    a.box(2, 19, 8, 28, "S")
    a.box(1, 18, 9, 19, "s")
    a.box(4, 22, 6, 26, "K")
    a.dot(5, 21, "K")
    a.box(3, 10, 7, 17, "S")
    a.box(2, 9, 8, 10, "s")
    a.box(5, 12, 5, 15, "K")                     # the bells
    a.box(3, 6, 7, 8, "D")
    a.box(4, 4, 6, 5, "D")
    a.dot(5, 3, "D")
    a.box(5, 0, 5, 2, "G")
    a.box(4, 1, 6, 1, "G")
    return a


def _cathedral() -> Art:
    """The Nativity Cathedral: a pale block, a porch of six columns under a pediment, and one large
    dark dome on a drum, with a cross."""
    a = Art(43, 42)
    a.dome(13, 5, 29, 15, "D")
    a.box(20, 3, 22, 5, "D")                     # the lantern
    a.box(21, 0, 21, 3, "G")
    a.box(20, 1, 22, 1, "G")
    a.box(14, 16, 28, 22, "S")                   # the drum
    a.box(13, 15, 29, 16, "s")
    for x in (16, 19, 23, 26):
        a.box(x, 18, x, 21, "K")
    a.box(2, 26, 40, 41, "S")                    # the body
    a.box(1, 25, 41, 26, "s")
    a.box(5, 30, 6, 35, "K")
    a.box(36, 30, 37, 35, "K")
    a.box(11, 27, 31, 41, "s")                   # the porch
    for x in (12, 15, 18, 24, 27, 30):
        a.box(x, 28, x, 39, "W")
    a.box(20, 32, 22, 39, "K")
    a.poly([(21, 19), (9, 26), (33, 26)], "S")   # the pediment
    a.line([(9, 26), (21, 19), (33, 26), (9, 26)], "s")
    a.box(0, 40, 42, 41, "s")                    # the steps
    return a


def _romanita() -> Art:
    """Romanița: a round tower on a thin stem, ringed with balconies, wider where it begins and ends."""
    a = Art(9, 46)
    a.box(3, 32, 5, 45, "s")
    a.box(2, 9, 6, 31, "S")
    for y in range(10, 31, 3):
        a.box(1, y, 7, y, "s")
    a.windows(2, 11, 6, 30, 2, 3)
    a.box(0, 7, 8, 9, "s")
    a.box(0, 29, 8, 31, "s")
    a.box(2, 3, 6, 6, "S")
    a.box(4, 0, 4, 2, "D")
    a.beacons.append((4, 0))
    return a


def _gate() -> Art:
    """One of the two City Gates: a white block of flats in steps, the highest next to the road."""
    a = Art(17, 28)
    for i, top in enumerate((22, 16, 11, 5, 0)):
        a.box(i * 3, top, i * 3 + 2, 27, "S")
        a.box(i * 3, top, i * 3 + 2, top, "s")
    a.box(15, 0, 16, 27, "S")                    # the tall end is a little wider than a step
    a.box(15, 0, 16, 0, "s")
    a.box(16, 1, 16, 27, "s")
    a.windows(1, 2, 15, 26, 2, 3)
    return a


def chisinau() -> City:
    marks = [
        place(_gate(), 84, FAR_BASE, "far", 0.42, flood=False),
        place(_gate().mirrored(), 103, FAR_BASE, "far", 0.42, flood=False),
        place(_romanita(), 1, FAR_BASE, "far", 0.36, flood=False),
        place(tree(9, 12), -2), place(tree(10, 13), 34), place(tree(8, 11), 100), place(tree(10, 13), 110),
        place(_arch(), 11), place(_bell_tower(), 40), place(_cathedral(), 53),
    ]
    far = blocks(31, FAR_BASE, 12, 30, 14, 26, gaps=((0, 11), (82, LW)))
    return city("chisinau", "Chișinău", ("chisinau", "kishinev", "kishinau"), far, (), marks, lamps=(7, 49, 98))


# ---------------------------------------------------------------- Paris
def _eiffel() -> Art:
    """The Eiffel Tower: four legs that lean in to a first platform with an arch between them, meet
    above the second, and go up as one thin shaft to the top."""
    a, c = Art(37, 92), 18
    a.box(c, 0, c, 7, "e")
    a.box(c - 1, 8, c + 1, 10, "E")
    a.poly([(c - 1, 11), (c + 1, 11), (c + 4, 55), (c - 4, 55)], "E")
    a.box(c - 2, 14, c + 2, 15, "e")             # the third platform
    a.box(c - 6, 56, c + 6, 57, "e")             # the second
    a.poly([(c - 5, 58), (c - 2, 58), (c - 7, 72), (c - 11, 72)], "E")
    a.poly([(c + 2, 58), (c + 5, 58), (c + 11, 72), (c + 7, 72)], "E")
    for y in (62, 67):
        a.line([(c - 7, y), (c + 7, y)], "e")
    a.box(c - 13, 73, c + 13, 75, "e")           # the first
    a.poly([(c - 12, 76), (c - 7, 76), (c - 13, 91), (c - 18, 91)], "E")
    a.poly([(c + 7, 76), (c + 12, 76), (c + 18, 91), (c + 13, 91)], "E")
    a.d.arc([c - 10, 76, c + 10, 100], 180, 360, fill=ord("e"))
    a.beacons.append((c, 0))
    return a


def _haussmann(width: int, height: int = 28, shop: str = "K") -> Art:
    """A Paris block of flats: cream stone, long balconies, a grey zinc roof with dormers and chimneys."""
    a = Art(width, height + 3)
    top = 3
    a.box(1, top + 1, width - 2, top + 5, "Z")
    a.box(2, top, width - 3, top, "Z")
    for x in range(4, width - 3, 4):
        a.dot(x, top + 3, "o")
    for x in (3, width - 5):
        a.box(x, 0, x + 1, top, "B")
    a.box(0, top + 6, width - 1, height + 2, "S")
    a.box(0, top + 6, width - 1, top + 6, "s")
    for row in (top + 8, top + 12, top + 16):
        a.windows(2, row, width - 3, row, 3, 9, tall=2)
    a.line([(0, top + 11), (width - 1, top + 11)], "e")      # the balcony that runs the whole front
    a.box(0, height - 3, width - 1, height + 2, "s")
    for x in range(2, width - 3, 5):
        a.box(x, height - 2, x + 2, height + 2, shop)
    a.box(width - 1, top + 6, width - 1, height + 2, "s")
    return a


def _sacre_coeur() -> Art:
    """Sacré-Cœur: a tall white dome, small ones beside it, and the bell tower behind."""
    a = Art(17, 18)
    a.box(1, 9, 11, 17, "W")
    a.dome(3, 1, 9, 8, "W")
    a.box(5, 0, 7, 2, "W")
    a.dome(0, 6, 3, 9, "W")
    a.dome(9, 6, 12, 9, "W")
    a.box(13, 3, 15, 17, "w")
    a.dot(14, 2, "w")
    a.box(5, 12, 7, 17, "K")
    return a


def paris() -> City:
    marks = [
        place(_sacre_coeur(), 3, 114, "far", 0.5),
        place(slab(8, 50), 105, FAR_BASE, "far", 0.5, flood=False),          # the Montparnasse tower
        place(_eiffel(), 43, 119, "far", 0.18),
    ]
    x = -3
    for width, height, shop in ((23, 23, "K"), (27, 25, "R"), (21, 22, "K"), (29, 24, "b"), (25, 23, "K")):
        marks.append(place(_haussmann(width, height, shop), x, flood=False))
        x += width
    return city("paris", "Paris", ("paris",), (), (), marks, lamps=(21, 68, 113))


# ---------------------------------------------------------------- London
def _big_ben() -> Art:
    """The Elizabeth Tower: a tall square shaft, a clock face on every side, and a dark pointed roof."""
    a = Art(11, 68)
    a.poly([(5, 0), (2, 12), (8, 12)], "D")
    a.box(5, 0, 5, 2, "G")
    a.box(3, 8, 7, 9, "Z")
    a.box(1, 13, 9, 16, "H")                     # where the bells hang
    for x in (3, 5, 7):
        a.box(x, 14, x, 15, "K")
    a.box(0, 12, 0, 16, "h")
    a.box(10, 12, 10, 16, "h")
    a.box(0, 17, 10, 26, "H")                    # the clock
    a.d.ellipse([2, 18, 8, 24], fill=ord("L"))
    a.box(5, 19, 5, 21, "K")
    a.dot(6, 21, "K")
    a.box(0, 27, 10, 27, "h")
    a.box(1, 28, 9, 67, "H")
    for x in (3, 5, 7):
        a.box(x, 29, x, 67, "h")
    return a


def _parliament() -> Art:
    """The Palace of Westminster: one long front of honey-coloured stone, upright lines, small spires."""
    a = Art(58, 30)
    a.box(0, 10, 57, 29, "H")
    a.box(0, 7, 57, 9, "D")
    for x in range(1, 58, 4):                    # the buttresses, each ending in a pinnacle
        a.box(x, 5, x, 29, "h")
        a.dot(x, 4, "h")
    for row in (13, 18, 23):
        a.windows(3, row, 56, row, 4, 9, tall=3)
    a.poly([(28, 0), (26, 7), (30, 7)], "h")     # the spire in the middle
    return a


def _victoria() -> Art:
    """The Victoria Tower: square and heavy, a turret at each corner, a flag."""
    a = Art(13, 48)
    a.box(1, 10, 11, 47, "H")
    for x in (0, 12):
        a.box(x, 6, x, 47, "h")
        a.dot(x, 5, "h")
    a.box(1, 9, 11, 9, "h")
    for row in (13, 20, 27, 34):
        a.windows(3, row, 9, row, 3, 9, tall=4)
    a.box(6, 0, 6, 9, "D")
    a.box(7, 1, 9, 3, "R")
    return a


def _eye() -> Art:
    """The London Eye: a great thin wheel on two legs, with its capsules round the rim."""
    a = Art(39, 44)
    a.ring([0, 0, 38, 38], "W")
    for dx, dy in ((19, 0), (0, 19), (13, 13), (13, -13)):
        a.line([(19 - dx, 19 - dy), (19 + dx, 19 + dy)], "w")
    a.ring([0, 0, 38, 38], "W")
    for x, y in ((19, 0), (9, 3), (3, 9), (0, 19), (3, 29), (9, 35), (19, 38), (29, 35), (35, 29), (38, 19), (35, 9), (29, 3)):
        a.dot(x, y, "C")
    a.line([(19, 19), (10, 43)], "W")
    a.line([(19, 19), (28, 43)], "W")
    a.box(18, 18, 20, 20, "W")
    return a


def _shard() -> Art:
    """The Shard: a spike of glass, lighter on the side the sky is on."""
    a = Art(15, 84)
    a.poly([(7, 0), (8, 0), (14, 83), (0, 83)], "g")
    a.poly([(7, 0), (7, 0), (8, 83), (0, 83)], "q")
    a.hole(8, 0, 8, 4)
    a.windows(3, 30, 12, 80, 3, 6)
    a.beacons.append((7, 0))
    return a


def _gherkin() -> Art:
    """30 St Mary Axe: a glass bullet with bands that wind round it."""
    a = Art(13, 46)
    a.d.ellipse([0, 0, 12, 78], fill=ord("g"))
    for y in range(6, 60, 14):                   # ... which catch the light
        a.line([(1, y + 12), (11, y)], "q")
    a.dome(4, 0, 8, 3, "q")
    return a


def london() -> City:
    marks = [
        place(_shard(), 20, FAR_BASE, "far", 0.42, flood=False),
        place(_gherkin(), 44, FAR_BASE, "far", 0.42, flood=False),
        place(_eye(), 84, 128, "far", 0.25),
        place(_victoria(), 1), place(_parliament(), 14), place(_big_ben(), 72),
        place(tree(9, 12), 86), place(tree(10, 13), 108),
    ]
    far = blocks(77, FAR_BASE, 20, 44, 9, 16, gaps=((18, 36), (42, 58), (82, LW)))
    return city("london", "London", ("london",), far, (), marks, lamps=(98,))


# ---------------------------------------------------------------- New York
def _empire() -> Art:
    """The Empire State Building: a shaft that steps in as it rises, and a mast on top."""
    a = Art(21, 100)
    a.box(10, 0, 10, 9, "w")
    a.box(9, 10, 11, 15, "S")
    a.box(8, 16, 12, 19, "S")
    a.box(7, 20, 13, 27, "S")
    a.box(5, 28, 15, 55, "S")
    a.box(3, 56, 17, 77, "S")
    a.box(1, 78, 19, 99, "S")
    for x0, x1, y0 in ((5, 15, 28), (3, 17, 56), (1, 19, 78)):
        a.box(x0, y0, x0, 99, "s")
        a.box(x1, y0, x1, 99, "s")
    a.box(10, 20, 10, 99, "s")
    a.windows(6, 30, 14, 54, 2, 3)
    a.windows(4, 58, 16, 76, 2, 3)
    a.windows(2, 80, 18, 98, 2, 3)
    a.beacons.append((10, 0))
    return a


def _chrysler() -> Art:
    """The Chrysler Building: a steel crown of arches that narrows into a needle."""
    a = Art(13, 90)
    a.box(6, 0, 6, 15, "w")
    a.poly([(6, 12), (2, 34), (10, 34)], "W")
    for x, y in ((6, 19), (5, 23), (7, 23), (4, 27), (6, 27), (8, 27), (3, 31), (5, 31), (7, 31), (9, 31)):
        a.dot(x, y, "k")                         # its triangular windows
    a.box(2, 35, 10, 89, "S")
    a.box(1, 60, 11, 89, "S")
    a.box(0, 78, 12, 89, "s")
    a.box(1, 35, 1, 36, "w")
    a.box(11, 35, 11, 36, "w")
    a.windows(3, 37, 9, 76, 2, 3)
    a.beacons.append((6, 0))
    return a


def _one_wtc() -> Art:
    """One World Trade Center: glass in long triangles, a spire."""
    a = Art(15, 106)
    a.box(7, 0, 7, 17, "w")
    a.box(5, 16, 9, 19, "k")
    a.poly([(4, 20), (10, 20), (14, 105), (0, 105)], "g")
    a.poly([(4, 20), (10, 20), (7, 96)], "q")
    a.box(0, 96, 14, 105, "k")
    a.beacons.append((7, 0))
    return a


def _water_tower() -> Art:
    """A wooden water tank on a roof: a barrel under a pointed lid, on legs."""
    a = Art(5, 8)
    a.dot(2, 0, "b")
    a.box(1, 1, 3, 1, "b")
    a.box(0, 2, 4, 5, "B")
    a.box(0, 3, 4, 3, "b")
    a.box(0, 6, 0, 7, "b")
    a.box(4, 6, 4, 7, "b")
    return a


def new_york() -> City:
    near = blocks(23, STREET, 18, 46, 11, 19)
    marks = [
        place(_one_wtc(), 4, FAR_BASE, "far", 0.45), place(_empire(), 47, FAR_BASE, "far", 0.26),
        place(_chrysler(), 88, FAR_BASE, "far", 0.36),
    ]
    marks += [place(_water_tower(), x0 + 2, top - 1, flood=False) for i, (x0, top, x1, _) in enumerate(near)
              if i % 2 == 0 and x1 - x0 >= 12 and x0 >= 0]
    far = blocks(8, FAR_BASE, 30, 72, 8, 15, gaps=((2, 20), (45, 69), (86, 102)))
    return city("new_york", "New York", ("new york", "new york city", "manhattan", "nyc"), far, near, marks)


# ---------------------------------------------------------------- Tokyo
def _fuji() -> Art:
    """Mount Fuji: one wide cone, snow down its top."""
    a = Art(96, 48)
    a.poly([(0, 47), (37, 6), (42, 1), (45, 0), (51, 0), (54, 1), (59, 6), (95, 47)], "M")
    a.poly([(37, 6), (42, 1), (45, 0), (51, 0), (54, 1), (59, 6), (64, 12), (60, 11), (57, 16), (53, 11), (48, 17),
            (44, 11), (40, 15), (37, 11), (32, 12)], "F")
    return a


def _tokyo_tower() -> Art:
    """Tokyo Tower: the Eiffel Tower's shape, thinner, in bands of red and white."""
    a, c = Art(27, 80), 13
    a.box(c, 0, c, 17, "R")
    a.poly([(c - 1, 18), (c + 1, 18), (c + 3, 44), (c - 3, 44)], "R")
    a.poly([(c - 3, 50), (c - 1, 50), (c - 9, 79), (c - 13, 79)], "R")
    a.poly([(c + 1, 50), (c + 3, 50), (c + 13, 79), (c + 9, 79)], "R")
    for y in (56, 63, 71):
        reach = 4 + (y - 50) // 3
        a.line([(c - reach, y), (c + reach, y)], "R")
    a.d.arc([c - 8, 66, c + 8, 92], 180, 360, fill=ord("R"))
    for y0, y1 in ((6, 11), (30, 36), (57, 62), (72, 76)):
        a.swap(y0, y1, "R", "W")
    a.box(c - 2, 25, c + 2, 27, "W")             # the top deck
    a.box(c - 4, 45, c + 4, 49, "W")             # the main deck
    a.box(c - 4, 47, c + 4, 47, "o")
    a.beacons.append((c, 0))
    return a


def _skytree() -> Art:
    """Tokyo Skytree: a pale needle, two decks near the top, a long mast."""
    a, c = Art(13, 112), 6
    a.box(c, 0, c, 25, "w")
    a.box(c - 1, 26, c + 1, 33, "W")
    a.box(c - 3, 34, c + 3, 37, "C")             # the upper deck
    a.box(c - 2, 38, c + 2, 41, "W")
    a.box(c - 4, 42, c + 4, 46, "C")             # the lower one
    a.poly([(c - 1, 47), (c + 1, 47), (c + 4, 111), (c - 4, 111)], "W")
    a.box(c, 47, c, 111, "w")
    a.beacons.append((c, 0))
    return a


def _sign(material: str, height: int = 7) -> Art:
    a = Art(2, height)
    a.box(0, 0, 1, height - 1, material)
    return a


def tokyo() -> City:
    near = blocks(41, STREET, 16, 42, 9, 17, gaps=((60, 96),))      # the tower stands in a park
    marks = [
        place(_fuji(), -10, FAR_BASE - 8, "back", 0.55, flood=False),
        place(_tokyo_tower(), 64, FAR_BASE + 4, "far", 0.14), place(_skytree(), 100, FAR_BASE, "far", 0.36),
        place(tree(9, 12), 61), place(tree(10, 13), 72), place(tree(8, 11), 84), place(tree(9, 12), 91),
    ]
    marks += [place(_sign("NC"[i % 2], 5 + i % 3 * 2), x0 + 1, top + 12, flood=False) for i, (x0, top, x1, foot) in enumerate(near)
              if i % 3 != 2 and foot - top > 20 and x0 >= 0]
    far = blocks(63, FAR_BASE, 14, 34, 8, 15, gaps=((98, 114),))
    return city("tokyo", "Tokyo", ("tokyo", "tokio"), far, near, marks, lamps=(14, 56, 79))


CITIES: tuple[City, ...] = (anywhere(), chisinau(), paris(), london(), new_york(), tokyo())
BY_ID = {c.id: c for c in CITIES}
ANYWHERE = CITIES[0]


def plain(text: str) -> str:
    """A name without accents and capitals: "Chișinău" as "chisinau"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower().strip()


def find(name: str) -> City:
    """The city a place's name means, or the made-up one."""
    name = plain(name).split(",")[0].strip()
    return next((c for c in CITIES if name and name in c.known_as), ANYWHERE)
