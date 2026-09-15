"""
access_token / refresh_token을 DB에 평문으로 저장하지 않기 위한 간단한 대칭키 암복호화.
FERNET_KEY는 .env에 저장하고, Fernet.generate_key()로 한 번만 발급해서 고정 사용한다.
"""

import os
from cryptography.fernet import Fernet

_FERNET_KEY = os.environ["FERNET_KEY"].encode()  # 없으면 바로 에러나게 둠 (설정 누락 방지)
_fernet = Fernet(_FERNET_KEY)


def encrypt_token(raw: str) -> str:
    return _fernet.encrypt(raw.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()
