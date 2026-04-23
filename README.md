# Proxy List Fetcher

A Python CLI tool that fetches proxy lists from multiple sources, optionally tests their availability and latency, and writes the results to a file.

## Features

- **Fetch proxies** from multiple HTTP/SOCKS4/SOCKS5 sources concurrently.
- **Validate proxies** by testing them against a target URL with latency scoring.
- **Filter by protocol** — HTTP only, SOCKS4 only, or SOCKS5 only.
- **Add custom prefixes** to proxy lines for downstream tools.
- **Limit results** to the *N* fastest working proxies.
- **Progress bars** via `tqdm` for real-time feedback.
- **Environment-driven configuration** for CI/CD and automation.
- **Connection pooling** using a reusable `requests.Session` for improved performance.

## Requirements

- Python 3.12+
- `requests`
- `click`
- `tqdm`
- `python-dotenv` (optional, for local `.env` files)
- `requests[socks]` (optional, for SOCKS proxy testing)

## Installation

```bash
# Clone the repository
git clone https://github.com/jn-s3s/proxy-list.git
cd proxy-list

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip instal dotenv
```

## Usage

### Basic — fetch all proxies without testing

```bash
python main.py all
```

### Fetch and test proxies against a target URL

```bash
python main.py httpbin.org/get --limit 50
```

### Filter by protocol

```bash
python main.py all --http-only
python main.py all --socks4-only
python main.py all --socks5-only
```

### Add custom prefixes

```bash
python main.py all --add-prefix HTTP=px- --add-prefix SOCKS5=ss-
```

### Custom output file

```bash
python main.py all --filename proxies.txt
```

### Add extra HTTP headers

```bash
python main.py httpbin.org/get -H "User-Agent: MyApp/1.0" -H "Accept: application/json"
```

## CLI Options

| Option | Short | Description |
|--------|-------|-------------|
| `URL` | — | Target URL for latency scoring. Use `all` to skip testing. |
| `--filename` | `-F` | Output file name (default: `all.txt`) |
| `--headers` | `-H` | Extra HTTP header (repeatable, format: `Key: Value`) |
| `--http-only` | — | Return only HTTP proxies |
| `--socks4-only` | — | Return only SOCKS4 proxies |
| `--socks5-only` | — | Return only SOCKS5 proxies |
| `--add-prefix` | `-P` | Add prefix to proxy lines (format: `PROTOCOL=prefix`) |
| `--limit` | `-L` | Limit the number of working proxies to return |
| `--help` | — | Show help message and exit |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_URLS` | — | Newline-separated URLs for HTTP proxy sources |
| `SOCKS4_URLS` | — | Newline-separated URLs for SOCKS4 proxy sources |
| `SOCKS5_URLS` | — | Newline-separated URLs for SOCKS5 proxy sources |
| `FETCH_PROXY_TIMEOUT` | `2` | Timeout (seconds) for fetching proxy lists |
| `MAX_WORKERS` | `100` | Maximum concurrent worker threads |
| `PROXY_CHECK_TIMEOUT` | `5.0` | Timeout (seconds) for proxy validation requests |
| `PROXY_CHECK_RETRIES` | `1` | Number of retries when testing a proxy |

> **Note:** `python-dotenv` is automatically loaded when running outside of GitHub Actions (detected via the absence of the `GITHUB_ACTIONS` environment variable).

### Example `.env` file

```env
HTTP_URLS=https://api.proxyscrape.com/v2/?request=get&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all&simplified=true
SOCKS4_URLS=https://api.proxyscrape.com/v2/?request=get&protocol=socks4&timeout=10000&country=all&simplified=true
SOCKS5_URLS=https://api.proxyscrape.com/v2/?request=get&protocol=socks5&timeout=10000&country=all&simplified=true
MAX_WORKERS=200
PROXY_CHECK_TIMEOUT=10
```

## GitHub Actions Workflows

This repository includes two automated workflows:

| Workflow | File | Trigger | Description |
|----------|------|---------|-------------|
| Update All | `.github/workflows/all.yml` | Every 2 hours / manual | Fetches and commits all proxy lists |
| Update Mega | `.github/workflows/mega.yml` | Every 8 hours / manual | Fetches proxies formatted for Megabasterd |

Both workflows run with a `timeout-minutes` safeguard to prevent runaway jobs on large proxy lists.

> **How it works:** Workflows check out the `main` branch (source code only), generate the proxy files, and commit them to the `gh-pages` branch. This keeps the source branch lightweight while keeping the raw URLs accessible via GitHub Pages.

## Raw Proxy URLs

Once GitHub Pages is enabled on the `gh-pages` branch, the generated proxy files are accessible at:

```
https://jn-s3s.github.io/proxy-list/all/all.txt
https://jn-s3s.github.io/proxy-list/all/http.txt
https://jn-s3s.github.io/proxy-list/all/socks4.txt
https://jn-s3s.github.io/proxy-list/all/socks5.txt
https://jn-s3s.github.io/proxy-list/megabasterd/all.txt
https://jn-s3s.github.io/proxy-list/megabasterd/formatted-only.txt
```

## Project Structure

```
proxy-list/
├── .github/
│   └── workflows/
│       ├── all.yml           # Scheduled proxy list update
│       └── mega.yml          # Megabasterd-formatted proxy update
├── .gitignore                # Git ignore rules
├── main.py                   # Main CLI application
└── README.md                 # This file
```

> **Note:** The `all/` and `megabasterd/` directories are generated by CI and live on the `gh-pages` branch only. They are ignored in the `main` branch via `.gitignore`.

## License

[MIT](LICENSE) — feel free to use and modify.
