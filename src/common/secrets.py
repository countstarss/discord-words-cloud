from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional


# MARK: - Secret Cipher
# 说明：
# - 优先使用 CREDENTIALS_ENCRYPTION_KEY 作为加密主密钥
# - 若未配置，则退化为 plain: 前缀编码（便于开发环境快速跑通）
class SecretCipher:
    def __init__(self):
        self._fernet = None
        raw_key = os.getenv("CREDENTIALS_ENCRYPTION_KEY") or os.getenv("APP_SECRET")

        if not raw_key:
            return

        try:
            from cryptography.fernet import Fernet

            if len(raw_key) == 44 and raw_key.endswith("="):
                key = raw_key.encode("utf-8")
            else:
                digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
                key = base64.urlsafe_b64encode(digest)
            self._fernet = Fernet(key)
        except Exception:
            self._fernet = None

    # 对外只暴露字符串形式，便于直接存数据库。
    def encrypt(self, value: str) -> str:
        if self._fernet is None:
            # Fallback encoding if encryption key is not configured.
            return "plain:" + base64.urlsafe_b64encode(value.encode("utf-8")).decode("utf-8")
        token = self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")
        return "enc:" + token

    # 解密失败统一返回 None，调用方据此决定是否忽略该凭据。
    def decrypt(self, value: str) -> Optional[str]:
        if not value:
            return None
        try:
            if value.startswith("enc:"):
                if self._fernet is None:
                    return None
                raw = value[len("enc:") :]
                return self._fernet.decrypt(raw.encode("utf-8")).decode("utf-8")
            if value.startswith("plain:"):
                raw = value[len("plain:") :]
                return base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8")
            return value
        except Exception:
            return None


# MARK: - Masking
# 仅用于 UI 展示，避免泄露完整 API Key。
def mask_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


# MARK: - Singleton
_cipher: Optional[SecretCipher] = None


def get_secret_cipher() -> SecretCipher:
    global _cipher
    if _cipher is None:
        _cipher = SecretCipher()
    return _cipher
