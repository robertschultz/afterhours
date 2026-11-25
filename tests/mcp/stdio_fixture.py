"""Test launcher: only the disposable engine's exact bound port is reachable."""
import os
from pathlib import Path
import socket
import sys

sys.dont_write_bytecode=True
port=int(os.environ['AFTERHOURS_TEST_PEER_PORT'])
if port in (7474,7475,8765,8768):raise RuntimeError('Fixture cannot use installed-service ports')
real_socket=socket.socket
real_getaddrinfo=socket.getaddrinfo


class FixtureSocket(real_socket):
    def connect(self,address):
        if address!=('127.0.0.1',port):raise PermissionError('Non-fixture I/O blocked')
        return super().connect(address)
    def connect_ex(self,address):
        if address!=('127.0.0.1',port):raise PermissionError('Non-fixture I/O blocked')
        return super().connect_ex(address)
    def bind(self,address):raise PermissionError('No listener allowed in MCP adapter')
    def sendto(self,*args):raise PermissionError('No datagrams allowed in MCP adapter')


def getaddrinfo(host,*args,**kwargs):
    if host!='127.0.0.1':raise PermissionError('Non-fixture DNS blocked')
    return real_getaddrinfo(host,*args,**kwargs)


socket.socket=FixtureSocket
socket.getaddrinfo=getaddrinfo
if sys.argv[1:2]==['--management']:
    import runpy
    sys.argv=sys.argv[1:]
    runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts'/'mcp-client.py'),run_name='__main__')
else:
    from afterhours.mcp_stdio import main
    raise SystemExit(main())
