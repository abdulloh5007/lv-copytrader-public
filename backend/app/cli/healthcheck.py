"""Container liveness without credentials in command-line arguments."""
import base64
import os
from urllib.request import Request, urlopen


def main() -> None:
    request = Request('http://127.0.0.1:8000/api/health')
    username = os.environ.get('SP_AUTH_USER', '')
    password = os.environ.get('SP_AUTH_PASSWORD', '')
    if username and password:
        token = base64.b64encode(f'{username}:{password}'.encode()).decode()
        request.add_header('Authorization', 'Basic ' + token)
    with urlopen(request, timeout=4) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
