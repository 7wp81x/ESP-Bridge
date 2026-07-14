"""
usb_device.py - Auto-detect an ESP32 over USB and wrap it for libusb, with or
without root. Part of the `espbridge` library.

Supports two backends selected automatically at runtime:

  termux   - No-root Termux: all USB access goes through the Android USB host
             stack via termux-api Usb + TERMUX_USB_FD.  The two-process
             bootstrap dance (parent requests permission -> child inherits fd)
             is required.  Works on stock Android with Termux + Termux:API.

  root     - Running as uid 0 (Termux:Root, Nethunter terminal, plain Linux,
             etc.): libusb opens /dev/bus/usb directly - no termux-api
             binary, no Android permission dialog, no child process needed.

Call detect_backend() early in main() to pick the right path.
All other public helpers work the same regardless of backend.
"""

import os
import json
import ctypes
import usb.core
import usb.backend.libusb1 as libusb1

_TERMUX_API = "/data/data/com.termux/files/usr/libexec/termux-api"

# Known ESP32 native USB VID:PIDs
ESP32_KNOWN = {
    (0x303A, 0x1001): "ESP32 native USB CDC",
    (0x303A, 0x0002): "ESP32 native USB CDC",
    (0x10C4, 0xEA60): "CP2102 (ESP32 devboard)",
    (0x1A86, 0x7523): "CH340 (ESP32 devboard)",
    (0x1A86, 0x55D4): "CH9102 (ESP32 devboard)",
    (0x0403, 0x6001): "FTDI FT232 (ESP32 devboard)",
}


# Runtime environment detection

def is_root() -> bool:
    """Returns True if the current process is running as uid 0."""
    return os.getuid() == 0


def has_termux_api() -> bool:
    """Returns True if the termux-api binary is present (Termux environment)."""
    return os.path.isfile(_TERMUX_API)


def detect_backend() -> str:
    """
    Determine which USB access backend to use and return its name.

    Decision order:
      1. uid == 0              -> 'root'   (Nethunter, Termux:Root, sudo)
      2. termux-api present    -> 'termux' (stock Termux, no-root)
      3. neither               -> RuntimeError with install hint

    Returns: 'root' | 'termux'
    """
    if is_root():
        return "root"
    if has_termux_api():
        return "termux"
    raise RuntimeError(
        "No USB backend available.\n"
        "  • Run as root (Nethunter / Termux:Root / sudo), OR\n"
        "  • Install Termux:API add-on and: pkg install termux-api"
    )


# Low-level: call termux-api Usb without subprocess 

def _run_termux_api(*args, timeout: int = 15) -> str:
    """
    Fork + exec termux-api with the given args, collect stdout, return it.

    Equivalent to:
        /data/.../termux-api Usb -a <action> [params…]
    but using only os.fork / os.execve / os.pipe - no subprocess module.

    Args are everything *after* the binary path, e.g.:
        _run_termux_api("Usb", "-a", "list")
    """
    r_fd, w_fd = os.pipe()

    pid = os.fork()
    if pid == 0:
        try:
            os.close(r_fd)
            os.dup2(w_fd, 1)
            os.close(w_fd)
            devnull = os.open("/dev/null", os.O_WRONLY)
            os.dup2(devnull, 2)
            os.close(devnull)
            argv = [_TERMUX_API] + list(args)
            os.execve(_TERMUX_API, argv, os.environ.copy())
        except Exception:
            os._exit(1)
        os._exit(0)  # unreachable, but safe
    else:
        os.close(w_fd)
        chunks = []
        while True:
            chunk = os.read(r_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r_fd)
        _, status = os.waitpid(pid, 0)
        exit_code = os.waitstatus_to_exitcode(status)
        output = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if exit_code != 0 and not output:
            raise RuntimeError(
                f"termux-api exited {exit_code} with no output (args={args})"
            )
        return output


# Termux backend: public API

def list_usb_devices() -> list[str]:
    """
    Return a list of available USB device paths.
    Replaces: subprocess.check_output(["termux-usb", "-l"])
    Termux backend only.
    """
    out = _run_termux_api("Usb", "-a", "list")
    try:
        devices = json.loads(out)
        return devices if isinstance(devices, list) else []
    except json.JSONDecodeError as e:
        raise RuntimeError(f"termux-api Usb list returned bad JSON: {e!r}\n{out!r}")


def request_permission(device_path: str) -> bool:
    """
    Show the Android USB-permission dialog for device_path.
    Returns True if the user granted permission.
    Termux backend only.
    """
    try:
        out = _run_termux_api(
            "Usb", "-a", "permission",
            "--ez", "request", "true",
            "--es", "device", device_path,
        )
        return "yes" in out.lower() or "granted" in out.lower()
    except Exception:
        return False


def open_usb_device(device_path: str, callback_cmd: str,
                    export_as_env: bool = True) -> None:
    """
    Ask termux-api to open device_path and call callback_cmd with the fd.
    Termux backend only.
    """
    params = [
        "Usb", "-a", "open",
        "--ez", "request", "true",
        "--es", "device", device_path,
    ]
    out = _run_termux_api(*params)
    _check_api_output(out)

    fd_str = out.strip()
    env = os.environ.copy()
    if export_as_env:
        env["TERMUX_USB_FD"] = fd_str
        env["TERMUX_USB_DEVICE"] = device_path
        argv = callback_cmd.split()
        os.execvpe(argv[0], argv, env)
    else:
        argv = callback_cmd.split() + [fd_str]
        os.execvpe(argv[0], argv, env)


def _check_api_output(out: str):
    """Raise RuntimeError on known termux-api error strings."""
    lower = out.lower()
    if "no such device" in lower:
        raise RuntimeError("No such device.")
    if "no permission" in lower or "permission denied" in lower:
        raise RuntimeError("Permission denied.")
    if "permission request timeout" in lower:
        raise RuntimeError("Permission request timeout.")
    if "failed to open" in lower or "open device failed" in lower:
        raise RuntimeError("Open device failed.")


def auto_detect_device() -> str:
    """
    Return the first available USB device path (termux backend).
    Raises RuntimeError if none found.
    """
    devices = list_usb_devices()
    if not devices:
        raise RuntimeError("No USB devices found. Is the ESP32 plugged in via OTG?")
    return devices[0]


def launch_with_fd(cmd: str, device_path: str, log_file: str,
                   tail_fn=None) -> tuple:
    """
    Fork-exec termux-api Usb open, log stdout+stderr to log_file (append),
    and tail the log in a daemon thread.  Termux backend only.

    Returns (child_pid, child_done_event, tail_thread).

    The CALLER owns os.waitpid(child_pid, 0) and must call
    child_done.set() afterward so the tail thread can drain final output
    without racing on waitpid itself (which caused ChildProcessError).
    """
    import threading
    import time

    env = os.environ.copy()
    env["TERMUX_CALLBACK"] = cmd
    env["TERMUX_EXPORT_FD"] = "true"
    argv = [_TERMUX_API, "Usb", "-a", "open",
            "--ez", "request", "true", "--es", "device", device_path]

    pid = os.fork()
    if pid == 0:
        try:
            fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
            os.execve(_TERMUX_API, argv, env)
        except Exception:
            os._exit(1)

    # The tail thread checks this Event instead of calling waitpid itself,
    # avoiding ChildProcessError when the parent reaps the child first.
    child_done = threading.Event()

    def _tail():
        fd = os.open(log_file, os.O_RDONLY | os.O_NONBLOCK)
        # Start at EOF: the parent already printed its own pre-fork lines
        # (and the header) directly to the terminal, so replaying them from
        # the beginning of the file here would duplicate them. Only content
        # appended after the tail starts (i.e. the child's own logging)
        # should flow through to the terminal via tail_fn.
        os.lseek(fd, 0, os.SEEK_END)
        buf = b""
        try:
            while True:
                done = child_done.is_set()
                try:
                    chunk = os.read(fd, 4096)
                    buf += chunk
                except BlockingIOError:
                    pass

                # Flush on EITHER \n or \r as a line boundary. Progress bars
                # (see nrflash.py's _progress()) update in place using \r
                # with no \n until the transfer completes - splitting only
                # on \n meant every \r-joined progress update sat buffered
                # here until the final \n showed up at 100%, so the whole
                # flash appeared "stuck" and then jumped straight to 100%
                # once it finally flushed. Treating \r as a boundary too
                # lets each progress tick stream through immediately, same
                # as it would on a real terminal.
                while True:
                    nl = buf.find(b"\n")
                    cr = buf.find(b"\r")
                    if nl == -1 and cr == -1:
                        break
                    if nl == -1:
                        idx, sep = cr, b"\r"
                    elif cr == -1:
                        idx, sep = nl, b"\n"
                    else:
                        idx, sep = (cr, b"\r") if cr < nl else (nl, b"\n")
                    line, buf = buf[:idx], buf[idx + 1:]
                    if tail_fn:
                        tail_fn(line.decode("utf-8", errors="replace") + sep.decode())

                if done:
                    try:
                        buf += os.read(fd, 65536)
                    except BlockingIOError:
                        pass
                    while True:
                        nl = buf.find(b"\n")
                        cr = buf.find(b"\r")
                        if nl == -1 and cr == -1:
                            break
                        if nl == -1:
                            idx, sep = cr, b"\r"
                        elif cr == -1:
                            idx, sep = nl, b"\n"
                        else:
                            idx, sep = (cr, b"\r") if cr < nl else (nl, b"\n")
                        line, buf = buf[:idx], buf[idx + 1:]
                        if tail_fn:
                            tail_fn(line.decode("utf-8", errors="replace") + sep.decode())
                    if buf and tail_fn:
                        tail_fn(buf.decode("utf-8", errors="replace") + "\n")
                    break

                time.sleep(0.05)
        finally:
            os.close(fd)

    tail_thread = threading.Thread(target=_tail, daemon=True)
    tail_thread.start()

    return pid, child_done, tail_thread


def relaunch_with_fd(device_path: str, script_path: str):
    """
    Re-invoke this script so TERMUX_USB_FD gets set by termux-api Usb open.
    This replaces the current process - it does not return.
    Termux backend only.
    """
    argv = [
        _TERMUX_API, "Usb",
        "-a", "open",
        "--ez", "request", "true",
        "--es", "device", device_path,
    ]
    env = os.environ.copy()
    env["TERMUX_CALLBACK"] = f"python {script_path}"
    env["TERMUX_EXPORT_FD"] = "true"
    os.execve(_TERMUX_API, argv, env)



def find_usb_device_direct(vid_pid_filter: list[tuple] | None = None):
    """
    Root path: scan the USB bus via libusb and return the first matching device.

    vid_pid_filter: list of (vid, pid) tuples to try in order.
                    Defaults to all entries in ESP32_KNOWN if None.

    Falls back to the first non-hub device found if no known VID:PID matches.
    Raises RuntimeError if nothing is found at all.
    """
    candidates = list(vid_pid_filter or ESP32_KNOWN.keys())

    for vid, pid in candidates:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is not None:
            return dev

    all_devs = list(usb.core.find(find_all=True))
    non_hubs = [d for d in all_devs if d.bDeviceClass != 9]
    if non_hubs:
        return non_hubs[0]

    raise RuntimeError(
        "No USB device found on the bus. Is the ESP32 plugged in via OTG?"
    )

def init_uart_bridge(device):
    """
    Sends raw vendor control requests to wake up and configure
    external hardware serial bridges to 115200 baud, 8N1.
    Includes an explicit hardware reset sequence to pull the ESP32
    out of bootloader loops/reset traps caused by Android connection probes.
    """
    import time
    vid, pid = device.idVendor, device.idProduct

    # ── CASE 1: Silicon Labs CP2102 ───────────────────────────────────────────
    if (vid, pid) == (0x10C4, 0xEA60):
        print("[*] Initializing CP2102 line control registers...", flush=True)
        try:
            device.ctrl_transfer(0x41, 0x00, 0x0001, 0, None)       # Enable UART port
            device.ctrl_transfer(0x40, 0x1E, 0xC200, 0x0001, None)  # Set baud rate to 115200
            device.ctrl_transfer(0x40, 0x03, 0x0800, 0, None)       # 8N1 line control

            # Hardware reset toggle for CP2102 auto-reset circuit
            device.ctrl_transfer(0x41, 0x01, 0x0300, 0, None)       # De-assert DTR+RTS
            time.sleep(0.05)
            device.ctrl_transfer(0x41, 0x01, 0x0303, 0, None)       # Assert DTR+RTS (normal boot)
            time.sleep(0.1)

            print("[+] CP2102 successfully configured and reset!", flush=True)
        except Exception as e:
            print(f"[-] Warning: CP2102 initialization encountered an error: {e}", flush=True)

    # ── CASE 2: WCH CH340 / CH341 ────────────────────────────────────────────
    elif (vid, pid) in [(0x1A86, 0x7523), (0x1A86, 0x55D4)]:
        print("[*] Initializing CH340 register state machine...", flush=True)
        try:
            # CH340 line initialization handshake
            device.ctrl_transfer(0x40, 0xA1, 0, 0, None)

            # Set baud rate to 115200 using the real CH340/CH341 divisor
            # algorithm (from the Linux kernel's ch341.c driver - this is
            # what actually runs whenever esptool.py works over a real
            # /dev/ttyUSB* on Linux, so it's a known-correct reference).
            #
            # An earlier version of this code sent
            #   ctrl_transfer(0x40, 0x9A, 0xC380, 0xEB00, None)
            # which has wValue and wIndex reversed relative to the real
            # protocol (wValue must be the constant 0x1312, wIndex the
            # computed divisor) - it was writing garbage to the baud rate
            # generator. Combined with a bogus 8N1 byte (0x0050 instead of
            # the real LCR encoding 0xC3), the bridge was very likely never
            # actually running at 115200 8N1 - explaining sync failures
            # that had nothing to do with the reset-pin timing.
            baud_rate = 115200
            factor = 1532620800 // baud_rate  # CH341_BAUDBASE_FACTOR
            divisor = 3                        # CH341_BAUDBASE_DIVMAX
            while factor > 0xFFF0 and divisor:
                factor >>= 3
                divisor -= 1
            factor = 0x10000 - factor
            a = ((factor & 0xFF00) | divisor) | 0x80  # BIT(7): CH341A
            # packet-buffering flag - harmless on plain CH340/CH340G, and
            # is what the modern kernel driver sets unconditionally.
            lcr_8n1 = 0x80 | 0x40 | 0x03  # ENABLE_RX | ENABLE_TX | CS8
            device.ctrl_transfer(0x40, 0x9A, 0x1312, a, None)
            device.ctrl_transfer(0x40, 0x9A, 0x2518, lcr_8n1, None)

            # ── HARDWARE RESET TOGGLE FOR ESP32 AUTO-RESET CIRCUIT ────────────
            # 0xA4 modem control bits are ACTIVE-LOW. Bit weights per the
            # Linux kernel ch341.c driver: bit5 (0x20) = DTR, bit6 (0x40) =
            # RTS. (Earlier revision of this code used bit4 (0x10) for RTS,
            # which isn't wired to anything - RTS never actually toggled,
            # so this reset pulse silently did nothing on real hardware.)
            #   0xFF = DTR+RTS de-asserted (idle / lines released)
            #   0x9F = DTR+RTS asserted   (bits 5+6 pulled low → EN+BOOT driven)
            #
            # To get a clean normal-sketch boot (not bootloader):
            #   1. Assert both  → ESP32 goes into reset
            #   2. Release both → EN rises, BOOT pin=1 → runs sketch
            device.ctrl_transfer(0x40, 0xA4, 0x9F, 0, None)  # Assert DTR+RTS → reset
            time.sleep(0.1)                                    # Hold reset
            device.ctrl_transfer(0x40, 0xA4, 0xFF, 0, None)  # Release → normal boot
            time.sleep(0.5)               
            
            # Assert DTR so CH340 forwards RX data to host
            device.ctrl_transfer(0x40, 0xA4, 0xDF, 0, None)  # DTR asserted, RTS released
            time.sleep(0.05)

            print("[+] CH340 successfully configured and released from reset!", flush=True)
        except Exception as e:
            print(f"[-] Warning: CH340 initialization encountered an error: {e}", flush=True)

    # ── CASE 3: FTDI FT232 ───────────────────────────────────────────────────
    elif (vid, pid) == (0x0403, 0x6001):
        print("[*] Initializing FTDI FT232 line settings...", flush=True)
        try:
            device.ctrl_transfer(0x40, 0x00, 0x0000, 0, None)       # Reset device
            time.sleep(0.05)
            device.ctrl_transfer(0x40, 0x03, 0x001A, 0, None)       # Set baud rate to 115200
            device.ctrl_transfer(0x40, 0x04, 0x0008, 0, None)       # 8N1 line control
            device.ctrl_transfer(0x40, 0x01, 0x0202, 0, None)       # Assert DTR+RTS
            time.sleep(0.1)

            print("[+] FTDI FT232 successfully configured!", flush=True)
        except Exception as e:
            print(f"[-] Warning: FTDI initialization encountered an error: {e}", flush=True)

    # ── CASE 4: Native USB ESP32-S3 / C3 — no bridge init needed ─────────────
    else:
        pass


# Backward-compatibility alias — callers using the old name keep working
init_cp2102_bridge = init_uart_bridge


def set_uart_bridge_baud(device, baud: int):
    """
    Reprogram just the baud-rate divisor on the already-initialized UART
    bridge, without touching DTR/RTS or re-running the reset handshake.

    init_uart_bridge() hardcodes every bridge to 115200 and nothing ever
    calls it again, so a run stays pinned at ~11.5 KB/s (115200 baud / 10
    bits-per-byte) even once the stub flasher is up and could handle far
    more. Real esptool renegotiates to a high rate (921600 is the common
    default) right after the stub greets it, using CMD_CHANGE_BAUDRATE on
    the wire side plus the equivalent host-side divisor write. This is
    the host-side half of that; the caller is responsible for also
    sending CMD_CHANGE_BAUDRATE to the stub with the same value so both
    ends agree before the next command is sent.
    """
    vid, pid = device.idVendor, device.idProduct

    if (vid, pid) == (0x10C4, 0xEA60):  # CP2102
        # CP210x wants the literal baud rate written as a 4-byte
        # little-endian value via a separate vendor request, not the
        # 0xC200-style divisor code used for the fixed 115200 case in
        # init_uart_bridge().
        import struct
        device.ctrl_transfer(0x40, 0x1E, 0, 0, struct.pack("<I", baud))

    elif (vid, pid) in [(0x1A86, 0x7523), (0x1A86, 0x55D4)]:  # CH340/CH341
        factor = 1532620800 // baud  # CH341_BAUDBASE_FACTOR
        divisor = 3
        while factor > 0xFFF0 and divisor:
            factor >>= 3
            divisor -= 1
        factor = 0x10000 - factor
        a = ((factor & 0xFF00) | divisor) | 0x80
        device.ctrl_transfer(0x40, 0x9A, 0x1312, a, None)

    elif (vid, pid) == (0x0403, 0x6001):  # FTDI FT232
        # FTDI's baud divisor is chip-clock/16/baud rounded to its fixed-
        # point encoding; 3000000 is the FT232's base clock.
        divisor = 3000000 // baud
        device.ctrl_transfer(0x40, 0x03, divisor & 0xFFFF, 0, None)

    else:
        raise RuntimeError(
            f"set_uart_bridge_baud: no baud-rate handler for VID:PID "
            f"{vid:04X}:{pid:04X}"
        )


# UART bridge VID:PIDs this tool knows how to drive DTR/RTS on directly.
# (Native USB-CDC ESP32-S3/C3/S2 boards go through cdc_reset.py instead —
# see get_cdc_endpoints()'s native-CDC branch above.)
UART_BRIDGE_VIDPIDS = {
    (0x10C4, 0xEA60),  # CP2102
    (0x1A86, 0x7523),  # CH340 / CH340G
    (0x1A86, 0x55D4),  # CH9102
    (0x0403, 0x6001),  # FTDI FT232
}


def is_uart_bridge(device) -> bool:
    """True if this device is a CP2102/CH340/CH9102/FTDI serial bridge."""
    return (device.idVendor, device.idProduct) in UART_BRIDGE_VIDPIDS


def set_dtr_rts(device, dtr: bool, rts: bool) -> None:
    """
    Assert/de-assert the DTR and RTS handshake lines on a UART bridge chip.

    This is the single primitive uart_reset.py builds the classic
    ESP32/ESP8266 "GPIO0 + EN" reset dance on top of. `dtr=True` /
    `rts=True` means the signal is ASSERTED — on every board covered here
    that's wired through an inverting NPN transistor, so asserted actually
    drives the physical pin (GPIO0 or EN) LOW. That matches how esptool.py
    treats its own `_setDTR`/`_setRTS` calls, so the sequences in
    uart_reset.py read the same as esptool's classic_reset/usb_reset.

    Silently ignored (raises RuntimeError up to caller) if the device isn't
    one of the known bridge chips — native USB CDC boards don't have real
    DTR/RTS lines and must use cdc_reset.py instead.
    """
    vid, pid = device.idVendor, device.idProduct

    if (vid, pid) == (0x10C4, 0xEA60):
        # CP2102: SILABSER_SET_MHS_REQUEST. High byte = which lines to
        # drive (mask), low byte = state to drive them to.
        state = (0x01 if dtr else 0) | (0x02 if rts else 0)
        device.ctrl_transfer(0x41, 0x01, 0x0300 | state, 0, None)

    elif (vid, pid) in [(0x1A86, 0x7523), (0x1A86, 0x55D4)]:
        # CH340/CH340G/CH9102: modem control byte on request 0xA4 is
        # ACTIVE-LOW. Bit weights taken from the Linux kernel ch341.c
        # driver (CH341_BIT_DTR/CH341_BIT_RTS), which is what actually
        # gets exercised when esptool.py works over a real /dev/ttyUSB*:
        #   bit5 (0x20) = DTR
        #   bit6 (0x40) = RTS
        # (An earlier version of this code used bit4 (0x10) for RTS,
        # which isn't wired to anything meaningful on real hardware - RTS
        # never actually toggled, so bootloader entry silently no-opped
        # while everything else (line coding, DTR-only writes) kept
        # working, which is exactly what made this bug hard to spot.)
        value = 0xFF
        if dtr:
            value &= ~0x20
        if rts:
            value &= ~0x40
        device.ctrl_transfer(0x40, 0xA4, value & 0xFF, 0, None)

    elif (vid, pid) == (0x0403, 0x6001):
        # FTDI FT232: SIO_SET_MODEM_CTRL_REQUEST. Low byte = state
        # (bit0=DTR, bit1=RTS), high byte = mask (which bits to apply).
        state = (0x01 if dtr else 0) | (0x02 if rts else 0)
        device.ctrl_transfer(0x40, 0x01, 0x0300 | state, 0, None)

    else:
        raise RuntimeError(
            f"set_dtr_rts: {vid:04X}:{pid:04X} is not a known UART bridge chip"
        )


def wrap_direct(vid_pid_filter: list[tuple] | None = None):
    """
    Root backend equivalent of wrap_fd().

    Opens the ESP32 directly via libusb without termux-api or an Android
    file descriptor.  Returns a usb.core.Device ready for claim_device()
    and get_cdc_endpoints() - same contract as wrap_fd().

    Requires root (uid 0) so libusb can open /dev/bus/usb directly.
    On Nethunter, ensure libusb-1.0 is installed:
        apt install libusb-1.0-0
    """
    device = find_usb_device_direct(vid_pid_filter)
    return device



def wrap_fd(fd: int):
    """
    Wrap Android's USB file descriptor into a PyUSB Device object.
    Must call LIBUSB_OPTION_NO_DEVICE_DISCOVERY before libusb_init.
    Returns a usb.core.Device ready for use.
    Termux backend only.
    """
    backend = libusb1.get_backend()
    lib     = backend.lib
    ctx     = backend.ctx

    lib.libusb_set_option.argtypes = [libusb1.c_void_p, ctypes.c_int]
    lib.libusb_set_option.restype  = ctypes.c_int
    lib.libusb_set_option(None, 5)   # LIBUSB_OPTION_NO_DEVICE_DISCOVERY

    lib.libusb_wrap_sys_device.argtypes = [
        libusb1.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(libusb1._libusb_device_handle),
    ]
    lib.libusb_get_device.argtypes = [libusb1._libusb_device_handle]
    lib.libusb_get_device.restype  = ctypes.c_void_p

    handle = libusb1._libusb_device_handle()
    ret = lib.libusb_wrap_sys_device(ctx, int(fd), ctypes.byref(handle))
    if ret != 0:
        raise RuntimeError(f"libusb_wrap_sys_device failed: {ret}")

    devid = lib.libusb_get_device(handle)

    class _Dummy:
        def __init__(self, devid, handle):
            self.devid  = devid
            self.handle = handle

    dummy  = _Dummy(devid, handle)
    device = usb.core.Device(dummy, backend)
    device._ctx.handle = dummy
    return device



def find_cdc_control_interface(device, data_intf_num: int) -> int:
    """
    Find the CDC-ACM *control/communications* interface (bInterfaceClass 0x02)
    paired with the given CDC *data* interface (bInterfaceClass 0x0A).

    Boards that expose more than just CDC (e.g. ESP32-S3 with USB-JTAG active)
    don't always put the control interface at #0 - JTAG can sit at interface 0
    and push CDC control/data to 1/2 instead. Hardcoding control_interface=0
    in open_native_cdc_port() can then target the wrong (possibly
    kernel/OS-claimed) interface, producing "Resource busy" when asserting DTR.
    """
    cfg = device.get_active_configuration()
    candidate = data_intf_num - 1
    for intf in cfg:
        if intf.bInterfaceNumber == candidate and intf.bInterfaceClass == 0x02:
            return candidate
    for intf in cfg:
        if intf.bInterfaceClass == 0x02:
            return intf.bInterfaceNumber
    return 0


def is_native_cdc(device) -> bool:
    """True if this is a native USB-CDC ESP32 (S2/S3/C3), not a UART bridge chip."""
    return (device.idVendor, device.idProduct) in [(0x303A, 0x1001), (0x303A, 0x0002)]


def open_native_cdc_port(device, control_interface: int = 0) -> None:
    """
    Assert DTR on a native USB-CDC ESP32 via the standard CDC-ACM
    SET_CONTROL_LINE_STATE class request (bRequest 0x22, bmRequestType
    0x21; wValue bit0=DTR, bit1=RTS).

    Native USB-CDC boards (ESP32-S2/S3/C3 with ARDUINO_USB_CDC_ON_BOOT)
    gate `Serial` on the host having "opened" the port - firmware using
    the common `while (!Serial) { delay(10); }` startup guard will never
    leave setup() until DTR is asserted, so nothing (not even
    Serial.println, not the BridgeProtocol command loop) ever runs.

    UART-bridge chips (CP2102/CH340/FTDI) are untouched by this - they
    get DTR/RTS handled separately by init_uart_bridge()/set_dtr_rts(),
    which use each chip's own vendor-specific control requests instead
    of the CDC-ACM class request used here.

    No-op (raises) if called on a non-native-CDC device; callers should
    guard with `if is_native_cdc(device):` first, same as is_uart_bridge().
    """
    if not is_native_cdc(device):
        raise RuntimeError(
            "open_native_cdc_port: device is not a known native USB-CDC "
            "ESP32 - use init_uart_bridge()/set_dtr_rts() for bridge chips instead"
        )
    value = 0x01  # DTR asserted, RTS released
    device.ctrl_transfer(0x21, 0x22, value, control_interface, None)


def describe_device(device) -> str:
    vid, pid = device.idVendor, device.idProduct
    label = ESP32_KNOWN.get((vid, pid), "Unknown device")
    return f"{vid:04X}:{pid:04X}  {label}"


def get_cdc_endpoints(device):
    """
    Find bulk IN and OUT endpoints. 
    Completely compatible drop-in replacement that works for BOTH:
      1. Native USB CDC devices (ESP32-S3, ESP32-C3)
      2. Serial Bridge chips (CP2102, CH340, CH9102, FTDI)
    
    Returns (ep_in_addr, ep_out_addr, interface_number).
    """
    import usb.util
    vid, pid = device.idVendor, device.idProduct
    cfg = device.get_active_configuration()

    if (vid, pid) in [(0x303A, 0x1001), (0x303A, 0x0002)]:
        # Native ESP32 S3/C3 USB CDC always maps bulk data to Interface 1
        try:
            intf = cfg[(1, 0)]
            eps = list(intf)
            ep_in = next((e.bEndpointAddress for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
            ep_out = next((e.bEndpointAddress for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
            if ep_in and ep_out:
                return ep_in, ep_out, 1
        except Exception:
            pass

    elif (vid, pid) in [(0x10C4, 0xEA60), (0x1A86, 0x7523), (0x1A86, 0x55D4), (0x0403, 0x6001)]:
        # Hardware UART Bridges — enumerate from descriptor instead of hardcoding.
        # Hardcoded 0x81/0x01 fails on fd-wrapped devices because set_configuration()
        # is skipped (Termux no-root) so _ep_info is never populated.
        try:
            intf = cfg[(0, 0)]
            eps = list(intf)
            ep_in = next((e.bEndpointAddress for e in eps
                          if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                          and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
            ep_out = next((e.bEndpointAddress for e in eps
                           if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                           and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
            if ep_in and ep_out:
                return ep_in, ep_out, 0
        except Exception:
            pass
        # Absolute fallback if descriptor walk failed
        return 0x81, 0x01, 0

    # If a new or unknown device is plugged in, look for CDC Data class (0x0A) first
    for intf in cfg:
        if intf.bInterfaceClass == 0x0A:
            eps = list(intf)
            ep_in = next((e.bEndpointAddress for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
            ep_out = next((e.bEndpointAddress for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
            if ep_in and ep_out:
                return ep_in, ep_out, intf.bInterfaceNumber

    # Final Fallback: Grab the first valid bulk pair found anywhere on the device
    for intf in cfg:
        eps = list(intf)
        ep_in = next((e.bEndpointAddress for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
        ep_out = next((e.bEndpointAddress for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK), None)
        if ep_in and ep_out:
            return ep_in, ep_out, intf.bInterfaceNumber

    raise RuntimeError("No bulk endpoint pair found on device")

def claim_device(device, interface_num: int, fd_wrapped: bool = False):
    """
    Detach kernel driver if needed and claim the interface.

    fd_wrapped: set True when the device came from wrap_fd() (Termux no-root).
    In that case we must NOT call set_configuration() — Android has already
    configured the device and libusb's internal refcount doesn't survive a
    second configuration attempt on a fd-wrapped handle.  Calling it on the
    ESP32-S3 (which uses TinyUSB with two CDC interfaces) triggers:
        assertion "refcnt >= 2" failed
    and crashes the process.  The root (wrap_direct) path is fine.
    """
    import usb.util
    try:
        if device.is_kernel_driver_active(interface_num):
            device.detach_kernel_driver(interface_num)
    except Exception:
        pass
    if not fd_wrapped:
        try:
            device.set_configuration()
        except Exception:
            pass
    try:
        usb.util.claim_interface(device, interface_num)
    except Exception:
        pass


def reset_endpoint_toggles(device, ep_in_addr: int, ep_out_addr: int):
    """
    Clear the DATA0/DATA1 toggle on both bulk endpoints.

    Each invocation is a fresh OS process wrapping the *same* physical USB
    device via TERMUX_USB_FD (termux) or a direct libusb open (root).
    The toggle bit lives in the peripheral's endpoint state, not in process
    memory, so a clean exit does NOT reset it.  Call this once right after
    claim_device(), before any read/write.
    """
    for ep in (ep_in_addr, ep_out_addr):
        try:
            device.clear_halt(ep)
        except usb.core.USBError:
            pass