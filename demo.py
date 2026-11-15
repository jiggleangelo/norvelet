import os

from app.hosting import configure_server
from app.intake import FakeIntake
from app.store import Store
from app.web import SystemClock, make_server


def _enabled(value):
    return value.strip().casefold() in ('1', 'true', 'yes', 'on')


def main():
    host = os.environ.get('DEMO_HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8080'))
    public_base_url = os.environ.get('PUBLIC_BASE_URL')
    https_terminated = _enabled(os.environ.get('DEMO_HTTPS_TERMINATED', 'false'))

    server = make_server(
        port=port,
        host=host,
        store=Store(),
        clock=SystemClock(),
        intake=FakeIntake(),
    )
    configure_server(server, public_base_url, https_terminated)
    display_url = public_base_url or 'http://{}:{}'.format(*server.server_address)
    print('PackPilot fictional demo: ' + display_url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
