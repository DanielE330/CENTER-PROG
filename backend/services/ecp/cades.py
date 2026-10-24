"""Вход по ЭЦП: challenge-response с откреплённой подписью CAdES.

Сервер выдаёт одноразовый challenge, браузер (КриптоПро ЭЦП Browser plug-in) подписывает его
сертификатом пользователя, сервер проверяет подпись утилитой cryptcp из КриптоПро CSP и
привязывает личность к отпечатку сертификата (SHA-256), а не к email или паролю.
"""
import base64
import hashlib
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

from asn1crypto import cms, x509

from . import gost_verify

CRYPTCP = os.environ.get("CRYPTCP", "/opt/cprocsp/bin/amd64/cryptcp")
CHALLENGE_TTL = 300
MAX_SIGNATURE_BYTES = 256 * 1024
# Проверка цепочки до доверенного корня (в проде включить: ECP_REQUIRE_CHAIN=1; нужны корни УЦ в хранилище CSP)
REQUIRE_CHAIN = os.environ.get("ECP_REQUIRE_CHAIN") == "1"
PREFIX = "ecp-login:"
VERIFIER = os.environ.get("ECP_VERIFIER", "gost")


class EcpError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Signer:
    fingerprint: str
    subject: str
    issuer: str
    serial: str
    display_name: str
    # данные организации из сертификата (пусто, если УЦ их не указал)
    inn: str = ""
    ogrn: str = ""


def challenge_text(nonce: str) -> str:
    return PREFIX + nonce


def _name(n: x509.Name) -> dict:
    return {k: v for k, v in n.native.items()} if n else {}


# OID реквизитов в subject сертификатов УЦ РФ
OID_INN_LEGAL = "1.2.643.100.4"
OID_INN = "1.2.643.3.131.1.1"
OID_OGRN = "1.2.643.100.1"
OID_OGRNIP = "1.2.643.100.5"


def _subject_oids(n: x509.Name) -> dict:
    """Атрибуты subject по точечной записи OID (asn1crypto не знает имён российских OID)."""
    out: dict = {}
    for rdn in n.chosen:
        for atv in rdn:
            v = atv["value"]
            try:
                val = v.native
                if val is None and hasattr(v, "parsed"):
                    val = v.parsed.native
            except Exception:
                continue
            if isinstance(val, bytes):
                val = val.decode("utf-8", "ignore")
            out.setdefault(atv["type"].dotted, str(val))
    return out


def _digits(v: str, lengths: tuple[int, ...]) -> str:
    d = "".join(ch for ch in v if ch.isdigit())
    # у юрлиц ИНН физлица-подписанта бывает «00» + ИНН10
    if len(d) == 12 and d.startswith("00") and 10 in lengths:
        d = d[2:]
    return d if len(d) in lengths else ""


def parse_signer(sig_der: bytes, now: float | None = None) -> Signer:
    """Достаёт сертификат единственного подписанта и проверяет срок его действия (подпись здесь не проверяется)."""
    try:
        ci = cms.ContentInfo.load(sig_der)
        sd = ci["content"]
        if ci["content_type"].native != "signed_data":
            raise EcpError("bad_signature")
        signers = list(sd["signer_infos"])
        certs = [c.chosen for c in (sd["certificates"] or []) if c.name == "certificate"]
        if len(signers) != 1:
            raise EcpError("bad_signature")
        sid = signers[0]["sid"]
        cert = None
        for c in certs:
            if sid.name == "issuer_and_serial_number":
                if c.serial_number == sid.chosen["serial_number"].native and c.issuer == sid.chosen["issuer"]:
                    cert = c
            elif sid.name == "subject_key_identifier" and c.key_identifier == sid.chosen.native:
                cert = c
        if cert is None:
            raise EcpError("no_certificate")
    except EcpError:
        raise
    except Exception:
        raise EcpError("bad_signature")
    t = now if now is not None else time.time()
    tbs = cert["tbs_certificate"]["validity"]
    if not (tbs["not_before"].native.timestamp() <= t <= tbs["not_after"].native.timestamp()):
        raise EcpError("certificate_expired")
    subj = _name(cert.subject)
    display = subj.get("organization_name") or subj.get("common_name") or ""
    oids = _subject_oids(cert.subject)
    inn = _digits(oids.get(OID_INN_LEGAL) or oids.get(OID_INN) or "", (10, 12))
    ogrn = _digits(oids.get(OID_OGRN) or oids.get(OID_OGRNIP) or "", (13, 15))
    return Signer(
        fingerprint=hashlib.sha256(cert.dump()).hexdigest(),
        subject=cert.subject.human_friendly,
        issuer=cert.issuer.human_friendly,
        serial=format(cert.serial_number, "x"),
        display_name=str(display)[:200],
        inn=inn,
        ogrn=ogrn,
    )


def verify_detached(message: bytes, sig_der: bytes) -> None:
    """Проверяет откреплённую подпись CAdES. ECP_VERIFIER=gost (по умолчанию, чистый Python, работает где угодно)
    или cryptcp (КриптоПро CSP на хосте, позволяет проверять цепочку: ECP_REQUIRE_CHAIN=1). Бросает EcpError."""
    if VERIFIER == "gost":
        try:
            gost_verify.verify_cades_detached(message, sig_der)
        except gost_verify.GostVerifyError as e:
            logging.getLogger("ecp").warning("проверка ЭЦП не пройдена: %s", e)  # только причина, без данных подписи
            if os.environ.get("ECP_DEBUG_DUMP") == "1":  # отладка: сохранить неверную подпись для разбора (не включать в проде)
                with open(f"/tmp/ecp-failed-{int(time.time())}.sig", "wb") as f:
                    f.write(sig_der)
                with open(f"/tmp/ecp-failed-{int(time.time())}.msg", "wb") as f:
                    f.write(message)
            raise EcpError("signature_invalid")
        return
    _verify_cryptcp(message, sig_der)


def _verify_cryptcp(message: bytes, sig_der: bytes) -> None:
    """Проверка утилитой cryptcp (-vsignf читает подпись из <файл>.sgn)."""
    with tempfile.TemporaryDirectory() as d:
        msg = os.path.join(d, "msg")
        open(msg, "wb").write(message)
        open(msg + ".sgn", "wb").write(sig_der)
        cmd = [CRYPTCP, "-vsignf", "-detached", "-norev"]
        if not REQUIRE_CHAIN:
            cmd.append("-nochain")
        cmd.append(msg)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30, cwd=d)
        except (OSError, subprocess.TimeoutExpired):
            raise EcpError("verifier_unavailable")
        if r.returncode != 0:
            raise EcpError("signature_invalid")


def decode_signature(b64: str) -> bytes:
    try:
        raw = base64.b64decode("".join(b64.split()), validate=True)
    except Exception:
        raise EcpError("bad_signature")
    if not raw or len(raw) > MAX_SIGNATURE_BYTES:
        raise EcpError("bad_signature")
    return raw
