"""
MT5 Credential Manager
Handles encrypted storage and retrieval of MT5 credentials from Supabase.
Each user can have exactly one MT5 account linked.
"""

import os
from typing import Optional, Dict, Any
from dataclasses import dataclass
from cryptography.fernet import Fernet, InvalidToken
from supabase import Client


@dataclass
class MT5Credentials:
    """Decrypted MT5 credentials ready for use"""
    user_id: str
    login: str
    password: str  # Decrypted
    server: str
    is_connected: bool = False


class MT5CredentialManager:
    """
    Manages encrypted MT5 credentials in Supabase.
    
    Security Model:
    - Passwords are encrypted with Fernet (AES-128-CBC) before storage
    - Encryption key is stored only in server's .env file
    - Supabase RLS ensures users can only access their own credentials
    """
    
    TABLE_NAME = "user_mt5_credentials"
    
    def __init__(self, supabase_client: Client, encryption_key: Optional[str] = None):
        """
        Initialize credential manager.
        
        Args:
            supabase_client: Authenticated Supabase client
            encryption_key: Fernet encryption key (from .env)
        """
        self.supabase = supabase_client
        
        # Get encryption key from env if not provided
        key = encryption_key or os.getenv("MT5_ENCRYPTION_KEY")
        if not key:
            raise ValueError(
                "MT5_ENCRYPTION_KEY not found. Generate one with:\n"
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        
        # Ensure key is bytes
        if isinstance(key, str):
            key = key.encode()
        
        self.fernet = Fernet(key)
    
    def _encrypt_password(self, password: str) -> str:
        """Encrypt password for storage"""
        return self.fernet.encrypt(password.encode()).decode()
    
    def _decrypt_password(self, encrypted_password: str) -> str:
        """Decrypt password for use"""
        try:
            return self.fernet.decrypt(encrypted_password.encode()).decode()
        except InvalidToken:
            raise ValueError("Failed to decrypt password - invalid encryption key")
    
    async def save_credentials(
        self, 
        user_id: str, 
        login: str, 
        password: str, 
        server: str
    ) -> Dict[str, Any]:
        """
        Save or update MT5 credentials for a user.
        
        Args:
            user_id: Supabase user ID
            login: MT5 login number
            password: MT5 password (will be encrypted)
            server: MT5 server name
            
        Returns:
            Saved record (without decrypted password)
            
        Raises:
            Exception if MT5 account is already linked to another user
        """
        encrypted_password = self._encrypt_password(password)
        
        data = {
            "user_id": user_id,
            "mt5_login": login,
            "mt5_password_encrypted": encrypted_password,
            "mt5_server": server,
            "is_connected": False
        }
        
        try:
            # Try upsert (insert or update)
            result = self.supabase.table(self.TABLE_NAME).upsert(
                data,
                on_conflict="user_id"  # Update if user already has credentials
            ).execute()
            
            if result.data:
                saved = result.data[0]
                # Don't return the encrypted password
                saved.pop("mt5_password_encrypted", None)
                return saved
            
            raise Exception("Failed to save credentials")
            
        except Exception as e:
            error_msg = str(e)
            # Check for unique constraint violation (MT5 already linked)
            if "unique_mt5_account" in error_msg or "duplicate key" in error_msg.lower():
                raise Exception(
                    f"MT5 account {login}@{server} is already linked to another user"
                )
            raise
    
    async def get_credentials(self, user_id: str) -> Optional[MT5Credentials]:
        """
        Retrieve decrypted MT5 credentials for a user.
        
        Args:
            user_id: Supabase user ID
            
        Returns:
            MT5Credentials object with decrypted password, or None if not linked
        """
        result = self.supabase.table(self.TABLE_NAME).select("*").eq(
            "user_id", user_id
        ).execute()
        
        if not result.data:
            return None
        
        record = result.data[0]
        
        return MT5Credentials(
            user_id=user_id,
            login=record["mt5_login"],
            password=self._decrypt_password(record["mt5_password_encrypted"]),
            server=record["mt5_server"],
            is_connected=record.get("is_connected", False)
        )
    
    async def has_credentials(self, user_id: str) -> bool:
        """Check if user has linked MT5 credentials"""
        result = self.supabase.table(self.TABLE_NAME).select("id").eq(
            "user_id", user_id
        ).execute()
        return len(result.data) > 0
    
    async def delete_credentials(self, user_id: str) -> bool:
        """
        Delete MT5 credentials for a user (unlink account).
        
        Args:
            user_id: Supabase user ID
            
        Returns:
            True if deleted, False if no credentials existed
        """
        result = self.supabase.table(self.TABLE_NAME).delete().eq(
            "user_id", user_id
        ).execute()
        
        return len(result.data) > 0
    
    async def set_connected_status(self, user_id: str, is_connected: bool) -> None:
        """Update the connection status for a user's MT5 credentials"""
        self.supabase.table(self.TABLE_NAME).update({
            "is_connected": is_connected,
            "last_connected_at": "now()" if is_connected else None
        }).eq("user_id", user_id).execute()
    
    async def is_mt5_available(self, login: str, server: str, user_id: Optional[str] = None) -> bool:
        """
        Check if an MT5 account is available for linking.
        
        Args:
            login: MT5 login number
            server: MT5 server name
            user_id: Optional - exclude this user from the check
            
        Returns:
            True if MT5 account is not linked to another user
        """
        query = self.supabase.table(self.TABLE_NAME).select("user_id").eq(
            "mt5_login", login
        ).eq("mt5_server", server)
        
        result = query.execute()
        
        if not result.data:
            return True  # No one has this MT5
        
        # If user_id provided, check if it's the same user
        existing_user = result.data[0]["user_id"]
        return existing_user == user_id


# Utility function to generate encryption key
def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key for .env"""
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    # Generate a key when run directly
    print("Generated MT5_ENCRYPTION_KEY:")
    print(generate_encryption_key())
    print("\nAdd this to your .env file as:")
    print("MT5_ENCRYPTION_KEY=<the-key-above>")
