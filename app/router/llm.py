"""OpenAI 兼容接口的薄封装，要求模型返回 JSON。"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from app.config import Settings


def extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型输出中未找到 JSON")
    return json.loads(text[start:end + 1])


class LLMClient:
    def __init__(self, s: Settings) -> None:
        from openai import OpenAI

        self.client = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=90, max_retries=1)

    def chat_json(self, model: str, system: str, user: str, images: list[str] | None = None) -> tuple[dict, int]:
        content: list | str = user
        if images:
            content = [{"type": "text", "text": user}]
            for p in images:
                b64 = base64.b64encode(Path(p).read_bytes()).decode()
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        resp = self.client.chat.completions.create(
            model=model, temperature=0.1,
            messages=[{"role": "system", "content": system + "\n只输出一个 JSON 对象，不要输出其他内容。"},
                      {"role": "user", "content": content}])
        tokens = resp.usage.total_tokens if resp.usage else 0
        return extract_json(resp.choices[0].message.content or ""), tokens
