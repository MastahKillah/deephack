"""Dump USB descriptors of the MYSTIQUE (3633:0009). Read-only, does not claim the interface."""
import libusb_package
import usb.core
import usb.util

dev = libusb_package.find(idVendor=0x3633, idProduct=0x0009)
if dev is None:
    raise SystemExit("MYSTIQUE not found")

print(f"bcdUSB={dev.bcdUSB:#06x} bcdDevice={dev.bcdDevice:#06x} class={dev.bDeviceClass:#04x} "
      f"maxPacket0={dev.bMaxPacketSize0} configs={dev.bNumConfigurations} speed={dev.speed}")
for cfg in dev:
    print(f"config {cfg.bConfigurationValue}: interfaces={cfg.bNumInterfaces} maxPower={cfg.bMaxPower * 2}mA")
    for intf in cfg:
        print(f"  interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting}: "
              f"class={intf.bInterfaceClass:#04x} sub={intf.bInterfaceSubClass:#04x} proto={intf.bInterfaceProtocol:#04x}")
        for ep in intf:
            d = "IN " if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
            t = {0: "control", 1: "iso", 2: "bulk", 3: "interrupt"}[usb.util.endpoint_type(ep.bmAttributes)]
            print(f"    ep {ep.bEndpointAddress:#04x} {d} {t} maxPacket={ep.wMaxPacketSize} interval={ep.bInterval}")
