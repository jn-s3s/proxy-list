"""Tests for the proxy list CLI."""

import pytest
from click.testing import CliRunner

import main


class FakeResponse:
    """Stand-in for requests.Response covering only what the fetcher touches."""

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        """No-op, the fake response always represents a successful request."""


@pytest.fixture
def fake_sources(monkeypatch):
    """Return a callable that maps source URLs to the body they should serve."""

    def install(payloads: dict[str, str]) -> None:
        monkeypatch.setattr(
            main._SESSION, "get", lambda url, **kwargs: FakeResponse(payloads[url])
        )

    return install


def test_env_or_falls_back_on_blank(monkeypatch):
    monkeypatch.setenv("SOME_TUNING_VAR", "   ")
    assert main._env_or("SOME_TUNING_VAR", "7") == "7"


def test_env_or_strips_and_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_TUNING_VAR", " 42 ")
    assert main._env_or("SOME_TUNING_VAR", "7") == "42"


def test_session_pool_matches_worker_count():
    adapter = main._SESSION.get_adapter("https://example.com")
    assert adapter._pool_maxsize == main._MAX_WORKERS
    assert adapter._pool_connections == main._MAX_WORKERS


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("httpbin.org/get", "https://httpbin.org/get"),
        ("http://example.com/get", "http://example.com/get"),
        ("https://example.com/get", "https://example.com/get"),
    ],
)
def test_normalize_target(url, expected):
    assert main._normalize_target(url) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "1.2.3.4:8080",
        "proxy.example.com:3128",
        "[::1]:8080",
        "::1:8080",
        "10.0.0.1:1",
        "10.0.0.1:65535",
    ],
)
def test_valid_endpoints_accepted(endpoint):
    assert main._is_valid_endpoint(endpoint) is True


@pytest.mark.parametrize(
    "endpoint",
    [
        '<div class="x">',
        '{"ip": "1.2.3.4"}',
        "1.2.3.4",
        "1.2.3.4:99999",
        "1.2.3.4:0",
        "1.2.3.4:",
        "not a proxy line: here",
        "HTTP/1.1 200 OK",
        "1.2.3.4:80 extra",
        "",
    ],
)
def test_malformed_endpoints_rejected(endpoint):
    assert main._is_valid_endpoint(endpoint) is False


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("socks5://1.2.3.4:80", "1.2.3.4:80"),
        ("HTTP1.2.3.4:80", "1.2.3.4:80"),
        ("http://SOCKS51.2.3.4:80", "1.2.3.4:80"),
        ("1.2.3.4:80", "1.2.3.4:80"),
    ],
)
def test_strip_protocol_prefix(line, expected):
    assert main._strip_protocol_prefix(line) == expected


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("SOCKS41.2.3.4:80", ("SOCKS4", "1.2.3.4:80")),
        ("SOCKS51.2.3.4:80", ("SOCKS5", "1.2.3.4:80")),
        ("*1.2.3.4:80", ("SOCKS5", "1.2.3.4:80")),
        ("HTTP1.2.3.4:80", ("HTTP", "1.2.3.4:80")),
        ("1.2.3.4:80", ("HTTP", "1.2.3.4:80")),
    ],
)
def test_parse_proxy_str(proxy, expected):
    assert main._parse_proxy_str(proxy) == expected


def test_format_proxies_applies_prefix_and_drops_identifier():
    formatted = main._format_proxies(
        ["HTTP1.1.1.1:80", "SOCKS52.2.2.2:80"], {"SOCKS5": "*"}
    )
    assert formatted == ["1.1.1.1:80", "*2.2.2.2:80"]


def test_write_lines_ends_with_a_newline(tmp_path):
    target = tmp_path / "nested" / "out.txt"
    main._write_lines(target, ["1.1.1.1:80", "2.2.2.2:80"])
    assert target.read_text(encoding="utf-8") == "1.1.1.1:80\n2.2.2.2:80\n"


def test_write_lines_with_no_lines_writes_empty_file(tmp_path):
    target = tmp_path / "out.txt"
    main._write_lines(target, [])
    assert target.read_text(encoding="utf-8") == ""


def test_protocol_files_are_a_consistent_snapshot(tmp_path):
    proxies = ["HTTP1.1.1.1:80", "SOCKS42.2.2.2:80", "SOCKS53.3.3.3:80"]
    main._write_protocol_files(tmp_path, proxies, {})

    combined: list[str] = []
    for name in ("http.txt", "socks4.txt", "socks5.txt"):
        combined += (tmp_path / name).read_text(encoding="utf-8").splitlines()
    everything = (tmp_path / "all.txt").read_text(encoding="utf-8").splitlines()

    assert sorted(combined) == sorted(everything)
    assert (tmp_path / "http.txt").read_text(encoding="utf-8") == "1.1.1.1:80\n"
    assert (tmp_path / "socks4.txt").read_text(encoding="utf-8") == "2.2.2.2:80\n"
    assert (tmp_path / "socks5.txt").read_text(encoding="utf-8") == "3.3.3.3:80\n"


def test_protocol_files_apply_prefixes(tmp_path):
    proxies = ["SOCKS41.1.1.1:80", "SOCKS52.2.2.2:80", "HTTP3.3.3.3:80"]
    main._write_protocol_files(tmp_path, proxies, {"SOCKS4": "*", "SOCKS5": "*"})

    assert (tmp_path / "socks4.txt").read_text(encoding="utf-8") == "*1.1.1.1:80\n"
    assert (tmp_path / "socks5.txt").read_text(encoding="utf-8") == "*2.2.2.2:80\n"
    assert (tmp_path / "http.txt").read_text(encoding="utf-8") == "3.3.3.3:80\n"


def test_limit_keeps_the_fastest_rather_than_the_first_to_answer(monkeypatch):
    latencies = {
        "HTTP1.1.1.1:1": 0.5,
        "HTTP2.2.2.2:2": 0.3,
        "HTTP3.3.3.3:3": 0.1,
        "HTTP4.4.4.4:4": None,
        "HTTP5.5.5.5:5": 0.4,
    }
    monkeypatch.setattr(
        main, "_test_proxy", lambda url, headers, proxy: (proxy, latencies[proxy])
    )

    assert main._score_proxies("example.com", {}, list(latencies), 2) == [
        "HTTP3.3.3.3:3",
        "HTTP2.2.2.2:2",
    ]
    assert main._score_proxies("example.com", {}, list(latencies), None) == [
        "HTTP3.3.3.3:3",
        "HTTP2.2.2.2:2",
        "HTTP5.5.5.5:5",
        "HTTP1.1.1.1:1",
    ]


def test_score_proxies_breaks_latency_ties_deterministically(monkeypatch):
    latencies = {"HTTP2.2.2.2:2": 0.2, "HTTP1.1.1.1:1": 0.2}
    monkeypatch.setattr(
        main, "_test_proxy", lambda url, headers, proxy: (proxy, latencies[proxy])
    )

    assert main._score_proxies("example.com", {}, list(latencies), None) == [
        "HTTP1.1.1.1:1",
        "HTTP2.2.2.2:2",
    ]


def test_score_proxies_survives_an_unexpected_worker_failure(monkeypatch):
    def explode(url, headers, proxy):
        if proxy == "HTTP2.2.2.2:2":
            raise RuntimeError("socks stack blew up")
        return proxy, 0.1

    monkeypatch.setattr(main, "_test_proxy", explode)
    ranked = main._score_proxies(
        "example.com", {}, ["HTTP1.1.1.1:1", "HTTP2.2.2.2:2"], None
    )
    assert ranked == ["HTTP1.1.1.1:1"]


def test_fetch_proxy_rejects_malformed_lines(fake_sources):
    fake_sources(
        {
            "http://source.test/list": (
                "1.2.3.4:8080\n"
                "socks5://5.6.7.8:1080\n"
                "<!DOCTYPE html>\n"
                '<div class="proxy">9.9.9.9:3128</div>\n'
                "HTTP11.22.33.44:8000\n"
                "garbage line without a port\n"
                "1.2.3.4:8080\n"
                "999.1:99999999\n"
            )
        }
    )

    proxies, rejected = main._fetch_proxy("http://source.test/list", "HTTP")

    assert proxies == {
        "HTTP1.2.3.4:8080",
        "HTTP5.6.7.8:1080",
        "HTTP11.22.33.44:8000",
    }
    assert rejected == 2


def test_fetch_proxy_ignores_non_http_sources():
    assert main._fetch_proxy("ftp://source.test/list", "HTTP") == (set(), 0)


def test_fetch_from_urls_returns_sorted_unique_proxies(fake_sources):
    fake_sources(
        {
            "http://source.test/a": "3.3.3.3:80\n1.1.1.1:80\n",
            "http://source.test/b": "2.2.2.2:80\n1.1.1.1:80\n",
        }
    )

    proxies = main._fetch_from_urls(
        ["http://source.test/a", "http://source.test/b"], "HTTP"
    )

    assert proxies == ["HTTP1.1.1.1:80", "HTTP2.2.2.2:80", "HTTP3.3.3.3:80"]


def test_cli_rejects_filename_together_with_output_dir():
    result = CliRunner().invoke(main.main, ["all", "-F", "x.txt", "-O", "out"])
    assert result.exit_code == 2
    assert "not both" in result.output


@pytest.mark.parametrize("value", ["0", "-1"])
def test_cli_rejects_non_positive_limit(value):
    result = CliRunner().invoke(main.main, ["example.com", "--limit", value])
    assert result.exit_code == 2


def test_cli_rejects_limit_without_a_target():
    result = CliRunner().invoke(main.main, ["all", "--limit", "5"])
    assert result.exit_code == 2
    assert "--limit requires a target URL" in result.output


def test_cli_errors_when_no_source_yields_a_proxy(monkeypatch):
    for name in ("HTTP_URLS", "SOCKS4_URLS", "SOCKS5_URLS"):
        monkeypatch.setenv(name, "")

    result = CliRunner().invoke(main.main, ["all"])

    assert result.exit_code == 1
    assert "No proxies were fetched" in result.output


def test_cli_output_dir_writes_every_file_from_one_fetch(
    tmp_path, monkeypatch, fake_sources
):
    monkeypatch.setenv("HTTP_URLS", "http://source.test/http")
    monkeypatch.setenv("SOCKS4_URLS", "http://source.test/socks4")
    monkeypatch.setenv("SOCKS5_URLS", "http://source.test/socks5")
    fake_sources(
        {
            "http://source.test/http": "1.1.1.1:80\nmalformed line: here\n",
            "http://source.test/socks4": "2.2.2.2:80\n",
            "http://source.test/socks5": "3.3.3.3:80\n",
        }
    )

    out_dir = tmp_path / "all"
    result = CliRunner().invoke(main.main, ["all", "--output-dir", str(out_dir)])

    assert result.exit_code == 0, result.output
    assert (out_dir / "http.txt").read_text(encoding="utf-8") == "1.1.1.1:80\n"
    assert (out_dir / "socks4.txt").read_text(encoding="utf-8") == "2.2.2.2:80\n"
    assert (out_dir / "socks5.txt").read_text(encoding="utf-8") == "3.3.3.3:80\n"
    assert sorted((out_dir / "all.txt").read_text(encoding="utf-8").splitlines()) == [
        "1.1.1.1:80",
        "2.2.2.2:80",
        "3.3.3.3:80",
    ]


def test_cli_filename_still_writes_a_single_file(tmp_path, monkeypatch, fake_sources):
    monkeypatch.setenv("HTTP_URLS", "http://source.test/http")
    monkeypatch.setenv("SOCKS4_URLS", "")
    monkeypatch.setenv("SOCKS5_URLS", "")
    fake_sources({"http://source.test/http": "1.1.1.1:80\n"})

    out = tmp_path / "nested" / "proxies.txt"
    result = CliRunner().invoke(main.main, ["all", "-F", str(out)])

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == "1.1.1.1:80\n"


def test_cli_defaults_to_all_txt_when_no_destination_given(
    tmp_path, monkeypatch, fake_sources
):
    monkeypatch.setenv("HTTP_URLS", "http://source.test/http")
    monkeypatch.setenv("SOCKS4_URLS", "")
    monkeypatch.setenv("SOCKS5_URLS", "")
    fake_sources({"http://source.test/http": "1.1.1.1:80\n"})
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main.main, ["all"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "all.txt").read_text(encoding="utf-8") == "1.1.1.1:80\n"


def test_cli_applies_prefix_in_megabasterd_style(tmp_path, monkeypatch, fake_sources):
    monkeypatch.setenv("HTTP_URLS", "")
    monkeypatch.setenv("SOCKS4_URLS", "http://source.test/socks4")
    monkeypatch.setenv("SOCKS5_URLS", "http://source.test/socks5")
    fake_sources(
        {
            "http://source.test/socks4": "1.1.1.1:80\n",
            "http://source.test/socks5": "2.2.2.2:80\n",
        }
    )

    out = tmp_path / "formatted-only.txt"
    result = CliRunner().invoke(
        main.main,
        ["all", "-F", str(out), "-P", "SOCKS4=*", "-P", "SOCKS5=*"],
    )

    assert result.exit_code == 0, result.output
    assert sorted(out.read_text(encoding="utf-8").splitlines()) == [
        "*1.1.1.1:80",
        "*2.2.2.2:80",
    ]


def test_cli_protocol_filter_limits_the_fetch(tmp_path, monkeypatch, fake_sources):
    monkeypatch.setenv("HTTP_URLS", "http://source.test/http")
    monkeypatch.setenv("SOCKS4_URLS", "http://source.test/socks4")
    monkeypatch.setenv("SOCKS5_URLS", "http://source.test/socks5")
    fake_sources(
        {
            "http://source.test/http": "1.1.1.1:80\n",
            "http://source.test/socks4": "2.2.2.2:80\n",
            "http://source.test/socks5": "3.3.3.3:80\n",
        }
    )

    out = tmp_path / "socks5.txt"
    result = CliRunner().invoke(main.main, ["all", "-F", str(out), "--socks5-only"])

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == "3.3.3.3:80\n"
