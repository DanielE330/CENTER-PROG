import uuid

from ci_common.auth import tenant_from_claims
from ci_common.logs import redact
from ci_common.tls import spiffe_service


def test_redact_hides_identifiers():
    text = redact("ИНН 0048401219 счёт 40702810000000000001 a@b.ru Bearer eyJabc.def")
    for secret in ["0048401219", "40702810000000000001", "a@b.ru", "eyJabc"]:
        assert secret not in text


def test_spiffe_id_from_peer_certificate():
    peer = {"subjectAltName": (("DNS", "forecast"), ("URI", "spiffe://ci1/svc/gateway"))}
    assert spiffe_service(peer) == "gateway"
    assert spiffe_service({"subjectAltName": (("URI", "spiffe://other/svc/x"),)}) is None


def test_tenant_from_organization_claim():
    tid = uuid.uuid4()
    assert tenant_from_claims({"organization": {"demo-a": {"id": str(tid)}}}) == tid
    assert tenant_from_claims({"organization": ["demo-a"]}) is None
    merged = {"organization": ["demo-a", {"demo-a": {"id": str(tid)}}]}
    assert tenant_from_claims(merged) == tid
