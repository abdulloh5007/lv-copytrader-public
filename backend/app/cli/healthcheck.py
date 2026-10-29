"""HTTP liveness; independent of dashboard password rotation and broker readiness."""
from urllib.request import Request, urlopen


def main() -> None:
    request = Request('http://127.0.0.1:8000/api/auth/status')
    with urlopen(request, timeout=4) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
