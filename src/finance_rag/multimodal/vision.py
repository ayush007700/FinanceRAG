"""Vision captioning for multimodal RAG v1."""

from __future__ import annotations

import base64

from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)

CAPTION_PROMPT = (
    "You are helping Source Advisors index tax/finance documents. "
    "Describe this image for retrieval: include tables, figures, statute/form labels, "
    "dollar amounts, and any visible section titles. Be factual and concise (120-200 words)."
)


class VisionCaptioner:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = OpenAI(api_key=self.settings.openai_api_key or None)

    def caption_bytes(self, image_bytes: bytes, mime: str = "image/png") -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        completion = self.client.chat.completions.create(
            model=self.settings.openai_vision_model,
            temperature=0.1,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": CAPTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
        )
        text = (completion.choices[0].message.content or "").strip()
        logger.info("image_captioned", chars=len(text), mime=mime)
        return text

    def caption_path(self, path: str) -> str:
        from pathlib import Path

        data = Path(path).read_bytes()
        suffix = Path(path).suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/png")
        return self.caption_bytes(data, mime=mime)
