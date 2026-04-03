

import json
from openai import OpenAI
from typing import List, Dict, Any, Optional

import re
from pathlib import Path
import platform



import locale
import os
import subprocess

class ExecTool():
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return ""

    def get_description(self):
        return {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Execute a shell command and return its output. Use with caution.",
                "parameters": {
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute",
                        },
                        "working_dir": {
                            "type": "string",
                            "description": "Optional working directory for the command",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": (
                                "Timeout in seconds. Increase for long-running commands "
                                "like compilation or installation (default 60, max 600)."
                            ),
                            "minimum": 1,
                            "maximum": 600,
                        },
                    },
                    "required": ["command"]
                }
            }
        }

    def run(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, **kwargs,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        
        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            


            process.wait(effective_timeout)

            output_parts = []

            if process.stdout:
                preferred_encoding = locale.getpreferredencoding(False)
                output_parts.append(process.stdout.read().decode(preferred_encoding, errors="replace"))

            if process.stderr:
                preferred_encoding = locale.getpreferredencoding(False)
                stderr_text = process.stderr.read().decode(preferred_encoding, errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Head + tail truncation to preserve both start and end of output
            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        from nanobot.security.network import contains_internal_url
        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)   # Windows: C:\...
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths


class SkillManager:
    def __init__(self, workspace_path: str = None):
        if workspace_path is None:
            self.workspace = Path.cwd()
        else:
            self.workspace = Path(workspace_path)
        self.workspace_skills = self.workspace / "skills"
    
    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        skills = []
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        meta = self.get_skill_metadata(skill_file)
                        description = meta["description"]
                        content = meta["content"]
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "description":description, "content":content})
        return skills
    
    def get_skill_metadata(self, skill_path: str) -> dict:
        skill_file = Path(skill_path)
        if not skill_file.exists():
            return None
        try:
            content = skill_file.read_text(encoding="utf-8")
            if not content:
                return None
            
            if content.startswith("---"):
                match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
                if match:# Simple YAML parsing
                    metadata = {}
                    for line in match.group(1).split("\n"):
                        if ":" in line:
                            key, value = line.split(":", 1)
                            metadata[key.strip()] = value.strip().strip('"\'')
                    metadata["content"] = content[match.end():].strip()
                    return metadata
        except Exception as e:
            print(f"Error reading skill metadata: {e}")
            return None
        return {}
    
    def get_skill_description(self, skill_path: str) -> str:
        meta = self.get_skill_metadata(skill_path)
        if meta and meta.get("description"):
            return meta["description"]
        return ""

class Contextor:
    def __init__(self):
        self.conversation_history: List[Dict[str, Any]] = []
    
    def reset_message(self, workspace_path: str) -> None:
        workspace_path = str(Path(workspace_path).expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}"
        content=f"You are a helpful personal assistant. Your runtime is {runtime}, and your workspace is located at {workspace_path}。\n\n"
        self.conversation_history.clear()
        skill_manager = SkillManager(workspace_path)
        skills = skill_manager.list_skills()
        if len(skills) > 0: content += "You can use skills. The following skills extend your capabilities. To use a skill, read its SKILL.md file.\n"
        for skill in skills:
            content += f"- name:{skill['name']}\npath: {skill['path']}\ndescription: {skill['description']}\n"
        self.conversation_history.append({
            "role": "system",
            "content": [{'type': 'text', 'text': content, 'cache_control': {'type': 'ephemeral'}}]
        })
    
    def add_user_message(self, content: str) -> None:
        self.conversation_history.append({
            "role": "user",
            "content": content
        })
    
    def add_assistant_message(self, content: str, reasoning: str | None = None) -> None:
        assistant_message = {
            "role": "assistant",
            "content": content
        }
        if reasoning: assistant_message["reasoning_content"] = reasoning
        self.conversation_history.append(assistant_message)
    
    def add_tool_message(self, tool_results: List, max_tokens: int) -> None:
        for tr in tool_results:
            self.conversation_history.append({
                "tool_call_id":tr["tool_call_id"],
                "role": "tool",
                "content": json.dumps({"tool_calls":tr["tool_calls"],"tool_results":tr["tool_results"]}, ensure_ascii=False)[:max_tokens]
            })

class Tool:
    def __init__(self, name_list):
        self.tools = {}
        for n in name_list:
            self.add_tool(n)
    
    def get_tools_def(self):
        return [self.tools[tool].get_description() for tool in self.tools]
    
    def add_tool(self, tool) -> None:
        t = tool()
        self.tools[t.name] = t
    
    def execute_tool_calls(self, tool_calls: List[Dict]) -> List[Dict]:
        results = []
        for tool_call in tool_calls:
            function = tool_call.function
            function_args = json.loads(function.arguments)
            content = {"tool_call_id":tool_call.id,"tool_calls":{"name":function.name,"arguments":function_args},"tool_results":""}
            if function.name in self.tools:
                try:
                    content["tool_results"] = self.tools[function.name].run(**function_args)
                except Exception as e:
                    content["tool_results"] = f"Tool execution error: {str(e)}"
            else:
                content["tool_results"] = f"Unknown tool: {function.name}"
            results.append(content)
        return results


class ChatTool:
    def __init__(self, cfg):
        self.cfg = cfg
        self.contextor = Contextor()
        self.tools = Tool([ExecTool])
        self.client = OpenAI(base_url=cfg["base_url"],api_key=cfg["api_key"])
    
    def get_response(self) -> Dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.cfg["model"],
            messages=self.contextor.conversation_history,
            tools=self.tools.get_tools_def(),
            tool_choice=self.cfg["tool_choice"],
            temperature=self.cfg["temperature"],
            max_tokens=self.cfg["max_tokens"]
        )
        response_data = {
            "content":response.choices[0].message.content,
            "reasoning":response.choices[0].message.reasoning,
            "tool_calls":response.choices[0].message.tool_calls,
            "model":response.model,
            "total_tokens":response.usage.total_tokens,
            "finish_reason":response.choices[0].finish_reason
        }
        return response_data

    def chat_round(self, user_prompt: str, max_iterations: int = 5) -> Dict[str, Any]:
        iteration = 0
        self.contextor.reset_message(self.cfg["workspace"])
        self.contextor.add_user_message(user_prompt)
        
        while iteration < max_iterations:
            iteration += 1
            response_data = self.get_response()   
            print(response_data["content"],"total_tokens:",response_data["total_tokens"])
            self.contextor.add_assistant_message(response_data["content"], response_data["reasoning"])
            
            if response_data["tool_calls"]:
                tool_results = self.tools.execute_tool_calls(response_data["tool_calls"])
                self.contextor.add_tool_message(tool_results, self.cfg["max_tokens"])
                if self.cfg["supervisor_mode"]:
                    print("reasoning:",response_data["reasoning"])
                    user_prompt = input("supervisor> ").strip()
                    if user_prompt: self.contextor.add_user_message(user_prompt)
            else:
                return response_data
        return {"content": "Reaching the maximum number of iterations"}
    

def main():
    file = open('config.json', 'r')
    js = file.read()
    cfg = json.loads(js)
    file.close()
    chat_tool = ChatTool(cfg)
    
    print("Welcome to use personal assistant! Enter 'quit' to exit\n")
    while True:
        try:
            user_prompt = input("> ").strip()
            if user_prompt.lower() == 'quit':
                print("Bye!")
                break
            elif user_prompt:
                response = chat_tool.chat_round(user_prompt)
        except KeyboardInterrupt:
            print("Bye!")
            break
        except Exception as e:
            print(f"\nError: {e}")


if __name__ == "__main__":
    main()
