"""Statistics over the captured periodic sensor frames (command 0x01, endpoint 2) to work out the payload layout."""
import json
import sys
from pathlib import Path

CAPTURES = Path(__file__).resolve().parent.parent / "captures"
files = [Path(a) for a in sys.argv[1:]] or sorted(CAPTURES.glob("session*.jsonl"))
payloads = []
for f in files:
    for line in open(f, encoding="utf8"):
        e = json.loads(line)
        if e.get("k") == "out" and e.get("ep") == 2 and e["hex"].startswith("aa2e01"):
            payloads.append(bytes.fromhex(e["hex"])[3:42])
print(f"{len(payloads)} sensor frames from {len(files)} capture(s)")
if not payloads:
    sys.exit()
print("first:", payloads[0].hex(" "))
print("last: ", payloads[-1].hex(" "))
print("\nbyte  min  max  distinct  sample values")
for i in range(39):
    col = [p[i] for p in payloads]
    if max(col) == 0:
        continue
    vals = sorted(set(col))
    print(f"{i:4d}  {min(col):3d}  {max(col):3d}  {len(vals):8d}  {vals[:12]}")
print("\nu16 little-endian at each offset (min..max):")
for i in range(0, 26):
    col = [p[i] | p[i + 1] << 8 for p in payloads]
    if max(col):
        print(f"  @{i:2d}: {min(col):6d} .. {max(col):6d}")
