import textwrap
from app.config import load_config


def test_load_config_parses_all_sections(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        yandex_cloud:
          service_account_key_file: /secrets/sa-key.json
          node_group_id: cat123
          cluster_id: cl-abc
        kubernetes:
          namespace: ml
          label_selectors:
            - team=ml
            - workload=batch
        scaling:
          pending_pod_threshold: 3
          headroom: 0.15
          max_size: 20
          poll_interval_seconds: 30
          dry_run: false
          node_capacity_fallback:
            cpu: 4
            memory_gib: 16
    """))

    cfg = load_config(str(cfg_file))

    assert cfg.yandex_cloud.node_group_id == "cat123"
    assert cfg.yandex_cloud.cluster_id == "cl-abc"
    assert cfg.kubernetes.namespace == "ml"
    assert cfg.kubernetes.label_selectors == ["team=ml", "workload=batch"]
    assert cfg.scaling.pending_pod_threshold == 3
    assert cfg.scaling.headroom == 0.15
    assert cfg.scaling.max_size == 20
    assert cfg.scaling.node_capacity_fallback.cpu == 4
    assert cfg.scaling.node_capacity_fallback.memory_gib == 16


def test_load_config_applies_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        yandex_cloud:
          service_account_key_file: /secrets/sa-key.json
          node_group_id: cat123
          cluster_id: cl-abc
        kubernetes:
          namespace: default
          label_selectors:
            - team=ml
        scaling:
          max_size: 10
    """))

    cfg = load_config(str(cfg_file))

    assert cfg.scaling.pending_pod_threshold == 0
    assert cfg.scaling.headroom == 0.15
    assert cfg.scaling.poll_interval_seconds == 30
    assert cfg.scaling.dry_run is False
    assert cfg.yandex_cloud.master_endpoint == "internal"


def test_scale_down_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        yandex_cloud:
          service_account_key_file: /secrets/sa-key.json
          node_group_id: cat123
          cluster_id: cl-abc
        kubernetes:
          namespace: default
          label_selectors:
            - team=ml
        scaling:
          max_size: 10
    """))

    cfg = load_config(str(cfg_file))

    assert cfg.scaling.min_size == 1
    assert cfg.scaling.scale_down_cooldown_polls == 3


def test_scale_down_fields_parsed(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        yandex_cloud:
          service_account_key_file: /secrets/sa-key.json
          node_group_id: cat123
          cluster_id: cl-abc
        kubernetes:
          namespace: default
          label_selectors:
            - team=ml
        scaling:
          max_size: 10
          min_size: 0
          scale_down_cooldown_polls: 5
    """))

    cfg = load_config(str(cfg_file))

    assert cfg.scaling.min_size == 0
    assert cfg.scaling.scale_down_cooldown_polls == 5


def test_scale_down_validation_rejects_bad_values(tmp_path):
    import pytest
    from pydantic import ValidationError

    def _load(extra):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent(f"""
            yandex_cloud:
              service_account_key_file: /secrets/sa-key.json
              node_group_id: cat123
              cluster_id: cl-abc
            kubernetes:
              namespace: default
              label_selectors:
                - team=ml
            scaling:
              max_size: 10
              {extra}
        """))
        return load_config(str(cfg_file))

    with pytest.raises(ValidationError):
        _load("min_size: -1")
    with pytest.raises(ValidationError):
        _load("scale_down_cooldown_polls: 0")


def test_min_size_exceeding_max_size_rejected(tmp_path):
    import pytest
    from pydantic import ValidationError

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        yandex_cloud:
          service_account_key_file: /secrets/sa-key.json
          node_group_id: cat123
          cluster_id: cl-abc
        kubernetes:
          namespace: default
          label_selectors:
            - team=ml
        scaling:
          max_size: 5
          min_size: 10
    """))

    with pytest.raises(ValidationError):
        load_config(str(cfg_file))


def test_yandex_cloud_config_requires_cluster_id():
    import pytest
    from pydantic import ValidationError
    from app.config import YandexCloudConfig
    with pytest.raises(ValidationError):
        YandexCloudConfig(
            service_account_key_file="/secrets/sa-key.json",
            node_group_id="ng-1",
        )


def test_yandex_cloud_config_master_endpoint_defaults_internal():
    from app.config import YandexCloudConfig
    cfg = YandexCloudConfig(
        service_account_key_file="/secrets/sa-key.json",
        node_group_id="ng-1",
        cluster_id="cl-1",
    )
    assert cfg.cluster_id == "cl-1"
    assert cfg.master_endpoint == "internal"


def test_yandex_cloud_config_master_endpoint_rejects_unknown():
    import pytest
    from pydantic import ValidationError
    from app.config import YandexCloudConfig
    with pytest.raises(ValidationError):
        YandexCloudConfig(
            service_account_key_file="/secrets/sa-key.json",
            node_group_id="ng-1",
            cluster_id="cl-1",
            master_endpoint="public",
        )


def test_yandex_cloud_config_master_endpoint_accepts_external():
    from app.config import YandexCloudConfig
    cfg = YandexCloudConfig(
        service_account_key_file="/secrets/sa-key.json",
        node_group_id="ng-1",
        cluster_id="cl-1",
        master_endpoint="external",
    )
    assert cfg.master_endpoint == "external"


def test_kubernetes_config_no_longer_has_kubeconfig():
    from app.config import KubernetesConfig
    assert "kubeconfig" not in KubernetesConfig.model_fields
