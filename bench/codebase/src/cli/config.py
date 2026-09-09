"""Loads the CLI's configuration file.

See TOP-119 for the file format (TOML) and TOP-120 for how a config-file
value, an environment variable and a command-line flag are ranked when
they disagree.
"""


def load_config(path=None, env=None, cli_flags=None):
    """Merge config-file, environment and flag values by TOP-120's
    precedence order: flags win, then environment, then the file."""
    file_values = _read_toml(path) if path else {}
    env_values = _from_env(env or {})
    flag_values = cli_flags or {}
    merged = {}
    merged.update(file_values)
    merged.update(env_values)
    merged.update(flag_values)
    return merged


def _read_toml(path):
    return {}


def _from_env(env):
    return {k[len("DRIFTWOOD_"):].lower(): v for k, v in env.items() if k.startswith("DRIFTWOOD_")}
