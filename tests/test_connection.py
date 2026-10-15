from types import SimpleNamespace
import unittest

from connection import open_instrument


class MissingResource(Exception):
    error_code = -1073807343


class ConnectionTests(unittest.TestCase):
    def test_default_vxi11_missing_uses_socket(self):
        attempts = []
        instrument = object()

        def open_resource(address):
            attempts.append(address)
            if address.endswith('INSTR'):
                raise MissingResource('VXI-11 unavailable')
            return instrument

        self.assertIs(open_instrument(SimpleNamespace(open_resource=open_resource), '192.168.10.103'), instrument)
        self.assertEqual(attempts, ['TCPIP0::192.168.10.103::inst0::INSTR',
                                   'TCPIP0::192.168.10.103::5025::SOCKET'])

    def test_explicit_resource_does_not_fallback(self):
        attempts = []

        def open_resource(address):
            attempts.append(address)
            raise MissingResource('not present')

        resource = 'TCPIP0::192.168.10.103::custom::INSTR'
        with self.assertRaisesRegex(RuntimeError, 'custom::INSTR'):
            open_instrument(SimpleNamespace(open_resource=open_resource), '192.168.10.103', resource)
        self.assertEqual(attempts, [resource])

    def test_failure_preserves_both_attempted_addresses(self):
        def open_resource(address):
            raise MissingResource('not present')

        with self.assertRaisesRegex(RuntimeError, 'inst0::INSTR.*5025::SOCKET'):
            open_instrument(SimpleNamespace(open_resource=open_resource), '192.168.10.103')
