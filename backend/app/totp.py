"""RFC 6238 TOTP (SHA-1, 30 s, 6 digits) so Kotak sessions can be re-established
without a human pasting a code that expires in 30 seconds."""
import base64
import hashlib
import hmac
import struct
import time


def totp(secret_b32, at=None, step=30, digits=6):
    key = base64.b32decode(secret_b32.replace(' ', '').upper() + '=' * (-len(secret_b32.replace(' ', '')) % 8))
    counter = int((at if at is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)
