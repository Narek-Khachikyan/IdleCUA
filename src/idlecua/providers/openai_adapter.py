from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Tiny 1x1 transparent PNG (base64 without prefix) for vision probing
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)

from ..accounting import record_llm_call
from ..contracts.model import ModelProvider
from .config import ProviderConfig


class VisionTestStatus(str, Enum):
    ok = "ok"
    unreachable = "unreachable"
    not_vision = "not_vision"


def _is_vision_error_message(msg: str) -> bool:
    lower = msg.lower()
    # Common phrasing from OpenAI-compatible gateways when model lacks vision
    vision_hints = [
        "vision",
        "image",
        "multimodal",
        "does not support",
        "unsupported content",
        "content type",
        "image_url",
        "not supported",
    ]
    return any(h in lower for h in vision_hints)


def _build_chat_payload(model: str, messages: list[dict]) -> dict:
    """Strict OpenAI chat payload — only standard fields.

    The OpenCode Go gateway rejects non-standard fields, so we send
    exactly ``model`` + ``messages``. No ``temperature``, ``stream``,
    ``max_tokens``, etc.
    """
    return {"model": model, "messages": messages}


def _extract_content(data: dict) -> str:
    try:
        choices = data.get("choices", [])
        if not choices:
            return ""
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Concatenate text parts
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
            return "".join(parts)
        return str(content)
    except Exception:
        return ""


@dataclass
class OpenAICompatibleProvider(ModelProvider):
    """OpenAI-compatible chat+vision adapter over HTTP.

    Speaks the standard OpenAI ``/chat/completions`` format. Works with
    OpenRouter, OpenCode Go gateway (https://opencode.ai/zen/go/v1), or any
    other OpenAI-compatible endpoint.

    Secrets are injected at construction (from the credential store) and
    never written to disk or logs. The payload is intentionally minimal
    because the gateway rejects non-standard fields.
    """

    config: ProviderConfig
    api_key: str
    timeout: float = 30.0
    data_dir: Optional[Path] = None  # for daily accounting hook; None = no accounting

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise ValueError("api_key must be non-empty")
        # normalize base_url
        self.config = ProviderConfig(
            name=self.config.name,
            base_url=self.config.base_url.rstrip("/"),
            model=self.config.model,
        )

    # -- low-level HTTP --

    def _url(self) -> str:
        return f"{self.config.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict) -> dict:
        """Synchronous POST using httpx if available, else urllib.

        Raises on transport/HTTP errors so callers can map to unreachable/vision.
        """
        url = self._url()
        headers = self._headers()
        body = json.dumps(payload).encode("utf-8")

        # Prefer httpx (more ergonomic, better timeouts)
        try:
            import httpx  # type: ignore

            # httpx will raise on network errors; we treat HTTP 4xx/5xx as response
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            # Raise for 4xx/5xx to unify error handling
            if resp.status_code >= 400:
                # Try to parse error body for vision detection
                try:
                    err_data = resp.json()
                except Exception:
                    err_data = {"error": {"message": resp.text}}
                # Attach status for caller
                err_data["_status"] = resp.status_code  # type: ignore
                raise _HTTPError(resp.status_code, err_data)
            return resp.json()
        except _HTTPError:
            raise
        except ImportError:
            # Fallback to stdlib urllib
            import urllib.error
            import urllib.request

            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:  # type: ignore
                    raw = r.read()
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8")
                    err_data = json.loads(err_body) if err_body else {}
                except Exception:
                    err_data = {"error": {"message": str(e)}}
                err_data["_status"] = e.code  # type: ignore
                raise _HTTPError(e.code, err_data) from e
            except Exception as e:  # network etc.
                raise _TransportError(str(e)) from e
        except Exception as e:
            # httpx network errors (ConnectError, Timeout, etc.)
            if isinstance(e, _HTTPError) or isinstance(e, _TransportError):
                raise
            # Detect httpx transport errors by class name to avoid hard dep
            name = type(e).__name__
            if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException", "NetworkError"):
                raise _TransportError(str(e)) from e
            # Unknown httpx error — treat as transport
            # But if it has response attached, it's already handled
            raise _TransportError(str(e)) from e

    # -- ModelProvider contract --

    def complete(self, prompt: str) -> str:
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        messages = [{"role": "user", "content": prompt}]
        # chat() handles accounting exactly once
        return self.chat(messages)

    async def acomplete(self, prompt: str) -> str:
        return self.complete(prompt)

    def chat(self, messages: list[dict]) -> str:
        payload = _build_chat_payload(self.config.model, messages)
        data = self._post(payload)
        content = _extract_content(data)
        if self.data_dir is not None:
            try:
                record_llm_call(self.data_dir, model=self.config.model)
            except Exception:
                pass
        return content

    # -- Vision test --

    def test_vision(self) -> tuple[VisionTestStatus, str]:
        """Connectivity + vision probe.

        Returns (status, message):
        - ok: reachable and vision-capable
        - not_vision: reachable but model rejects image input
        - unreachable: cannot reach provider / auth failed / other transport error

        The probe uses a minimal 1x1 PNG so cost is negligible. It is the
        hook for ``models test`` and the future planner's call-cap gate.
        """
        # First: plain text probe to check reachability
        try:
            plain = _build_chat_payload(
                self.config.model, [{"role": "user", "content": "hi"}]
            )
            self._post(plain)
            # account? test should not count toward daily cap? but spec says
            # `models test` verifies connectivity — not a real LLM call for cap?
            # We treat test as not counted; so we do not call accounting here.
        except _TransportError as e:
            return VisionTestStatus.unreachable, f"Unreachable: {e}"
        except _HTTPError as e:
            # Auth errors (401/403) count as unreachable for test distinction,
            # because user cannot use the provider.
            # Provide detailed message but map to unreachable.
            msg = _error_message(e.data)
            if e.status in (401, 403):
                return VisionTestStatus.unreachable, f"Authentication failed ({e.status}): {msg}"
            if e.status in (400, 404, 422):
                # Might be model not found etc. — treat as unreachable for now
                # unless it's clearly vision-related (but plain text shouldn't trigger vision error)
                if _is_vision_error_message(msg):
                    return VisionTestStatus.not_vision, f"Model rejected vision: {msg}"
                return VisionTestStatus.unreachable, f"Provider error ({e.status}): {msg}"
            return VisionTestStatus.unreachable, f"Provider error ({e.status}): {msg}"
        except Exception as e:
            return VisionTestStatus.unreachable, f"Unreachable: {e}"

        # Second: vision probe
        vision_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image? Reply with one word."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{_TINY_PNG_B64}",
                        },
                    },
                ],
            }
        ]
        try:
            payload = _build_chat_payload(self.config.model, vision_messages)
            data = self._post(payload)
            # If we got a response, vision is supported
            _ = _extract_content(data)
            return VisionTestStatus.ok, "OK — provider reachable and vision-capable"
        except _TransportError as e:
            return VisionTestStatus.unreachable, f"Unreachable during vision probe: {e}"
        except _HTTPError as e:
            msg = _error_message(e.data)
            if _is_vision_error_message(msg):
                return VisionTestStatus.not_vision, f"Reachable but not vision-capable: {msg}"
            if e.status in (401, 403):
                return VisionTestStatus.unreachable, f"Authentication failed ({e.status}): {msg}"
            # If gateway returns 400 with vision hint, that's not_vision
            # Otherwise treat as not_vision if message hints vision, else unreachable
            if e.status == 400 and _is_vision_error_message(msg):
                return VisionTestStatus.not_vision, f"Reachable but not vision-capable: {msg}"
            return VisionTestStatus.unreachable, f"Provider error ({e.status}): {msg}"
        except Exception as e:
            return VisionTestStatus.unreachable, f"Unreachable: {e}"


class _HTTPError(Exception):
    def __init__(self, status: int, data: dict) -> None:
        super().__init__(f"HTTP {status}: {data}")
        self.status = status
        self.data = data


class _TransportError(Exception):
    pass


def _error_message(data: dict) -> str:
    try:
        err = data.get("error", {})
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or ""
            if msg:
                return str(msg)
        # OpenRouter style: data["error"]["message"]
        # Some gateways: {"detail": "..."}
        if "detail" in data:
            return str(data["detail"])
        # Fallback to raw
        return json.dumps(data)[:500]
    except Exception:
        return str(data)[:500]
