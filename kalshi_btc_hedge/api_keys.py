from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

def load_kalshi_api_key():
    kalshi_api_key_file_path = "eshan-kalshi-api-key.txt"
    with open(kalshi_api_key_file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    return private_key