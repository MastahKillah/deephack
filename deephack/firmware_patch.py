"""The direct drawing patch for the MYSTIQUE's firmware v2.70, as the bytes it changes.

Made by `python tools/fw/patch.py --table` from the patch's source there, which is what to read (and
what tests/test_firmware.py compares this with): do not edit. deephack.firmware applies it to DeepCool's
own image, which is not part of deephack.
"""
VERSION = 2
CHANGES = {
    1: "the first: draws a JPEG from USB. Answers 0 where the version is now",
    2: "frames are also shown while lv_task_handler does not return (#22); the reply has the version",
}
STOCK_SHA256 = "df9c19b5ca87879bad559da9bb00f2560e1e9330eba44b89a549590414017e6e"      # MYSTIQUE_2.70.bin
STOCK_SIZE = 451096
SHA256 = "161cdbd8b8441a60c3bed17ef05aab0b969aec365a90a579b95f177b979bedcb"            # the patched image
JUMPS = {                 # offset in the file: the four bytes there afterwards
    0x029a4: "6bf038fc",
    0x029a8: "6bf086fc",
    0x56088: "18f01ef9",
    0x21158: "4df0c0f8",
    0x26c54: "47f092bb",
}
CODE = bytes.fromhex(     # goes after the image's last byte
    "012101704ff20000c2f2000000212422043a8150fcd1704770b54ff20004c2f200046078d0b10425605990b940f21000"
    "c0f20200e0f7bcf858b148f20101c5f20701016000214160c16000f110018160605104350c2de7d10020607020780228"
    "1dd140f23270c2f20000008840f60e11884212d1a07880f00100a07004eb80014968e068486041f27400c2f200000068"
    "dcf744fd206a013020620020207070bd10b5fff7b9ffbde81040e7f7e5bd000010b50446fff7b0ff2046bde81040bcf7"
    "c1bd0000202801d0b8f750bd70b54ff20004c2f2000440f27463c2f20003d3f80310b3f807301222d9b1b1f5003f18d8"
    "1122606898b1a06888b12078022810d0e160002020616061a36147f2dc10c2f200000068e06101202070102201e00120"
    "6070154640f2a464c2f2000420463021adf702fd42f6556020802020a070e5700220207144f64810c4f24430c4f82a00"
    "05f2bd10e085012021463022bde87040b8f788ba4ff20002c2f200021378012b3ad147f2dc13c2f200031b68d5695b1b"
    "b3f5fa6f2ed80878d36815695b1b984288bf1846937883f0010302eb83035b6810332b4405441561556930b114f8017b"
    "03f8017b3d440138f8d155611069d368984206d19069adb20223a84218bf0023137049f21050c4f20000042101600821"
    "0160b0bd002313702078aa2801d0b8f7d2bbb8f726bc0000"
)
