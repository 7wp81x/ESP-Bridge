# ESP-Bridge

Framed binary USB bridge protocol for talking to an ESP32 from Termux — or
any Linux host — **no root required**.

This is the transport layer extracted from [NRSuite](https://github.com/7wp81x/NRSuite).
It doesn't know anything about Wi-Fi, packet capture, or any specific
use case — it just gets JSON commands and binary chunks reliably between
an ESP32 and a host process over USB CDC, with automatic root/no-root
backend detection. Use it to build your own ESP32-to-Termux tools.

Pairs with the [`BridgeProtocol`](https://github.com/7wp81x/BridgeProtocol) PlatformIO library on
the firmware side — install both and the two speak the same frames out of
the box.

## Install

```bash
pip install espbridge          # once published
# or, for local development:
git clone https://github.com/7wp81x/ESP-Bridge
cd ESP-Bridge
pip install -e .
```

On stock Termux you'll also need:
```bash
pkg install python termux-api libusb
```
plus the **Termux:API** app from F-Droid (not the Play Store version).

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

## What's included

- `protocol.py` — frame builder/parser, `Protocol` class (send_cmd/on_event/on_pcap)
- `sender.py` — thread-safe bulk OUT writer with retry
- `receiver.py` — background bulk IN reader thread
- `usb_device.py` — root vs. no-root backend detection, permission flow, endpoint discovery

## License

MIT
