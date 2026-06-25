import pytest
from pydantic import ValidationError

from app.api import ScaleRequest


def test_scale_request_accepts_positive_count():
    assert ScaleRequest(nodes_to_add=3).nodes_to_add == 3


@pytest.mark.parametrize("bad", [0, -1])
def test_scale_request_rejects_non_positive(bad):
    with pytest.raises(ValidationError):
        ScaleRequest(nodes_to_add=bad)


def test_scale_request_requires_field():
    with pytest.raises(ValidationError):
        ScaleRequest()
