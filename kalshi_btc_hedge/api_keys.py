from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import serialization
import base64
from cryptography.exceptions import InvalidSignature


def load_kalshi_rsa_private_key():
    kalshi_api_key_file_path = "eshan-kalshi-rsa-private-key.txt"
    with open(kalshi_api_key_file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    return private_key

def sign_pss_text(private_key: rsa.RSAPrivateKey, text: str) -> str:
    message = text.encode('utf-8')
    try:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')
    except InvalidSignature as e:
        raise ValueError("RSA sign PSS failed") from e

def load_kalshi_api_key():
    kalshi_api_key_file_path = "eshan-kalshi-api-key.txt"
    with open(kalshi_api_key_file_path, "r") as key_file:
        return key_file.read().strip()