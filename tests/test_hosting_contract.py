import http.client
import os
import pathlib
import socket
import subprocess
import sys
import time
import unittest


class HostingContractTests(unittest.TestCase):
    def test_contract_environment_serves_public_host(self):
        root = pathlib.Path(__file__).parents[1]
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]

        public_base_url = 'https://try-norvelet.manykiln.com'
        env = os.environ.copy()
        env.pop('HOST', None)
        env.update({
            'DEMO_HOST': '127.0.0.1',
            'PORT': str(port),
            'PUBLIC_BASE_URL': public_base_url,
            'DEMO_HTTPS_TERMINATED': 'true',
        })
        process = subprocess.Popen(
            [sys.executable, 'demo.py'],
            cwd=root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            last_error = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail('demo.py exited before the hosting-contract health check')
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
                try:
                    connection.request('GET', '/healthz', headers={
                        'Host': 'try-norvelet.manykiln.com',
                    })
                    response = connection.getresponse()
                    body = response.read().decode()
                    self.assertEqual((response.status, body), (200, 'ok'))
                    break
                except OSError as error:
                    last_error = error
                    time.sleep(0.05)
                finally:
                    connection.close()
            else:
                self.fail('demo did not become healthy: {!r}'.format(last_error))

            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                connection.request('GET', '/', headers={
                    'Host': 'try-norvelet.manykiln.com',
                })
                response = connection.getresponse()
                page = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertIn('action="' + public_base_url + '/intake"', page)
                self.assertIn('Secure', response.getheader('Set-Cookie'))
            finally:
                connection.close()

            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                connection.request('GET', '/healthz', headers={'Host': 'untrusted.example'})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 400)
            finally:
                connection.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
