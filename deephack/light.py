"""The colours of the 14 lights on the pump head, picked from the picture on screen.

The lights sit along the screen's two long sides, seven each (device.light_packet has their order). The
cooler's own firmware gives each the colour of one pixel, 5 pixels in from the edge, and sends it as it
is: a letter or a thin line there decides a light, a dark margin switches it off, and a new colour is a
jump. With the direct drawing firmware (patch 16) deephack says what the lights show, and the cooler fades
to it. What each light gets here:

* **A share of the whole edge.** A strip a quarter of the picture wide along each side, weighted towards
  the edge, and along the side a light's own seventh with some of its neighbours', so that the seven run
  into each other as the picture does.
* **The colour that is there, not the grey around it.** Every bit of the strip counts by how much colour
  it has (in light, not in pixel values): a coloured shape on a dark page gives its colour, white text on
  it only some white. A picture without colour gives white.
* **A level the eye follows.** How bright a light is goes by how bright its strip is, lifted for dark
  pictures (LIFT), which would leave the lights all but off: these LEDs are linear, a screen is not.

All of it on a picture reduced to 6 x 21 cells first, so it costs well under a millisecond.

cooler_colours is what DeepCool's firmware picks, for the window's preview of a cooler that picks itself.

A mode may say what the lights show itself (Mode.lights, issue #57): `led`, `meter` and `arrange` are for
that.
"""
from __future__ import annotations

from functools import lru_cache

from PIL import Image

from .device import LIGHTS

SIDE = LIGHTS // 2             # lights on one side
DEPTH = 0.25                   # how far into the picture a light looks, as a share of its width
COLUMNS, ROWS = 3, 21          # cells a strip is reduced to: across (from the edge inwards) and along the side
ACROSS = (1.0, 0.6, 0.3)       # weight of a cell by how far in it is
REACH = 0.75                   # how far along the side a light looks, in sevenths of it, either way from its middle
GAMMA = 2.2                    # from pixel values to light
GREY = 0.08                    # what a cell without colour counts, for a cell of one full colour as bright: 1
LIFT = 0.5                     # level = brightness ** LIFT: 1 would be true to the picture and dark for most pages
DEEPEN = 1.4                   # the two weaker channels are taken to this power: more of the colour, less white
DARK = 0.0015                  # a strip darker than this (as light, 0 to 1) is black: no glow from an empty screen
_TO_LIGHT = tuple((v / 255) ** GAMMA for v in range(256))
COOLER_PIXELS = ((5, 635), (5, 505), (5, 405), (5, 305), (5, 205), (5, 105), (5, 5),             # the pixel of a
                 (475, 5), (475, 105), (475, 205), (475, 305), (475, 405), (475, 505), (475, 635))  # 480 x 640 picture
                                                # that DeepCool's firmware gives each light (docs/firmware-analysis.md)


@lru_cache(maxsize=1)
def _along() -> tuple[tuple[tuple[int, float], ...], ...]:
    """For each of a side's seven lights, top to bottom: the rows of cells it looks at, and how much."""
    out = []
    for light in range(SIDE):
        middle = (light + 0.5) / SIDE
        rows = []
        for row in range(ROWS):
            away = abs((row + 0.5) / ROWS - middle) * SIDE          # in sevenths of the side
            if away < REACH:
                rows.append((row, 1 - away / REACH))
        out.append(tuple(rows))
    return tuple(out)


def _side(cells, mirrored: bool) -> list[tuple[float, float, float]]:
    """The seven colours of one side, top to bottom, as light (0 to 1). `cells`: the strip's pixels as
    bytes, R G B each, row by row; `mirrored`: its first column is the one furthest from the edge (the
    right side's)."""
    across = ACROSS[::-1] if mirrored else ACROSS
    prepared = []                                   # per cell: its light, how much colour it has, how bright it is
    for i in range(COLUMNS * ROWS):
        r, g, b = _TO_LIGHT[cells[3 * i]], _TO_LIGHT[cells[3 * i + 1]], _TO_LIGHT[cells[3 * i + 2]]
        high = max(r, g, b)
        prepared.append((r, g, b, (high - min(r, g, b) + GREY * high + 1e-6) * across[i % COLUMNS], high * across[i % COLUMNS]))
    lights = []
    for rows in _along():
        red = green = blue = weight = bright = share = 0.0
        for row, near in rows:
            for column in range(COLUMNS):
                r, g, b, colour, high = prepared[row * COLUMNS + column]
                colour *= near
                red, green, blue, weight = red + r * colour, green + g * colour, blue + b * colour, weight + colour
                bright, share = bright + high * near, share + across[column] * near
        bright /= share
        top = max(red, green, blue)
        if bright < DARK or top <= 0:
            lights.append((0.0, 0.0, 0.0))
            continue
        level = bright ** LIFT
        lights.append(tuple(level * (c / top) ** (DEEPEN if c < top else 1) for c in (red, green, blue)))
    return lights


def led(colour, power: float = 1.0) -> tuple[int, int, int]:
    """A colour as a picture has it, as what a light shows at `power` (0 to 1) of what it can: the colour
    alone says which colour, not how bright. Deepened as the picked ones are."""
    r, g, b = (_TO_LIGHT[max(0, min(255, round(c)))] for c in colour)
    top = max(r, g, b)
    if top <= 0 or power <= 0:
        return 0, 0, 0
    return tuple(round(255 * min(1.0, power) * (c / top) ** (DEEPEN if c < top else 1)) for c in (r, g, b))


def meter(level: float, colours, power: float = 1.0) -> list[tuple[int, int, int]]:
    """A side's lights as a level meter, bottom to top: as many of them lit as `level` (0 to 1) of their
    number, the topmost of those as much as is left of it. `colours`: theirs, bottom to top."""
    lit = max(0.0, min(1.0, level)) * len(colours)
    return [led(colour, power * max(0.0, min(1.0, lit - i))) for i, colour in enumerate(colours)]


def arrange(left, right) -> list[tuple[int, int, int]]:
    """The 14 lights in the cooler's order from the two sides' seven, each given bottom to top."""
    left, right = list(left), list(right)
    if len(left) != SIDE or len(right) != SIDE:
        raise ValueError(f"a side has {SIDE} lights")
    return left + right[::-1]


def cooler_colours(picture: Image.Image) -> list[tuple[int, int, int]]:
    """What the cooler's own firmware has the 14 lights show for this picture: one pixel each, as its frame
    buffer holds it (5, 6 and 5 bits), sent to the light as it is."""
    rgb = picture if picture.mode == "RGB" else picture.convert("RGB")
    width, height = rgb.size
    picked = []
    for x, y in COOLER_PIXELS:
        r, g, b = rgb.getpixel((min(width - 1, x * width // 480), min(height - 1, y * height // 640)))
        picked.append((r & 0xF8, g & 0xFC, b & 0xF8))
    return picked


def colours(picture: Image.Image, level: float = 1.0) -> list[tuple[int, int, int]]:
    """What the 14 lights are to show for this picture, in the cooler's order, 0 to 255 each. `level`: all
    of them that much dimmer (0 to 1)."""
    rgb = picture if picture.mode == "RGB" else picture.convert("RGB")
    width, height = rgb.size
    deep = max(COLUMNS, round(width * DEPTH))
    left = _side(rgb.resize((COLUMNS, ROWS), Image.BOX, box=(0, 0, deep, height)).tobytes(), False)
    right = _side(rgb.resize((COLUMNS, ROWS), Image.BOX, box=(width - deep, 0, width, height)).tobytes(), True)
    scale = 255 * max(0.0, min(1.0, level))
    return [tuple(round(c * scale) for c in light) for light in left[::-1] + right]      # up the left, down the right
