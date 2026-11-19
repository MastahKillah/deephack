"""An SD card and the SWM341's SD host controller, for the firmware emulator.

The controller follows the standard SD host register layout (SDHCI):

    0x00 DMA_MEM_ADDR   0x04 BLK (size, count << 16)   0x08 ARG   0x0C CMD   0x10..0x1C RESP
    0x20 DATA   0x24 STAT   0x28 CR1   0x2C CR2 (clock, resets)   0x30 IF (write 1 to clear)   0x34 IM   0x38 IE

Writing CMD runs the command at once: the response is in RESP and the flags in IF before the write
returns. Data moves either through the DATA port, four bytes a time, or straight to and from memory at
DMA_MEM_ADDR when the command asks for DMA.

The card is a high-capacity SD card whose blocks live in a dict and can be saved as a raw image, to be
looked at with ordinary tools (or mounted). Blocks never written read as zero.
"""
from __future__ import annotations

import struct
from pathlib import Path

BLOCK = 512
# IF bits
CMD_DONE, TRX_DONE, BUF_WR_RDY, BUF_RD_RDY, ERROR, CMD_TIMEOUT = 1 << 0, 1 << 1, 1 << 4, 1 << 5, 1 << 15, 1 << 16
# STAT bits
STAT_BUF_WR, STAT_BUF_RD, STAT_CARD = 1 << 10, 1 << 11, (1 << 16) | (1 << 17) | (1 << 18) | (0xF << 20) | (1 << 24)


class SdCard:
    def __init__(self, megabytes: int = 256, image: Path | None = None):
        self.megabytes = megabytes
        self.blocks: dict[int, bytes] = {}
        self.image = image
        self.dirty = False
        if image and image.exists():
            self.load(image.read_bytes())
        self.rca = 0x1234
        self.app = False                     # the next command is an application command (after CMD55)
        self.log: list[tuple[str, int]] = []

    # 128-bit registers, as the 120 bits a host controller shows (the CRC byte is dropped)
    @property
    def cid(self) -> int:
        raw = bytes([0x03, 0x53, 0x44]) + b"EMU01" + bytes([0x10, 0xDE, 0xE9, 0x4A, 0xC0, 0x01, 0x4B, 0x01])
        return int.from_bytes(raw, "big") >> 8

    @property
    def csd(self) -> int:
        c_size = self.megabytes * 2 - 1                           # units of 512 KiB
        raw = bytes([0x40, 0x0E, 0x00, 0x32, 0x5B, 0x59, 0x00, (c_size >> 16) & 0x3F, (c_size >> 8) & 0xFF, c_size & 0xFF,
                     0x7F, 0x80, 0x0A, 0x40, 0x00, 0x01])
        return int.from_bytes(raw, "big") >> 8

    def read(self, lba: int, count: int) -> bytes:
        return b"".join(self.blocks.get(lba + i, bytes(BLOCK)) for i in range(count))

    def write(self, lba: int, data: bytes) -> None:
        for i in range(len(data) // BLOCK):
            self.blocks[lba + i] = bytes(data[i * BLOCK:(i + 1) * BLOCK])
        self.dirty = True

    def load(self, raw: bytes) -> None:
        """Replace the card's contents (only the blocks that hold something are kept)."""
        self.blocks = {}
        for lba in range(len(raw) // BLOCK):
            chunk = raw[lba * BLOCK:(lba + 1) * BLOCK]
            if chunk.count(0) != BLOCK:
                self.blocks[lba] = chunk

    def save(self) -> None:
        if self.image is None or not self.dirty:
            return
        top = max(self.blocks) + 1 if self.blocks else 0
        raw = bytearray(top * BLOCK)
        for lba, chunk in self.blocks.items():
            raw[lba * BLOCK:(lba + 1) * BLOCK] = chunk
        self.image.write_bytes(raw)

    def command(self, index: int, arg: int):
        """Run a command. Returns (response words or None for "no answer", data to read or None,
        bytes the host will send or 0)."""
        app, self.app = self.app, False
        name = f"ACMD{index}" if app else f"CMD{index}"
        self.log.append((name, arg))
        status = 0x900                                            # ready for data, transfer state
        if app:
            if index == 41:                                       # power up done, high capacity
                return [0xC0FF8000], None, 0
            if index == 6 or index == 23 or index == 42:
                return [status], None, 0
            if index == 51:                                       # SCR: SD 2.0, 1 and 4 bit bus
                return [status], bytes([0x02, 0x35, 0x80, 0x00, 0, 0, 0, 0]), 0
            if index == 13:                                       # SD status
                return [status], bytes(64), 0
            return None, None, 0
        if index == 0:
            return [], None, 0
        if index == 8:
            return [arg & 0xFFF], None, 0
        if index == 55:
            self.app = True
            return [status | 0x20], None, 0
        if index in (2, 10):
            return [(self.cid >> (32 * i)) & 0xFFFFFFFF for i in range(4)], None, 0
        if index == 9:
            return [(self.csd >> (32 * i)) & 0xFFFFFFFF for i in range(4)], None, 0
        if index == 3:
            return [self.rca << 16 | 0x500], None, 0
        if index in (7, 12, 13, 16, 23, 32, 33, 38):
            return [status], None, 0
        if index == 6:                                            # switch function: whatever was asked is granted
            reply = bytearray(64)
            reply[0:2] = b"\x00\x64"
            reply[13] = 0x03
            reply[16] = arg & 0x0F
            return [status], bytes(reply), 0
        if index in (17, 18):
            return [status], ("read", arg), 0
        if index in (24, 25):
            return [status], None, ("write", arg)
        return None, None, 0                                      # anything else: the card stays silent


class SdHost:
    """The host controller's registers. `memory` reads and writes the machine's RAM, for DMA."""
    size = 0x100

    def __init__(self, base: int, card: SdCard, memory):
        self.base, self.card, self.memory = base, card, memory
        self.reg = {offset: 0 for offset in range(0, 0x40, 4)}
        self.rx = bytearray()                # data waiting to be read through the DATA port
        self.tx: bytearray | None = None     # data being collected from the DATA port
        self.tx_target = (0, 0)              # (first block, bytes expected)
        self.transfers: list[tuple[str, int, int]] = []

    def read(self, offset: int) -> int:
        if offset == 0x20:                                        # DATA port
            word, self.rx = bytes(self.rx[:4]).ljust(4, b"\0"), self.rx[4:]
            if not self.rx:
                self.reg[0x30] = (self.reg[0x30] & ~BUF_RD_RDY) | TRX_DONE
            return struct.unpack("<I", word)[0]
        if offset == 0x24:
            return STAT_CARD | (STAT_BUF_RD if self.rx else 0) | (STAT_BUF_WR if self.tx is not None else 0)
        if offset == 0x2C:                                        # clock stable once on; resets finish at once
            value = self.reg[0x2C] & ~(7 << 24)
            return value | 2 if value & 1 else value
        return self.reg.get(offset, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        if size != 4:                                             # byte and half-word writes into a register
            shift = (offset & 3) * 8
            mask = ((1 << (size * 8)) - 1) << shift
            offset &= ~3
            if offset == 0x30:
                value = value << shift                            # write-1-to-clear: only these bits
            else:
                value = (self.reg.get(offset, 0) & ~mask) | ((value << shift) & mask)
        if offset == 0x30:
            self.reg[0x30] &= ~value
            if self.rx:
                self.reg[0x30] |= BUF_RD_RDY                      # more data is waiting
            if self.tx is not None:
                self.reg[0x30] |= BUF_WR_RDY
        elif offset == 0x20:
            if self.tx is not None:
                self.tx += struct.pack("<I", value)
                first, expected = self.tx_target
                if len(self.tx) >= expected:
                    self.card.write(first, bytes(self.tx[:expected]))
                    self.tx = None
                    self.reg[0x30] = (self.reg[0x30] & ~BUF_WR_RDY) | TRX_DONE
        elif offset == 0x0C:
            self.reg[0x0C] = value
            self._command(value)
        else:
            self.reg[offset] = value

    def _command(self, cmd: int) -> None:
        index, has_data, dma = (cmd >> 24) & 0x3F, bool(cmd & (1 << 21)), bool(cmd & 1)
        block_size = self.reg[0x04] & 0xFFF
        count = (self.reg[0x04] >> 16) if cmd & 0x22 else 1       # block count enabled or multi block
        count = max(1, count)
        response, read, write = self.card.command(index, self.reg[0x08])
        if response is None:
            self.reg[0x30] |= ERROR | CMD_TIMEOUT
            return
        for i, word in enumerate(response[:4]):
            self.reg[0x10 + 4 * i] = word
        flags = CMD_DONE
        if isinstance(read, tuple):                               # block read
            data = self.card.read(read[1], count)
            self.transfers.append(("read", read[1], count))
        else:
            data = read
        if data is not None and has_data:
            if dma:
                self.memory(self.reg[0x00], data)
                flags |= TRX_DONE
            else:
                self.rx = bytearray(data)
                flags |= BUF_RD_RDY
        elif write and has_data:
            expected = count * (block_size or BLOCK)
            self.transfers.append(("write", write[1], count))
            if dma:
                self.card.write(write[1], self.memory(self.reg[0x00], expected))
                flags |= TRX_DONE
            else:
                self.tx, self.tx_target = bytearray(), (write[1], expected)
                flags |= BUF_WR_RDY
        elif (cmd >> 16) & 3 == 3:                                # a "busy" response also ends the transfer
            flags |= TRX_DONE
        self.reg[0x30] |= flags
