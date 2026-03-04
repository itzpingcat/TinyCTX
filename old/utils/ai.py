import os
import json
import requests
from typing import List, Dict, Optional, Iterator, Union


class LLM:
    """
    Universal OpenAI-compatible LLM client.

    Works with:
    - OpenAI
    - OpenRouter
    - LM Studio
    - Ollama (OpenAI compatibility mode)
    - Any OpenAI-style endpoint

    Just configure:
        base_url
        api_key
        model
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        timeout: int = 60,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tools = tools
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

        self.endpoint = f"{self.base_url}/v1/chat/completions"

    # ---------------------------------------------------------------------
    # PUBLIC QUERY METHOD (Streaming)
    # ---------------------------------------------------------------------

    def query(
        self,
        messages: List[Dict[str, str]]
    ) -> Iterator[Dict[str, Union[str, dict]]]:

        headers = {
            "Content-Type": "application/json",
        }

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }

        if self.tools:
            payload["tools"] = self.tools

        # --- DEBUG START ---
        # print("\n" + "="*50)
        # print("DEBUG: LLM PAYLOAD SENT")
        # print(json.dumps(payload, indent=2))
        # print("="*50 + "\n")
        # --- DEBUG END ---

        try:
            response = requests.post(
                self.endpoint,
                headers=headers,
                json=payload,
                stream=True,
                timeout=self.timeout
            )

            response.raise_for_status()

            for line in response.iter_lines():
                # print(f"[debug] {line}", flush=True)
                if not line:
                    continue

                line_str = line.decode("utf-8")

                if not line_str.startswith("data: "):
                    continue

                data_str = line_str[6:]

                if data_str.strip() == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if "choices" not in data or not data["choices"]:
                    continue

                delta = data["choices"][0].get("delta", {})

                # Normal text content
                if "content" in delta and delta["content"]:
                    yield {
                        "type": "content",
                        "delta": delta["content"]
                    }

                # Tool calls (if supported by endpoint)
                if "tool_calls" in delta:
                    for tool_call in delta["tool_calls"]:
                        yield {
                            "type": "tool_call",
                            "delta": tool_call
                        }

        except requests.exceptions.HTTPError as e:
            yield {
                "type": "error",
                "delta": f"HTTP error: {e.response.status_code} - {e.response.text}"
            }

        except requests.exceptions.ConnectionError:
            yield {
                "type": "error",
                "delta": "Connection failed. Check base_url and server."
            }

        except Exception as e:
            yield {
                "type": "error",
                "delta": str(e)
            }