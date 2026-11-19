"""Disassemble part of the firmware: python tools/emu/disasm.py <real address> [instructions]"""
import struct
import sys
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs

sys.path.insert(0, str(Path(__file__).parent))
import boot  # noqa: E402

image = boot.DEFAULT_IMAGE.read_bytes()
start = int(sys.argv[1], 16)
count = int(sys.argv[2]) if len(sys.argv) > 2 else 40
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
offset, shown = start - boot.APP, 0
while shown < count and offset < len(image):
    ins = next(md.disasm(image[offset:offset + 4], offset + boot.APP), None)
    if ins is None:
        print(f"  {offset + boot.APP:#07x}: .word {struct.unpack_from('<H', image, offset)[0]:#06x}")
        offset += 2
    else:
        note = ""
        if ins.mnemonic.startswith("ldr") and "[pc" in ins.op_str:          # show the literal it loads
            try:
                disp = int(ins.op_str.split("#")[1].rstrip("]"), 0)
                target = ((ins.address + 4) & ~3) + disp
                value = struct.unpack_from("<I", image, target - boot.APP)[0]
                note = f"   ; = {value:#010x} {boot.register_name(value) if value >= 0x40000000 else ''}"
            except (IndexError, ValueError, struct.error):
                pass
        print(f"  {ins.address:#07x}: {ins.mnemonic:8} {ins.op_str}{note}")
        offset += ins.size
    shown += 1
