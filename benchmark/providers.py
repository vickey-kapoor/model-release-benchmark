"""Vendor API adapters behind one interface, using only the standard library.

Each provider keeps conversation history in its vendor's native format, so
vendor-specific response parts survive multi-step tool calls unchanged.
"""

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
REQUEST_TIMEOUT_S = 600


class TransientError(Exception):
    """A failure worth retrying, such as a rate limit or outage."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """A failure retrying can't fix, such as a bad request or invalid key."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Response:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    refusal: bool = False
    served_model: str = ""
    assistant_message: object = None
    simulated_latency: float | None = None


def http_post_json(url, headers, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:500]
        message = f"HTTP {error.code}: {body}"
        if error.code in RETRYABLE_STATUS:
            retry_after = error.headers.get("Retry-After")
            raise TransientError(message, float(retry_after) if (retry_after or "").isdigit() else None)
        raise PermanentError(message)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise TransientError(f"{type(error).__name__}: {error}")


def tool_schema(tool):
    """Mock tools have fixed shapes, so every vendor sees identical definitions."""
    properties = {}
    if tool["op"] in {"read", "write"}:
        properties["key"] = {"type": "string", "description": "Record name"}
    if tool["op"] == "write":
        properties["value"] = {"type": "string", "description": "New value"}
    return {"type": "object", "properties": properties, "required": list(properties)}


def _require_key(name):
    key = os.environ.get(name)
    if not key:
        raise PermanentError(f"{name} is not set")
    return key


class OpenAIProvider:
    url = "https://api.openai.com/v1/chat/completions"

    def __init__(self, post=http_post_json):
        self.post = post

    def start(self, prompt, system):
        return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]

    def call(self, model, system, messages, tools, max_tokens, task=None):
        payload = {"model": model["api_model"], "messages": messages, "max_completion_tokens": max_tokens}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": tool_schema(t)},
                }
                for t in tools
            ]
        headers = {"Authorization": f"Bearer {_require_key('OPENAI_API_KEY')}"}
        data = self.post(self.url, headers, payload)
        if not data.get("choices"):
            raise TransientError("response has no choices")
        choice = data["choices"][0]
        message = choice["message"]
        usage = data.get("usage", {})
        calls = []
        for call in message.get("tool_calls") or []:
            calls.append(ToolCall(call["id"], call["function"]["name"], _parse_args(call["function"].get("arguments"))))
        return Response(
            text=message.get("content") or "",
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", ""),
            refusal=bool(message.get("refusal")) or choice.get("finish_reason") == "content_filter",
            served_model=data.get("model", ""),
            assistant_message=message,
        )

    def extend(self, messages, response, results):
        messages.append(response.assistant_message)
        for call, result in results:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})


class AnthropicProvider:
    url = "https://api.anthropic.com/v1/messages"

    def __init__(self, post=http_post_json):
        self.post = post

    def start(self, prompt, system):
        return [{"role": "user", "content": prompt}]

    def call(self, model, system, messages, tools, max_tokens, task=None):
        payload = {"model": model["api_model"], "messages": messages, "max_tokens": max_tokens}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": tool_schema(t)} for t in tools
            ]
        headers = {"x-api-key": _require_key("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"}
        data = self.post(self.url, headers, payload)
        blocks = data.get("content") or []
        usage = data.get("usage", {})
        return Response(
            text="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
            tool_calls=[ToolCall(b["id"], b["name"], b.get("input") or {}) for b in blocks if b.get("type") == "tool_use"],
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            finish_reason=data.get("stop_reason") or "",
            refusal=data.get("stop_reason") == "refusal",
            served_model=data.get("model", ""),
            assistant_message={"role": "assistant", "content": blocks},
        )

    def extend(self, messages, response, results):
        messages.append(response.assistant_message)
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": c.id, "content": r} for c, r in results],
            }
        )


class GeminiProvider:
    base_url = "https://generativelanguage.googleapis.com/v1beta/models"
    refusal_reasons = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}

    def __init__(self, post=http_post_json):
        self.post = post

    def start(self, prompt, system):
        return [{"role": "user", "parts": [{"text": prompt}]}]

    def call(self, model, system, messages, tools, max_tokens, task=None):
        payload = {"contents": messages, "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            declarations = []
            for tool in tools:
                declaration = {"name": tool["name"], "description": tool["description"]}
                schema = tool_schema(tool)
                # Gemini rejects object schemas with no properties, so omit them.
                if schema["properties"]:
                    declaration["parameters"] = schema
                declarations.append(declaration)
            payload["tools"] = [{"functionDeclarations": declarations}]
        headers = {"x-goog-api-key": _require_key("GEMINI_API_KEY")}
        data = self.post(f"{self.base_url}/{model['api_model']}:generateContent", headers, payload)
        usage = data.get("usageMetadata", {})
        common = {
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
            "served_model": data.get("modelVersion", ""),
        }
        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "")
            if reason:
                return Response(finish_reason=reason, refusal=True, **common)
            raise TransientError("response has no candidates")
        candidate = candidates[0]
        content = candidate.get("content") or {"role": "model", "parts": []}
        parts = content.get("parts") or []
        calls = []
        for index, part in enumerate(parts):
            if "functionCall" in part:
                call = part["functionCall"]
                calls.append(ToolCall(call.get("id", f"call-{index}"), call["name"], call.get("args") or {}))
        finish = candidate.get("finishReason", "")
        return Response(
            text="".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought")),
            tool_calls=calls,
            finish_reason=finish,
            refusal=finish in self.refusal_reasons,
            assistant_message=content,
            **common,
        )

    def extend(self, messages, response, results):
        messages.append(response.assistant_message)
        messages.append(
            {
                "role": "user",
                "parts": [{"functionResponse": {"name": c.name, "response": {"result": r}}} for c, r in results],
            }
        )


class MockProvider:
    """Deterministic fake model for tests and demos. It never touches the network."""

    def start(self, prompt, system):
        return {"written": 0}

    def _hash(self, seed):
        return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF

    def _roll(self, model, task, salt=""):
        return self._hash(f"{model['id']}|{task.id}|{model.get('_repeat', 0)}|{salt}")

    def _skill(self, model, task):
        offset = (self._hash(f"{model['id']}|{task.category}") - 0.5) * 0.3
        hard = {"easy": 0.15, "medium": 0.0, "hard": -0.15}[task.difficulty]
        return max(0.02, min(0.98, model.get("mock_skill", 0.7) + offset + hard))

    def call(self, model, system, messages, tools, max_tokens, task=None):
        correct = self._roll(model, task) < self._skill(model, task)
        response = Response(
            input_tokens=len(task.full_prompt()) // 4,
            output_tokens=150 + int(self._roll(model, task, "tokens") * 400),
            finish_reason="stop",
            served_model=model["api_model"],
            assistant_message=None,
            simulated_latency=model.get("mock_latency", 5.0) * (0.6 + self._roll(model, task, "time") * 0.8),
        )
        if self._roll(model, task, "refuse") < 0.005:
            response.refusal = True
            response.finish_reason = "refusal"
            return response
        if task.scoring == "tool_state":
            pending = [
                (k, v) for k, v in task.spec["expected_state"].items() if task.spec["initial_state"].get(k) != v
            ]
            writer = next(t["name"] for t in task.spec["tools"] if t["op"] == "write")
            if correct and messages["written"] < len(pending):
                key, value = pending[messages["written"]]
                response.tool_calls = [ToolCall(f"call-{key}", writer, {"key": key, "value": value})]
            else:
                response.text = "Done."
            return response
        response.text = self._output(task, correct)
        return response

    def _output(self, task, correct):
        spec = task.spec
        if task.scoring == "answer_key":
            return f"Working through it.\nANSWER: {spec['answers'][0] if correct else 'not sure'}"
        if task.scoring == "fields":
            return json.dumps(spec["expected"] if correct else {})
        if task.scoring == "document":
            answer = spec["answers"][0] if correct else "something else"
            citations = [] if answer == "UNANSWERABLE" else [spec.get("reference_citation", spec["document"][:30])]
            return json.dumps({"answer": answer, "citations": citations})
        if task.scoring == "tests":
            return f"```python\n{spec['reference_solution'] if correct else 'broken = True'}\n```"
        return "Draft response for this writing task."

    def extend(self, messages, response, results):
        messages["written"] += len(results)


def _parse_args(raw):
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "google": GeminiProvider,
    "mock": MockProvider,
}


def get_provider(name):
    return PROVIDERS[name]()
