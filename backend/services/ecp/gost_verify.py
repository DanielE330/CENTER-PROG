"""Проверка откреплённой подписи CAdES-BES на ГОСТ Р 34.10-2012 / Стрибог без КриптоПро CSP (чистый Python, gostcrypto).

Проверяет: (1) хэш сообщения равен messageDigest в подписанных атрибутах, (2) подпись над DER подписанных атрибутов
верна открытым ключом сертификата подписанта. Цепочка доверия и отзыв сертификата НЕ проверяются.
"""
from asn1crypto import cms, core
from gostcrypto import gosthash, gostsignature

# алгоритмы хэша (Стрибог) и подписи
OID_STREEBOG_256 = "1.2.643.7.1.1.2.2"
OID_STREEBOG_512 = "1.2.643.7.1.1.2.3"
OID_GOST_2012_256 = "1.2.643.7.1.1.1.1"
OID_GOST_2012_512 = "1.2.643.7.1.1.1.2"
OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"

# наборы параметров кривых → имена в gostcrypto (CryptoPro-A/B/C 2001 совпадают с tc26 paramSetB/C/D)
CURVES = {
    "1.2.643.7.1.2.1.1.1": "id-tc26-gost-3410-2012-256-paramSetA",
    "1.2.643.7.1.2.1.1.2": "id-tc26-gost-3410-2012-256-paramSetB",
    "1.2.643.7.1.2.1.1.3": "id-tc26-gost-3410-2012-256-paramSetC",
    "1.2.643.7.1.2.1.1.4": "id-tc26-gost-3410-2012-256-paramSetD",
    "1.2.643.2.2.35.1": "id-tc26-gost-3410-2012-256-paramSetB",
    "1.2.643.2.2.35.2": "id-tc26-gost-3410-2012-256-paramSetC",
    "1.2.643.2.2.35.3": "id-tc26-gost-3410-2012-256-paramSetD",
    "1.2.643.2.2.36.0": "id-tc26-gost-3410-2012-256-paramSetB",  # CryptoPro-XchA = кривая CryptoPro-A
    "1.2.643.2.2.36.1": "id-tc26-gost-3410-2012-256-paramSetD",  # CryptoPro-XchB = кривая CryptoPro-C
    "1.2.643.7.1.2.1.2.1": "id-tc26-gost-3410-12-512-paramSetA",
    "1.2.643.7.1.2.1.2.2": "id-tc26-gost-3410-12-512-paramSetB",
    "1.2.643.7.1.2.1.2.3": "id-tc26-gost-3410-2012-512-paramSetC",
}


class GostVerifyError(Exception):
    """Подпись неверна или не поддерживается (message — причина для логов, наружу не отдаётся)."""


def streebog(data: bytes, bits: int) -> bytes:
    h = gosthash.new("streebog256" if bits == 256 else "streebog512", data=data)
    return bytes(h.digest())


def _swap_halves(b: bytes) -> bytes:
    h = len(b) // 2
    return b[h:] + b[:h]


def verify_raw(mode: int, curve: str, key: bytes, digest: bytes, signature: bytes) -> bool:
    """Проверка подписи над готовым хэшем в «сертификатном» формате КриптоПро/CMS (подтверждено на реальной подписи):
    ключ — X и Y в little-endian (по половинам), подпись — s||r, хэш — в обратном порядке байт относительно вывода Стрибога."""
    h = len(key) // 2
    try:
        engine = gostsignature.new(mode, gostsignature.CURVES_R_1323565_1_024_2019[curve])
        return bool(engine.verify(bytearray(key[:h][::-1] + key[h:][::-1]), bytearray(digest[::-1]),
                                  bytearray(_swap_halves(signature))))
    except Exception:
        return False


def verify_cades_detached(message: bytes, sig_der: bytes) -> None:
    """Бросает GostVerifyError, если подпись не проходит проверку."""
    try:
        ci = cms.ContentInfo.load(sig_der)
        sd = ci["content"]
        si = list(sd["signer_infos"])[0]
        certs = [c.chosen for c in (sd["certificates"] or []) if c.name == "certificate"]
    except Exception:
        raise GostVerifyError("не удалось разобрать CMS")
    sid = si["sid"]
    cert = None
    for c in certs:
        if sid.name == "issuer_and_serial_number" and c.serial_number == sid.chosen["serial_number"].native \
                and c.issuer == sid.chosen["issuer"]:
            cert = c
        elif sid.name == "subject_key_identifier" and c.key_identifier == sid.chosen.native:
            cert = c
    if cert is None:
        raise GostVerifyError("сертификат подписанта не найден")

    hash_oid = si["digest_algorithm"]["algorithm"].dotted
    if hash_oid not in (OID_STREEBOG_256, OID_STREEBOG_512):
        raise GostVerifyError(f"неподдерживаемый алгоритм хэша {hash_oid}")
    bits = 256 if hash_oid == OID_STREEBOG_256 else 512

    attrs = si["signed_attrs"]
    if attrs.native is None:
        raise GostVerifyError("нет подписанных атрибутов")
    md = None
    for a in attrs:
        if a["type"].dotted == OID_MESSAGE_DIGEST:
            md = bytes(a["values"][0].native)
    if md is None or md != streebog(message, bits):
        raise GostVerifyError("messageDigest не совпадает с хэшем сообщения")

    # SubjectPublicKeyInfo разбираем без схемы asn1crypto: для ГОСТ-ключей у библиотеки нет описания
    spki = core.load(cert["tbs_certificate"]["subject_public_key_info"].dump())
    alg_oid = spki[0][0].dotted
    if alg_oid not in (OID_GOST_2012_256, OID_GOST_2012_512):
        raise GostVerifyError(f"неподдерживаемый алгоритм ключа {alg_oid}")
    mode = gostsignature.MODE_256 if alg_oid == OID_GOST_2012_256 else gostsignature.MODE_512
    curve = CURVES.get(spki[0][1][0].dotted)
    if curve is None:
        raise GostVerifyError(f"неизвестный набор параметров кривой {spki[0][1][0].dotted}")
    key_raw = bytes(core.load(spki[1].contents[1:]).native)

    signed = bytearray(attrs.dump())
    signed[0] = 0x31  # [0] IMPLICIT → универсальный SET OF для хэширования
    sign_alg = si["signature_algorithm"]["algorithm"].dotted
    sign_bits = 256 if sign_alg == OID_GOST_2012_256 else 512
    digest = streebog(bytes(signed), sign_bits)
    if not verify_raw(mode, curve, key_raw, digest, bytes(si["signature"].native)):
        raise GostVerifyError("подпись не прошла проверку")
