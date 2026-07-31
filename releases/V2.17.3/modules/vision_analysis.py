"""Image error analysis through a configured OpenAI-compatible vision model."""

import base64
import mimetypes
from pathlib import Path


class VisionAnalysisService:
    MAX_IMAGE_BYTES = 10 * 1024 * 1024

    def __init__(self, model_router, logger=None):
        self.model_router = model_router
        self.logger = logger

    def analyze(self, image_path):
        path = Path(image_path)
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("图片文件为空")
        if size > self.MAX_IMAGE_BYTES:
            raise ValueError("图片超过 10MB，无法进行视觉分析")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "你是企业 IT 运维截图分析助手。请识别图片中的系统或窗口、错误码、"
                    "关键报错文字和故障现象。给出简洁的结构化中文结果：系统/窗口、"
                    "错误信息、初步判断、建议补充信息。看不清的内容必须明确说明，不要编造。"
                )},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }]
        answer, model_id = self.model_router.chat(messages, "vision")
        if not answer:
            return None, None
        return answer.strip(), model_id

