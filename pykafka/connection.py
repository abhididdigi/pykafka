from __future__ import division
"""
Author: Keith Bourgoin, Emmett Butler
"""
__license__ = """
Copyright 2015 Parse.ly, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
__all__ = ["BrokerConnection"]
import logging
import ssl
import socket
import struct

from .exceptions import SocketDisconnectedError
from .utils.socket import recvall_into
from .utils.compat import buffer

log = logging.getLogger(__name__)


class SslConfig(object):
    """Config object for SSL connections

    Supplanting this class with your own is simple: if you are not going to
    be using the `pykafka.rdkafka` classes, only a method `wrap_socket()` is
    expected (so you can eg. simply pass in a plain `ssl.SSLContext`
    instance instead).  The `pykafka.rdkafka` classes require four further
    attributes: `cafile`, `certfile`, `keyfile`, and `password` (for details,
    see init docstring)
    """
    def __init__(self,
                 cafile,
                 certfile=None,
                 keyfile=None,
                 password=None):
        """Specify certificates for SSL connection

        :param cafile: Path to trusted CA certificate
        :type cafile: str
        :param certfile: Path to client certificate
        :type certfile: str
        :param keyfile: Path to client private-key file
        :type keyfile: str
        :param password: Password for private key
        :type password: bytes
        """
        self.cafile = cafile
        self.certfile = certfile
        self.keyfile = keyfile
        self.password = password
        self._wrap_socket = None

    def wrap_socket(self, sock):
        """Wrap a socket in an SSL context (see `ssl.wrap_socket`)

        :param socket: Plain socket
        :type socket: :class:`socket.socket`
        """
        if self._wrap_socket is None:
            if hasattr(ssl, 'SSLContext'):
                ssl_context = ssl.create_default_context(cafile=self.cafile)
                ssl_context.check_hostname = False
                if self.certfile is not None:
                    ssl_context.load_cert_chain(certfile=self.certfile,
                                                keyfile=self.keyfile,
                                                password=self.password)
                self._wrap_socket = ssl_context.wrap_socket
            else:  # Python version pre-2.7.9
                raise NotImplementedError("TODO")
        return self._wrap_socket(sock)


class BrokerConnection(object):
    """
    BrokerConnection thinly wraps a `socket.create_connection` call
    and handles the sending and receiving of data that conform to the
    kafka binary protocol over that socket.
    """
    def __init__(self,
                 host,
                 port,
                 buffer_size=1024 * 1024,
                 source_host='',
                 source_port=0,
                 ssl_config=None):
        """Initialize a socket connection to Kafka.

        :param host: The host to which to connect
        :type host: str
        :param port: The port on the host to which to connect.  Assumed to be
            an ssl-endpoint if (and only if) `ssl_config` is also provided
        :type port: int
        :param buffer_size: The size (in bytes) of the buffer in which to
            hold response data.
        :type buffer_size: int
        :param source_host: The host portion of the source address for
            the socket connection
        :type source_host: str
        :param source_port: The port portion of the source address for
            the socket connection
        :type source_port: int
        :param ssl_config: Config object for SSL connection
        :type ssl_config: :class:`pykafka.connection.SslConfig`
        """
        self._buff = bytearray(buffer_size)
        self.host = host
        self.port = port
        self._socket = None
        self.source_host = source_host
        self.source_port = source_port
        self._wrap_socket = (
            ssl_config.wrap_socket if ssl_config else lambda x: x)

    def __del__(self):
        """Close this connection when the object is deleted."""
        self.disconnect()

    @property
    def connected(self):
        """Returns true if the socket connection is open."""
        return self._socket is not None

    def connect(self, timeout):
        """Connect to the broker."""
        log.debug("Connecting to %s:%s", self.host, self.port)
        self._socket = self._wrap_socket(socket.create_connection(
            (self.host, self.port),
            timeout / 1000,
            (self.source_host, self.source_port)
        ))
        if self._socket is not None:
            log.debug("Successfully connected to %s:%s", self.host, self.port)

    def disconnect(self):
        """Disconnect from the broker."""
        if self._socket is None:
            return
        try:
            self._socket.close()
        except IOError:
            pass
        finally:
            self._socket = None

    def reconnect(self):
        """Disconnect from the broker, then reconnect"""
        self.disconnect()
        self.connect(10 * 1000)

    def request(self, request):
        """Send a request over the socket connection"""
        bytes_ = request.get_bytes()
        if not self._socket:
            raise SocketDisconnectedError
        try:
            self._socket.sendall(bytes_)
        except SocketDisconnectedError:
            self.disconnect()
            raise

    def response(self):
        """Wait for a response from the broker"""
        size = bytes()
        expected_len = 4  # Size => int32
        while len(size) != expected_len:
            try:
                r = self._socket.recv(expected_len - len(size))
            except IOError:
                r = None
            if r is None or len(r) == 0:
                # Happens when broker has shut down
                self.disconnect()
                raise SocketDisconnectedError
            size += r
        size = struct.unpack('!i', size)[0]
        try:
            recvall_into(self._socket, self._buff, size)
        except SocketDisconnectedError:
            self.disconnect()
            raise
        # Drop CorrelationId => int32
        return buffer(self._buff[4:4 + size])
