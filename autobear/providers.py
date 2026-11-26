import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .selection import fzf_choice
from .state import Error

CLI = {"claude-cli": "claude", "codex-cli": "codex", "agy-cli": "agy"}
DEFAULT_PROVIDER = "codex-cli"
RECOMMENDATION = ("Choose the strongest model you have, such as Claude Opus. "
                  "Autobear calls it only when you import or repair a log.")


@dataclass(frozen=True)
class Provider:
    name: str
    model: str = ""

    def unavailable(self):
        if self.name == "openrouter":
            if not self.model or not os.getenv("OPENROUTER_API_KEY"):
                return "OpenRouter needs a model and OPENROUTER_API_KEY."
        elif not shutil.which(CLI[self.name]):
            return f"{CLI[self.name]} is absent from PATH."
        return ""

    def request(self, task, context):
        problem = self.unavailable()
        if problem:
            raise Error(problem)
        template = files("autobear").joinpath("prompts", f"{task}.txt").read_text()
        prompt = f"{template}\n\nInput data (JSON)\n{json.dumps(context)}"
        try:
            output = self.openrouter(prompt) if self.name == "openrouter" else self.run_cli(prompt)
            value = json.loads(output)
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object.")
            return value
        except HTTPError as exc:
            raise Error(f"{self.name} returned HTTP {exc.code}.") from exc
        except (OSError, URLError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise Error(f"{self.name} did not return a usable JSON response ({type(exc).__name__}).") from exc

    def run_cli(self, prompt):
        model = ["--model", self.model] if self.model else []
        with tempfile.TemporaryDirectory(prefix="autobear-provider-") as temp:
            output = Path(temp) / "response.json"
            if self.name == "claude-cli":
                command = ["claude", "--print", "--tools", "", "--output-format", "json", *model]
                stdin = prompt
            elif self.name == "codex-cli":
                command = [
                    "codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                    "--ephemeral", "--ignore-user-config", "--output-last-message", str(output), *model, "-",
                ]
                stdin = prompt
            else:
                command = ["agy", "--sandbox", "--output-format", "json", *model, "--print", prompt]
                stdin = ""
            result = subprocess.run(command, input=stdin, cwd=temp, text=True,
                                    capture_output=True, timeout=300)
            if result.returncode:
                raise Error(f"{self.name} exited with status {result.returncode}.")
            if self.name == "codex-cli":
                return output.read_text()
            message = json.loads(result.stdout)
            if self.name == "claude-cli" and not message["is_error"]:
                return message["result"]
            if self.name == "agy-cli" and message["status"] == "SUCCESS":
                return message["response"]
            raise Error(f"{self.name} reported a failed request.")

    def openrouter(self, prompt):
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        request = Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=300) as response:
            value = json.load(response)
        return value["choices"][0]["message"]["content"]


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    description: str = ""


def model_catalog(provider):
    if provider == "codex-cli":
        result = subprocess.run(["codex", "debug", "models"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, check=True, timeout=15)
        items = json.loads(result.stdout)["models"]
        return [Model(item["slug"], item["display_name"], item.get("description") or "")
                for item in items if item["visibility"] == "list"]
    if provider == "openrouter":
        request = Request("https://openrouter.ai/api/v1/models")
        if key := os.getenv("OPENROUTER_API_KEY"):
            request.add_header("Authorization", f"Bearer {key}")
        with urlopen(request, timeout=15) as response:
            items = json.load(response)["data"]
        return [Model(item["id"], item.get("name") or item["id"])
                for item in items if "response_format" in (item.get("supported_parameters") or [])]
    return []


def model_label(model):
    description = " ".join(model.description.split())[:120]
    suffix = f" · {description}" if description else ""
    name = " ".join(model.name.split())
    return f"{name} ({model.id}){suffix}"


def choose_model(label, provider, required, catalogs):
    if provider not in catalogs:
        try:
            catalogs[provider] = model_catalog(provider)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            print(f"Could not load the {provider} model catalog. Enter a model ID.")
            catalogs[provider] = []
    models = catalogs[provider]
    if not models:
        if not required:
            choice = fzf_choice(f"{label} model", {"CLI default": "", "Enter a model ID": "manual"})
            if choice == "":
                return choice
            if choice is None:
                return input(f"{label} model [CLI default]? ").strip()
        while not (model := input(f"{label} model ID for {provider}? ").strip()):
            pass
        return model

    options = {} if required else {"CLI default": ""}
    options.update((model_label(model), model.id) for model in models)
    choice = fzf_choice(f"{label} model", options)
    if choice is not None:
        return choice
    choices = models
    while len(choices) > 20:
        answer = input(f"Search {provider} models for {label}? ").strip().casefold()
        if not answer and not required:
            return ""
        if answer:
            choices = [model for model in models
                       if answer in model.id.casefold() or answer in model.name.casefold()]
            if not choices:
                print("No matching models.")
                choices = models
            elif len(choices) > 20:
                print(f"Found {len(choices)} models. Enter a more specific search.")
    print(f"{label} models")
    numbered = {str(index): model for index, model in enumerate(choices, 1)}
    for index, model in numbered.items():
        print(f"  {index}. {model_label(model)}")
    prompt = f"{label} model? " if required else f"{label} model [CLI default]? "
    while True:
        answer = input(prompt).strip()
        if not answer and not required:
            return ""
        if answer in numbered:
            return numbered[answer].id
        matches = [model for model in choices
                   if answer.casefold() in {model.name.casefold(), model.id.casefold()}]
        if len(matches) == 1:
            return matches[0].id
        print("Select a model from the list.")


def choose_provider(installed, catalogs):
    label = "LLM"
    provider_env = "AUTOBEAR_PROVIDER"
    name = os.getenv(provider_env)
    if name is None:
        ready = [*installed]
        if os.getenv("OPENROUTER_API_KEY"):
            ready.append("openrouter")
        default = DEFAULT_PROVIDER
        if default not in ready:
            default = ready[0] if ready else "openrouter"
        options = [*installed, "openrouter"]
        labels = {("openrouter (requires OPENROUTER_API_KEY)" if option == "openrouter" else option): option
                  for option in options}
        ordered = dict(sorted(labels.items(), key=lambda item: item[1] != default))
        name = fzf_choice(f"{label} provider", ordered)
        if name is None:
            print(f"{label} providers")
            numbers = {str(index): option for index, option in enumerate(options, 1)}
            for index, item in enumerate(labels, 1):
                print(f"  {index}. {item}")
            while name not in options:
                answer = input(f"{label} provider [{default}]? ").strip()
                name = numbers.get(answer, answer or default)
                if name not in options:
                    print("Select a provider from the list.")
    elif name not in (*CLI, "openrouter"):
        raise Error(f"Invalid provider in {provider_env}.")

    model_env = "AUTOBEAR_MODEL"
    model = os.getenv(model_env)
    required = name == "openrouter"
    if model is None:
        model = choose_model(label, name, required, catalogs)
    if required and not model:
        raise Error(f"Set {model_env} to a model ID for {name}.")
    return Provider(name, model)


def setup_provider(path):
    installed = [name for name, command in CLI.items() if shutil.which(command)]
    print(f"Set up the autobear LLM in {path}.")
    print(RECOMMENDATION)
    try:
        provider = choose_provider(installed, {})
    except (EOFError, KeyboardInterrupt) as exc:
        raise Error("Provider setup canceled.") from exc
    content = (f"[llm]\nprovider = {json.dumps(provider.name)}\n"
               f"model = {json.dumps(provider.model, ensure_ascii=False)}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        config = Path(temp) / path.name
        config.touch(mode=0o600)
        config.write_text(content, encoding="utf-8")
        try:
            os.link(config, path)
        except FileExistsError:
            print(f"Using the provider settings in {path}.")
        else:
            print(f"Saved provider settings in {path}.")


def settings(*, query=False):
    path = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "autobear" / "config.toml"
    if query and not path.exists() and sys.stdin.isatty():
        setup_provider(path)
    if path.exists():
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    else:
        data = {}
    if "llm" not in data and ("architect" in data or "worker" in data):
        raise Error(f"Replace the [architect] and [worker] tables in {path} with one [llm] table.")
    table = data.get("llm", {})
    if not isinstance(table, dict):
        raise Error(f"Expected an [llm] table in {path}.")
    name = os.getenv("AUTOBEAR_PROVIDER", table.get("provider", DEFAULT_PROVIDER))
    model = os.getenv("AUTOBEAR_MODEL", table.get("model", ""))
    if not isinstance(name, str) or name not in (*CLI, "openrouter") or not isinstance(model, str):
        raise Error(f"Invalid LLM provider or model in {path} or the environment.")
    return Provider(name, model)
