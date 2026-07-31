# ============================================================
# 语音识别工具 — 基于 OpenAI Whisper 本地 ASR
# ============================================================
#
# 将钉钉语音消息（AMR/MP3 格式）转写为文字。
# 首次使用会自动下载模型（base 模型约 150MB）。
#
# 依赖：openai-whisper（pip install openai-whisper）
# ============================================================

import os
import logging
import subprocess
import shutil

logger = logging.getLogger("speech-to-text")

# Whisper 模型名称 → 大小参考
# tiny: 39M, base: 74M, small: 244M, medium: 769M, large: 1550M
DEFAULT_MODEL = "base"


class SpeechToText:
    """本地语音转文字（Whisper）"""

    _instance = None
    _model = None

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self.logger = logger

    @classmethod
    def get_instance(cls, model_name: str = DEFAULT_MODEL) -> "SpeechToText":
        """单例模式，避免重复加载模型"""
        if cls._instance is None:
            cls._instance = cls(model_name)
        return cls._instance

    def _load_model(self):
        """延迟加载 Whisper 模型"""
        if self._model is not None:
            return
        try:
            import whisper
            self.logger.info(f"正在加载 Whisper 模型: {self.model_name}")
            self._model = whisper.load_model(self.model_name)
            self.logger.info(f"Whisper 模型加载完成: {self.model_name}")
        except ImportError:
            raise RuntimeError(
                "whisper 未安装，请运行: pip install openai-whisper"
            )

    def transcribe(self, audio_path: str, language: str = "zh") -> str:
        """
        将音频文件转写为文字。

        :param audio_path: 音频文件路径（支持 AMR/MP3/WAV/M4A 等）
        :param language: 语言代码（zh=中文, en=英文, 默认自动检测）
        :return: 转写文本
        """
        self._load_model()

        # 钉钉语音消息通常是 AMR 格式，Whisper 不直接支持
        # 需要先转换为 WAV（使用 ffmpeg）
        converted_path = None
        ext = os.path.splitext(audio_path)[1].lower()
        if ext in (".amr", ".silk"):
            converted_path = self._convert_to_wav(audio_path)
            audio_path = converted_path

        try:
            result = self._model.transcribe(
                audio_path,
                language=language if language else None,
                fp16=False,  # CPU 模式下更稳定
            )
            text = result.get("text", "").strip()
            self.logger.info(f"语音识别完成: {len(text)} 字符 | 原始文件: {os.path.basename(audio_path)}")
            return text
        finally:
            # 清理临时转换文件
            if converted_path and os.path.exists(converted_path):
                os.remove(converted_path)

    def _convert_to_wav(self, input_path: str) -> str:
        """使用 ffmpeg 将音频转换为 WAV 格式"""
        # 检查 ffmpeg 是否可用
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # Windows 下检查常见路径
            common_paths = [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                os.path.expanduser("~\\ffmpeg\\bin\\ffmpeg.exe"),
            ]
            for p in common_paths:
                if os.path.exists(p):
                    ffmpeg = p
                    break

        if not ffmpeg:
            self.logger.warning("ffmpeg 未安装，尝试直接用 whisper 处理（可能失败）")
            return input_path

        output_path = input_path.rsplit(".", 1)[0] + ".wav"
        try:
            cmd = [
                ffmpeg, "-y", "-i", input_path,
                "-ar", "16000",  # 16kHz 采样率
                "-ac", "1",      # 单声道
                "-f", "wav",
                output_path,
            ]
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            self.logger.info(f"音频转换: {os.path.basename(input_path)} → WAV")
            return output_path
        except Exception as e:
            self.logger.warning(f"ffmpeg 转换失败: {e}，尝试直接处理")
            return input_path

    @staticmethod
    def is_available() -> bool:
        """检查 whisper 是否已安装"""
        try:
            import whisper  # noqa: F401
            return True
        except ImportError:
            return False
