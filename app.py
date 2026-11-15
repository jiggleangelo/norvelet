import os

from app.usable import make_server


def _enabled(value):
    return value.strip().casefold() in ('1', 'true', 'yes', 'on')


def main():
    host = os.environ.get('APP_HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8080'))
    public_base_url = os.environ.get('PUBLIC_BASE_URL', 'https://app-norvelet.manykiln.com')
    data_dir = os.environ.get('DATA_DIR', '/tmp/norvelet-data')
    cap_mb = int(os.environ.get('APP_DATA_CAP_MB', '200'))
    https_terminated = _enabled(os.environ.get('APP_HTTPS_TERMINATED', 'false'))
    server = make_server(
        host=host,
        port=port,
        public_base_url=public_base_url,
        data_dir=data_dir,
        data_cap_mb=cap_mb,
        https_terminated=https_terminated,
    )
    print('Norvelet: ' + public_base_url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
