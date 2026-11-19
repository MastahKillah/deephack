"""The SWM341's USB device controller, as far as the MYSTIQUE's firmware uses it.

Worked out from the firmware's interrupt handler (IRQ 77, at 0x34AEC in v2.70) and the register names
of the swm341-pac crate; there is no public description of this controller, so this is "what makes the
firmware's own driver work", not a reference model.

    host side                           what the firmware sees
    reset()                             DEVIF.RST
    configure()                         DEVIF.SETCFG; the handler arms OUT endpoints 1 and 2
    setup(bytes of a SETUP packet)      SETUPD1/SETUPD2, SETUPSR (and with it DEVIF.SETUP)
    send(endpoint, data)                the packet at RXBUF, its size and endpoint in RXSR, EPIF.OUTEPn
    take(endpoint)                      the next packet the firmware queued on an IN endpoint

An OUT packet is handed over only when the firmware has armed that endpoint again (RXCR.FFRDY), which
is how the real controller holds the host off with NAKs while the firmware is busy.
"""
from __future__ import annotations

import collections
import struct

IRQ = 77
TXBUF, RXBUF = 0x0000, 0x4000                 # TXBUF of endpoint n is at 0x400 * n
INEP, OUTEP = 0x4400, 0x4500                  # 0x10 apart: TXCR, TXSR, -, TXTRSZ / RXCR
DEVCR, DEVSR, DEVIF, DEVIE, EPIE, RXSR, SETUPSR, EPIF = 0x4600, 0x4604, 0x4608, 0x460C, 0x4614, 0x461C, 0x4620, 0x4624
SETUPD1, SETUPD2 = 0x4700, 0x4704
FLUSH, READY = 4, 8                           # TXCR / RXCR
SUCC, DONE = 1, 2                             # RXSR / SETUPSR; TXSR has SUCC, DATSNT (2) and NAKSNT (8)
IF_SETCFG, IF_RST, IF_SETUP = 1, 8, 0x40


class UsbDevice:
    size = 0x5000

    def __init__(self, base: int):
        self.base = base
        self.mem = bytearray(self.size)
        self.devif = self.rxsr = self.setupsr = 0
        self.txsr = [0] * 16
        self.ready: set[int] = set()                              # OUT endpoints the firmware has armed
        self.events: collections.deque = collections.deque()      # what the host does next
        self.sent: list[tuple[int, bytes]] = []
        self.delivered = 0

    # ---- the host
    def reset(self) -> None:
        self.events.append(("reset",))

    def configure(self) -> None:
        self.events.append(("configure",))

    def setup(self, packet: bytes) -> None:
        assert len(packet) == 8
        self.events.append(("setup", bytes(packet)))

    def send(self, endpoint: int, data: bytes) -> None:
        self.events.append(("out", endpoint, bytes(data)))

    def take(self, endpoint: int) -> bytes | None:
        """The oldest packet the firmware has sent on an IN endpoint and the host has not read yet."""
        for i, (ep, data) in enumerate(self.sent):
            if ep == endpoint:
                del self.sent[i]
                return data
        return None

    # ---- the machine
    def _epif(self) -> int:
        flags = sum(1 << n for n in range(16) if self.txsr[n] & 0xA)
        if self.rxsr & DONE:
            flags |= 1 << (16 + ((self.rxsr >> 16) & 0xF))
        return flags

    def _devif(self) -> int:
        """RST and SETCFG are cleared by writing them; SETUP stands for as long as SETUPSR.DONE does
        (the firmware's handler clears only that)."""
        return self.devif | (IF_SETUP if self.setupsr & DONE else 0)

    def _word(self, offset: int) -> int:
        return struct.unpack_from("<I", self.mem, offset)[0]

    def interrupt(self) -> bool:
        """Start the next thing the host does when nothing is waiting to be handled; True when the
        controller's interrupt line is up."""
        if not self._devif() and not self._epif() and self.events:
            event = self.events[0]
            if event[0] == "out" and event[1] not in self.ready:
                return False                                      # NAK: the firmware is not ready for it
            self.events.popleft()
            self.delivered += 1
            if event[0] == "reset":
                self.devif |= IF_RST
            elif event[0] == "configure":
                struct.pack_into("<I", self.mem, DEVSR, 1)        # configuration 1
                self.devif |= IF_SETCFG
            elif event[0] == "setup":
                self.mem[SETUPD1:SETUPD1 + 8] = event[1]
                self.setupsr = SUCC | DONE
            else:
                _, endpoint, data = event
                self.mem[RXBUF:RXBUF + len(data)] = data
                self.rxsr = len(data) << 22 | endpoint << 16 | SUCC | DONE
                self.ready.discard(endpoint)
        return bool(self._devif() & self._word(DEVIE) or self._epif() & self._word(EPIE))

    def read(self, offset: int) -> int:
        if offset == DEVIF:
            return self._devif()
        if offset == RXSR:
            return self.rxsr
        if offset == SETUPSR:
            return self.setupsr
        if offset == EPIF:
            return self._epif()
        if INEP <= offset < OUTEP and offset & 0xF == 4:
            return self.txsr[(offset - INEP) >> 4]
        if INEP <= offset < DEVCR and offset & 0xF == 0:
            return 0                                              # TXCR / RXCR: the command bits clear themselves
        return self._word(offset)

    def write(self, offset: int, value: int, size: int) -> None:
        self.mem[offset:offset + size] = (value & (1 << 8 * size) - 1).to_bytes(size, "little")
        if offset == DEVIF:
            self.devif &= ~value
        elif offset == RXSR:
            self.rxsr &= ~(value & DONE)
        elif offset == SETUPSR:
            self.setupsr &= ~(value & DONE)
        elif INEP <= offset < OUTEP:
            n, register = (offset - INEP) >> 4, offset & 0xF
            if register == 4:
                self.txsr[n] &= ~value
            elif register == 0 and value & READY:                 # the firmware has filled TXBUF: the host takes it
                length = self._word(INEP + 0x10 * n + 0xC) & 0x7FF
                self.sent.append((n, bytes(self.mem[TXBUF + 0x400 * n:TXBUF + 0x400 * n + length])))
                self.txsr[n] = SUCC | 2
        elif OUTEP <= offset < DEVCR and offset & 0xF == 0 and value & READY:
            self.ready.add((offset - OUTEP) >> 4)
