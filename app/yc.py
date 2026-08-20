from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager

import jwt
from yandexcloud import SDK
from yandex.cloud.k8s.v1.node_group_service_pb2 import (
    GetNodeGroupRequest,
    UpdateNodeGroupRequest,
)
from yandex.cloud.k8s.v1.node_group_service_pb2_grpc import NodeGroupServiceStub
from yandex.cloud.k8s.v1.node_group_pb2 import ScalePolicy
from yandex.cloud.k8s.v1.cluster_service_pb2 import GetClusterRequest
from yandex.cloud.k8s.v1.cluster_service_pb2_grpc import ClusterServiceStub
from yandex.cloud.iam.v1.iam_token_service_pb2 import CreateIamTokenRequest
from yandex.cloud.iam.v1.iam_token_service_pb2_grpc import IamTokenServiceStub
from yandex.cloud.operation.operation_service_pb2 import GetOperationRequest
from yandex.cloud.operation.operation_service_pb2_grpc import OperationServiceStub
from google.protobuf.field_mask_pb2 import FieldMask

from app import metrics

logger = logging.getLogger(__name__)


@contextmanager
def _counting_errors(op: str):
    try:
        yield
    except Exception:
        metrics.record_yc_error(op)
        raise


_TOKEN_REFRESH_SKEW_SECONDS = 300
_IAM_TOKEN_AUDIENCE = "https://iam.api.cloud.yandex.net/iam/v1/tokens"


def _load_sa_key(service_account_key_file: str) -> dict:
    with open(service_account_key_file) as fh:
        return json.load(fh)


class YcClient:
    def __init__(self, service_account_key_file: str, node_group_id: str):
        sa_key = _load_sa_key(service_account_key_file)
        self._sdk = SDK(service_account_key=sa_key)
        self._svc = self._sdk.client(NodeGroupServiceStub)
        self._ops = self._sdk.client(OperationServiceStub)
        self._node_group_id = node_group_id
        # Id of the most recent resize operation, cleared once it completes.
        self._last_operation_id: str | None = None

    def get_current_size(self) -> int:
        with _counting_errors("get_size"):
            group = self._svc.Get(
                GetNodeGroupRequest(node_group_id=self._node_group_id)
            )
        return int(group.scale_policy.fixed_scale.size)

    def set_size(self, size: int) -> None:
        request = UpdateNodeGroupRequest(
            node_group_id=self._node_group_id,
            update_mask=FieldMask(paths=["scale_policy.fixed_scale.size"]),
            scale_policy=ScalePolicy(
                fixed_scale=ScalePolicy.FixedScale(size=size)
            ),
        )
        with _counting_errors("update"):
            operation = self._svc.Update(request)
        self._last_operation_id = getattr(operation, "id", None) or None
        logger.info(
            "Requested node-group %s resize to %d (operation %s)",
            self._node_group_id, size, self._last_operation_id or "?",
        )

    def operation_in_progress(self) -> bool:
        if not self._last_operation_id:
            return False
        with _counting_errors("get_operation"):
            operation = self._ops.Get(
                GetOperationRequest(operation_id=self._last_operation_id)
            )
        if getattr(operation, "done", False):
            self._last_operation_id = None
            return False
        return True


class YcKubeCredentials:
    def __init__(
        self,
        service_account_key_file: str,
        cluster_id: str,
        endpoint_type: str = "internal",
    ):
        self._sa_key = _load_sa_key(service_account_key_file)
        self._cluster_id = cluster_id
        sdk = SDK(service_account_key=self._sa_key)
        self._iam = sdk.client(IamTokenServiceStub)
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._capture_connection(sdk.client(ClusterServiceStub), endpoint_type)

    def _capture_connection(self, cluster_svc, endpoint_type: str) -> None:
        cluster = cluster_svc.Get(GetClusterRequest(cluster_id=self._cluster_id))
        endpoints = cluster.master.endpoints
        if endpoint_type == "internal":
            self.endpoint = endpoints.internal_v4_endpoint
        elif endpoint_type == "external":
            self.endpoint = endpoints.external_v4_endpoint
        else:
            raise ValueError(
                f"endpoint_type must be 'internal' or 'external', got "
                f"{endpoint_type!r}"
            )
        self.ca_cert = cluster.master.master_auth.cluster_ca_certificate
        logger.info("Resolved %s master endpoint %s", endpoint_type, self.endpoint)

    def get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - _TOKEN_REFRESH_SKEW_SECONDS:
            return self._token
        return self._mint_token()

    def _mint_token(self) -> str:
        now = int(time.time())
        payload = {
            "iss": self._sa_key["service_account_id"],
            "aud": _IAM_TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + 3600,
        }
        signed = jwt.encode(
            payload,
            self._sa_key["private_key"],
            algorithm="PS256",
            headers={"kid": self._sa_key["id"]},
        )
        with _counting_errors("iam_token"):
            response = self._iam.Create(CreateIamTokenRequest(jwt=signed))
        metrics.record_iam_token_mint()
        self._token = response.iam_token
        expires_at = response.expires_at
        self._token_expiry = expires_at.seconds + expires_at.nanos / 1e9
        logger.info("Minted IAM token (expires_at=%s)", self._token_expiry)
        return self._token
