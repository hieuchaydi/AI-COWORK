"""
Session management and authentication.
"""
import hmac
import os
import secrets
from typing import Optional

class SessionManager:
    def __init__(self, token_file: Optional[str] = None):
        self.token = self._load_token(token_file) if token_file else secrets.token_hex(16)
        self.authenticated = False
        
    def _load_token(self, token_file: str) -> str:
        if os.path.exists(token_file):
            with open(token_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        else:
            token = secrets.token_hex(16)
            os.makedirs(os.path.dirname(token_file), exist_ok=True)
            with open(token_file, "w", encoding="utf-8") as f:
                f.write(token)
            return token
            
    def authenticate(self, client_token: str) -> bool:
        """Constant-time token comparison."""
        if hmac.compare_digest(self.token, client_token):
            self.authenticated = True
            return True
        return False
        
    def check_auth(self):
        if not self.authenticated:
            raise PermissionError("Session not authenticated")
