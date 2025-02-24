import sys
import errno
import types
from pr2modules import config
from pr2modules.common import Namespace
from pr2modules.common import AddrPool
from pr2modules.common import DEFAULT_RCVBUF
from pr2modules.proxy import NetlinkProxy
from pr2modules.netlink import NETLINK_ROUTE
from pr2modules.netlink.nlsocket import NetlinkSocket
from pr2modules.netlink.nlsocket import BatchSocket
from pr2modules.netlink.nlsocket import ChaoticNetlinkSocket
from pr2modules.netlink import rtnl
from pr2modules.netlink.rtnl.marshal import MarshalRtnl

if sys.platform.startswith('linux'):
    if config.kernel < [3, 3, 0]:
        from pr2modules.netlink.rtnl.ifinfmsg.compat import proxy_newlink
        from pr2modules.netlink.rtnl.ifinfmsg.compat import proxy_setlink
        from pr2modules.netlink.rtnl.ifinfmsg.compat import proxy_dellink
        from pr2modules.netlink.rtnl.ifinfmsg.compat import proxy_linkinfo
    else:
        from pr2modules.netlink.rtnl.ifinfmsg.proxy import proxy_newlink
        from pr2modules.netlink.rtnl.ifinfmsg.proxy import proxy_setlink


class IPRSocketBase(object):
    def __init__(self, *argv, **kwarg):
        if 'family' in kwarg:
            kwarg.pop('family')
        super(IPRSocketBase, self).__init__(NETLINK_ROUTE, *argv[1:], **kwarg)
        self.marshal = MarshalRtnl()
        self._s_channel = None
        if sys.platform.startswith('linux'):
            self._gate = self._gate_linux
            self.sendto_gate = self._gate_linux
            send_ns = Namespace(
                self,
                {'addr_pool': AddrPool(0x10000, 0x1FFFF), 'monitor': False},
            )
            self._sproxy = NetlinkProxy(policy='return', nl=send_ns)
            self._sproxy.pmap = {
                rtnl.RTM_NEWLINK: proxy_newlink,
                rtnl.RTM_SETLINK: proxy_setlink,
            }
            if config.kernel < [3, 3, 0]:
                self._recv_ns = Namespace(
                    self,
                    {
                        'addr_pool': AddrPool(0x20000, 0x2FFFF),
                        'monitor': False,
                    },
                )
                self._sproxy.pmap[rtnl.RTM_DELLINK] = proxy_dellink
                # inject proxy hooks into recv() and...
                self.__recv = self._recv
                self._recv = self._p_recv
                # ... recv_into()
                self._recv_ft = self.recv_ft
                self.recv_ft = self._p_recv_ft

    def bind(self, groups=rtnl.RTMGRP_DEFAULTS, **kwarg):
        super(IPRSocketBase, self).bind(groups, **kwarg)

    def _gate_linux(self, msg, addr):
        msg.reset()
        msg.encode()
        ret = self._sproxy.handle(msg)
        if ret is not None:
            if ret['verdict'] == 'forward':
                return self._sendto(ret['data'], addr)
            elif ret['verdict'] in ('return', 'error'):
                if self._s_channel is not None:
                    return self._s_channel.send(ret['data'])
                else:
                    msgs = self.marshal.parse(ret['data'])
                    for msg in msgs:
                        seq = msg['header']['sequence_number']
                        if seq in self.backlog:
                            self.backlog[seq].append(msg)
                        else:
                            self.backlog[seq] = [msg]
                    return len(ret['data'])
            else:
                ValueError('Incorrect verdict')

        return self._sendto(msg.data, addr)

    def _p_recv_ft(self, bufsize, flags=0):
        data = self._recv_ft(bufsize, flags)
        ret = proxy_linkinfo(data, self._recv_ns)
        if ret is not None:
            if ret['verdict'] in ('forward', 'error'):
                return ret['data']
            else:
                ValueError('Incorrect verdict')

        return data

    def _p_recv(self, bufsize, flags=0):
        data = self.__recv(bufsize, flags)
        ret = proxy_linkinfo(data, self._recv_ns)
        if ret is not None:
            if ret['verdict'] in ('forward', 'error'):
                return ret['data']
            else:
                ValueError('Incorrect verdict')

        return data


class IPBatchSocket(IPRSocketBase, BatchSocket):
    pass


class ChaoticIPRSocket(IPRSocketBase, ChaoticNetlinkSocket):
    pass


class IPRSocket(IPRSocketBase, NetlinkSocket):
    '''
    The simplest class, that connects together the netlink parser and
    a generic Python socket implementation. Provides method get() to
    receive the next message from netlink socket and parse it. It is
    just simple socket-like class, it implements no buffering or
    like that. It spawns no additional threads, leaving this up to
    developers.

    Please note, that netlink is an asynchronous protocol with
    non-guaranteed delivery. You should be fast enough to get all the
    messages in time. If the message flow rate is higher than the
    speed you parse them with, exceeding messages will be dropped.

    *Usage*

    Threadless RT netlink monitoring with blocking I/O calls:

        >>> from pr2modules import IPRSocket
        >>> from pprint import pprint
        >>> s = IPRSocket()
        >>> s.bind()
        >>> pprint(s.get())
        [{'attrs': [('RTA_TABLE', 254),
                    ('RTA_DST', '2a00:1450:4009:808::1002'),
                    ('RTA_GATEWAY', 'fe80:52:0:2282::1fe'),
                    ('RTA_OIF', 2),
                    ('RTA_PRIORITY', 0),
                    ('RTA_CACHEINFO', {'rta_clntref': 0,
                                       'rta_error': 0,
                                       'rta_expires': 0,
                                       'rta_id': 0,
                                       'rta_lastuse': 5926,
                                       'rta_ts': 0,
                                       'rta_tsage': 0,
                                       'rta_used': 1})],
          'dst_len': 128,
          'event': 'RTM_DELROUTE',
          'family': 10,
          'flags': 512,
          'header': {'error': None,
                     'flags': 0,
                     'length': 128,
                     'pid': 0,
                     'sequence_number': 0,
                     'type': 25},
          'proto': 9,
          'scope': 0,
          'src_len': 0,
          'table': 254,
          'tos': 0,
          'type': 1}]
        >>>
    '''

    _brd_socket = None

    def bind(self, *argv, **kwarg):
        if kwarg.pop('clone_socket', False):
            self._brd_socket = self.clone()

            def get(
                self,
                bufsize=DEFAULT_RCVBUF,
                msg_seq=0,
                terminate=None,
                callback=None,
            ):
                if msg_seq == 0:
                    return self._brd_socket.get(
                        bufsize, msg_seq, terminate, callback
                    )
                else:
                    return super(IPRSocket, self).get(
                        bufsize, msg_seq, terminate, callback
                    )

            def close(self, code=errno.ECONNRESET):
                with self.sys_lock:
                    self._brd_socket.close()
                    return super(IPRSocket, self).close(code=code)

            self.get = types.MethodType(get, self)
            self.close = types.MethodType(close, self)
            kwarg['recursive'] = True
            return self._brd_socket.bind(*argv, **kwarg)
        else:
            return super(IPRSocket, self).bind(*argv, **kwarg)
