"""多模型主备路由。"""

from collections.abc import Mapping

from utils.admin_settings import AdminSettingsStore
from utils.credential_scope import llm_profile_is_usable, resolve_scoped_api_key
from utils.deepseek_client import DeepSeekClient, DeepSeekAPIError


class ModelRouter:
    def __init__(self, base_llm_config, logger=None, settings_store=None):
        self.base = base_llm_config or {}
        self.logger = logger
        self.settings_store = settings_store or AdminSettingsStore()

    def _client(self, profile, request_options=None):
        base = self.base
        options = request_options or {}
        return DeepSeekClient(
            api_key=resolve_scoped_api_key(profile, base=base),
            model=profile.get("model") or base.get("model", "deepseek-chat"),
            base_url=profile.get("base_url") or base.get("base_url", ""),
            chat_completions_path=profile.get("chat_completions_path") or base.get("chat_completions_path", "/v1/chat/completions"),
            temperature=(options["temperature"] if "temperature" in options
                         else profile.get("temperature", base.get("temperature", 0.3))),
            max_tokens=(options["max_tokens"] if "max_tokens" in options
                        else profile.get("max_tokens", base.get("max_tokens", 1024))),
            timeout=(options["timeout"] if "timeout" in options
                     else profile.get("timeout", base.get("timeout", 30))),
        )

    def chat(self, messages, route="general", request_options=None):
        settings = self.settings_store.load()
        if not isinstance(settings, Mapping):
            settings = {}
        profiles = {
            p.get("id"): p for p in settings.get("models", [])
            if isinstance(p, Mapping) and p.get("id") and p.get("enabled", True)
        }
        routing = settings.get("model_routing", {})
        if not isinstance(routing, Mapping):
            routing = {}
        requested = []
        order = []
        for model_id in (routing.get(route), routing.get("fallback")):
            if model_id and model_id not in requested:
                requested.append(model_id)
                if model_id in profiles:
                    order.append(model_id)
        if not requested:
            # The first-run wizard configures one default OpenAI-compatible
            # endpoint. Treat it as the implicit route until an administrator
            # creates an explicit model profile.
            if not llm_profile_is_usable(self.base):
                return None, None
            try:
                if self.logger:
                    self.logger.info("多模型路由: route=%s model=default", route)
                client = (self._client({}) if request_options is None
                          else self._client({}, request_options))
                return client.chat(messages), "default"
            except Exception as exc:
                if self.logger:
                    self.logger.warning("默认模型调用失败: %s", exc)
                raise DeepSeekAPIError(str(exc)) from None
        last_error = None
        for model_id in order:
            if not llm_profile_is_usable(profiles[model_id], base=self.base):
                last_error = ValueError(f"模型配置不可用: {model_id}")
                if self.logger:
                    self.logger.warning("跳过不可用模型: %s", model_id)
                continue
            try:
                if self.logger:
                    self.logger.info("多模型路由: route=%s model=%s", route, profiles[model_id].get("name", model_id))
                client = (self._client(profiles[model_id]) if request_options is None
                          else self._client(profiles[model_id], request_options))
                return client.chat(messages), model_id
            except Exception as exc:
                last_error = exc
                if self.logger:
                    self.logger.warning("模型 %s 调用失败，尝试备用模型: %s", model_id, exc)
        raise DeepSeekAPIError(str(last_error) if last_error else "没有可用模型")
