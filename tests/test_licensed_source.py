"""Licensed downloads must not redirect the server to arbitrary hosts."""
from urllib.request import Request

import pytest

from songwriter.corpus.licensed import _LicensedRedirects


@pytest.mark.parametrize("target", [
    "https://127.0.0.1/private",
    "https://huggingface.co.attacker.example/songs.parquet",
    "http://us.aws.cdn.hf.co/songs.parquet",
    "https://us.aws.cdn.hf.co:8443/songs.parquet",
    "https://user:password@us.aws.cdn.hf.co/songs.parquet",
])
def test_licensed_source_rejects_redirects_outside_approved_https_hosts(target):
    with pytest.raises(RuntimeError, match="approved HTTPS hosts"):
        _LicensedRedirects().redirect_request(
            Request("https://huggingface.co/source", method="HEAD"),
            None, 302, "Found", {}, target,
        )
