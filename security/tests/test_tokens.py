import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def test_token_roundtrip():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo)
    import time
    tok = jwt.encode({"iss": "x", "scope": "score:write", "exp": int(time.time()) + 60},
                     pem, algorithm="RS256")
    assert jwt.decode(tok, pub, algorithms=["RS256"])["scope"] == "score:write"
