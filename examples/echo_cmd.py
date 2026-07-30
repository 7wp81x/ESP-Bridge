#!/usr/bin/env python3
"""
echo_cmd.py - Minimal example: connect to an ESP32 running the matching
BridgeProtocol firmware (see the EchoCmd.ino example on the ESP32 side),
send a PING command, and print the response.

Run with:
    python3 echo_cmd.py

On stock Termux (no root) this script re-execs itself once Android grants
USB permission, so run it as a normal script — don't try to import it
interactively for the first connection.
"""

import os
import sys
import espbridge as eb


def connect():
    backend = eb.detect_backend()
    print(f"[*] USB backend: {backend}")

    if backend == "termux":
        if "TERMUX_USB_FD" not in os.environ:
            # First run: find the device, ask Android for permission, and
            # re-exec this same script with TERMUX_USB_FD set.
            device_path = eb.auto_detect_device()
            print(f"[*] Found device: {device_path}")
            eb.open_usb_device(device_path, f"{sys.executable} {os.path.abspath(__file__)}")
            return None  # unreachable — open_usb_device() execs a new process

        fd = int(os.environ["TERMUX_USB_FD"])
        device = eb.wrap_fd(fd)
        fd_wrapped = True
    else:
        device = eb.wrap_direct()
        fd_wrapped = False

    print(f"[*] Device: {eb.describe_device(device)}")

    ep_in, ep_out, iface = eb.get_cdc_endpoints(device)
    eb.claim_device(device, iface, fd_wrapped=fd_wrapped)
    eb.reset_endpoint_toggles(device, ep_in, ep_out)

    if eb.is_native_cdc(device):
        # Required on ESP32-S2/S3 native-USB boards: firmware using
        # `while (!Serial) {}` blocks in setup() until DTR is asserted.
        ctrl_iface = eb.find_cdc_control_interface(device, iface)
        eb.open_native_cdc_port(device, ctrl_iface)

    return device, ep_in, ep_out


def main():
    result = connect()
    if result is None:
        return  # re-exec'd, this process is done
    device, ep_in, ep_out = result

    sender = eb.Sender(device, ep_out)
    receiver = eb.ReceiverThread(device, ep_in)
    receiver.start()

    proto = eb.Protocol(sender, receiver)
    proto.on_event = lambda ev: print(f"[event] {ev}")
    proto.start()

    print("[*] Sending PING...")
    resp = proto.send_cmd("PING", timeout=5.0)
    print(f"[*] Response: {resp}")

    proto.stop()
    receiver.stop()


if __name__ == "__main__":
    main()
