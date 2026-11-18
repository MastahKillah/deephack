"""Dissect a captured upload: header fields, reassembled payload, checksum hunt."""
import json, sys, zlib, binascii, struct, glob, os
from pathlib import Path

CAPTURES = Path(__file__).resolve().parent.parent / "captures"

start = int(sys.argv[1])
ev = [json.loads(l) for l in open(CAPTURES / "session.jsonl", encoding="utf8")]
ups = [e for e in ev[start:] if e["k"] in ("out", "in") and e["ep"] in (0x01, 0x81)]

def hx(b, w=16):
    return "\n".join(f"  {i:04x}  " + " ".join(f"{x:02x}" for x in b[i:i + w]) + "   " + "".join(chr(x) if 32 <= x < 127 else "." for x in b[i:i + w]) for i in range(0, len(b), w))

hdr = None; data = bytearray(); fin = None; ins = []
state = "pre"
for e in ups:
    b = bytes.fromhex(e["hex"])
    st = e.get("st", "")
    if e["k"] == "in":
        ins.append((state, len(data), b)); continue
    if state == "pre" and b[:4] == b"DCLd":
        hdr = b; state = "data"; continue
    if state == "data":
        if "finishDownload" in st:
            fin = b; state = "done"; continue
        if "uploadChunk" in st:
            data += b
if hdr is None:
    sys.exit("no upload found")
print("HEADER", len(hdr)); print(hx(hdr))
print("FINISH", len(fin)); print(hx(fin))
print("payload bytes:", len(data), "starts", data[:4].hex(), "ends", data[-2:].hex())
print("IN packets on ep81 during transfer:", [(s, off, b[:8].hex()) for s, off, b in ins][:10])

size_le = struct.unpack_from("<I", hdr, 5)[0]
f2 = struct.unpack_from("<I", hdr, 9)[0]
print(f"hdr[4]={hdr[4]}  u32@5={size_le} ({size_le:#x})  u32@9={f2} ({f2:#x})")

fid = hdr[20:52].decode("ascii", "replace")
print("file id:", fid)
cands = glob.glob(os.path.join(os.environ["APPDATA"], "DeepCool", "Pictures", "*", "*", fid, "*"))
for c in cands:
    blob = open(c, "rb").read()
    print(f"  {os.path.basename(c)} size={len(blob)} equal_to_payload={blob == bytes(data)}")

d = bytes(data)
checks = {
    "sum8": sum(d) & 0xFF, "sum16": sum(d) & 0xFFFF, "sum32": sum(d) & 0xFFFFFFFF,
    "crc32": zlib.crc32(d), "crc16-xmodem/ccitt(0)": binascii.crc_hqx(d, 0), "crc16-ccitt-false(ffff)": binascii.crc_hqx(d, 0xFFFF),
    "adler32": zlib.adler32(d),
}
def crc16_arc(data, init=0x0000, poly=0xA001):
    c = init
    for x in data:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
    return c
checks["crc16-arc"] = crc16_arc(d)
checks["crc16-modbus"] = crc16_arc(d, 0xFFFF)
checks["crc16-kermit"] = crc16_arc(d, 0, 0x8408)
checks["crc16-x25"] = crc16_arc(d, 0xFFFF, 0x8408) ^ 0xFFFF
for k, v in checks.items():
    print(f"  {k:26s} {v:#010x} {'<== MATCH' if v == f2 else ''}")
open(CAPTURES / "upload_payload.jpg", "wb").write(d)
