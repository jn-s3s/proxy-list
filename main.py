"""CLI tool to fetch, optionally validate, and persist proxy lists."""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click
import requests
from tqdm.auto import tqdm

if os.environ.get("GITHUB_ACTIONS") is None:
    import dotenv

    dotenv.load_dotenv()


def _env_or(name: str, default: str) -> str:
    """
    Return a stripped environment variable, or the default when it is unset or blank.

    CI passes an empty string for repository variables that were never defined, so a
    bare "os.environ.get" default is not enough.

    Parameters:
        name: Environment variable name.
        default: Value used when the variable is missing or blank.

    Returns:
        The stripped variable value, or default.
    """
    return os.environ.get(name, "").strip() or default


_FETCH_PROXY_TIMEOUT = int(_env_or("FETCH_PROXY_TIMEOUT", "2"))
_MAX_WORKERS = int(_env_or("MAX_WORKERS", "100"))
_PROXY_CHECK_TIMEOUT = float(_env_or("PROXY_CHECK_TIMEOUT", "5.0"))
_PROXY_CHECK_ATTEMPTS = int(_env_or("PROXY_CHECK_ATTEMPTS", "1"))

_HTTP_ID = "HTTP"
_SOCKS4_ID = "SOCKS4"
_SOCKS5_ID = "SOCKS5"

_SCHEME_PREFIXES = ("http://", "https://", "socks4://", "socks5://")

# Bracketed IPv6, bare IPv6, IPv4 or a hostname, followed by a numeric port.
_ENDPOINT_PATTERN = re.compile(
    r"^(?:\[[0-9a-fA-F:]+\]|[0-9a-fA-F:]+|[A-Za-z0-9][A-Za-z0-9.\-]*):[0-9]{1,5}$"
)

# Reusable session for connection pooling. The pool is sized to the worker count so
# concurrent threads stop evicting each other's connections.
_POOL_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=_MAX_WORKERS,
    pool_maxsize=_MAX_WORKERS,
)
_SESSION = requests.Session()
_SESSION.mount("http://", _POOL_ADAPTER)
_SESSION.mount("https://", _POOL_ADAPTER)


def _strip_protocol_prefix(line: str) -> str:
    """
    Remove any scheme or bare protocol identifier from a source line.

    Parameters:
        line: Raw line read from a proxy source.

    Returns:
        The address with at most one scheme and one protocol identifier removed.
    """
    cleaned = line
    for prefix in _SCHEME_PREFIXES:
        if cleaned.lower().startswith(prefix):
            offset = len(prefix)
            cleaned = cleaned[offset:]
            break
    for prefix in (_HTTP_ID, _SOCKS4_ID, _SOCKS5_ID):
        if cleaned.startswith(prefix):
            offset = len(prefix)
            cleaned = cleaned[offset:]
            break
    return cleaned


def _is_valid_endpoint(endpoint: str) -> bool:
    """
    Report whether an endpoint is a host:port pair a proxy client could dial.

    Parameters:
        endpoint: Candidate address with any protocol prefix already removed.

    Returns:
        True when the shape matches and the port falls inside the valid range.
    """
    if _ENDPOINT_PATTERN.match(endpoint) is None:
        return False
    port = int(endpoint.rpartition(":")[2])
    return 1 <= port <= 65535


def _normalize_target(url: str) -> str:
    """
    Return a dialable target URL, defaulting to https for bare hosts.

    Parameters:
        url: Target as supplied on the command line.

    Returns:
        The url unchanged when it already carries a scheme, otherwise prefixed
        with "https://".
    """
    return url if "://" in url else f"https://{url}"


_PROTOCOL_FILENAMES = {
    _HTTP_ID: "http.txt",
    _SOCKS4_ID: "socks4.txt",
    _SOCKS5_ID: "socks5.txt",
}


def _write_lines(path: Path, lines: list[str]) -> None:
    """
    Write lines to path one per line, creating parent directories as needed.

    Parameters:
        path: Destination file.
        lines: Proxy lines to write.

    Raises:
        OSError: If the directory cannot be created or the file cannot be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _format_proxies(proxy_list: list[str], add_prefix: dict[str, str]) -> list[str]:
    """
    Apply protocol prefixes and drop the internal protocol identifiers.

    Parameters:
        proxy_list: Raw proxy strings such as "SOCKS51.2.3.4:80".
        add_prefix: Mapping of protocol identifier to output prefix.

    Returns:
        Formatted endpoint lines in the same order as proxy_list.
    """
    formatted = []
    for proxy in proxy_list:
        proxy_id, endpoint = _parse_proxy_str(proxy)
        formatted.append(add_prefix.get(proxy_id, "") + endpoint)
    return formatted


def _write_protocol_files(
    directory: Path, proxy_list: list[str], add_prefix: dict[str, str]
) -> None:
    """
    Write all.txt plus one file per protocol from a single fetched set.

    Every file is derived from the same snapshot, so all.txt is always the exact
    union of the three protocol files.

    Parameters:
        directory: Destination directory, created if missing.
        proxy_list: Raw proxy strings covering every requested protocol.
        add_prefix: Mapping of protocol identifier to output prefix.

    Raises:
        OSError: If any output file cannot be written.
    """
    buckets: dict[str, list[str]] = {proto: [] for proto in _PROTOCOL_FILENAMES}
    for proxy in proxy_list:
        proxy_id, endpoint = _parse_proxy_str(proxy)
        line = add_prefix.get(proxy_id, "") + endpoint
        if proxy_id in buckets:
            buckets[proxy_id].append(line)

    all_lines = _format_proxies(proxy_list, add_prefix)
    _write_lines(directory / "all.txt", all_lines)
    click.echo(f"  all.txt: {len(all_lines):,}")
    for proto_id, filename in _PROTOCOL_FILENAMES.items():
        _write_lines(directory / filename, buckets[proto_id])
        click.echo(f"  {filename}: {len(buckets[proto_id]):,}")


def _parse_prefixes(
    _ctx: click.Context, _param: click.Parameter, values: tuple[str, ...]
) -> dict[str, str]:
    """
    Parse "PROTOCOL=prefix" strings into a mapping.

    Parameters:
        _ctx: Click context (unused).
        _param: Click parameter (unused).
        values: Strings supplied via "--add-prefix".

    Returns:
        Mapping of upper-cased protocol name to prefix string.

    Raises:
        click.BadParameter: If a value does not contain "=".
    """
    output: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise click.BadParameter("Format must be PROTOCOL=prefix")
        proto, prefix = value.split("=", 1)
        output[proto.upper()] = prefix
    return output


def _parse_headers(headers: tuple[str, ...]) -> dict[str, str]:
    """
    Parse 'Key: Value' header strings into a dictionary.

    Parameters:
        headers: Strings supplied via "--headers".

    Returns:
        Mapping of header key to value.

    Raises:
        click.BadParameter: If a value does not contain ":".
    """
    result: dict[str, str] = {}
    for header in headers:
        if ":" not in header:
            raise click.BadParameter(f"Header must be in 'Key: Value' format: {header}")
        key, value = header.split(":", 1)
        result[key.strip()] = value.strip()
    return result


@click.command()
@click.argument("url", default="all")
@click.option(
    "--filename", "-F", default=None, help="Output file name (default: all.txt)."
)
@click.option("--headers", "-H", multiple=True, help="Extra HTTP header (repeatable).")
@click.option(
    "--http-only", "protocol", flag_value=_HTTP_ID, help="Return only HTTP proxies."
)
@click.option(
    "--socks4-only",
    "protocol",
    flag_value=_SOCKS4_ID,
    help="Return only SOCKS4 proxies.",
)
@click.option(
    "--socks5-only",
    "protocol",
    flag_value=_SOCKS5_ID,
    help="Return only SOCKS5 proxies.",
)
@click.option(
    "--add-prefix",
    "-P",
    multiple=True,
    callback=_parse_prefixes,
    help="Add prefix to proxy lines (format: PROTOCOL=prefix).",
)
@click.option(
    "--limit",
    "-L",
    type=click.IntRange(min=1),
    help="Keep the N fastest working proxies (min 1, requires a target URL).",
)
@click.option(
    "--output-dir",
    "-O",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Write all.txt plus one file per protocol from a single fetch.",
)
def main(
    url: str = "all",
    filename: str | None = None,
    headers: tuple[str, ...] = (),
    protocol: str | None = None,
    add_prefix: dict[str, str] | None = None,
    limit: int | None = None,
    output_dir: Path | None = None,
) -> None:
    """
    Fetch proxy lists, optionally test latency, and write results to filename.

    Parameters:
        url: Target URL for latency scoring. Use "all" to skip testing.
        filename: Output file path. Mutually exclusive with output_dir.
        headers: Custom request headers.
        protocol: "HTTP", "SOCKS4", "SOCKS5" or "None" (all protocols).
        add_prefix: Mapping of protocol to prefix string.
        limit: Maximum number of fastest working proxies to keep. Requires a
            target URL other than "all".
        output_dir: Directory to receive all.txt plus one file per protocol.
    """
    if filename is not None and output_dir is not None:
        raise click.UsageError("Pass either --filename or --output-dir, not both.")
    if limit is not None and url == "all":
        raise click.UsageError("--limit requires a target URL, not 'all'.")

    header_dict = _parse_headers(headers)
    add_prefix = add_prefix or {}
    protocol = protocol or "all"

    proxy_list: list[str] = []
    for proto_id, env_name in (
        (_HTTP_ID, f"{_HTTP_ID}_URLS"),
        (_SOCKS4_ID, f"{_SOCKS4_ID}_URLS"),
        (_SOCKS5_ID, f"{_SOCKS5_ID}_URLS"),
    ):
        if protocol not in {"all", proto_id}:
            continue
        urls = _get_lines(env_name)
        proxy_list.extend(_fetch_from_urls(urls, proto_id))

    if not proxy_list:
        raise click.ClickException(
            "No proxies were fetched. Check your environment variables or proxy sources."
        )

    if url != "all":
        proxy_list = _score_proxies(url, header_dict, proxy_list, limit)

    click.echo(f"Total proxies: {len(proxy_list):,}")

    destination = output_dir if output_dir is not None else (filename or "all.txt")
    try:
        if output_dir is not None:
            _write_protocol_files(output_dir, proxy_list, add_prefix)
        else:
            _write_lines(Path(destination), _format_proxies(proxy_list, add_prefix))
    except OSError as exc:
        raise click.ClickException(f"Error writing to {destination!r}: {exc}")


def _get_lines(env_name: str) -> list[str]:
    """
    Read a multiline environment variable and return stripped, non-empty lines.

    Parameters:
        env_name: Name of the environment variable to read.

    Returns:
        List of non-empty, stripped lines.
    """
    env_val = os.environ.get(env_name, "")
    if not env_val:
        click.echo(f'Environment variable "{env_name}" is empty. Skipping...')
    return [line.strip() for line in env_val.splitlines() if line.strip()]


def _fetch_from_urls(urls: list[str], proto_id: str) -> list[str]:
    """
    Fetch proxy lists from multiple URLs concurrently.

    Parameters:
        urls: Source URLs.
        proto_id: Proxy type identifier (e.g. "HTTP").

    Returns:
        De-duplicated proxy strings in sorted order, so repeated runs over the
        same sources write byte-identical files.
    """
    proxies: set[str] = set()
    rejected = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_proxy, url, proto_id): url for url in urls}
        pbar = tqdm(
            as_completed(futures),
            total=len(futures),
            desc=f"Fetching {proto_id} proxies",
            unit="proxy",
        )
        for future in pbar:
            url = futures[future]
            try:
                retrieved, invalid = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad source must not abort
                tqdm.write(f"Skipping proxy source: {url} due to an error: {exc}")
                continue
            proxies.update(retrieved)
            rejected += invalid
            pbar.set_description(
                f"Fetching {proto_id} proxies "
                f"| Total: {len(proxies):,} | Malformed: {rejected:,}"
            )
    return sorted(proxies)


def _fetch_proxy(url: str, proto_id: str) -> tuple[set[str], int]:
    """
    Retrieve a single proxy source and normalise each line.

    Parameters:
        url: Source URL.
        proto_id: Proxy type identifier to prepend.

    Returns:
        Tuple of the set of normalised proxy strings and the number of lines
        dropped because they were not a valid host:port address.
    """
    proxies: set[str] = set()
    rejected = 0
    if not url.lower().startswith(("http://", "https://")):
        return proxies, rejected

    try:
        response = _SESSION.get(url, timeout=_FETCH_PROXY_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        tqdm.write(f"Skipping proxy source: {url} due to an error: {exc}")
        return proxies, rejected

    for line in response.text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        cleaned = _strip_protocol_prefix(line)
        if not _is_valid_endpoint(cleaned):
            rejected += 1
            continue
        proxies.add(f"{proto_id}{cleaned}")
    return proxies, rejected


def _score_proxies(
    url: str,
    headers: dict[str, str],
    proxy_list: list[str],
    limit: int | None,
) -> list[str]:
    """
    Test each proxy against url and return results ordered by latency.

    Parameters:
        url: Target URL for validation.
        headers: Request headers.
        proxy_list: List of proxy strings to test.
        limit: Maximum number of results to return.

    Returns:
        Working proxies sorted by ascending latency, truncated to limit.
    """
    results: list[tuple[str, float]] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {
            executor.submit(_test_proxy, url, headers, proxy): proxy
            for proxy in proxy_list
        }
        with tqdm(
            as_completed(futures),
            total=len(futures),
            desc="Checking availability",
            unit="proxy",
        ) as pbar:
            for future in pbar:
                try:
                    proxy, latency = future.result()
                except Exception as exc:  # noqa: BLE001 - one odd failure must not stop
                    tqdm.write(f"Skipping {futures[future]} due to an error: {exc}")
                    continue
                if latency is not None:
                    results.append((proxy, latency))
                    pbar.set_description(
                        f"Checking availability | Active: {len(results):,} "
                        f"| Limit: {'♾️' if limit is None else limit}"
                    )
    # Rank every survivor before truncating, otherwise limit keeps whichever
    # proxies happened to answer first rather than the fastest ones. Ties break
    # by proxy string so equal latencies still produce a stable order.
    results.sort(key=lambda item: (item[1], item[0]))
    if limit is not None:
        results = results[:limit]
    return [proxy for proxy, _ in results]


def _test_proxy(
    url: str,
    headers: dict[str, str],
    proxy: str,
) -> tuple[str, float | None]:
    """
    Measure request latency through a single proxy.

    Parameters:
        url: Target URL.
        headers: Request headers.
        proxy: Raw proxy string with identifier (e.g. "HTTP127.0.0.1:8080").

    Returns:
        The original proxy string and its latency in seconds, or "None" on failure.
    """
    proxy_id, endpoint = _parse_proxy_str(proxy)

    if proxy_id == _SOCKS5_ID:
        proxy_url = f"socks5://{endpoint}"
    elif proxy_id == _SOCKS4_ID:
        proxy_url = f"socks4://{endpoint}"
    else:
        proxy_url = f"http://{endpoint}"

    proxies = {"http": proxy_url, "https": proxy_url}
    target = _normalize_target(url)

    for _ in range(max(1, _PROXY_CHECK_ATTEMPTS)):
        try:
            start = time.perf_counter()
            # stream=True stops after the headers, so latency measures round-trip
            # time rather than how fast the proxy downloads the whole body.
            with _SESSION.get(
                target,
                headers=headers,
                proxies=proxies,
                timeout=_PROXY_CHECK_TIMEOUT,
                stream=True,
            ) as response:
                elapsed = time.perf_counter() - start
                if response.status_code == 200:
                    return proxy, elapsed
        except requests.RequestException:
            continue
    return proxy, None


def _parse_proxy_str(proxy: str) -> tuple[str, str]:
    """
    Split a raw proxy string into its identifier and endpoint.

    Supported identifiers: "HTTP", "SOCKS4", "SOCKS5".
    A leading "*" is treated as a "SOCKS5" shortcut.

    Parameters:
        proxy: Raw proxy string.

    Returns:
        Tuple of *(identifier, endpoint)*.
    """
    proxy = proxy.strip()
    if proxy.startswith(_SOCKS4_ID):
        return _SOCKS4_ID, proxy[len(_SOCKS4_ID) :]
    if proxy.startswith(_SOCKS5_ID):
        return _SOCKS5_ID, proxy[len(_SOCKS5_ID) :]
    if proxy.startswith("*"):
        return _SOCKS5_ID, proxy[1:]
    if proxy.startswith(_HTTP_ID):
        return _HTTP_ID, proxy[len(_HTTP_ID) :]
    return _HTTP_ID, proxy


if __name__ == "__main__":
    main()
