"""User-configured image model services. No discovery, uploads or paid retries on load."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
import tomllib
from typing import Literal, Protocol
from urllib.parse import urlsplit
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from litbridge.errors import BridgeError, Code

VERSION = '1.0-litbridge-0.1.1-model-api.2'
KEY_NAME = r'LITBRIDGE_MODEL_[A-Z0-9_]{1,60}'
FORMULA_PROMPT = ('Transcribe only the formula visible in this image into LaTeX. '
    'Preserve symbols, subscripts, superscripts and equation tags. Do not solve it, '
    'explain it or infer missing symbols. Return only LaTeX, without Markdown fences.')


class FormulaRecognizer(Protocol):
    """Adapters return a candidate, never a claim of verified mathematical fidelity."""
    engine: str
    method: str
    version: str
    async def recognize(self, image: bytes) -> dict: ...


def endpoint(base_url, path, *, allow_insecure_http=False):
    try:
        url = urlsplit(base_url)
        local = url.hostname == 'localhost'
        if not local:
            try: local = ipaddress.ip_address(url.hostname or '').is_loopback
            except ValueError: pass
        if (url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password or
                url.query or url.fragment or (url.scheme == 'http' and not local and not allow_insecure_http) or
                re.search(r'[\s\\\x00-\x1f]', base_url) or '%' in base_url or
                not re.fullmatch(r'/[a-zA-Z0-9_/.-]+', path) or '..' in path or '//' in path or
                '..' in url.path or '//' in url.path):
            raise ValueError()
        _ = url.port
        return base_url.rstrip('/') + path
    except ValueError as exc:
        raise BridgeError(Code.INVALID_INPUT, 'Model endpoint requires HTTPS or explicit insecure HTTP opt-in, without URL credentials/query/fragment') from exc


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    protocol: Literal['openai_chat'] = 'openai_chat'
    base_url: str = Field(default='', max_length=2048)
    allow_insecure_http: bool = Field(default=False,strict=True)
    endpoint_path: str = '/chat/completions'
    model: str = Field(min_length=1, max_length=160)
    vision: bool = True
    api_key_env: str = 'LITBRIDGE_MODEL_API_KEY'
    api_key_header: str = 'Authorization'
    api_key_prefix: str = Field(default='Bearer ',max_length=200)
    prompt: str = Field(default=FORMULA_PROMPT, min_length=1, max_length=2048)
    max_tokens: int = Field(default=2048, ge=64, le=8192)
    temperature: float = Field(default=0, ge=0, le=2)
    timeout_seconds: float = Field(default=45, ge=5, le=180)
    extra_body: dict = Field(default_factory=dict)

    @field_validator('api_key_env')
    @classmethod
    def key_name(cls, value):
        if value and not re.fullmatch(KEY_NAME, value): raise ValueError('Unsupported credential variable')
        return value

    @field_validator('api_key_header')
    @classmethod
    def header(cls, value):
        if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,60}', value) or
                value.lower() in ('host', 'content-type', 'content-length', 'proxy-authorization')):
            raise ValueError('Invalid authentication header')
        return value

    @field_validator('model', 'api_key_prefix')
    @classmethod
    def no_controls(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value): raise ValueError('Control characters')
        return value

    @field_validator('api_key_prefix')
    @classmethod
    def ascii_prefix(cls,value):
        if not value.isascii(): raise ValueError('Authentication prefix must be ASCII')
        return value

    @field_validator('temperature', 'timeout_seconds')
    @classmethod
    def finite(cls, value):
        if not math.isfinite(value): raise ValueError('Non-finite parameter')
        return value

    @field_validator('extra_body')
    @classmethod
    def extras(cls, value):
        reserved = {'model', 'messages', 'stream', 'max_tokens', 'temperature', 'tools', 'tool_choice'}
        if reserved.intersection(value) or len(json.dumps(value, allow_nan=False).encode()) > 8192:
            raise ValueError('Invalid extra request parameters')
        return value


class ModelFile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    profiles: dict[str, ModelProfile] = Field(default_factory=dict, max_length=12)

    @field_validator('profiles')
    @classmethod
    def ids(cls, value):
        if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', name) for name in value):
            raise ValueError('Invalid model profile ID')
        return value


def load_profiles(path):
    if not path:
        raise BridgeError(Code.NOT_CONFIGURED, 'Model services file is not configured',
            action='Set model_services in the explicit local LitBridge config; see docs/MODEL_SERVICES.md')
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > 64 * 1024: raise ValueError()
        result = ModelFile.model_validate(tomllib.loads(path.read_text(encoding='utf-8')))
        for profile in result.profiles.values():
            if profile.base_url: endpoint(profile.base_url, profile.endpoint_path,allow_insecure_http=profile.allow_insecure_http)
        return result.profiles
    except (OSError, ValueError, ValidationError) as exc:
        raise BridgeError(Code.INVALID_INPUT, 'Model services TOML failed validation; check the local template') from exc


def info(path):
    if not path: return {'configured': False, 'profiles': []}
    profiles = load_profiles(path)
    return {'configured': True, 'profiles': [
        {'id': name, 'model': p.model, 'protocol': p.protocol, 'enabled': p.enabled, 'vision': p.vision,
         'endpoint_configured': bool(p.base_url),
         'credentials_configured': not p.api_key_env or bool(os.getenv(p.api_key_env, '').strip())}
        for name, p in profiles.items()], 'note': 'Configuration status only; no model request was sent'}


def create(path, profile_id, *, transport=None):
    profiles = load_profiles(path)
    if not isinstance(profile_id,str) or profile_id not in profiles:
        raise BridgeError(Code.INVALID_INPUT, 'Unknown model profile; use models to list configured profile IDs')
    return OpenAIChat(profile_id, profiles[profile_id], transport=transport)


def formula_text(text):
    """Remove display wrappers only, without silently rewriting mathematical symbols."""
    if not isinstance(text, str) or not 0 < len(text.strip()) <= 8192:
        raise BridgeError(Code.INVALID_CONTENT, 'Model formula text is missing or oversized')
    text = text.strip()
    fence = re.fullmatch(r'```(?:latex|tex)?\s*\n([\s\S]*?)\n```', text, flags=re.IGNORECASE)
    if fence: text = fence.group(1).strip()
    if text.startswith('```'):
        raise BridgeError(Code.INVALID_CONTENT, 'Model formula has an unsupported code wrapper')
    for left, right in (('$$', '$$'), (r'\[', r'\]'), (r'\(', r'\)'),('$','$')):
        if text.startswith(left) and text.endswith(right): text = text[len(left):-len(right)].strip(); break
    if not text or re.search(r'</?think\b',text,re.IGNORECASE) or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        raise BridgeError(Code.INVALID_CONTENT, 'Model did not return a usable formula candidate')
    return text


class OpenAIChat:
    engine = 'model'
    method = 'model_api'

    def __init__(self, profile_id, profile, *, transport=None):
        if not profile.enabled:
            raise BridgeError(Code.NOT_CONFIGURED, 'Selected model profile is disabled')
        if not profile.vision:
            raise BridgeError(Code.UNSUPPORTED, 'Selected model profile does not support image input')
        if not profile.base_url:
            raise BridgeError(Code.NOT_CONFIGURED, 'Selected model endpoint is not configured')
        self.url = endpoint(profile.base_url, profile.endpoint_path,allow_insecure_http=profile.allow_insecure_http)
        self.key = os.getenv(profile.api_key_env, '').strip() if profile.api_key_env else ''
        if profile.api_key_env and not self.key:
            raise BridgeError(Code.NOT_CONFIGURED, 'Selected model API credential is not configured')
        if not self.key.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in self.key) or len(self.key) > 8192:
            raise BridgeError(Code.INVALID_INPUT, 'Model API credential has invalid format')
        self.profile_id, self.profile, self.transport = profile_id, profile, transport
        identity = json.dumps(profile.model_dump(), sort_keys=True, ensure_ascii=False) + profile_id
        identity += hashlib.sha256(self.key.encode()).hexdigest()
        self.version = VERSION + '-' + hashlib.sha256(identity.encode()).hexdigest()[:24]

    async def recognize(self, image):
        if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 1024 * 1024:
            raise BridgeError(Code.INVALID_CONTENT, 'Model input must be a bounded PNG formula crop')
        p = self.profile
        payload = {**p.extra_body, 'model': p.model, 'stream': False, 'max_tokens': p.max_tokens,
            'temperature': p.temperature, 'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': p.prompt}, {'type': 'image_url', 'image_url': {
                    'url': 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')}}]}]}
        headers = {p.api_key_header: p.api_key_prefix + self.key} if self.key else {}
        start = time.monotonic()
        try:
            # No redirects, ambient proxies, request/response logs or automatic retries.
            async with asyncio.timeout(p.timeout_seconds):
                async with httpx.AsyncClient(timeout=p.timeout_seconds, trust_env=False,
                        follow_redirects=False, transport=self.transport) as client:
                    async with client.stream('POST', self.url, json=payload, headers=headers) as response:
                        code = response.status_code
                        if code in (401, 403): raise BridgeError(Code.AUTH_REQUIRED, 'Model service rejected authentication/access')
                        if code == 429: raise BridgeError(Code.RATE_LIMITED, 'Model service quota/rate limit reached')
                        if code != 200: raise BridgeError(Code.UPSTREAM, f'Model service returned HTTP {code}; no automatic retry')
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 256 * 1024: raise BridgeError(Code.TOO_LARGE, 'Model response exceeds bound')
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('error'): raise ValueError()
            choices = data['choices']
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict): raise ValueError()
            choice = choices[0]
            if choice.get('finish_reason') != 'stop':
                raise BridgeError(Code.INVALID_CONTENT, 'Model output is truncated/refused or has an unsupported finish reason')
            message = choice['message']
            if not isinstance(message, dict) or message.get('refusal') or message.get('tool_calls'): raise ValueError()
            text = formula_text(message.get('content'))
            model = data.get('model', p.model)
            if not isinstance(model, str) or not 0 < len(model) <= 160: raise ValueError()
            if self.key and (self.key in text or self.key in model): raise ValueError()
            usage = data.get('usage') or {}
            if not isinstance(usage, dict): raise ValueError()
            counts = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens') if k in usage}
            if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10_000_000 for v in counts.values()): raise ValueError()
            return {'text': text, 'confidence': None, 'model': model, 'profile': self.profile_id,
                'latency_seconds': round(time.monotonic() - start, 3), 'usage': counts}
        except BridgeError: raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise BridgeError(Code.TIMEOUT, 'Model service timed out; may have been billed; no automatic retry') from exc
        except httpx.HTTPError as exc:
            raise BridgeError(Code.NETWORK, 'Model service transport failed; may have been billed; no automatic retry') from exc
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise BridgeError(Code.INVALID_CONTENT, 'Model service response is incompatible; expected one completed text candidate') from exc
