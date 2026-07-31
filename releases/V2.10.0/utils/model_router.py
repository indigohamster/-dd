"""多模型主备路由。"""

from utils.admin_settings import AdminSettingsStore
from utils.deepseek_client import DeepSeekClient, DeepSeekAPIError


class ModelRouter:
    def __init__(self, base_llm_config, logger=None):
        self.base = base_llm_config or {}
        self.logger = logger

    def _client(self, profile):
        base = self.base
        return DeepSeekClient(
            api_key=profile.get("api_key") or base.get("api_key", ""),
            model=profile.get("model") or base.get("model", "deepseek-chat"),
            base_url=profile.get("base_url") or base.get("base_url", ""),
            chat_completions_path=profile.get("chat_completions_path") or base.get("chat_completions_path", "/v1/chat/completions"),
            temperature=profile.get("temperature", base.get("temperature", 0.3)),
            max_tokens=profile.get("max_tokens", base.get("max_tokens", 1024)),
            timeout=profile.get("timeout", base.get("timeout", 30)),
        )

    def chat(self, messages, route="general"):
        settings = AdminSettingsStore().load()
        profiles = {p.get("id"): p for p in settings.get("models", []) if p.get("enabled", True)}
        routing = settings.get("model_routing", {})
        order = []
        for model_id in (routing.get(route), routing.get("fallback")):
            if model_id and model_id not in order and model_id in profiles:
                order.append(model_id)
        if not order:
            return None, None
        last_error = None
        for model_id in order:
            try:
                if self.logger:
                    self.logger.info("多模型路由: route=%s model=%s", route, profiles[model_id].get("name", model_id))
                return self._client(profiles[model_id]).chat(messages), model_id
            except Exception as exc:
                last_error = exc
                if self.logger:
                    self.logger.warning("模型 %s 调用失败，尝试备用模型: %s", model_id, exc)
        raise DeepSeekAPIError(str(last_error) if last_error else "没有可用模型")
