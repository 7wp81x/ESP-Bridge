# ESP-Bridge

Framed binary USB bridge protocol for talking to an ESP32 from Termux —
or any Linux host — **no root required**.

This is the transport/USB layer shared by two separate tools:

- **[NRSuite](https://github.com/7wp81x/NRSuite)** — Wi-Fi scanning/sniffing, BLE HID, USB mass storage, and BadUSB over the framed CMD/RESP/EVENT protocol.
- **[Termux-ESP-Flasher](https://github.com/7wp81x/Termux-ESP-Flasher)** (`nrflash`) — a Termux-native `esptool.py` replacement that flashes ESP32/ESP8266 firmware with no root and no pyserial, using this package's fd-wrapping and endpoint discovery instead of a vendored `usb_device.py`.

It doesn't know anything about Wi-Fi, packet capture, HID keyboards, or
bootloader protocols — it just gets bytes reliably between an ESP32 and
a host process over USB (native USB CDC or a UART bridge chip), with
automatic root/no-root backend detection and USB permission handling.
Use it to build your own ESP32-to-Termux tools instead of solving the
no-root USB problem from scratch.

Pairs with the [`BridgeProtocol`](https://github.com/7wp81x/BridgeProtocol)
PlatformIO library on the firmware side — install both and the two speak
the same CMD/RESP/EVENT frames out of the box. (Tools that don't need the
framed protocol — like `nrflash`, which speaks the ROM bootloader's own
SLIP framing instead — still use this package for the USB backend
detection, fd wrapping, and UART-bridge register access, just not the
`Protocol`/frame layer.)

## Install

```bash
pip install espbridge
```
or
```bash
git clone https://github.com/7wp81x/ESP-Bridge
cd ESP-Bridge
pip install -e .
```

On stock Termux you'll also need:
```bash
pkg install python termux-api libusb
```
plus the **Termux:API** app from F-Droid (not the Play Store version).

You normally won't `pip install espbridge` directly — both NRSuite and
`nrflash` declare it as a dependency and pull it in automatically (or
auto-install it on first run if missing). Install it by hand only if
you're building your own tool on top of it, vendoring offline, or
debugging the package itself.

## Quick start

See [`examples/echo_cmd.py`](examples/echo_cmd.py) for a full runnable
example. The short version:

```python
import os, espbridge as eb

backend = eb.detect_backend()          # "termux" or "root"
device  = eb.wrap_fd(int(os.environ["TERMUX_USB_FD"])) if backend == "termux" \
          else eb.wrap_direct()

ep_in, ep_out, iface = eb.get_cdc_endpoints(device)
eb.claim_device(device, iface, fd_wrapped=(backend == "termux"))
eb.reset_endpoint_toggles(device, ep_in, ep_out)

sender   = eb.Sender(device, ep_out)
receiver = eb.ReceiverThread(device, ep_in); receiver.start()
proto    = eb.Protocol(sender, receiver);    proto.start()

resp = proto.send_cmd("PING")          # blocks until RESP or timeout
print(resp)
```

## Frame format

```
[MAGIC 2B: 0xAD 0xDE][TYPE 1B][ID 1B][LENGTH 4B LE][PAYLOAD NB]
```

| Type  | Hex  | Direction     | Payload                              |
|-------|------|---------------|---------------------------------------|
| CMD   | 0x01 | Host → ESP32  | JSON `{"cmd": "...", "args": {...}}` |
| RESP  | 0x02 | ESP32 → Host  | JSON response, matched by frame ID    |
| EVENT | 0x03 | ESP32 → Host  | Async JSON, id=0                      |
| PCAP  | 0x04 | ESP32 → Host  | Raw binary chunk (id = chunk index) — despite the name, use this for any binary stream |
| ACK   | 0x05 | Host → ESP32  | JSON `{"chunk": N}` — flow control    |
| HTML  | 0x06 | Host → ESP32  | Chunked raw payload upload            |

Commands (`CMD`) are entirely up to you — define whatever `cmd` strings and
`args` your firmware understands. This library only handles framing,
transport, and request/response matching.

This frame format is what NRSuite uses end-to-end. `nrflash` doesn't use
it at all — the ROM bootloader it talks to has its own fixed SLIP-based
protocol, so `nrflash` only pulls in the USB backend/endpoint pieces
below, not `Protocol`.

## What's included

- `protocol.py` — frame builder/parser, `Protocol` class (send_cmd/on_event/on_pcap)
- `sender.py` — thread-safe bulk OUT writer with retry
- `receiver.py` — background bulk IN reader thread
- `usb_device.py` — root vs. no-root backend detection, permission flow, endpoint discovery, native-CDC control interface handling, UART-bridge line-coding setup

Not every consumer uses every piece — see "Used by" above for which
tool relies on which parts.

## Used by

| Project | Uses |
|---|---|
| [NRSuite](https://github.com/7wp81x/NRSuite) | Full stack — `Protocol`, `Sender`, `ReceiverThread`, and all of `usb_device.py` |
| [Termux-ESP-Flasher](https://github.com/7wp81x/Termux-ESP-Flasher) (`nrflash`) | `usb_device.py` only — backend detection, fd wrapping, endpoint discovery, and UART-bridge register access; brings its own SLIP/ROM-bootloader protocol on top instead of `Protocol` |

If you're building something similar — anything that needs to talk to
an ESP32 (or generic USB-CDC/UART-bridge device) from Termux without
root — this package is meant to be the reusable base rather than
something you fork per project.

## License

MIT
