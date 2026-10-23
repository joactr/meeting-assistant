# pyrage 1.4.0 exports extension-created module objects as package attributes.
# Neither namespace is an importable Python source submodule.
from collections.abc import Sequence
from typing import Protocol

class _PassphraseNamespace(Protocol):
    def encrypt(self, plaintext: bytes, passphrase: str, armored: bool = False) -> bytes: ...
    def decrypt(self, ciphertext: bytes, passphrase: str, armored: bool = False) -> bytes: ...

class _Identity:
    @classmethod
    def generate(cls) -> _Identity: ...
    @classmethod
    def from_str(cls, v: str) -> _Identity: ...
    def to_public(self) -> _Recipient: ...

class _Recipient:
    @classmethod
    def from_str(cls, v: str) -> _Recipient: ...

class _X25519Namespace(Protocol):
    Identity: type[_Identity]
    Recipient: type[_Recipient]

passphrase: _PassphraseNamespace
x25519: _X25519Namespace

def encrypt(plaintext: bytes, recipients: Sequence[_Recipient], armored: bool = False) -> bytes: ...
def decrypt(ciphertext: bytes, identities: Sequence[_Identity]) -> bytes: ...
