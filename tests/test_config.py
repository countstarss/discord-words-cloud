from src.common.config import load_config


def test_load_config_resolves_env_placeholders_with_types(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                'enabled: "${TEST_CFG_ENABLED:false}"',
                'port: "${TEST_CFG_PORT:8080}"',
                'ratio: "${TEST_CFG_RATIO:0.25}"',
                'token: "${TEST_CFG_TOKEN:}"',
                'values: "${TEST_CFG_VALUES:[1, 2]}"',
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("TEST_CFG_ENABLED", "true")
    monkeypatch.setenv("TEST_CFG_PORT", "9000")
    monkeypatch.setenv("TEST_CFG_RATIO", "0.5")
    monkeypatch.setenv("TEST_CFG_TOKEN", "secret-token")
    monkeypatch.setenv("TEST_CFG_VALUES", "[3, 4, 5]")

    cfg = load_config(str(config_path))

    assert cfg["enabled"] is True
    assert cfg["port"] == 9000
    assert cfg["ratio"] == 0.5
    assert cfg["token"] == "secret-token"
    assert cfg["values"] == [3, 4, 5]


def test_load_config_keeps_empty_default_as_string(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('token: "${TEST_CFG_EMPTY:}"\n', encoding="utf-8")

    monkeypatch.setenv("TEST_CFG_EMPTY", "123,456")

    cfg = load_config(str(config_path))

    assert cfg["token"] == "123,456"
    assert isinstance(cfg["token"], str)
