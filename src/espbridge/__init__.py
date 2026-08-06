"""
espbridge - Framed USB bridge protocol for ESP32 <-> Termux/Linux, no root required.

Typical usage:

    import espbridge as eb

    backend = eb.detect_backend()
    if backend == "termux":
        path = eb.auto_detect_device()
        eb.request_permission(path)
        # ... open_usb_device() re-execs your script with TERMUX_USB_FD set ...
        device = eb.wrap_fd(int(os.environ["TERMUX_USB_FD"]))
    else:
        device = eb.wrap_direct()

    ep_in, ep_out, iface = eb.get_cdc_endpoints(device)
    eb.claim_device(device, iface, fd_wrapped=(backend == "termux"))
    eb.reset_endpoint_toggles(device, ep_in, ep_out)

    if eb.is_native_cdc(device):
        # ESP32-S2/S3 native USB-CDC boards gate `Serial` on the host
        # asserting DTR (firmware using `while (!Serial) {}` never leaves
        # setup() otherwise). UART-bridge chips (CP2102/CH340/CH9102/FTDI)
        # don't need this - they're handled by init_uart_bridge() below.
        ctrl_iface = eb.find_cdc_control_interface(device, iface)
        eb.open_native_cdc_port(device, ctrl_iface)
    elif eb.is_uart_bridge(device):
        # UART-bridge chips must be programmed to 115200 8N1 and have DTR
        # asserted before anything will come back on the IN endpoint - skip
        # this and send_cmd()/PING will silently time out with no response.
        eb.init_uart_bridge(device)

    sender   = eb.Sender(device, ep_out)
    receiver = eb.ReceiverThread(device, ep_in)
    receiver.start()

    proto = eb.Protocol(sender, receiver)
    proto.start()

    resp = proto.send_cmd("PING")
    print(resp)   # -> {"ok": True, "msg": "pong"}  (if your firmware implements PING)

See examples/echo_cmd.py for a complete runnable version of the above, and
the companion BridgeProtocol PlatformIO library for the ESP32-side firmware
that speaks the same frames.
"""

from .protocol import (
    Protocol,
    FrameParser,
    build_frame,
    build_cmd,
    build_ack,
    build_html_frame,
    MAGIC,
    TYPE_CMD,
    TYPE_RESP,
    TYPE_EVENT,
    TYPE_PCAP,
    TYPE_ACK,
    TYPE_HTML,
    HEADER_SIZE,
    MAX_PAYLOAD,
)
from .sender import Sender
from .receiver import ReceiverThread
from .usb_device import (
    detect_backend,
    is_root,
    has_termux_api,
    list_usb_devices,
    request_permission,
    open_usb_device,
    auto_detect_device,
    launch_with_fd,
    relaunch_with_fd,
    wrap_fd,
    wrap_direct,
    find_usb_device_direct,
    claim_device,
    get_cdc_endpoints,
    reset_endpoint_toggles,
    describe_device,
    init_uart_bridge,
    set_uart_bridge_baud,
    is_uart_bridge,
    set_dtr_rts,
    is_native_cdc,
    find_cdc_control_interface,
    open_native_cdc_port,
    UART_BRIDGE_VIDPIDS,
    ESP32_KNOWN,
)

__all__ = [
    # protocol
    "Protocol", "FrameParser", "build_frame", "build_cmd", "build_ack", "build_html_frame",
    "MAGIC", "TYPE_CMD", "TYPE_RESP", "TYPE_EVENT", "TYPE_PCAP", "TYPE_ACK", "TYPE_HTML",
    "HEADER_SIZE", "MAX_PAYLOAD",
    # transport
    "Sender", "ReceiverThread",
    # device / backend
    "detect_backend", "is_root", "has_termux_api",
    "list_usb_devices", "request_permission", "open_usb_device", "auto_detect_device",
    "launch_with_fd", "relaunch_with_fd", "wrap_fd", "wrap_direct", "find_usb_device_direct",
    "claim_device", "get_cdc_endpoints", "reset_endpoint_toggles",
    "describe_device", "init_uart_bridge", "set_uart_bridge_baud", "is_uart_bridge",
    "set_dtr_rts", "is_native_cdc", "find_cdc_control_interface",
    "open_native_cdc_port", "UART_BRIDGE_VIDPIDS", "ESP32_KNOWN",
]

__version__ = "1.2.0"
__author__ = "7wp81x"
__url__ = "https://github.com/7wp81x/ESP-Bridge"