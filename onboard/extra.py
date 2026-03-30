import yaml
import os
import platform
import subprocess

class Config:
    def __init__(self, file_path):
        self.file_path = file_path
        self.data = self._load()

    def _load(self):
        """Internal method to load YAML or return empty dict."""
        if os.path.exists(self.file_path):
            with open(self.file_path, 'r') as f:
                # safe_load returns None if file is empty, so we default to {}
                return yaml.safe_load(f) or {}
        return {}

    def set(self, key_path, value):
        """Sets a value at a nested path and persists to disk."""
        keys = key_path.strip('/').split('/')
        current = self.data
        
        # Traverse and build the nested structure
        for key in keys[:-1]:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]

        # Set the leaf value
        current[keys[-1]] = value
        self._save()

    def _save(self):
        """Internal method to write current state to the YAML file."""
        # Ensure the directory exists
        dir_name = os.path.dirname(self.file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
            
        with open(self.file_path, 'w') as f:
            yaml.safe_dump(self.data, f, default_flow_style=False, sort_keys=False)


def set_env(key, value):
    """Sets an environment variable permanently based on the OS."""
    current_os = platform.system()

    if current_os == "Windows":
        # Uses 'setx', the standard Windows CLI for permanent user env vars
        # Note: This won't affect the *current* terminal session, only new ones.
        subprocess.run(["setx", key, str(value)], check=True, capture_output=True)

    elif current_os in ["Linux", "Darwin"]:  # Darwin is macOS
        # On Unix, we usually append to the shell profile (.bashrc or .zshrc)
        # We'll target .bashrc or .zshrc depending on what exists
        shell_profile = os.path.expanduser("~/.bashrc")
        if current_os == "Darwin": # macOS usually uses zsh now
            shell_profile = os.path.expanduser("~/.zshrc")
            
        line = f'\nexport {key}="{value}"\n'
        
        with open(shell_profile, "a") as f:
            f.write(line)

    else:
        raise NotImplementedError(f"OS {current_os} not supported for permanent env.")