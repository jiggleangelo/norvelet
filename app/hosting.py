import re
from urllib.parse import urlsplit


_ROOT_URL_ATTRIBUTE = re.compile(r'(?P<prefix>\b(?:action|href|src)=["\'])(?P<path>/[^"\']*)')


def _parse_public_base_url(value):
    if not value:
        return None, None, None
    parts = urlsplit(value)
    if (
        parts.scheme not in ('http', 'https')
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ('', '/')
        or parts.query
        or parts.fragment
    ):
        raise ValueError('PUBLIC_BASE_URL must be an http(s) origin without credentials, path, query or fragment')
    try:
        parts.port
    except ValueError as error:
        raise ValueError('PUBLIC_BASE_URL contains an invalid port') from error
    return value.rstrip('/'), parts.scheme, _normalized_authority(parts.netloc, parts.scheme)


def _normalized_authority(value, scheme):
    if not value or any(character.isspace() for character in value) or '@' in value:
        return None
    try:
        parsed = urlsplit(scheme + '://' + value)
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        return None
    effective_port = port if port is not None else (443 if scheme == 'https' else 80)
    return parsed.hostname.rstrip('.').casefold(), effective_port


def configure_server(server, public_base_url=None, https_terminated=False):
    """Apply the studio hosting contract without trusting proxy-supplied addresses."""
    public_base, public_scheme, public_authority = _parse_public_base_url(public_base_url)
    base_handler = server.RequestHandlerClass

    class HostedHandler(base_handler):
        _public_base_url = public_base
        _public_scheme = public_scheme
        _public_authority = public_authority
        _https_terminated = bool(https_terminated)

        def _host_is_allowed(self):
            if self._public_authority is None:
                return True
            values = self.headers.get_all('Host', [])
            return (
                len(values) == 1
                and _normalized_authority(values[0], self._public_scheme) == self._public_authority
            )

        def _reject_untrusted_host(self):
            if self._host_is_allowed():
                return False
            self._error(400, 'Invalid Host header')
            return True

        def do_GET(self):
            if self._reject_untrusted_host():
                return
            super().do_GET()

        def do_POST(self):
            if self._reject_untrusted_host():
                return
            super().do_POST()

        def send_header(self, keyword, value):
            if (
                self._public_base_url
                and keyword.casefold() == 'set-cookie'
                and not re.search(r'(?:^|;)\s*secure(?:;|$)', value, re.IGNORECASE)
            ):
                value += '; Secure'
            super().send_header(keyword, value)

        def _send(self, status, payload, content_type='text/html; charset=utf-8', headers=None):
            if self._public_base_url and content_type.lower().startswith('text/html'):
                payload = _ROOT_URL_ATTRIBUTE.sub(
                    lambda match: match.group('prefix') + self._public_base_url + match.group('path'),
                    payload,
                )
            if self._public_base_url and headers and headers.get('Location', '').startswith('/'):
                headers = {**headers, 'Location': self._public_base_url + headers['Location']}
            super()._send(status, payload, content_type, headers)

    server.RequestHandlerClass = HostedHandler
    server.public_base_url = public_base
    server.https_terminated = bool(https_terminated)
    return server
