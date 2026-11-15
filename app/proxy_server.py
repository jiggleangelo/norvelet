from http.server import ThreadingHTTPServer
from threading import BoundedSemaphore


class ProxySafeBoundedHTTPServer(ThreadingHTTPServer):
    """Globally bounded server without per-IP quotas that collapse behind a proxy."""

    daemon_threads = True

    def __init__(self, address, handler, max_threads=16):
        if max_threads < 1:
            raise ValueError('max_threads must be positive')
        self._slots = BoundedSemaphore(max_threads)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
