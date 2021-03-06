# Electrum - Lightweight Bitcoin Client
# Copyright (c) 2011-2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from collections import defaultdict
import errno
import json
import os
import queue
import random
import re
import select
import socket
import stat
import threading
import time

import socks
from bitcoinx import MissingHeader, IncorrectBits, InsufficientPoW

from . import bitcoin
from . import blockchain
from . import util
from .app_state import app_state
from .bitcoin import COIN, bfh
from .blockchain import Blockchain
from .crypto import sha256d
from .i18n import _
from .interface import Connection, Interface
from .logs import logs
from .networks import Net
from .version import PACKAGE_VERSION, PROTOCOL_VERSION
from .simple_config import SimpleConfig


logger = logs.get_logger("network")


class RPCError(Exception):
    pass


NODES_RETRY_INTERVAL = 60
SERVER_RETRY_INTERVAL = 10

# Called by util.py:get_peers()
def parse_servers(result):
    """ parse servers list into dict format"""
    servers = {}
    for item in result:
        host = item[1]
        out = {}
        version = None
        pruning_level = '-'
        if len(item) > 2:
            for v in item[2]:
                if re.match(r"[st]\d*", v):
                    protocol, port = v[0], v[1:]
                    if port == '': port = Net.DEFAULT_PORTS[protocol]
                    out[protocol] = port
                elif re.match(r"v(.?)+", v):
                    version = v[1:]
                elif re.match(r"p\d*", v):
                    pruning_level = v[1:]
                if pruning_level == '': pruning_level = '0'
        if out:
            out['pruning'] = pruning_level
            out['version'] = version
            servers[host] = out
    return servers

# Imported by scripts/servers.py
def filter_version(servers):
    def is_recent(version):
        try:
            return util.normalize_version(version) >= util.normalize_version(PROTOCOL_VERSION)
        except Exception as e:
            return False
    return {k: v for k, v in servers.items() if is_recent(v.get('version'))}


# Imported by scripts/peers.py
def filter_protocol(hostmap, protocol = 's'):
    '''Filters the hostmap for those implementing protocol.
    The result is a list in serialized form.'''
    eligible = []
    for host, portmap in hostmap.items():
        port = portmap.get(protocol)
        if port:
            eligible.append(serialize_server(host, port, protocol))
    return eligible

def _get_eligible_servers(hostmap=None, protocol="s", exclude_set=None):
    if exclude_set is None:
        exclude_set = set()
    if hostmap is None:
        hostmap = Net.DEFAULT_SERVERS
    return list(set(filter_protocol(hostmap, protocol)) - exclude_set)

def _pick_random_server(hostmap=None, protocol='s', exclude_set=None):
    if exclude_set is None:
        exclude_set = set()
    eligible = _get_eligible_servers(hostmap, protocol, exclude_set)
    return random.choice(eligible) if eligible else None

proxy_modes = ['socks4', 'socks5', 'http']


def _serialize_proxy(p):
    if not isinstance(p, dict):
        return None
    return ':'.join([p.get('mode'), p.get('host'), p.get('port'),
                     p.get('user', ''), p.get('password', '')])


def _deserialize_proxy(s):
    if not isinstance(s, str):
        return None
    if s.lower() == 'none':
        return None
    proxy = { "mode":"socks5", "host":"localhost" }
    args = s.split(':')
    n = 0
    if proxy_modes.count(args[n]) == 1:
        proxy["mode"] = args[n]
        n += 1
    if len(args) > n:
        proxy["host"] = args[n]
        n += 1
    if len(args) > n:
        proxy["port"] = args[n]
        n += 1
    else:
        proxy["port"] = "8080" if proxy["mode"] == "http" else "1080"
    if len(args) > n:
        proxy["user"] = args[n]
        n += 1
    if len(args) > n:
        proxy["password"] = args[n]
    return proxy


# Imported by gui.qt.network_dialog.py
def deserialize_server(server_str):
    host, port, protocol = str(server_str).rsplit(':', 2)
    assert protocol in 'st'
    int(port)    # Throw if cannot be converted to int
    return host, port, protocol

# Imported by gui.qt.network_dialog.py
def serialize_server(host, port, protocol):
    return str(':'.join([host, port, protocol]))


class Network(util.DaemonThread):
    """
    The Network class manages a set of connections to remote electrum
    servers, each connected socket is handled by an Interface() object.
    Connections are initiated by a Connection() thread which stops once
    the connection succeeds or fails.
    """

    def __init__(self, config=None):
        super().__init__('network')
        if config is None:
            config = {}  # Do not use mutables as default values!
        self.config = SimpleConfig(config) if isinstance(config, dict) else config
        self.num_server = 10 if not self.config.get('oneserver') else 0
        # FIXME - this doesn't belong here; it's not a property of the Network
        # Leaving it here until startup is rationalized
        Blockchain.read_blockchains()
        # Server for addresses and transactions
        self.default_server = self.config.get('server', None)
        self.blacklisted_servers = set(self.config.get('server_blacklist', []))
        logger.debug("server blacklist: %s", self.blacklisted_servers)
        # Sanitize default server
        if self.default_server:
            try:
                deserialize_server(self.default_server)
            except:
                logger.error('failed to parse server-string; falling back to random.')
                self.default_server = None
        if not self.default_server or self.default_server in self.blacklisted_servers:
            self.default_server = _pick_random_server()

        self.lock = threading.Lock()
        # locks: if you need to take several acquire them in the order they are defined here!
        self.interface_lock = threading.RLock()            # <- re-entrant
        self.pending_sends_lock = threading.Lock()

        self.pending_sends = []
        self.message_id = 0
        self.verifications_required = 1
        # If the height is cleared from the network constants, we're
        # taking looking to get 3 confirmations of the first verification.
        if Net.VERIFICATION_BLOCK_HEIGHT is None:
            self.verifications_required = 3
        self.checkpoint_height = Net.VERIFICATION_BLOCK_HEIGHT
        self.debug = False
        self.irc_servers = {} # returned by interface (list from irc)
        self.recent_servers = self._read_recent_servers()

        self.banner = ''
        self.donation_address = ''
        self.relay_fee = None
        # callbacks passed with subscriptions
        self.subscriptions = defaultdict(list)
        self.sub_cache = {}                     # note: needs self.interface_lock
        # callbacks set by the GUI
        self.callbacks = defaultdict(list)

        dir_path = os.path.join( self.config.path, 'certs')
        if not os.path.exists(dir_path):
            os.mkdir(dir_path)
            os.chmod(dir_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        # subscriptions and requests
        self.subscribed_addresses = set()
        # Requests from client we've not seen a response to
        self.unanswered_requests = {}
        # retry times
        self.server_retry_time = time.time()
        self.nodes_retry_time = time.time()
        # kick off the network.  interface is the main server we are currently
        # communicating with.  interfaces is the set of servers we are connecting
        # to or have an ongoing connection with
        self.interface = None                   # note: needs self.interface_lock
        self.interfaces = {}                    # note: needs self.interface_lock
        self.auto_connect = self.config.get('auto_connect', True)
        self.connecting = set()
        self.socket_queue = queue.Queue()
        self._start_network(deserialize_server(self.default_server)[2],
                           _deserialize_proxy(self.config.get('proxy')))

    # Called by gui.qt.main_window.py:__init__()
    # Called by gui.qt.coinsplitting_tab.py:_on_split_button_clicked()
    # Called by gui.qt.network_dialog.py:__init__()
    # Called by scripts/stdio.py
    # Called by scripts/text.py
    def register_callback(self, callback, events):
        with self.lock:
            for event in events:
                self.callbacks[event].append(callback)

    # Called by gui.qt.main_window.py:clean_up()
    # Called by gui.qt.coinsplitting_tab.py:_split_cleanup()
    def unregister_callback(self, callback):
        with self.lock:
            for callbacks in self.callbacks.values():
                if callback in callbacks:
                    callbacks.remove(callback)

    # Called by exchange_rate.py:on_quotes()
    # Called by exchange_rate.py:on_history()
    # Called by synchronizer.py:tx_response()
    # Called by synchronizer.py:run()
    def trigger_callback(self, event, *args):
        with self.lock:
            callbacks = self.callbacks[event][:]
        [callback(event, *args) for callback in callbacks]

    def _recent_servers_file(self):
        return os.path.join(self.config.path, "recent-servers")

    def _read_recent_servers(self):
        if not self.config.path:
            return []
        try:
            with open(self._recent_servers_file(), "r", encoding='utf-8') as f:
                data = f.read()
                return json.loads(data)
        except:
            return []

    def _save_recent_servers(self):
        if not self.config.path:
            return
        s = json.dumps(self.recent_servers, indent=4, sort_keys=True)
        try:
            with open(self._recent_servers_file(), "w", encoding='utf-8') as f:
                f.write(s)
        except:
            pass

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.main_window.py:update_status()
    def get_server_height(self):
        return self.interface.tip if self.interface else 0

    def _server_is_lagging(self):
        sh = self.get_server_height()
        if not sh:
            logger.debug('no height for main interface')
            return True
        lh = self.get_local_height()
        result = (lh - sh) > 1
        if result:
            logger.debug('%s is lagging (%d vs %d)', self.default_server, sh, lh)
        return result

    def _set_status(self, status):
        self.connection_status = status
        self._notify('status')

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.main_window.py:notify_tx_cb()
    # Called by gui.qt.main_window.py:update_status()
    # Called by gui.qt.main_window.py:update_wallet()
    # Called by gui.qt.network_dialog.py:__init__()
    # Called by gui.stdio.py:get_balance()
    # Called by gui.text.py:print_balance()
    # Called by wallet.py:wait_until_synchronized()
    # Called by scripts/block_headers.py
    # Called by scripts/watch_address.py
    def is_connected(self):
        return self.interface is not None

    # Called by scripts/block_headers.py
    # Called by scripts/watch_address.py
    def is_connecting(self):
        return self.connection_status == 'connecting'

    def _queue_request(self, method, params, interface=None):
        # If you want to queue a request on any interface it must go
        # through this function so message ids are properly tracked
        if interface is None:
            interface = self.interface
        message_id = self.message_id
        self.message_id += 1
        if self.debug:
            logger.debug("%s --> %s %s %s", interface.host, method, params, message_id)
        interface.queue_request(method, params, message_id)
        return message_id

    def _send_subscriptions(self):
        logger.debug('sending subscriptions to %s %d %d', self.interface.server,
                     len(self.unanswered_requests), len(self.subscribed_addresses))
        self.sub_cache.clear()
        # Resend unanswered requests
        requests = self.unanswered_requests.values()
        self.unanswered_requests = {}
        for request in requests:
            message_id = self._queue_request(request[0], request[1])
            self.unanswered_requests[message_id] = request
        self._queue_request('server.banner', [])
        self._queue_request('server.donation_address', [])
        self._queue_request('server.peers.subscribe', [])
        self._queue_request('blockchain.relayfee', [])
        for h in self.subscribed_addresses:
            self._queue_request('blockchain.scripthash.subscribe', [h])

    def _get_status_value(self, key):
        if key == 'status':
            value = self.connection_status
        elif key == 'banner':
            value = self.banner
        elif key == 'updated':
            value = (self.get_local_height(), self.get_server_height())
        elif key == 'servers':
            value = self.get_servers()
        elif key == 'interfaces':
            value = self.get_interfaces()
        return value

    def _notify(self, key):
        if key in ['status', 'updated']:
            self.trigger_callback(key)
        else:
            self.trigger_callback(key, self._get_status_value(key))

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.main_window.py:donate_to_server()
    # Called by gui.qt.network_dialog.py:update()
    # Called by gui.qt.network_dialog.py:fill_in_proxy_settings()
    # Called by gui.qt.network_dialog.py:follow_server()
    # Called by gui.qt.network_dialog.py:set_server()
    # Called by gui.qt.network_dialog.py:set_proxy()
    def get_parameters(self):
        host, port, protocol = deserialize_server(self.default_server)
        return host, port, protocol, self.proxy, self.auto_connect

    # Called by gui.qt.main_window.py:donate_to_server()
    def get_donation_address(self):
        if self.is_connected():
            return self.donation_address

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.network_dialog.py:update()
    # Called by scripts/util.py
    def get_interfaces(self):
        '''The interfaces that are in connected state'''
        return list(self.interfaces.keys())

    # Called by commands.py:getservers()
    # Called by gui.qt.network_dialog.py:update()
    def get_servers(self):
        out = Net.DEFAULT_SERVERS
        if self.irc_servers:
            out.update(filter_version(self.irc_servers.copy()))
        else:
            for s in self.recent_servers:
                try:
                    host, port, protocol = deserialize_server(s)
                except:
                    continue
                if host not in out:
                    out[host] = { protocol:port }
        return out

    def _start_interface(self, server_key):
        """Start the given server if it is not already active or being connected to.

        Arguments:
        server_key --- server specifier in the form of '<host>:<port>:<protocol>'
        """
        if (not server_key in self.interfaces and not server_key in self.connecting):
            if server_key == self.default_server:
                logger.debug("connecting to %s as new interface", server_key)
                self._set_status('connecting')
            self.connecting.add(server_key)
            c = Connection(server_key, self.socket_queue, self.config.path)

    def _get_unavailable_servers(self):
        exclude_set = set(self.interfaces)
        exclude_set = exclude_set.union(self.connecting)
        exclude_set = exclude_set.union(self.disconnected_servers)
        exclude_set = exclude_set.union(self.blacklisted_servers)
        return exclude_set

    def _start_random_interface(self):
        exclude_set = self._get_unavailable_servers()
        server_key = _pick_random_server(self.get_servers(), self.protocol, exclude_set)
        if server_key:
            self._start_interface(server_key)

    def _start_interfaces(self):
        self._start_interface(self.default_server)
        for i in range(self.num_server - 1):
            self._start_random_interface()

    def _set_proxy(self, proxy):
        self.proxy = proxy
        # Store these somewhere so we can un-monkey-patch
        if not hasattr(socket, "_socketobject"):
            socket._socketobject = socket.socket
            socket._getaddrinfo = socket.getaddrinfo
        if proxy:
            logger.debug("setting proxy '%s'", proxy)
            proxy_mode = proxy_modes.index(proxy["mode"]) + 1
            socks.setdefaultproxy(proxy_mode,
                                  proxy["host"],
                                  int(proxy["port"]),
                                  # socks.py seems to want either None or a non-empty string
                                  username=(proxy.get("user", "") or None),
                                  password=(proxy.get("password", "") or None))
            socket.socket = socks.socksocket
            # prevent dns leaks, see http://stackoverflow.com/questions/13184205/dns-over-proxy
            socket.getaddrinfo = lambda *args: [(socket.AF_INET, socket.SOCK_STREAM,
                                                 6, '', (args[0], args[1]))]
        else:
            socket.socket = socket._socketobject
            socket.getaddrinfo = socket._getaddrinfo

    def _start_network(self, protocol, proxy):
        assert not self.interface and not self.interfaces
        assert not self.connecting and self.socket_queue.empty()
        logger.debug('starting network')
        self.disconnected_servers = set([])
        self.protocol = protocol
        self._set_proxy(proxy)
        self._start_interfaces()

    def _stop_network(self):
        logger.debug("stopping network")
        for interface in list(self.interfaces.values()):
            self._close_interface(interface)
        if self.interface:
            self._close_interface(self.interface)
        assert self.interface is None
        assert not self.interfaces
        self.connecting = set()
        # Get a new queue - no old pending connections thanks!
        self.socket_queue = queue.Queue()

    # Called by network_dialog.py:follow_server()
    # Called by network_dialog.py:set_server()
    # Called by network_dialog.py:set_proxy()
    def set_parameters(self, host, port, protocol, proxy, auto_connect):
        proxy_str = _serialize_proxy(proxy)
        server = serialize_server(host, port, protocol)
        # sanitize parameters
        try:
            deserialize_server(serialize_server(host, port, protocol))
            if proxy:
                proxy_modes.index(proxy["mode"]) + 1
                int(proxy['port'])
        except:
            return
        self.config.set_key('auto_connect', auto_connect, False)
        self.config.set_key("proxy", proxy_str, False)
        self.config.set_key("server", server, True)
        # abort if changes were not allowed by config
        if self.config.get('server') != server or self.config.get('proxy') != proxy_str:
            return
        self.auto_connect = auto_connect
        if self.proxy != proxy or self.protocol != protocol:
            # Restart the network defaulting to the given server
            self._stop_network()
            self.default_server = server
            self._start_network(protocol, proxy)
        elif self.default_server != server:
            self.switch_to_interface(server, self.SWITCH_SET_PARAMETERS)
        else:
            self._switch_lagging_interface()
            self._notify('updated')

    def _switch_to_random_interface(self):
        '''Switch to a random connected server other than the current one'''
        servers = self.get_interfaces()    # Those in connected state
        if self.default_server in servers:
            servers.remove(self.default_server)
        if servers:
            self.switch_to_interface(random.choice(servers))

    def _switch_lagging_interface(self):
        '''If auto_connect and lagging, switch interface'''
        if self._server_is_lagging() and self.auto_connect:
            # switch to one that has the longest chain
            interfaces = self.interfaces_by_blockchain().get(Blockchain.longest())
            if interfaces:
                choice = random.choice(interfaces)
                self.switch_to_interface(choice.server, self.SWITCH_LAGGING)

    SWITCH_DEFAULT = 'SWITCH_DEFAULT'
    SWITCH_RANDOM = 'SWITCH_RANDOM'
    SWITCH_LAGGING = 'SWITCH_LAGGING'
    SWITCH_SOCKET_LOOP = 'SWITCH_SOCKET_LOOP'
    SWITCH_FOLLOW_CHAIN = 'SWITCH_FOLLOW_CHAIN'
    SWITCH_SET_PARAMETERS = 'SWITCH_SET_PARAMETERS'

    # Called by network_dialog.py:follow_server()
    def switch_to_interface(self, server, switch_reason=None):
        '''Switch to server as our interface.  If no connection exists nor
        being opened, start a thread to connect.  The actual switch will
        happen on receipt of the connection notification.  Do nothing
        if server already is our interface.'''
        self.default_server = server
        if server not in self.interfaces:
            self.interface = None
            self._start_interface(server)
            return
        i = self.interfaces[server]
        if self.interface != i:
            logger.debug("switching to '%s' reason '%s'", server, switch_reason)
            # stop any current interface in order to terminate subscriptions
            # fixme: we don't want to close headers sub
            #self._close_interface(self.interface)
            self.interface = i
            self._send_subscriptions()
            self._set_status('connected')
            self._notify('updated')

    def _close_interface(self, interface):
        if interface:
            if interface.server in self.interfaces:
                self.interfaces.pop(interface.server)
            if interface.server == self.default_server:
                self.interface = None
            interface.close()

    def _add_recent_server(self, server):
        # list is ordered
        if server in self.recent_servers:
            self.recent_servers.remove(server)
        self.recent_servers.insert(0, server)
        self.recent_servers = self.recent_servers[0:20]
        self._save_recent_servers()

    def _process_response(self, interface, request, response, callbacks):
        if self.debug:
            logger.debug("<-- %s", response)
        error = response.get('error')
        result = response.get('result')
        method = response.get('method')
        params = response.get('params')

        # We handle some responses; return the rest to the client.
        if method == 'server.version':
            self._on_server_version(interface, result)
        elif method == 'blockchain.headers.subscribe':
            if error is None:
                self._on_notify_header(interface, result)
        elif method == 'server.peers.subscribe':
            if error is None:
                self.irc_servers = parse_servers(result)
                self._notify('servers')
        elif method == 'server.banner':
            if error is None:
                self.banner = result
                self._notify('banner')
        elif method == 'server.donation_address':
            if error is None:
                self.donation_address = result
        elif method == 'blockchain.relayfee':
            if error is None:
                self.relay_fee = int(result * COIN)
                logger.debug("relayfee %s", self.relay_fee)
        elif method == 'blockchain.block.headers':
            self._on_block_headers(interface, request, response)
        elif method == 'blockchain.block.header':
            self._on_header(interface, request, response)

        for callback in callbacks:
            callback(response)

    def _get_index(self, method, params):
        """ hashable index for subscriptions and cache"""
        return str(method) + (':' + str(params[0]) if params else '')

    def _process_responses(self, interface):
        responses = interface.get_responses()
        for request, response in responses:
            if request:
                method, params, message_id = request
                k = self._get_index(method, params)
                # client requests go through self.send() with a
                # callback, are only sent to the current interface,
                # and are placed in the unanswered_requests dictionary
                client_req = self.unanswered_requests.pop(message_id, None)
                if client_req:
                    assert interface == self.interface
                    callbacks = [client_req[2]]
                else:
                    # fixme: will only work for subscriptions
                    k = self._get_index(method, params)
                    callbacks = self.subscriptions.get(k, [])

                # Copy the request method and params to the response
                response['method'] = method
                response['params'] = params
                # Only once we've received a response to an addr subscription
                # add it to the list; avoids double-sends on reconnection
                if method == 'blockchain.scripthash.subscribe':
                    self.subscribed_addresses.add(params[0])
            else:
                if not response:  # Closed remotely / misbehaving
                    self._connection_down(interface.server)
                    break
                # Rewrite response shape to match subscription request response
                method = response.get('method')
                params = response.get('params')
                k = self._get_index(method, params)
                if method == 'blockchain.headers.subscribe':
                    response['result'] = params[0]
                    response['params'] = []
                elif method == 'blockchain.scripthash.subscribe':
                    response['params'] = [params[0]]  # addr
                    response['result'] = params[1]
                callbacks = self.subscriptions.get(k, [])

            # update cache if it's a subscription
            if method.endswith('.subscribe'):
                with self.interface_lock:
                    self.sub_cache[k] = response
            # Response is now in canonical form
            self._process_response(interface, request, response, callbacks)

    # Called by synchronizer.py:subscribe_to_addresses()
    def subscribe_to_scripthashes(self, scripthashes, callback):
        msgs = [('blockchain.scripthash.subscribe', [sh])
                for sh in scripthashes]
        self.send(msgs, callback)

    # Called by synchronizer.py:on_address_status()
    def request_scripthash_history(self, sh, callback):
        self.send([('blockchain.scripthash.get_history', [sh])], callback)

    # Called by commands.py:notify()
    # Called by websockets.py:reading_thread()
    # Called by websockets.py:run()
    # Called locally.
    def send(self, messages, callback):
        '''Messages is a list of (method, params) tuples'''
        if messages:
            with self.pending_sends_lock:
                self.pending_sends.append((messages, callback))

    def _process_pending_sends(self):
        # Requests needs connectivity.  If we don't have an interface,
        # we cannot process them.
        if not self.interface:
            return

        with self.pending_sends_lock:
            sends = self.pending_sends
            self.pending_sends = []

        for messages, callback in sends:
            for method, params in messages:
                r = None
                if method.endswith('.subscribe'):
                    k = self._get_index(method, params)
                    # add callback to list
                    l = self.subscriptions.get(k, [])
                    if callback not in l:
                        l.append(callback)
                    self.subscriptions[k] = l
                    # check cached response for subscriptions
                    r = self.sub_cache.get(k)
                if r is not None:
                    logger.debug("cache hit '%s'", k)
                    callback(r)
                else:
                    message_id = self._queue_request(method, params)
                    self.unanswered_requests[message_id] = method, params, callback

    # Called by synchronizer.py:release()
    def unsubscribe(self, callback):
        '''Unsubscribe a callback to free object references to enable GC.'''
        # Note: we can't unsubscribe from the server, so if we receive
        # subsequent notifications _process_response() will emit a harmless
        # "received unexpected notification" warning
        with self.lock:
            for v in self.subscriptions.values():
                if callback in v:
                    v.remove(callback)

    def _connection_down(self, server, blacklist=False):
        '''A connection to server either went down, or was never made.
        We distinguish by whether it is in self.interfaces.'''
        if blacklist:
            self.blacklisted_servers.add(server)
            # rt12 --- there might be a better place for this.
            self.config.set_key("server_blacklist", list(self.blacklisted_servers), True)
        else:
            self.disconnected_servers.add(server)
        if server == self.default_server:
            self._set_status('disconnected')
        if server in self.interfaces:
            self._close_interface(self.interfaces[server])
            self._notify('interfaces')
        for b in Blockchain.blockchains:
            if b.catch_up == server:
                b.catch_up = None

    def _new_interface(self, server_key, socket):
        self._add_recent_server(server_key)

        interface = Interface(server_key, socket)
        interface.requested_chunks = set()
        interface.blockchain = None
        interface.tip_raw = None
        interface.tip = 0
        interface.set_mode(Interface.MODE_VERIFICATION)

        with self.interface_lock:
            self.interfaces[server_key] = interface

        # server.version should be the first message
        params = [PACKAGE_VERSION, PROTOCOL_VERSION]
        self._queue_request('server.version', params, interface)
        if not self._request_checkpoint_headers(interface):
            self._subscribe_headers([interface])
        if server_key == self.default_server:
            self.switch_to_interface(server_key, self.SWITCH_DEFAULT)

    def _subscribe_headers(self, interfaces):
        # The interface will immediately respond with it's last known header.
        for interface in interfaces:
            interface.logger.debug('subscribing to headers')
            self._queue_request('blockchain.headers.subscribe', [], interface)

    def _maintain_sockets(self):
        '''Socket maintenance.'''
        # Responses to connection attempts?
        while not self.socket_queue.empty():
            server, socket = self.socket_queue.get()
            if server in self.connecting:
                self.connecting.remove(server)
            if socket:
                self._new_interface(server, socket)
            else:
                self._connection_down(server)

        # Send pings and shut down stale interfaces
        # must use copy of values
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        for interface in interfaces:
            if interface.has_timed_out():
                self._connection_down(interface.server)
            elif interface.ping_required():
                self._queue_request('server.ping', [], interface)

        now = time.time()
        # nodes
        with self.interface_lock:
            server_count = len(self.interfaces) + len(self.connecting)
            if server_count < self.num_server:
                self._start_random_interface()
                if now - self.nodes_retry_time > NODES_RETRY_INTERVAL:
                    logger.debug('retrying connections')
                    self.disconnected_servers = set([])
                    self.nodes_retry_time = now

        # main interface
        with self.interface_lock:
            if not self.is_connected():
                if self.auto_connect:
                    if not self.is_connecting():
                        self._switch_to_random_interface()
                else:
                    if self.default_server in self.disconnected_servers:
                        if now - self.server_retry_time > SERVER_RETRY_INTERVAL:
                            self.disconnected_servers.remove(self.default_server)
                            self.server_retry_time = now
                    else:
                        self.switch_to_interface(self.default_server, self.SWITCH_SOCKET_LOOP)

    def _request_headers(self, interface, base_height, count):
        assert count <=2016
        cp_height = app_state.headers.checkpoint.height
        params = (base_height, count, cp_height if base_height + count < cp_height else 0)
        # The verifier spams us...
        if params not in interface.requested_chunks:
            interface.requested_chunks.add(params)
            interface.logger.info(f'requesting {count:,d} headers from height {base_height:,d}')
            self._queue_request('blockchain.block.headers', params, interface)

    def _on_block_headers(self, interface, request, response):
        '''Handle receiving a chunk of block headers'''
        error = response.get('error')
        result = response.get('result')
        params = response.get('params')

        if not request or result is None or params is None or error is not None:
            interface.logger.error(error or 'bad response')
            return

        request_params = request[1]
        request_base_height, expected_header_count, cp_height = request_params

        # Ignore unsolicited chunks (how can this even happen with request provided?)
        try:
            interface.requested_chunks.remove(request_params)
        except KeyError:
            interface.logger.error("unsolicited chunk base_height=%s count=%s",
                                   request_base_height, expected_header_count)
            return

        hexdata = result['hex']
        header_hexsize = 80 * 2
        raw_chunk = bfh(hexdata)
        actual_header_count = len(raw_chunk) // 80
        # We accept fewer headers than we asked for, to cover the case where the distance
        # to the tip was unknown.
        if actual_header_count > expected_header_count:
            interface.logger.error("chunk data size incorrect expected_size=%s actual_size=%s",
                                   expected_header_count * 80, len(raw_chunk))
            return

        proof_was_provided = False
        if 'root' in result and 'branch' in result:
            header_height = request_base_height + actual_header_count - 1
            header_offset = (actual_header_count - 1) * header_hexsize
            header = hexdata[header_offset : header_offset + header_hexsize]
            if not self._validate_checkpoint_result(interface, result["root"],
                                                   result["branch"], header, header_height):
                # Got checkpoint validation data, server failed to provide proof.
                interface.logger.error("blacklisting server for incorrect checkpoint proof")
                self._connection_down(interface.server, blacklist=True)
                return
            proof_was_provided = True
        elif len(request_params) == 3 and request_params[2] != 0:
            # Expected checkpoint validation data, did not receive it.
            self._connection_down(interface.server)
            return

        were_needed = Blockchain.needs_checkpoint_headers
        try:
            interface.blockchain = Blockchain.connect_chunk(request_base_height, raw_chunk,
                                                            proof_was_provided)
        except (IncorrectBits, InsufficientPoW, MissingHeader) as e:
            interface.logger.error(f'blacklisting server for failed connect_chunk: {e}')
            self._connection_down(interface.server, blacklist=True)
            return

        interface.logger.debug("connected chunk, height=%s count=%s",
                               request_base_height, actual_header_count)

        # If we connected the checkpoint headers all interfaces can subscribe to headers
        if were_needed and not self._request_checkpoint_headers(interface):
            with self.interface_lock:
                self._subscribe_headers(self.interfaces.values())

        if not interface.requested_chunks:
            if interface.blockchain.height() < interface.tip:
                self._request_headers(interface, interface.blockchain.height(), 1000)
            else:
                interface.set_mode(Interface.MODE_DEFAULT)
                interface.logger.debug('catch up done %s', interface.blockchain.height())
                interface.blockchain.catch_up = None
        self._notify('updated')

    def _request_header(self, interface, height):
        '''
        This works for all modes except for 'default'.

        If it is to be used for piecemeal filling of the sparse blockchain
        headers file before the checkpoint height, it needs extra
        handling for the 'default' mode.

        A server interface does not get associated with a blockchain
        until it gets handled in the response to it's first header
        request.
        '''
        interface.logger.debug("requesting header %d", height)
        if height > Net.VERIFICATION_BLOCK_HEIGHT:
            params = [height]
        else:
            params = [height, Net.VERIFICATION_BLOCK_HEIGHT]
        self._queue_request('blockchain.block.header', params, interface)
        return True

    def _on_header(self, interface, request, response):
        '''Handle receiving a single block header'''
        result = response.get('result')
        if not result:
            interface.logger.error(response)
            self._connection_down(interface.server)
            return

        if not request:
            interface.logger.error("blacklisting server for sending unsolicited header, "
                                   "no request, params=%s", response['params'])
            self._connection_down(interface.server, blacklist=True)
            return
        request_params = request[1]
        height = request_params[0]

        response_height = response['params'][0]
        # This check can be removed if request/response params are reconciled in some sort
        # of rewrite.
        if height != response_height:
            interface.logger.error("unsolicited header request=%s request_height=%s "
                                   "response_height=%s", request_params, height, response_height)
            self._connection_down(interface.server)
            return

        # FIXME: we need to assert we get a proof if we need / requested one
        proof_was_provided = False
        hexheader = None
        if 'root' in result and 'branch' in result and 'header' in result:
            hexheader = result["header"]
            if not self._validate_checkpoint_result(interface, result["root"],
                                                   result["branch"], hexheader, height):
                # Got checkpoint validation data, failed to provide proof.
                interface.logger.error("unprovable header request=%s height=%s",
                                       request_params, height)
                self._connection_down(interface.server)
                return
            proof_was_provided = True
        else:
            hexheader = result

        # Simple header request.
        raw_header = bfh(hexheader)
        try:
            _header, interface.blockchain = Blockchain.connect(height, raw_header,
                                                               proof_was_provided)
            interface.logger.debug(f'Connected header at height {height:,d}')
        except MissingHeader as e:
            interface.logger.info(f'failed to connect header at height {height:,d}: {e}')
            interface.blockchain = None
        except (IncorrectBits, InsufficientPoW) as e:
            interface.logger.error(f'blacklisting server for failed _on_header connect: {e}')
            self._connection_down(interface.server, blacklist=True)
            return

        if interface.mode == Interface.MODE_BACKWARD:
            if interface.blockchain:
                interface.set_mode(Interface.MODE_BINARY)
                interface.good = height
                next_height = (interface.bad + interface.good) // 2
            else:
                # A backwards header request should not happen before the checkpoint
                # height. It isn't requested in this context, and it isn't requested
                # anywhere else. If this happens it is an error. Additionally, if the
                # checkpoint height header was requested and it does not connect, then
                # there's not much ElectrumSV can do about it (that we're going to
                # bother). We depend on the checkpoint being relevant for the blockchain
                # the user is running against.
                assert height > Net.VERIFICATION_BLOCK_HEIGHT
                interface.bad = height
                delta = interface.tip - height
                # If the longest chain does not connect at any point we check to the
                # chain this interface is serving, then we fall back on the checkpoint
                # height which is expected to work.
                next_height = max(Net.VERIFICATION_BLOCK_HEIGHT + 1,
                                  interface.tip - 2 * delta)
        elif interface.mode == Interface.MODE_BINARY:
            if interface.blockchain:
                interface.good = height
            else:
                interface.bad = height
            next_height = (interface.bad + interface.good) // 2
            if next_height == interface.good:
                interface.set_mode(Interface.MODE_CATCH_UP)
        elif interface.mode == Interface.MODE_CATCH_UP:
            if interface.blockchain is None:
                # go back
                interface.logger.info("cannot connect %d", height)
                interface.set_mode(Interface.MODE_BACKWARD)
                interface.bad = height
                next_height = height - 1
            else:
                next_height = height + 1 if height < interface.tip else None

            if next_height is None:
                # exit catch_up state
                interface.logger.debug('catch up done %d', interface.blockchain.height())
                interface.blockchain.catch_up = None
                self._switch_lagging_interface()
                self._notify('updated')
        elif interface.mode == Interface.MODE_DEFAULT:
            interface.logger.error(f'ignored header {_header} received in default mode')
            return

        # If not finished, get the next header
        if next_height:
            if interface.mode == Interface.MODE_CATCH_UP and interface.tip > next_height:
                self._request_headers(interface, next_height, 1000)
            else:
                self._request_header(interface, next_height)
        else:
            interface.set_mode(Interface.MODE_DEFAULT)
            self._notify('updated')
        # refresh network dialog
        self._notify('interfaces')

    def maintain_requests(self):
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        for interface in interfaces:
            if interface.unanswered_requests and time.time() - interface.request_time > 20:
                # The last request made is still outstanding, and was over 20 seconds ago.
                interface.logger.error("blockchain request timed out")
                self._connection_down(interface.server)
                continue

    def wait_on_sockets(self):
        # Python docs say Windows doesn't like empty selects.
        # Sleep to prevent busy looping
        if not self.interfaces:
            time.sleep(0.1)
            return
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        rin = [i for i in interfaces]
        win = [i for i in interfaces if i.num_requests()]
        try:
            rout, wout, xout = select.select(rin, win, [], 0.1)
        except socket.error as e:
            # TODO: py3, get code from e
            code = None
            if code == errno.EINTR:
                return
            raise
        assert not xout
        for interface in wout:
            interface.send_requests()
        for interface in rout:
            self._process_responses(interface)

    def run(self):
        while self.is_running():
            self._maintain_sockets()
            self.wait_on_sockets()
            self.maintain_requests()
            if not Blockchain.needs_checkpoint_headers:
                self.run_jobs()    # Synchronizer and Verifier and Fx
            self._process_pending_sends()
        self._stop_network()
        self.on_stop()

    def _on_server_version(self, interface, version_data):
        interface.server_version = version_data

    def _request_checkpoint_headers(self, interface):
        start_height, count = Blockchain.required_checkpoint_headers()
        if count:
            interface.logger.info('requesting checkpoint headers')
            self._request_headers(interface, start_height, count)
        return count != 0

    def _on_notify_header(self, interface, header_dict):
        '''
        When we subscribe for 'blockchain.headers.subscribe', a server will send
        us it's topmost header.  After that, it will forward on any additional
        headers as it receives them.
        '''
        if 'hex' not in header_dict or 'height' not in header_dict:
            self._connection_down(interface.server)
            return

        header_hex = header_dict['hex']
        raw_header = bfh(header_hex)
        height = header_dict['height']

        # If the server is behind the verification height, then something is wrong with
        # it.  Drop it.
        if height <= Net.VERIFICATION_BLOCK_HEIGHT:
            self._connection_down(interface.server)
            return

        # We will always update the tip for the server.
        interface.tip_raw = raw_header
        interface.tip = height
        interface.set_mode(Interface.MODE_DEFAULT)
        self._process_latest_tip(interface)

    def _process_latest_tip(self, interface):
        if interface.mode != Interface.MODE_DEFAULT:
            return

        try:
            header, blockchain = Blockchain.connect(interface.tip, interface.tip_raw, False)
        except MissingHeader as e:
            interface.logger.info(str(e))
        except (IncorrectBits, InsufficientPoW) as e:
            interface.logger.error(f'blacklisting server for failed connect: {e}')
            self._connection_down(interface.server, blacklist=True)
            return
        else:
            interface.logger.info(f'Connected {header}')
            interface.blockchain = blockchain
            self._switch_lagging_interface()
            self._notify('updated')
            self._notify('interfaces')
            return

        height = interface.tip
        heights = [x.height() for x in Blockchain.blockchains]
        tip = max(heights)
        if tip > Net.VERIFICATION_BLOCK_HEIGHT:
            interface.logger.debug('attempt to connect: our tip={tip:,d} their tip={height:,d}')
            interface.set_mode(Interface.MODE_BACKWARD)
            interface.bad = height
            self._request_header(interface, min(tip, height - 1))
        else:
            interface.logger.debug('attempt to catch up: our tip={tip:,d} their tip={height:,d}')
            chain = Blockchain.longest()
            if chain.catch_up is None:
                chain.catch_up = interface
                interface.set_mode(Interface.MODE_CATCH_UP)
                interface.blockchain = chain
                interface.logger.debug("switching to catchup mode %s", tip)
                self._request_header(interface,
                                    Net.VERIFICATION_BLOCK_HEIGHT + 1)
            else:
                interface.logger.debug("chain already catching up with %s", chain.catch_up.server)

    def _validate_checkpoint_result(self, interface, merkle_root, merkle_branch,
                                   header, header_height):
        '''
        header: hex representation of the block header.
        merkle_root: hex representation of the server's calculated merkle root.
        branch: list of hex representations of the server's calculated merkle root branches.

        Returns a boolean to represent whether the server's proof is correct.
        '''
        received_merkle_root = bytes(reversed(bfh(merkle_root)))
        if Net.VERIFICATION_BLOCK_MERKLE_ROOT:
            expected_merkle_root = bytes(reversed(bfh(
                Net.VERIFICATION_BLOCK_MERKLE_ROOT)))
        else:
            expected_merkle_root = received_merkle_root

        if received_merkle_root != expected_merkle_root:
            interface.logger.error("Sent unexpected merkle root, expected: '%s', got: '%s'",
                                   Net.VERIFICATION_BLOCK_MERKLE_ROOT,
                                   merkle_root)
            return False

        header_hash = sha256d(bfh(header))
        byte_branches = [ bytes(reversed(bfh(v))) for v in merkle_branch ]
        proven_merkle_root = blockchain.root_from_proof(header_hash, byte_branches, header_height)
        if proven_merkle_root != expected_merkle_root:
            interface.logger.error("Sent incorrect merkle branch, expected: '%s', proved: '%s'",
                                   Net.VERIFICATION_BLOCK_MERKLE_ROOT,
                                   util.hfu(reversed(proven_merkle_root)))
            return False

        return True

    def blockchain(self):
        if self.interface and self.interface.blockchain:
            return self.interface.blockchain   # Can be None
        return Blockchain.longest()

    def interfaces_by_blockchain(self):
        '''Returns a map {blockchain: list of interfaces} for each blockchain being
        followed by any interface.'''
        result = defaultdict(list)
        for interface in self.interfaces.values():
            if interface.blockchain:
                result[interface.blockchain].append(interface)
        return result

    def blockchain_count(self):
        return len(self.interfaces_by_blockchain())

    # Called by daemon.py:run_daemon()
    # Called by verifier.py:run()
    # Called by gui.qt.main_window.py:update_status()
    # Called by gui.qt.network_dialog.py:update()
    # Called by wallet.py:sweep()
    def get_local_height(self):
        return self.blockchain().height()

    # Called by gui.qt.main_window.py:do_process_from_txid()
    # Called by wallet.py:append_utxos_to_inputs()
    # Called by scripts/get_history.py
    def synchronous_get(self, request, timeout=30):
        q = queue.Queue()
        self.send([request], q.put)
        try:
            r = q.get(True, timeout)
        except queue.Empty:
            raise Exception('Server did not answer')
        if r.get('error'):
            raise Exception(r.get('error'))
        return r.get('result')

    @staticmethod
    def __wait_for(it):
        """Wait for the result of calling lambda `it`."""
        q = queue.Queue()
        it(q.put)
        try:
            result = q.get(block=True, timeout=30)
        except queue.Empty:
            raise util.TimeoutException(_('Server did not answer'))

        if result.get('error'):
            # Text should not be sanitized before user display
            raise RPCError(result['error'])

        return result.get('result')

    @staticmethod
    def __with_default_synchronous_callback(invocation, callback):
        """ Use this method if you want to make the network request
        synchronous. """
        if not callback:
            return Network.__wait_for(invocation)

        invocation(callback)

    # Called by commands.py:broadcast()
    # Called by main_window.py:broadcast_transaction()
    def broadcast_transaction(self, transaction):
        command = 'blockchain.transaction.broadcast'
        invocation = lambda c: self.send([(command, [str(transaction)])], c)
        our_txid = transaction.txid()

        try:
            their_txid = Network.__wait_for(invocation)
        except RPCError as e:
            msg = sanitized_broadcast_message(e.args[0])
            return False, _('transaction broadcast failed: ') + msg
        except util.TimeoutException:
            return False, e.args[0]

        if their_txid != our_txid:
            try:
                their_txid = int(their_txid, 16)
            except ValueError:
                return False, _('bad server response; it is unknown whether '
                                'the transaction broadcast succeeded')
            logger.warning(f'server TxID {their_txid} differs from ours {our_txid}')

        return True, our_txid

    # Called by verifier.py:run()
    def get_merkle_for_transaction(self, tx_hash, tx_height, callback=None):
        command = 'blockchain.transaction.get_merkle'
        invocation = lambda c: self.send([(command, [tx_hash, tx_height])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)


def sanitized_broadcast_message(error):
    unknown_reason = _('reason unknown')
    try:
        msg = str(error['message'])
    except:
        msg = ''   # fall-through

    if 'dust' in msg:
        return _('very small "dust" payments')
    if ('Missing inputs' in msg or 'Inputs unavailable' in msg or
        'bad-txns-inputs-spent' in msg):
        return _('missing, already-spent, or otherwise invalid coins')
    if 'insufficient priority' in msg:
        return _('insufficient fees or priority')
    if 'bad-txns-premature-spend-of-coinbase' in msg:
        return _('attempt to spend an unmatured coinbase')
    if 'txn-already-in-mempool' in msg or 'txn-already-known' in msg:
        return _("it already exists in the server's mempool")
    if 'txn-mempool-conflict' in msg:
        return _("it conflicts with one already in the server's mempool")
    if 'bad-txns-nonstandard-inputs' in msg:
        return _('use of non-standard input scripts')
    if 'absurdly-high-fee' in msg:
        return _('fee is absurdly high')
    if 'non-mandatory-script-verify-flag' in msg:
        return _('the script fails verification')
    if 'tx-size' in msg:
        return _('transaction is too large')
    if 'scriptsig-size' in msg:
        return _('it contains an oversized script')
    if 'scriptpubkey' in msg:
        return _('it contains a non-standard signature')
    if 'bare-multisig' in msg:
        return _('it contains a bare multisig input')
    if 'multi-op-return' in msg:
        return _('it contains more than 1 OP_RETURN input')
    if 'scriptsig-not-pushonly' in msg:
        return _('a scriptsig is not simply data')
    if 'bad-txns-nonfinal' in msg:
        return _("transaction is not final")
    logger.info(f'server error (untrusted): {error}')
    return unknown_reason
