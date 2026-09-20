#!/usr/bin/env python3
"""Una peticion al helper del host, para deploy.sh y para mirar a mano.

    python3 scripts/helper_cli.py /run/puente/helper.sock '{"op":"ping"}'
"""
import socket
import sys

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
s.connect(sys.argv[1])
s.sendall(sys.argv[2].encode() + b"\n")
print(s.makefile().readline().strip())
