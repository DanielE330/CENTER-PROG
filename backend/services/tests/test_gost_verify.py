"""Проверка ГОСТ-подписи (ecp.gost_verify): круговой тест на сгенерированном ключе и отказ при порче данных."""
from gostcrypto import gostsignature

from ecp import gost_verify as g

CURVE = "id-tc26-gost-3410-2012-256-paramSetA"


def _keypair():
    engine = gostsignature.new(gostsignature.MODE_256, gostsignature.CURVES_R_1323565_1_024_2019[CURVE])
    priv = bytes(range(1, 33))
    pub = bytes(engine.public_key_generate(bytearray(priv)))  # порядок байт библиотеки
    h = len(pub) // 2
    return engine, priv, pub[:h][::-1] + pub[h:][::-1]  # в формате сертификата: X и Y little-endian


def _sign(engine, priv, digest):
    sig = bytes(engine.sign(bytearray(priv), bytearray(digest[::-1])))  # r||s библиотеки
    return g._swap_halves(sig)  # в формате CMS: s||r


def test_roundtrip_and_tampering():
    engine, priv, cert_key = _keypair()
    digest = g.streebog(b"ecp-login:test", 256)
    sig = _sign(engine, priv, digest)
    mode = gostsignature.MODE_256
    assert g.verify_raw(mode, CURVE, cert_key, digest, sig)
    assert not g.verify_raw(mode, CURVE, cert_key, g.streebog(b"ecp-login:other", 256), sig)
    bad = bytearray(sig)
    bad[5] ^= 1
    assert not g.verify_raw(mode, CURVE, cert_key, digest, bytes(bad))
    other_key = bytearray(cert_key)
    other_key[3] ^= 1
    assert not g.verify_raw(mode, CURVE, bytes(other_key), digest, sig)


def test_streebog_known_vector():
    # контрольный вектор ГОСТ Р 34.11-2012 (M1 = «012…012», 63 байта), 256 бит
    m1 = bytes.fromhex("323130393837363534333231303938373635343332313039383736353433323130393837363534333231303938373635343332313039383736353433323130")[::-1]  # в стандарте записано в обратном порядке
    assert g.streebog(m1, 256).hex() == "9d151eefd8590b89daa6ba6cb74af9275dd051026bb149a452fd84e5e57b5500"


def test_garbage_is_rejected():
    import pytest
    with pytest.raises(g.GostVerifyError):
        g.verify_cades_detached(b"x", b"not a cms")
