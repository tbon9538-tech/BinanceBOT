"""签名与凭据（spec §6.1、§13）。测试向量取自官方 rest-api.md 与 RFC 8032。"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from bot.exchange.binance.client import encode_params, format_param, sign_query
from bot.exchange.binance.credentials import (
    ENV_API_KEY,
    ENV_API_SECRET,
    ENV_KEY_TYPE,
    ENV_PRIVATE_KEY_PATH,
    ApiCredentials,
    CredentialsError,
    Ed25519Signer,
    HmacSigner,
    KeyType,
    credentials_present,
    load_credentials,
    mask_api_key,
    redact_secrets,
)

# 官方 rest-api.md「SIGNED Endpoint Examples for POST /api/v3/order」HMAC 示例
DOC_API_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
UNICODE_SYMBOL = "１２３４５６"
UNICODE_ENCODED = "%EF%BC%91%EF%BC%92%EF%BC%93%EF%BC%94%EF%BC%95%EF%BC%96"


def _hmac_params(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "1",
        "price": "0.1",
        "recvWindow": 5000,
        "timestamp": 1499827319559,
    }


def test_hmac_ascii_vector_matches_official_example() -> None:
    creds = ApiCredentials(DOC_API_KEY, KeyType.HMAC, HmacSigner(DOC_SECRET))
    payload = encode_params(_hmac_params("LTCBTC"))
    assert payload == (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    assert creds.signer.sign(payload) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert sign_query(_hmac_params("LTCBTC"), creds) == (
        payload + "&signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )


def test_hmac_non_ascii_vector_percent_encodes_before_signing() -> None:
    creds = ApiCredentials(DOC_API_KEY, KeyType.HMAC, HmacSigner(DOC_SECRET))
    payload = encode_params(_hmac_params(UNICODE_SYMBOL))
    assert payload == (
        f"symbol={UNICODE_ENCODED}&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    assert creds.signer.sign(payload) == "e1353ec6b14d888f1164ae9af8228a3dbd508bc82eb867db8ab6046442f33ef3"


def test_signer_refuses_unencoded_payload() -> None:
    # 没有 percent-encode 的非 ASCII payload 必须直接失败，而不是签出一个 -1022 的签名
    with pytest.raises(UnicodeEncodeError):
        HmacSigner(DOC_SECRET).sign(f"symbol={UNICODE_SYMBOL}")


# RFC 8032 §7.1 TEST 1 / TEST 2（官方文档的 Ed25519 示例签名长度为 344 个 base64 字符，
# 实际是 RSA 示例的复制，无法作为 Ed25519 测试向量，见 doc/decisions_m5.md）
RFC_KEYS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        b"",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        bytes.fromhex("72"),
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


@pytest.mark.parametrize(("secret", "msg", "sig"), RFC_KEYS)
def test_ed25519_rfc8032_vectors(secret: str, msg: bytes, sig: str) -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret))
    signer = Ed25519Signer(key)
    assert base64.b64decode(signer.sign(msg.decode("ascii"))).hex() == sig


def _pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        (
            "BTCUSDT",
            "symbol=BTCUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=1&price=0.2"
            "&timestamp=1668481559918&recvWindow=5000",
        ),
        (
            UNICODE_SYMBOL,
            f"symbol={UNICODE_ENCODED}&side=SELL&type=LIMIT&timeInForce=GTC&quantity=1&price=0.2"
            "&timestamp=1668481559918&recvWindow=5000",
        ),
    ],
)
def test_ed25519_doc_payload_and_signature_roundtrip(symbol: str, expected: str) -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(RFC_KEYS[1][0]))
    creds = ApiCredentials(
        "4yNzx3yWC5bS6YTwEkSRaC0nRmSQIIStAUOh1b6kqaBrTLIhjCpI5lJH8q8R8WNO",
        KeyType.ED25519,
        Ed25519Signer.from_pem(_pem(key)),
    )
    params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "1",
        "price": "0.2",
        "timestamp": 1668481559918,
        "recvWindow": 5000,
    }
    assert encode_params(params) == expected  # 与官方文档 Ed25519 示例的 payload 完全一致
    query = sign_query(params, creds)
    payload, sig_part = query.rsplit("&signature=", 1)
    assert payload == expected
    raw_sig = creds.signer.sign(payload)
    assert len(base64.b64decode(raw_sig)) == 64
    # base64 中的 + / = 必须再 percent-encode
    assert not any(c in sig_part for c in "+/=")
    from urllib.parse import unquote

    assert unquote(sig_part) == raw_sig
    pub: Ed25519PublicKey = key.public_key()
    pub.verify(base64.b64decode(raw_sig), payload.encode("ascii"))  # 不抛异常即验签通过


def test_format_param_rules() -> None:
    from decimal import Decimal

    assert format_param(Decimal("1E-8")) == "0.00000001"
    assert format_param(True) == "true"
    assert format_param(KeyType.HMAC) == "hmac"
    with pytest.raises(TypeError):
        format_param(0.1)


def test_mask_and_repr_never_show_secrets() -> None:
    creds = ApiCredentials(DOC_API_KEY, KeyType.HMAC, HmacSigner(DOC_SECRET))
    text = repr(creds) + str(creds) + repr(creds.signer)
    assert DOC_SECRET not in text and DOC_API_KEY not in text
    assert creds.masked_key() == "vmPU…Eh8A"
    assert mask_api_key("short") == "****"


def test_redact_secrets() -> None:
    s = redact_secrets(f"GET /api/v3/account?timestamp=1&signature=abc%2Bdef X-MBX-APIKEY: {DOC_API_KEY}")
    assert "abc" not in s and DOC_API_KEY not in s and "signature=***" in s and "vmPU…Eh8A" in s


def test_load_credentials_hmac() -> None:
    env = {ENV_API_KEY: DOC_API_KEY, ENV_KEY_TYPE: "hmac   # 注释", ENV_API_SECRET: DOC_SECRET}
    creds = load_credentials(env)
    assert creds.key_type is KeyType.HMAC
    assert creds.signer.sign("a=1") == HmacSigner(DOC_SECRET).sign("a=1")
    assert credentials_present(env)
    assert (
        load_credentials({ENV_API_KEY: "k" * 20, ENV_API_SECRET: "s"}).key_type is KeyType.HMAC
    )  # 默认 hmac


def test_load_credentials_ed25519(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    p = tmp_path / "k.pem"
    p.write_bytes(_pem(key))
    env = {ENV_API_KEY: "x" * 64, ENV_KEY_TYPE: "ED25519", ENV_PRIVATE_KEY_PATH: str(p)}
    creds = load_credentials(env)
    assert creds.key_type is KeyType.ED25519
    key.public_key().verify(base64.b64decode(creds.signer.sign("a=1")), b"a=1")
    assert credentials_present(env)


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({}, ENV_API_KEY),
        ({ENV_API_KEY: "k"}, ENV_API_SECRET),
        ({ENV_API_KEY: "k", ENV_KEY_TYPE: "rsa"}, "hmac 或 ed25519"),
        ({ENV_API_KEY: "k", ENV_KEY_TYPE: "ed25519"}, ENV_PRIVATE_KEY_PATH),
        ({ENV_API_KEY: "k", ENV_KEY_TYPE: "ed25519", ENV_PRIVATE_KEY_PATH: "/no/such/file.pem"}, "无法读取"),
    ],
)
def test_load_credentials_errors(env: dict[str, str], match: str) -> None:
    with pytest.raises(CredentialsError, match=match):
        load_credentials(env)
    assert not credentials_present(env) or ENV_PRIVATE_KEY_PATH in env


def test_non_ed25519_pem_rejected(tmp_path: Path) -> None:
    ec_key = ec.generate_private_key(ec.SECP256R1())
    pem = ec_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(CredentialsError, match="不是 Ed25519"):
        Ed25519Signer.from_pem(pem)
    with pytest.raises(CredentialsError, match="无法解析"):
        Ed25519Signer.from_pem(b"not a pem")


def test_load_from_dotenv_only_at_call_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ENV_API_KEY, ENV_KEY_TYPE, ENV_API_SECRET, ENV_PRIVATE_KEY_PATH):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"{ENV_API_KEY}={DOC_API_KEY}\n{ENV_KEY_TYPE}=hmac            # hmac | ed25519\n"
        f"{ENV_API_SECRET}={DOC_SECRET}\n",
        encoding="utf-8",
    )
    import os

    assert ENV_API_KEY not in os.environ
    try:
        creds = load_credentials(dotenv_path=env_file)
        assert creds.api_key == DOC_API_KEY
        assert creds.key_type is KeyType.HMAC  # 行内注释被正确忽略
    finally:
        for name in (ENV_API_KEY, ENV_KEY_TYPE, ENV_API_SECRET):
            os.environ.pop(name, None)


def test_loading_logs_masked_key_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    load_credentials({ENV_API_KEY: DOC_API_KEY, ENV_API_SECRET: DOC_SECRET})
    assert DOC_API_KEY not in caplog.text and DOC_SECRET not in caplog.text
    assert "vmPU…Eh8A" in caplog.text
