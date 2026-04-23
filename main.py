"""CLI tool to fetch, optionally validate, and persist proxy lists."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import time

import click
import requests
from tqdm.auto import tqdm

if os.environ.get("GITHUB_ACTIONS") is None:
    from pathlib import Path
    import dotenv
    dotenv.load_dotenv()

_FETCH_PROXY_TIMEOUT = int(os.environ.get("FETCH_PROXY_TIMEOUT", "2"))
_MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "100"))
_PROXY_CHECK_TIMEOUT = float(os.environ.get("PROXY_CHECK_TIMEOUT", "5.0"))
_PROXY_CHECK_RETRIES = int(os.environ.get("PROXY_CHECK_RETRIES", "1"))

_HTTP_ID = "HTTP"
_SOCKS4_ID = "SOCKS4"
_SOCKS5_ID = "SOCKS5"

# Reusable session for connection pooling.
_SESSION = requests.Session()


def _parse_prefixes(_ctx: click.Context, _param: click.Parameter, values: tuple[str, ...]) -> dict[str, str]:
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
@click.option("--filename", "-F", default="all.txt", help="Output file name (default: all.txt).")
@click.option("--headers", "-H", multiple=True, help="Extra HTTP header (repeatable).")
@click.option("--http-only", "protocol", flag_value=_HTTP_ID, help="Return only HTTP proxies.")
@click.option("--socks4-only", "protocol", flag_value=_SOCKS4_ID, help="Return only SOCKS4 proxies.")
@click.option("--socks5-only", "protocol", flag_value=_SOCKS5_ID, help="Return only SOCKS5 proxies.")
@click.option(
    "--add-prefix",
    "-P",
    multiple=True,
    callback=_parse_prefixes,
    help="Add prefix to proxy lines (format: PROTOCOL=prefix).",
)
@click.option("--limit", "-L", type=int, help="Limit working proxy return.")
def main(
    url: str = "all",
    filename: str = "all.txt",
    headers: tuple[str, ...] = (),
    protocol: str | None = None,
    add_prefix: dict[str, str] | None = None,
    limit: int | None = None,
) -> None:
    """
    Fetch proxy lists, optionally test latency, and write results to filename.

    Parameters:
        url: Target URL for latency scoring. Use "all" to skip testing.
        filename: Output file path.
        headers: Custom request headers.
        protocol: "HTTP", "SOCKS4", "SOCKS5" or "None" (all protocols).
        add_prefix: Mapping of protocol to prefix string.
        limit: Maximum number of fastest working proxies to keep.
    """
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

    final_result = []
    for proxy in proxy_list:
        proxy_id, endpoint = _parse_proxy_str(proxy)
        final_result.append(add_prefix.get(proxy_id, "") + endpoint)
    click.echo(f"Total proxies: {len(final_result):,}")

    try:
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_text("\n".join(final_result), encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"Error writing to {filename!r}: {exc}")


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
        De-duplicated list of raw proxy strings.
    """
    proxies: set[str] = set()
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
                retrieved = future.result()
                proxies.update(retrieved)
                pbar.set_description(
                    f"Fetching {proto_id} proxies | Total: {len(proxies):,}"
                )
            except Exception as exc:  # noqa: BLE001
                tqdm.write(f"Skipping proxy source: {url} due to an error: {exc}")
    return list(proxies)


def _fetch_proxy(url: str, proto_id: str) -> set[str]:
    """
    Retrieve a single proxy source and normalise each line.

    Parameters:
        url: Source URL.
        proto_id: Proxy type identifier to prepend.

    Returns:
        Set of normalised proxy strings.
    """
    proxies: set[str] = set()
    if not url.lower().startswith(("http://", "https://")):
        return proxies

    try:
        response = _SESSION.get(url, timeout=_FETCH_PROXY_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        tqdm.write(f"Skipping proxy source: {url} due to an error: {exc}")
        return proxies

    for line in response.text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        # Strip any existing protocol prefix and prepend the canonical one.
        cleaned = line
        for prefix in ("http://", "https://", "socks4://", "socks5://"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix) :]
                break
        # Also strip bare uppercase prefixes to avoid duplication.
        for prefix in (_HTTP_ID, _SOCKS4_ID, _SOCKS5_ID):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        proxies.add(f"{proto_id}{cleaned}")
    return proxies


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
        Working proxies sorted by ascending latency.
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
                proxy, latency = future.result()
                if latency is not None:
                    results.append((proxy, latency))
                    pbar.set_description(
                        f"Checking availability | Active: {len(results):,} "
                        f"| Limit: {'♾️' if limit is None else limit}"
                    )
                if limit is not None and len(results) >= limit:
                    for f in futures:
                        f.cancel()
                    pbar.update(pbar.total - pbar.n)
                    pbar.refresh()
                    pbar.close()
                    break
    results.sort(key=lambda item: item[1])
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
    target = url if url.startswith("https://") else f"https://{url}"

    for _ in range(_PROXY_CHECK_RETRIES):
        try:
            start = time.perf_counter()
            response = _SESSION.get(
                target,
                headers=headers,
                proxies=proxies,
                timeout=_PROXY_CHECK_TIMEOUT,
            )
            elapsed = time.perf_counter() - start
            if response.status_code == 200:
                return proxy, elapsed
        except Exception:  # noqa: BLE001, S110
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
