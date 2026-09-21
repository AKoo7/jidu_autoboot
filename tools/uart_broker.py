#!/usr/bin/env python3
"""UART broker: owns /dev/ttyUSB0, broadcasts to TCP clients, logs to a file.

  watch :  tail -f /tmp/uart_broker.log
  type  :  nc 127.0.0.1 4600      (or: socat - TCP:127.0.0.1:4600)

Anything typed by a client is written to the serial port. Everything the device
sends is broadcast to all clients and appended to the log, so several readers
can watch at once and the log survives disconnects.
"""
import os
import selectors
import signal
import socket
import sys
import termios
import time

TTY = os.environ.get("UART_TTY", "/dev/ttyUSB0")
PORT = int(os.environ.get("UART_PORT", "4600"))
LOG = os.environ.get("UART_LOG", "/tmp/uart_broker.log")
BAUD = getattr(termios, os.environ.get("UART_BAUD", "B115200"))

fd = os.open(TTY, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
a = termios.tcgetattr(fd)
termios.tcsetattr(fd, termios.TCSANOW,
                  [0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0, BAUD, BAUD, list(a[6])])

log = open(LOG, "ab", buffering=0)
log.write(f"\n===== broker started {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"on {TTY} @ {os.environ.get('UART_BAUD','B115200')} =====\n".encode())

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(16)
srv.setblocking(False)

# Control channel: one-line commands. Lets a client send a serial BREAK or a
# SysRq sequence, which only the process holding the tty can do.
CTRL_PORT = int(os.environ.get("UART_CTRL_PORT", "4601"))
ctl = socket.socket()
ctl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ctl.bind(("127.0.0.1", CTRL_PORT))
ctl.listen(8)
ctl.setblocking(False)

sel = selectors.DefaultSelector()
sel.register(fd, selectors.EVENT_READ, ("tty", None))
sel.register(srv, selectors.EVENT_READ, ("srv", None))
sel.register(ctl, selectors.EVENT_READ, ("ctl", None))
clients = set()


def do_command(cmd):
    """break | sysrq <char> | sysrq-reboot | sysrq-poweroff"""
    cmd = cmd.strip().lower()
    if cmd == "break":
        termios.tcsendbreak(fd, 0)
        return "break sent"
    if cmd.startswith("sysrq "):
        termios.tcsendbreak(fd, 0)
        time.sleep(0.4)
        os.write(fd, cmd.split(" ", 1)[1].encode()[:1])
        return f"sysrq {cmd.split(' ', 1)[1][:1]} sent"
    if cmd == "sysrq-reboot":
        termios.tcsendbreak(fd, 0); time.sleep(0.4); os.write(fd, b"s"); time.sleep(2.0)
        termios.tcsendbreak(fd, 0); time.sleep(0.4); os.write(fd, b"b")
        return "sync+reboot sent"
    if cmd == "sysrq-poweroff":
        termios.tcsendbreak(fd, 0); time.sleep(0.4); os.write(fd, b"s"); time.sleep(1.5)
        termios.tcsendbreak(fd, 0); time.sleep(0.4); os.write(fd, b"o")
        return "sync+poweroff sent"
    return f"unknown command: {cmd!r}"


def drop(c):
    try:
        sel.unregister(c)
    except Exception:
        pass
    clients.discard(c)
    try:
        c.close()
    except Exception:
        pass


def broadcast(data):
    log.write(data)
    for c in list(clients):
        try:
            c.sendall(data)
        except Exception:
            drop(c)


def shutdown(signum=None, frame=None):
    try:
        srv.close()
    except Exception:
        pass
    for c in list(clients):
        drop(c)
    os.close(fd)
    log.close()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

print(f"[broker] {TTY} -> tcp/{PORT}, logging to {LOG}", flush=True)
while True:
    for key, _ in sel.select(1.0):
        kind = key.data[0]
        if kind == "tty":
            try:
                d = os.read(fd, 8192)
            except OSError:
                continue
            if d:
                broadcast(d)
        elif kind == "ctl":
            conn, _ = ctl.accept()
            try:
                conn.settimeout(3)
                cmd = conn.recv(128).decode(errors="replace").strip()
                reply = do_command(cmd) if cmd else "no command"
            except Exception as exc:  # noqa: BLE001
                reply = f"error: {exc}"
            try:
                conn.sendall(reply.encode() + b"\n")
                conn.close()
            except Exception:
                pass
        elif kind == "srv":
            c, addr = srv.accept()
            c.setblocking(False)
            clients.add(c)
            sel.register(c, selectors.EVENT_READ, ("cli", c))
            broadcast(f"\n===== client {addr[0]}:{addr[1]} attached =====\n".encode())
        else:
            c = key.fileobj
            try:
                d = c.recv(4096)
            except Exception:
                d = b""
            if not d:
                drop(c)
                broadcast(b"\n===== client detached =====\n")
            else:
                try:
                    os.write(fd, d)
                except OSError:
                    pass
