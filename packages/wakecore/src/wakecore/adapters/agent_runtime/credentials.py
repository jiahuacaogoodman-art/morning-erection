"""OS credential storage. Secret values never cross a command line or a log formatter."""
import ctypes
import json
import sys
import threading


class CredentialStoreError(RuntimeError):
    pass


class MacKeychain:
    """Generic-password items via Security.framework, with private in-process payloads."""

    service = "com.morning.agent.credentials.v1"
    NOT_FOUND = -25300

    def __init__(self, namespace):
        if sys.platform != "darwin":
            raise CredentialStoreError("secure_store_unavailable")
        self.namespace = namespace
        self.lock = threading.RLock()
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        ptr = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [ptr, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype = ptr
        self.cf.CFDataCreate.argtypes = [ptr, ctypes.c_void_p, ctypes.c_long]
        self.cf.CFDataCreate.restype = ptr
        self.cf.CFDataGetLength.argtypes = [ptr]
        self.cf.CFDataGetLength.restype = ctypes.c_long
        self.cf.CFDataGetBytePtr.argtypes = [ptr]
        self.cf.CFDataGetBytePtr.restype = ptr
        self.cf.CFDictionaryCreateMutable.argtypes = [ptr, ctypes.c_long, ptr, ptr]
        self.cf.CFDictionaryCreateMutable.restype = ptr
        self.cf.CFDictionarySetValue.argtypes = [ptr, ptr, ptr]
        self.cf.CFDictionarySetValue.restype = None
        self.cf.CFRelease.argtypes = [ptr]
        self.cf.CFRelease.restype = None
        self.security.SecItemAdd.argtypes = [ptr, ctypes.POINTER(ptr)]
        self.security.SecItemCopyMatching.argtypes = [ptr, ctypes.POINTER(ptr)]
        self.security.SecItemUpdate.argtypes = [ptr, ptr]
        self.security.SecItemDelete.argtypes = [ptr]
        for name in ("SecItemAdd", "SecItemCopyMatching", "SecItemUpdate", "SecItemDelete"):
            getattr(self.security, name).restype = ctypes.c_int32

    def _constant(self, name, library=None):
        return ctypes.c_void_p.in_dll(library or self.security, name).value

    def _dictionary(self, provider, *, data=None, read=False, attributes_only=False):
        owned = []
        result = self.cf.CFDictionaryCreateMutable(None, 0, None, None)
        owned.append(result)

        def put(key, value):
            self.cf.CFDictionarySetValue(result, self._constant(key), value)

        def string(value):
            ref = self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100)
            owned.append(ref)
            return ref

        if not attributes_only:
            put("kSecClass", self._constant("kSecClassGenericPassword"))
            put("kSecAttrService", string(self.service))
            put("kSecAttrAccount", string(self.namespace + ":" + provider))
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ref = self.cf.CFDataCreate(None, raw, len(raw))
            owned.append(ref)
            put("kSecValueData", ref)
        if read:
            put("kSecReturnData", self._constant("kCFBooleanTrue", self.cf))
            put("kSecMatchLimit", self._constant("kSecMatchLimitOne"))
        return result, owned

    def _release(self, owned):
        for ref in owned:
            self.cf.CFRelease(ref)

    @staticmethod
    def _check(status):
        if status:
            # OSStatus is safe diagnostic data; credential contents are never interpolated.
            raise CredentialStoreError(f"secure_store_error:{status}")

    def read(self, provider):
        with self.lock:
            query, owned = self._dictionary(provider, read=True)
            result = ctypes.c_void_p()
            try:
                status = self.security.SecItemCopyMatching(query, ctypes.byref(result))
                if status == self.NOT_FOUND:
                    return None
                self._check(status)
                raw = ctypes.string_at(self.cf.CFDataGetBytePtr(result), self.cf.CFDataGetLength(result))
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise CredentialStoreError("secure_store_invalid_record") from None
            finally:
                if result.value:
                    self.cf.CFRelease(result)
                self._release(owned)

    def write(self, provider, value):
        with self.lock:
            query, query_refs = self._dictionary(provider)
            attrs, attr_refs = self._dictionary(provider, data=value, attributes_only=True)
            try:
                status = self.security.SecItemUpdate(query, attrs)
                if status == self.NOT_FOUND:
                    entry, refs = self._dictionary(provider, data=value)
                    try:
                        status = self.security.SecItemAdd(entry, None)
                    finally:
                        self._release(refs)
                self._check(status)
            finally:
                self._release(attr_refs)
                self._release(query_refs)

    def delete(self, provider):
        with self.lock:
            query, refs = self._dictionary(provider)
            try:
                status = self.security.SecItemDelete(query)
                if status != self.NOT_FOUND:
                    self._check(status)
            finally:
                self._release(refs)
