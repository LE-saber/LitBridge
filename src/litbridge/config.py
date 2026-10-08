"""Explicit local configuration and opt-in, trusted Python provider plugins."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from importlib.metadata import entry_points
import os
from pathlib import Path
import re
import tomllib
from pydantic import BaseModel, ConfigDict, Field
from litbridge.browser_workflow import WorkflowBrowser, ManagedBrowser
from litbridge.core import Gateway
from litbridge.errors import BridgeError, Code
from litbridge.models import ProviderInfo
from litbridge.providers.base import Provider
from litbridge.storage import Store


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    home: Path = Field(default_factory=lambda: Path.home() / '.litbridge')
    profile: str = Field(default='default', min_length=1, max_length=100)
    timeout_seconds: float = Field(default=45, ge=5, le=180)
    cache_ttl_seconds: int = Field(default=21600, ge=0, le=604800)
    default_providers: list[str] | None = None
    cdp_url: str | None = None
    browser_context_index: int = Field(default=0, ge=0)
    managed_browser: bool = False
    browser_channel: Literal['chrome', 'msedge', 'chromium'] = 'chrome'
    verification_mode: Literal['selectors', 'stable'] = 'selectors'
    human_wait_seconds: int = Field(default=300, ge=1, le=900)
    enabled_plugins: list[str] = Field(default_factory=list, max_length=12)
    plugin_options: dict[str, dict] = Field(default_factory=dict)
    normalization_python: Path | None = None
    normalization_models: Path | None = None
    cloud_formula_ocr: bool = False
    model_services: Path | None = None


def load_credentials(config_path):
    """Only the explicit config's adjacent, ignored credential file is loaded."""
    path = Path(config_path).resolve().parent / '.env.litbridge.toml'
    if not path.exists(): return
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024: raise ValueError()
        values = tomllib.loads(path.read_text(encoding='utf-8'))
        allowed = {'MATHPIX_APP_ID','MATHPIX_APP_KEY'}
        if any((key not in allowed and not re.fullmatch(r'(?:LITBRIDGE_MODEL_[A-Z0-9_]{1,60}|[A-Z0-9_]{1,60}_(?:API_KEY|INSTTOKEN))',key)) or
                not isinstance(value,str) or len(value)>8192 or any(ord(c)<32 or ord(c)==127 for c in value)
                for key,value in values.items()): raise ValueError()
        for key,value in values.items():
            if value.strip(): os.environ[key]=value.strip()
    except (OSError,ValueError) as exc:
        raise BridgeError(Code.INVALID_INPUT,'Local credential TOML is invalid; check supported quoted-string keys') from exc


def load_settings(path: str | None = None, home: str | None = None):
    data = {}
    if path:
        try:
            with open(path, 'rb') as file:
                data = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise BridgeError(Code.INVALID_INPUT, 'Local TOML config cannot be read or parsed') from exc
    if home:
        data['home'] = home
    settings = Settings.model_validate(data)
    if path:
        if settings.model_services and not settings.model_services.is_absolute():
            settings.model_services = Path(path).resolve().parent / settings.model_services
        load_credentials(path)
    return settings


@dataclass(frozen=True)
class PluginContext:
    """Explicit options and an optional shared authorized browser; no implicit launch."""
    protocol: str
    options: dict
    browser: WorkflowBrowser | None = None
    home: Path | None = None


def build_gateway(settings: Settings) -> Gateway:
    if settings.cdp_url and settings.managed_browser:
        raise BridgeError(Code.INVALID_INPUT, 'Choose managed_browser or cdp_url, not both')
    store = Store(settings.home, settings.profile)
    browser = (ManagedBrowser(store.home / 'browser-profile', settings.browser_channel) if settings.managed_browser else
               WorkflowBrowser(settings.cdp_url, context_index=settings.browser_context_index) if settings.cdp_url else None)
    if browser:
        browser.verification_mode = settings.verification_mode
        browser.human_wait_seconds = settings.human_wait_seconds
    providers = []
    installed = {e.name: e for e in entry_points(group='litbridge.providers')}
    existing = {p.info.id for p in providers}
    protected = {p.info.id for p in providers if p.info.state != 'planned'}
    enabled = set()
    for name in settings.enabled_plugins:
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,40}', name) or name in enabled or name in protected:
            raise BridgeError(Code.INVALID_INPUT, 'Plugin name is invalid, duplicated or conflicts with another enabled provider')
        enabled.add(name)
        existing.add(name)
        # A separately packaged provider may replace a planned catalog entry without changing Core.
        providers = [p for p in providers if p.info.id != name]
        try:
            plugin = installed[name].load()(PluginContext('1.0', settings.plugin_options.get(name, {}), browser, store.home))
            info = ProviderInfo.model_validate(plugin.info.model_dump())
            if info.id != name or info.protocol != '1.0':
                raise ValueError('incompatible protocol')
            providers.append(plugin)
        except Exception:
            p = Provider()
            p.info = ProviderInfo(id=name, name=name, capabilities=[], state='incompatible',
                                 note='Plugin could not load or does not implement protocol 1.0; inspect local package')
            providers.append(p)
    if settings.default_providers is not None and (not settings.default_providers or
            set(settings.default_providers) - existing):
        raise BridgeError(Code.INVALID_INPUT, 'default_providers contains unknown IDs or is empty')
    return Gateway(providers, store, timeout=settings.timeout_seconds, cache_ttl=settings.cache_ttl_seconds,
                   browser=browser, defaults=settings.default_providers,
                   structured_python=settings.normalization_python, structured_models=settings.normalization_models,
                   cloud_formula_ocr=settings.cloud_formula_ocr,model_services=settings.model_services)
