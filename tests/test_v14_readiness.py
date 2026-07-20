from readiness import ReadinessState, classify_failure, validate_configuration


def test_dns_failure_is_not_misclassified_as_login_failure() -> None:
    message = "HTTPConnectionPool: NameResolutionError getaddrinfo failed for %3cyour_870_host%3e"
    assert classify_failure(message) == ReadinessState.CONFIG_MISSING


def test_real_host_dns_failure_is_network_failure() -> None:
    assert classify_failure("Failed to resolve internal-host: getaddrinfo failed") == ReadinessState.NETWORK_UNREACHABLE


def test_login_failure_is_actionable() -> None:
    assert classify_failure("870 PHPSESSID 登录失效") == ReadinessState.LOGIN_REQUIRED


def test_configuration_validation_reports_each_source() -> None:
    results = validate_configuration(
        {
            "base_url": "http://<YOUR_870_HOST>/",
            "extra_metrics": {"enabled": True, "fenxi_base": "https://fenxi", "manage_base": ""},
            "pc_web_metrics": {"enabled": False},
        }
    )
    assert [item.state for item in results] == [
        ReadinessState.CONFIG_MISSING,
        ReadinessState.UNCHECKED,
        ReadinessState.CONFIG_MISSING,
        ReadinessState.DISABLED,
    ]
